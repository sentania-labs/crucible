"""The administrative surface (25) on the fake provider: every row of the operations
table through the API and through `crucible-admin` in local mode, both landing in the
same audit trail; a mutation refused without a live supervisor lease; the orchestrator's
read-only view. The fake CLIs stand in for the three logins; every secret-shaped value
is built at runtime.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.admin.context import AdminContext
from crucible.application.auth import authenticate
from crucible.application.errors import ApplicationError
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from crucible.domain.entities import ImagePromotion, Role
from crucible.ports.execution import ImageInfo
from crucible.ports.harness import CredentialSource

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _quiet_cli_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI binds its logging to stderr; under capture that stream closes with the
    test, so the binding is skipped here. The CLI's output on stdout is what is read."""
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)


def _token(prefix: str, count: int = 40) -> str:
    return prefix + "x" * count


def seed_credentials(root: Path) -> dict[str, CredentialSource]:
    """Shape-valid auth files for the three harnesses, built at runtime."""
    (root / "claude_code").mkdir(parents=True)
    (root / "claude_code" / "oauth-token").write_text(_token("sk-ant-oat01-"), encoding="utf-8")
    (root / "codex").mkdir()
    (root / "codex" / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": _token("eyJ")}, "last_refresh": "2026-09-17T00:00:00Z"}
        ),
        encoding="utf-8",
    )
    inner = root / "agy" / ".gemini" / "antigravity-cli"
    inner.mkdir(parents=True)
    (inner / "antigravity-oauth-token").write_text(
        json.dumps({"token": {"access_token": _token("ya29."), "expiry": "2026-09-17T01:00:00Z"}}),
        encoding="utf-8",
    )
    for directory in (root / "claude_code", root / "codex", root / "agy"):
        directory.chmod(0o700)
    return {name: CredentialSource(str(root / name)) for name in ("claude_code", "codex", "agy")}


# One script per harness, because the three flows differ in exactly the way the driver
# has to tell apart: Claude Code prompts for a pasted code and then prints a token once,
# Codex prints a device code and never prompts, AGY prompts and prints no token. A single
# script for all three let the Codex leg pass on the banner line without ever reaching the
# paste path. The phrases follow the real CLIs (S1b transcripts); the token is built here.
FAKE_LOGIN_SCRIPTS: dict[str, str] = {
    "claude_code": (
        "#!/bin/bash\n"
        'echo "Visit https://example.invalid/device to authorize, then enter the code here"\n'
        'printf "Paste the code: "\n'
        "read -t 30 -r code\n"
        'echo "token: TOKEN_PLACEHOLDER"\n'
        "exit 0\n"
    ),
    "codex": (
        "#!/bin/bash\n"
        'echo "Open https://example.invalid/device in a browser"\n'
        'echo "Your code is ABCD-EFGH (it expires in 15 minutes)"\n'
        'echo "Waiting for authorization..."\n'
        "exit 0\n"
    ),
    "agy": (
        "#!/bin/bash\n"
        'echo "Sign in at https://example.invalid/oauth and copy the code shown"\n'
        'printf "Enter the code: "\n'
        "read -t 30 -r code\n"
        'echo "Signed in."\n'
        "exit 0\n"
    ),
}


def fake_login_cli(root: Path) -> dict[str, tuple[str, ...]]:
    commands: dict[str, tuple[str, ...]] = {}
    for name, script in FAKE_LOGIN_SCRIPTS.items():
        path = root / f"fake-login-{name}"
        path.write_text(
            script.replace("TOKEN_PLACEHOLDER", _token("sk-ant-oat01-")), encoding="utf-8"
        )
        path.chmod(0o755)
        commands[name] = (str(path),)
    return commands


@pytest.fixture
def credential_root(tmp_path: Path) -> Path:
    root = tmp_path / "credentials"
    root.mkdir()
    return root


@pytest.fixture
def admin_ctx(
    ctx: AppContext, provider: FakeProvider, credential_root: Path, tmp_path: Path
) -> AdminContext:
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        credential_sources=seed_credentials(credential_root),
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
        credential_retention_hours=0,
        login_commands=fake_login_cli(tmp_path),
    )
    ctx.admin = admin
    return admin


@pytest.fixture
def live_supervisor(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    """A supervisor on the system clock, so its lease reads as live to both the API
    (fake clock, earlier) and the CLI (system clock)."""
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="admin-tests",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )


@pytest.fixture
def admin_client(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


@pytest.fixture
def config_file(migrated: str, credential_root: Path, tmp_path: Path) -> Path:
    """The CLI's local mode reads configuration; the same database, the same
    credential directories, the fake logins."""
    logins = fake_login_cli(tmp_path)
    lines = [
        "[database]",
        f'url = "{migrated}"',
        "[supervisor]",
        "lease_ttl_seconds = 300",
        "[admin]",
        "credential_retention_hours = 0",
        "[admin.login_commands]",
        *[f'{name} = ["{argv[0]}"]' for name, argv in logins.items()],
    ]
    for name in ("claude_code", "codex", "agy"):
        lines += [f"[credentials.{name}]", f'path = "{credential_root / name}"']
    path = tmp_path / "crucible.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_cli(config: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> Any:
    cli.main(["--config", str(config), *argv])
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def audit_kinds(client: TestClient) -> list[tuple[str, str]]:
    items = client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
    return [(e["kind"], e["principal"]) for e in items]


def ui_sign_in(client: TestClient, token: str) -> str:
    form = client.get("/ui/sign-in")
    preauth = re.search(r'name="csrf" value="([a-f0-9]+)"', form.text)
    assert form.status_code == 200 and preauth is not None
    response = client.post(
        "/ui/sign-in",
        data={"csrf": preauth.group(1), "token": token, "next": "/ui"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/ui")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_sign_in_rejects_cross_site_form_without_the_preauth_nonce(
    ctx: AppContext, tokens: dict[str, str]
) -> None:
    with TestClient(create_app(ctx)) as browser:
        refused = browser.post(
            "/ui/sign-in",
            data={"token": tokens["admin"], "next": "/ui"},
            follow_redirects=False,
        )
        assert refused.status_code == 403
        assert "CSRF token is invalid" in refused.text
        assert "crucible_ui=" not in refused.headers.get("set-cookie", "")


# ----- the guard --------------------------------------------------------------------


def test_ui_session_csrf_reader_access_and_page_walk(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
) -> None:
    with TestClient(create_app(ctx)) as browser:
        assert browser.get("/ui", follow_redirects=False).status_code == 303
        csrf = ui_sign_in(browser, tokens["observer"])
        for path in (
            "/ui",
            "/ui/harnesses",
            "/ui/credentials",
            "/ui/credentials/hermes/login",
            "/ui/images",
            "/ui/routing",
            "/ui/repositories",
            "/ui/tokens",
            "/ui/github",
            "/ui/workers",
            "/ui/tasks",
            "/ui/wakes",
            "/ui/retention",
            "/ui/audit",
            "/ui/bootstrap",
            "/ui/settings",
        ):
            response = browser.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "Crucible" in response.text
        forbidden = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": csrf,
                "harness": "agy",
                "enabled": "false",
                "reason": "reader must not mutate",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert forbidden.status_code == 303
        assert "admin%20role%20required" in forbidden.headers["location"]


def test_ui_mutation_uses_the_same_harness_service_and_rejects_bad_csrf(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
) -> None:
    asyncio.run(live_supervisor.tick())
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        refused = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": "wrong",
                "harness": "agy",
                "enabled": "false",
                "reason": "bad csrf",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert "CSRF" in refused.headers["location"]
        changed = browser.post(
            "/ui/actions/harness",
            data={
                "csrf": csrf,
                "harness": "agy",
                "enabled": "false",
                "reason": "ui parity test",
                "return_to": "/ui/harnesses",
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303
    with ctx.uow_factory() as uow:
        state = uow.harnesses.get("agy")
        assert state is not None
        assert state.enabled is False and state.reason == "ui parity test"


def test_migrate_creates_and_prints_the_first_admin_once(
    migrated: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli._ensure_first_admin(migrated)
    first = capsys.readouterr().out
    assert "CRUCIBLE FIRST-RUN ADMIN TOKEN, SHOWN ONCE" in first
    shown = next(line for line in first.splitlines() if line.startswith("cru_"))
    cli._ensure_first_admin(migrated)
    assert capsys.readouterr().out == ""
    engine = make_engine(migrated)
    try:
        with SqlUnitOfWorkFactory(engine)() as uow:
            principal = authenticate(uow, shown)
            assert principal is not None
            assert principal.name == "first-run-admin" and principal.role is Role.ADMIN
            uow.principals.disable(principal.id, SystemClock().now())
            uow.commit()
        cli._ensure_first_admin(migrated)
        recovery = capsys.readouterr().out
        recovered_token = next(line for line in recovery.splitlines() if line.startswith("cru_"))
        with SqlUnitOfWorkFactory(engine)() as uow:
            recovered = authenticate(uow, recovered_token)
            assert recovered is not None
            assert recovered.name.startswith("first-run-admin-")
            assert recovered.role is Role.ADMIN and recovered.disabled_at is None
    finally:
        engine.dispose()


def test_token_and_repository_mutations_have_ui_api_and_cli_parity(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api_created = admin_client.post(
        "/v1/admin/tokens",
        json={"name": "api-reader", "role": "observer", "reason": "parity"},
    )
    assert api_created.status_code == 200
    assert api_created.json()["token"].startswith("cru_")
    cli_created = run_cli(
        config_file,
        "--reason",
        "parity",
        "token",
        "create",
        "--principal",
        "cli-reader",
        "--role",
        "observer",
        capsys=capsys,
    )
    assert set(cli_created) == {"principal", "role", "token"}
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        ui_created = browser.post(
            "/ui/actions/token-create",
            data={
                "csrf": csrf,
                "name": "ui-reader",
                "role": "observer",
                "reason": "parity",
                "return_to": "/ui/tokens",
            },
        )
        assert ui_created.status_code == 200
        assert re.search(r"cru_[A-Z0-9]{26}\.[A-Za-z0-9_-]+", ui_created.text)

        principals = admin_client.get("/v1/admin/tokens").json()["items"]
        ids = {item["name"]: item["id"] for item in principals}
        ui_revoked = browser.post(
            "/ui/actions/token-revoke",
            data={
                "csrf": csrf,
                "principal_id": ids["api-reader"],
                "reason": "parity",
                "return_to": "/ui/tokens",
            },
            follow_redirects=False,
        )
        assert ui_revoked.status_code == 303
    api_revoked = admin_client.post(
        f"/v1/admin/tokens/{ids['ui-reader']}/revoke", json={"reason": "parity"}
    )
    assert api_revoked.json()["revoked"] is True
    cli_revoked = run_cli(
        config_file,
        "--reason",
        "parity",
        "token",
        "revoke",
        ids["cli-reader"],
        capsys=capsys,
    )
    assert cli_revoked["revoked"] is True
    assert {
        item["name"] for item in run_cli(config_file, "token", "list", capsys=capsys)["items"]
    } >= {
        "api-reader",
        "cli-reader",
        "ui-reader",
    }

    for name in ("api-remove", "cli-remove", "ui-remove"):
        response = admin_client.put(
            f"/v1/admin/repositories/{name}",
            json={
                "url": f"https://github.com/example-org/{name}",
                "attested_all_prs": True,
                "reason": "parity",
            },
        )
        assert response.status_code == 200
    assert admin_client.request(
        "DELETE", "/v1/admin/repositories/api-remove", json={"reason": "parity"}
    ).json()["removed"]
    assert run_cli(
        config_file,
        "--reason",
        "parity",
        "repositories",
        "remove",
        "cli-remove",
        capsys=capsys,
    )["removed"]
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        removed = browser.post(
            "/ui/actions/repository-remove",
            data={
                "csrf": csrf,
                "name": "ui-remove",
                "reason": "parity",
                "return_to": "/ui/repositories",
            },
            follow_redirects=False,
        )
    assert removed.status_code == 303
    assert {
        item["repository"] for item in admin_client.get("/v1/admin/repositories").json()["items"]
    }.isdisjoint({"api-remove", "cli-remove", "ui-remove"})


def test_every_remaining_ui_mutation_dispatches_to_the_shared_application_service(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API and CLI parity tests above exercise the services themselves. This matrix
    proves every other mutating UI form reaches those same service functions."""
    ui = import_module("crucible.adapters.ui.router")
    calls: list[str] = []

    def stub(name: str) -> Any:
        def called(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return {"ok": True}

        return called

    def async_stub(name: str) -> Any:
        async def called(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(name)
            return {"ok": True}

        return called

    for owner, name, replacement in (
        (ui.credentials, "validate", async_stub("credential-validate")),
        (ui.credentials, "probe", async_stub("credential-probe")),
        (ui.credentials, "rotate", stub("credential-rotate")),
        (ui.credentials, "remove", stub("credential-remove")),
        (ui.login, "start_login", stub("login-start")),
        (ui.login, "submit_code", stub("login-code")),
        (ui.login, "cancel_login", stub("login-cancel")),
        (ui.login, "finish_login", stub("login-finish")),
        (ui.images, "promote", async_stub("image-promote")),
        (ui.routing, "clear_exhaustion", stub("routing-clear")),
        (ui.repositories, "register", stub("repository-register")),
        (ui.repositories, "remove", stub("repository-remove")),
        (ui.github, "check", stub("github-check")),
        (ui.bootstrap, "commit", stub("bootstrap-commit")),
    ):
        monkeypatch.setattr(owner, name, replacement)
    monkeypatch.setattr(ui, "put_routing_policy", stub("routing-upload"))
    monkeypatch.setattr(ui, "put_policy", stub("policy-upload"))

    asyncio.run(live_supervisor.tick())
    common = {"reason": "UI dispatch parity", "return_to": "/ui"}
    requests = [
        ("credential", {"verb": "validate", "harness": "codex"}),
        ("credential", {"verb": "probe", "harness": "codex"}),
        (
            "credential",
            {"verb": "rotate", "harness": "codex", "new_path": "/prepared/codex"},
        ),
        ("credential", {"verb": "remove", "harness": "codex"}),
        ("login-start", {"harness": "codex"}),
        ("login-code", {"harness": "codex", "code": "fixture-code"}),
        ("login-cancel", {"harness": "codex"}),
        ("login-finish", {"harness": "codex"}),
        ("image-promote", {"digest": "sha256:" + "a" * 64}),
        ("routing-clear", {"pool": "primary"}),
        (
            "routing-upload",
            {"name": "fixture-routing", "version": "1", "document": "{}"},
        ),
        (
            "policy-upload",
            {"name": "fixture-policy", "version": "1", "document": "{}"},
        ),
        (
            "repository-register",
            {
                "name": "fixture-repository",
                "url": "https://example.invalid/repository.git",
                "default_branch": "main",
                "policy_name": "default-software",
            },
        ),
        ("repository-remove", {"name": "fixture-repository"}),
        ("github-check", {}),
        ("bootstrap-commit", {"import_id": "01TESTIMPORT00000000000000"}),
    ]
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        for action, fields in requests:
            response = browser.post(
                f"/ui/actions/{action}",
                data={"csrf": csrf, **common, **fields},
                follow_redirects=False,
            )
            assert response.status_code == 303, (action, response.text)
    assert calls == [
        "credential-validate",
        "credential-probe",
        "credential-rotate",
        "credential-remove",
        "login-start",
        "login-code",
        "login-cancel",
        "login-finish",
        "image-promote",
        "routing-clear",
        "routing-upload",
        "policy-upload",
        "repository-register",
        "repository-remove",
        "github-check",
        "bootstrap-commit",
    ]


def test_a_mutation_is_refused_without_a_live_supervisor(
    admin_client: TestClient, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    response = admin_client.post("/v1/admin/harnesses/agy/disable", json={"reason": "test"})
    assert response.status_code == 503, response.text
    assert response.json()["type"].endswith("supervisor-not-live")
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config_file), "--reason", "test", "harnesses", "disable", "agy"])
    assert "supervisor-not-live" in capsys.readouterr().err


def test_a_mutation_requires_a_reason(
    admin_client: TestClient, live_supervisor: Supervisor
) -> None:
    asyncio.run(live_supervisor.tick())
    response = admin_client.post("/v1/admin/harnesses/agy/disable", json={})
    assert response.status_code == 422
    assert response.json()["errors"][0]["path"] == "reason"


# ----- parity: every operation through both entry points ----------------------------


def test_harnesses_list_enable_disable_through_api_and_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api = admin_client.get("/v1/admin/harnesses").json()["items"]
    local = run_cli(config_file, "harnesses", "list", capsys=capsys)["items"]
    assert [h["name"] for h in api] == [h["name"] for h in local]
    assert {h["name"]: h["credential"]["state"] for h in api} == {
        "claude_code": "configured",
        "codex": "configured",
        "agy": "configured",
        "hermes": "not_required",
        "script-harness": "not_required",
    }
    disabled = admin_client.post(
        "/v1/admin/harnesses/agy/disable", json={"reason": "api: rotating"}
    ).json()
    assert disabled["enabled"] is False and disabled["reason"] == "api: rotating"
    enabled = run_cli(
        config_file, "--reason", "cli: rotated", "harnesses", "enable", "agy", capsys=capsys
    )
    assert enabled["enabled"] is True and enabled["reason"] == "cli: rotated"
    kinds = audit_kinds(admin_client)
    assert ("harness_disabled", "admin-principal") in kinds
    assert ("harness_enabled", "crucible-admin") in kinds


def test_credentials_validate_and_probe_through_api_and_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    response = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "onboarding"}
    )
    assert response.status_code == 200, response.text
    validated = response.json()
    assert validated["validated"] is True
    assert validated["shape"]["ok"] and validated["probe"]["exit_class"] == "completed"
    assert validated["credential"]["state"] == "validated"
    assert provider.probes == ["codex"]
    # Nothing secret-shaped in the whole document.
    from crucible.domain.secrets import scan_text  # noqa: PLC0415

    assert scan_text(json.dumps(validated)) is None
    probed = run_cli(
        config_file,
        "--reason",
        "cli probe",
        "credentials",
        "probe",
        "--harness",
        "agy",
        capsys=capsys,
    )
    assert probed["probe"]["harness_version"] == "fake"
    assert probed["probe"]["auth_files_changed"] is False
    assert set(probed["probe"]) == {
        "harness",
        "exit_class",
        "exit_code",
        "harness_version",
        "image",
        "image_digest",
        "auth_files_changed",
        "mount_mode",
        "duration_seconds",
        "files",
        "detail",
        "conclusive",
        "cause",
    }
    kinds = audit_kinds(admin_client)
    assert ("credential_validated", "admin-principal") in kinds
    assert ("credential_probed", "crucible-admin") in kinds
    status = run_cli(config_file, "credentials", "status", "--harness", "codex", capsys=capsys)
    assert status["state"] == "validated" and status["last_validated_at"]


def test_credentials_rotate_and_remove_through_api_and_cli(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    config_file: Path,
    credential_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    old_token = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    incoming = tmp_path / "incoming-codex"
    incoming.mkdir()
    new_secret = _token("eyJ", 60)
    (incoming / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": new_secret}, "last_refresh": "2026-09-17T02:00:00Z"}
        ),
        encoding="utf-8",
    )
    rotated = admin_client.post(
        "/v1/admin/credentials/codex/rotate",
        json={"reason": "new login", "new_path": str(incoming)},
    ).json()
    assert rotated["retained_as"].startswith("codex.retired-")
    swapped = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    assert new_secret in swapped and swapped != old_token
    retired = credential_root / rotated["retained_as"]
    assert (retired / "auth.json").read_text(encoding="utf-8") == old_token
    # The caller's directory is untouched: Crucible copies it and never destroys a path
    # the operator named outside its own credential root.
    assert (incoming / "auth.json").read_text(encoding="utf-8").strip()
    assert rotated["source_kept"] == str(incoming)
    assert new_secret not in json.dumps(rotated) and old_token not in json.dumps(rotated)

    # Retention is 0 h here: the retired directory is shredded by the sweep.
    from crucible.application.admin.credentials import sweep_retired  # noqa: PLC0415

    time.sleep(1.1)
    with admin_ctx.uow_factory() as uow:
        assert sweep_retired(admin_ctx, uow, principal="crucible-admin") == 1
        uow.commit()
    assert not retired.exists()

    removed = run_cli(
        config_file,
        "--reason",
        "leaving",
        "credentials",
        "remove",
        "--harness",
        "agy",
        capsys=capsys,
    )
    assert removed["shredded"]["files"] == 1
    assert removed["credential"]["state"] == "absent"
    assert (credential_root / "agy").is_dir()
    assert not any(p.is_file() for p in (credential_root / "agy").rglob("*"))
    harness = next(
        h for h in admin_client.get("/v1/admin/harnesses").json()["items"] if h["name"] == "agy"
    )
    assert harness["enabled"] is False and "credential removed" in harness["reason"]
    kinds = audit_kinds(admin_client)
    for kind, who in (
        ("credential_rotated", "admin-principal"),
        ("credential_retired_shredded", "crucible-admin"),
        ("credential_removed", "crucible-admin"),
    ):
        assert (kind, who) in kinds, kinds


def test_login_through_api_and_cli_against_the_fake_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    refused = admin_client.post(
        "/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"}
    )
    assert refused.status_code == 409, refused.text
    assert "already passes the shape check" in refused.json()["detail"]
    started = admin_client.post(
        "/v1/admin/credentials/claude_code/login",
        json={"reason": "onboarding", "replace": True},
    ).json()
    assert "captured to oauth-token" in started["window"]
    assert started["retained_as"].startswith("claude_code.retired-")
    for _ in range(100):
        state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
        if state["state"] == "waiting_for_code":
            break
        time.sleep(0.05)
    assert state["url"] == "https://example.invalid/device", state
    admin_client.post(
        "/v1/admin/credentials/claude_code/login/code",
        json={"code": "ABCD-EFGH", "reason": "complete onboarding"},
    )
    for _ in range(100):
        state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
        if state["state"] in ("finished", "failed"):
            break
        time.sleep(0.05)
    assert state["state"] == "finished", state
    assert state["token_written"] is True
    finished = admin_client.post(
        "/v1/admin/credentials/claude_code/login/finish", json={"reason": "onboarded"}
    ).json()
    assert finished["shape"]["ok"]
    assert "sk-ant-" not in json.dumps(finished) + json.dumps(state)

    monkeypatch.setattr("builtins.input", lambda prompt="": "ABCD-EFGH")
    result = run_cli(
        config_file,
        "--reason",
        "cli onboarding",
        "credentials",
        "login",
        "--harness",
        "codex",
        "--replace",
        capsys=capsys,
    )
    assert result["login"]["state"] == "finished" and result["login"]["code"] == "ABCD-EFGH"
    kinds = audit_kinds(admin_client)
    assert ("credential_login_started", "admin-principal") in kinds
    assert ("credential_login_finished", "crucible-admin") in kinds


def test_images_list_and_promote_through_api_and_cli(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    provider: FakeProvider,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    # The fake provider lists what a test hands it (08); the CLI's own provider lists
    # nothing, so the promotion is exercised through the API and its refusal through
    # both.
    image = ImageInfo(
        reference="crucible-worker:codex-0.153.4-test",
        digest="sha256:" + "d" * 64,
        harness="codex",
        harness_version="0.153.4",
    )
    provider.images = [image]
    listed = admin_client.get("/v1/admin/images").json()["items"]
    assert listed[0]["promotion_state"] == "candidate" and listed[0]["supported"] is True
    promoted = admin_client.post(
        f"/v1/admin/images/{image.digest}/promote", json={"reason": "canary passed"}
    ).json()
    assert promoted["promotion_state"] == "default"
    provider.images = [
        image,
        ImageInfo("crucible-worker:codex-0.153.4-next", "sha256:" + "e" * 64, "codex", "0.153.4"),
    ]
    again = admin_client.post(
        "/v1/admin/images/sha256:" + "e" * 64 + "/promote", json={"reason": "next canary"}
    ).json()
    assert again["retained"] == [image.digest]
    states = {
        i["digest"]: i["promotion_state"]
        for i in admin_client.get("/v1/admin/images").json()["items"]
    }
    assert states[image.digest] == "retained" and states["sha256:" + "e" * 64] == "default"
    assert run_cli(config_file, "images", "list", capsys=capsys)["items"] == []
    with pytest.raises(SystemExit):
        cli.main(
            ["--config", str(config_file), "--reason", "x", "images", "promote", "sha256:nope"]
        )
    assert "not-found" in capsys.readouterr().err
    assert ("image_promoted", "admin-principal") in audit_kinds(admin_client)


def test_providers_github_audit_status_and_capabilities(
    ctx: AppContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    tokens: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    api_providers = admin_client.get("/v1/admin/providers").json()["items"]
    cli_providers = run_cli(config_file, "providers", "status", capsys=capsys)["items"]
    assert api_providers[0]["health"] == "ok" and cli_providers[0]["health"] == "ok"

    api_github = admin_client.get("/v1/admin/github").json()
    assert api_github["configured"] is False and api_github["key_present"] is False
    assert api_github["repositories"][0]["repository"] == "example-service"
    assert run_cli(config_file, "github", "status", capsys=capsys)["configured"] is False
    assert admin_client.post("/v1/admin/github/check", json={"reason": "x"}).status_code == 409
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config_file), "--reason", "x", "github", "check"])

    # A registration under /admin is a mutation like any other: a reason, and a live lease.
    assert (
        admin_client.put(
            "/v1/admin/repositories/second",
            json={"url": "https://github.com/example-org/second", "attested_all_prs": True},
        ).status_code
        == 422
    )
    registered = admin_client.put(
        "/v1/admin/repositories/second",
        json={
            "url": "https://github.com/example-org/second",
            "attested_all_prs": True,
            "reason": "onboarding the second repository",
        },
    ).json()
    assert registered["repository"] == "second"
    from_cli = run_cli(
        config_file,
        "--reason",
        "onboarding the third repository",
        "repositories",
        "register",
        "--name",
        "third",
        "--url",
        "https://github.com/example-org/third",
        "--attest-external-review-all-prs",
        capsys=capsys,
    )
    # One service, one document: the two entry points differ only in the values.
    assert set(from_cli) == set(registered)
    registration_events = [
        e
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if e["kind"] == "repository_registered" and e["payload"]["repository"] == "second"
    ]
    assert registration_events[0]["payload"]["reason"] == "onboarding the second repository"

    document = admin_client.get("/v1/admin/status").json()
    assert set(document) == {
        "harnesses",
        "credentials",
        "providers",
        "github",
        "supervisor",
        "workers",
        "tasks",
        "wakes",
        "retention",
        "bootstrap",
        "audit",
    }
    assert document["supervisor"]["healthy"] is True
    assert document["bootstrap"] == {"authoritative": None, "imports": []}
    assert {r["repository"] for r in document["github"]["repositories"]} >= {"second", "third"}
    local = run_cli(config_file, "status", capsys=capsys)
    assert set(local) == set(document)

    tail = admin_client.get("/v1/admin/audit", params={"limit": 2}).json()
    assert len(tail["items"]) == 2 and tail["next_cursor"] == tail["items"][-1]["seq"]
    rest = admin_client.get(
        "/v1/admin/audit", params={"cursor": tail["next_cursor"], "limit": 200}
    ).json()
    assert all(e["seq"] > tail["next_cursor"] for e in rest["items"])
    kinds = {e["kind"] for e in tail["items"] + rest["items"]}
    assert "repository_registered" in kinds and "attempt_running" not in kinds
    assert (
        run_cli(config_file, "audit", "tail", "--limit", "2", capsys=capsys)["items"]
        == tail["items"]
    )

    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['orchestrator']}"}
    ) as orchestrator:
        view = orchestrator.get("/v1/capabilities").json()
        assert set(view) == {"harnesses", "providers", "github", "workers", "tasks", "wakes"}
        assert set(view["harnesses"][0]["credential"]) == {"state", "session_compatibility"}
        assert orchestrator.get("/v1/admin/status").status_code == 403
        assert (
            orchestrator.post("/v1/admin/harnesses/agy/disable", json={"reason": "x"}).status_code
            == 403
        )
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['observer']}"}
    ) as observer:
        assert observer.get("/v1/capabilities").status_code == 403


def test_the_cli_remote_mode_builds_the_same_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(cli.Remote, "call", fake_call)
    monkeypatch.setenv(cli.TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    cli.main(["--api-url", "http://127.0.0.1:1", "--reason", "r", "harnesses", "disable", "agy"])
    cli.main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "credentials",
            "probe",
            "--harness",
            "codex",
        ]
    )
    cli.main(["--api-url", "http://127.0.0.1:1", "audit", "tail", "--cursor", "5", "--limit", "10"])
    cli.main(["--api-url", "http://127.0.0.1:1", "routing", "exhaustion"])
    cli.main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "routing",
            "clear-exhaustion",
            "pool-a",
        ]
    )
    assert calls == [
        ("POST", "/v1/admin/harnesses/agy/disable", {"reason": "r"}),
        ("POST", "/v1/admin/credentials/codex/probe", {"reason": "r"}),
        ("GET", "/v1/admin/audit?limit=10&cursor=5", None),
        ("GET", "/v1/admin/routing/exhaustion", None),
        ("POST", "/v1/admin/routing/exhaustion/pool-a/clear", {"reason": "r"}),
    ]
    assert Role.ADMIN.value == "admin"


def test_remote_login_submits_the_reason_with_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, Any]] = []
    states = iter(
        [
            {"state": "waiting_for_code", "output_tail": []},
            {"state": "finished", "output_tail": []},
        ]
    )

    def fake_call(self: Any, method: str, path: str, body: Any = None) -> Any:
        calls.append((method, path, body))
        if method == "GET":
            return next(states)
        if path.endswith("/login"):
            return {"window": "login window"}
        return {"state": "finished"}

    monkeypatch.setattr(cli.Remote, "call", fake_call)
    monkeypatch.setattr("crucible.cli.admin.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "operator-code")
    monkeypatch.setenv(cli.TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    cli.main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "operator approved login",
            "credentials",
            "login",
            "--harness",
            "claude_code",
        ]
    )

    assert (
        "POST",
        "/v1/admin/credentials/claude_code/login/code",
        {"reason": "operator approved login", "code": "operator-code"},
    ) in calls


# ----- the correction round ----------------------------------------------------------


def test_a_secret_shaped_reason_is_refused_on_both_entry_points(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """B2: the reason is written to the append-only event log and served back by
    `GET /admin/audit`, so a pasted token would be stored for ever."""
    asyncio.run(live_supervisor.tick())
    secret = _token("sk-ant-oat01-")
    response = admin_client.post(
        "/v1/admin/harnesses/agy/disable", json={"reason": f"rotating {secret}"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["path"] == "reason"
    assert secret not in response.text
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--config",
                str(config_file),
                "--reason",
                f"rotating {secret}",
                "harnesses",
                "disable",
                "agy",
            ]
        )
    err = capsys.readouterr().err
    assert "contract-validation" in err or "reason" in err
    assert secret not in err
    kinds = audit_kinds(admin_client)
    assert ("admin_refused", "admin-principal") in kinds, kinds
    assert secret not in json.dumps(
        admin_client.get("/v1/admin/audit", params={"limit": 200}).json()
    )


def test_registering_a_repository_takes_both_guards_on_both_entry_points(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """B3: the admin route called the legacy service directly, so it registered with no
    reason and with the supervisor down."""
    body = {"url": "https://github.com/example-org/guarded", "attested_all_prs": True}
    down = admin_client.put("/v1/admin/repositories/guarded", json={**body, "reason": "x"})
    assert down.status_code == 503, down.text
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--config",
                str(config_file),
                "--reason",
                "x",
                "repositories",
                "register",
                "--name",
                "guarded-cli",
                "--url",
                "https://github.com/example-org/guarded-cli",
                "--attest-external-review-all-prs",
            ]
        )
    assert "supervisor-not-live" in capsys.readouterr().err
    asyncio.run(live_supervisor.tick())
    assert admin_client.put("/v1/admin/repositories/guarded", json=body).status_code == 422
    with pytest.raises(SystemExit):
        cli.main(
            [
                "--config",
                str(config_file),
                "repositories",
                "register",
                "--name",
                "guarded-cli",
                "--url",
                "https://github.com/example-org/guarded-cli",
                "--attest-external-review-all-prs",
            ]
        )
    assert "reason" in capsys.readouterr().err


def test_finishing_a_login_takes_both_guards(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """B4: it writes session_compatibility and clears last_validated_at, so it is a
    mutation and was the one without the guards."""
    no_lease = admin_client.post(
        "/v1/admin/credentials/codex/login/finish", json={"reason": "done"}
    )
    assert no_lease.status_code == 503, no_lease.text
    asyncio.run(live_supervisor.tick())
    no_reason = admin_client.post("/v1/admin/credentials/codex/login/finish", json={})
    assert no_reason.status_code == 422, no_reason.text
    assert no_reason.json()["errors"][0]["path"] == "reason"


def test_a_failed_swap_leaves_the_configured_directory_exactly_as_it_was(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B6: the swap is two renames. When the second failed the configured path was gone,
    the exception propagated before any event, and every later launch failed on a missing
    credential with nothing in the audit to say why."""
    import crucible.application.admin.credentials as credentials_module  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    before = (credential_root / "codex" / "auth.json").read_text(encoding="utf-8")
    incoming = tmp_path / "incoming-rollback"
    incoming.mkdir()
    (incoming / "auth.json").write_text(
        json.dumps(
            {"tokens": {"refresh_token": _token("eyJ", 60)}, "last_refresh": "2026-09-17T03:00:00Z"}
        ),
        encoding="utf-8",
    )
    real_rename = os.rename
    calls = {"n": 0}

    def failing(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(18, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing)
    with admin_ctx.uow_factory() as uow, pytest.raises(ApplicationError) as caught:
        credentials_module.rotate(
            admin_ctx,
            uow,
            principal="admin-principal",
            harness="codex",
            new_path=str(incoming),
            reason="a swap that fails",
        )
    assert "rolled back" in str(caught.value.detail)
    monkeypatch.undo()
    assert (credential_root / "codex" / "auth.json").read_text(encoding="utf-8") == before
    assert not any(p.name.startswith("codex.incoming-") for p in credential_root.iterdir())
    assert not any(p.name.startswith("codex.retired-") for p in credential_root.iterdir())
    assert (incoming / "auth.json").is_file(), "the caller's directory is never touched"


def test_an_inconclusive_probe_leaves_the_credential_state_alone(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """A probe that timed out is evidence about the run, not about the credential. The
    first version of this fix marked the credential invalid on any unsuccessful probe,
    which would take a working harness out of service on latency alone."""
    asyncio.run(live_supervisor.tick())
    first = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "first pass"}
    ).json()
    assert first["validated"] is True and first["conclusive"] is True
    assert first["credential"]["state"] == "validated"

    provider.probe_outcome = "timeout"
    slow = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "a slow daemon"}
    ).json()
    assert slow["validated"] is False
    assert slow["conclusive"] is False and slow["cause"] == "timeout"
    assert slow["probe"]["conclusive"] is False
    assert slow["credential"]["state"] == "validated", slow["credential"]
    status = admin_client.get("/v1/admin/status").json()
    assert status["credentials"]["codex"]["last_launch_outcome"] == "probe:inconclusive:timeout"

    provider.probe_outcome = "auth_failure"
    refused = admin_client.post(
        "/v1/admin/credentials/codex/validate", json={"reason": "after a revoked token"}
    ).json()
    assert refused["validated"] is False
    assert refused["conclusive"] is True and refused["cause"] == ""
    assert refused["probe"]["exit_class"] == "auth_failure"
    assert refused["credential"]["state"] == "invalid", refused["credential"]
    assert admin_client.get("/v1/admin/credentials/codex").json()["state"] == "invalid"


def test_a_probe_the_provider_never_answered_is_inconclusive(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that will not answer says nothing about the credential either, and the
    operator gets a record with the cause rather than a 500."""
    from crucible.ports.execution import ProviderError  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())

    async def refuse(_request: object) -> None:
        raise ProviderError("the daemon socket is not there")

    monkeypatch.setattr(provider, "probe_credential", refuse)
    with admin_ctx.uow_factory() as uow:
        before = credentials_state(admin_ctx, uow, "codex")
    with admin_ctx.uow_factory() as uow:
        report = asyncio.run(
            credentials_module_probe(admin_ctx, uow, harness="codex", reason="daemon down")
        )
        uow.commit()
    assert report.probe is not None
    assert report.probe.conclusive is False
    assert report.probe.cause == "provider_unavailable"
    assert report.probe.detail.startswith("ProviderError")
    # Whatever the state was, it is what it still is: nothing was observed.
    assert report.state["state"] == before["state"]
    assert report.state["last_auth_failure_at"] == before["last_auth_failure_at"]
    assert report.state["last_launch_outcome"] == "probe:inconclusive:provider_unavailable"


def credentials_state(ctx: AdminContext, uow: Any, harness: str) -> Any:
    from crucible.application.admin import credentials as credentials_module  # noqa: PLC0415

    return credentials_module.state_view(ctx, uow, harness)


def credentials_module_probe(ctx: AdminContext, uow: Any, *, harness: str, reason: str) -> Any:
    from crucible.application.admin import credentials as credentials_module  # noqa: PLC0415

    return credentials_module.probe(
        ctx, uow, principal="admin-principal", harness=harness, reason=reason
    )


# ----- the second correction round --------------------------------------------------


def test_a_login_that_cannot_run_refuses_with_the_credential_still_at_its_path(
    admin_client: TestClient,
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    tmp_path: Path,
) -> None:
    """The harness CLIs are in the worker images and not in the Crucible service image
    (13), so on a normal deployment every login refuses on the missing executable. That
    refusal used to happen after the retire, which renamed a valid credential out of the
    configured path and left the retention sweep free to shred it. Nothing moves until
    every precondition that can refuse has been checked."""
    asyncio.run(live_supervisor.tick())
    admin_ctx.login_commands["agy"] = (str(tmp_path / "no-such-cli"),)
    before = {
        path.relative_to(credential_root): path.read_bytes()
        for path in (credential_root / "agy").rglob("*")
        if path.is_file()
    }
    assert before, "the fixture credential is the thing under test"
    refused = admin_client.post(
        "/v1/admin/credentials/agy/login", json={"reason": "onboarding", "replace": True}
    )
    assert refused.status_code == 409, refused.text
    assert "is not installed on this host" in refused.json()["detail"]
    after = {
        path.relative_to(credential_root): path.read_bytes()
        for path in (credential_root / "agy").rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not [p for p in credential_root.iterdir() if p.name.startswith("agy.retired-")]
    # The credential is still the harness's credential: present at the configured path,
    # not `absent`, which is what a retire with no login behind it would have left.
    assert admin_client.get("/v1/admin/credentials/agy").json()["state"] != "absent"


def test_a_start_that_fails_after_the_retire_puts_the_credential_back(
    admin_ctx: AdminContext,
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """The failure path is safe, not merely unlikely: the retire is a rename and a rename
    does not roll back with the transaction. A start that fails after a successful retire
    renames the credential back and records the failure."""
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")

    class StartFails:
        """Every precondition passes and the start itself falls over: the daemon thread
        could not be created, the CLI vanished between the check and the spawn."""

        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex",)

        def start(self, _ctx: AdminContext, _harness: str, _directory: str) -> None:
            raise RuntimeError("the login thread could not be started")

        def get(self, _harness: str) -> None:
            return None

    with admin_ctx.uow_factory() as uow:
        with pytest.raises(ApplicationError) as raised:
            start_login(
                admin_ctx,
                uow,
                StartFails(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="onboarding",
                replace=True,
            )
        # The caller's transaction rolls back with the exception and takes the retire
        # event with it; the directory has to come back on its own.
        uow.rollback()
    assert "put back at its configured path" in str(raised.value.detail)
    assert live.read_text(encoding="utf-8") == before
    assert not [p for p in credential_root.iterdir() if p.name.startswith("codex.retired-")]
    assert admin_client.get("/v1/admin/credentials/codex").json()["state"] != "absent"
    refusals = [
        e
        for e in admin_client.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        if e["kind"] == "admin_refused"
    ]
    assert any(
        r["payload"]["operation"] == "credentials login codex"
        and "renamed back to the configured path" in r["payload"]["detail"]
        for r in refusals
    ), refusals


def test_container_login_checks_promotion_before_retiring_a_credential(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")
    asyncio.run(live_supervisor.tick())

    class ContainerRegistry:
        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex", "login", "--device-auth")

        def container_runner(self, _ctx: AdminContext) -> object:
            return object()

    with admin_ctx.uow_factory() as uow:
        monkeypatch.setattr(uow.image_promotions, "list_all", lambda: [])
        with pytest.raises(ApplicationError, match="no promoted worker image"):
            start_login(
                admin_ctx,
                uow,
                ContainerRegistry(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="promotion precondition",
                replace=True,
            )
        uow.rollback()
    assert live.read_text(encoding="utf-8") == before
    assert not list(credential_root.glob("codex.retired-*"))


def test_container_login_restores_a_credential_when_replacement_mkdir_fails(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    credential_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crucible.application.admin.login import start_login  # noqa: PLC0415

    live = credential_root / "codex" / "auth.json"
    before = live.read_text(encoding="utf-8")
    asyncio.run(live_supervisor.tick())

    class ContainerRegistry:
        def resolve(self, _ctx: AdminContext, _harness: str) -> tuple[str, ...]:
            return ("codex", "login", "--device-auth")

        def container_runner(self, _ctx: AdminContext) -> object:
            return object()

        def start(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("mkdir must fail before the login thread starts")

    original_mkdir = Path.mkdir

    def fail_replacement_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == credential_root / "codex":
            raise OSError("fixture mkdir failure")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_replacement_mkdir)
    with admin_ctx.uow_factory() as uow:
        uow.image_promotions.put(
            ImagePromotion(
                digest="sha256:" + "b" * 64,
                reference="crucible-worker:codex-fixture",
                harness="codex",
                harness_version="fixture",
                state="default",
                updated_at=admin_ctx.clock.now(),
                updated_by="tests",
                reason="mkdir rollback test",
            )
        )
        uow.commit()
    with admin_ctx.uow_factory() as uow:
        with pytest.raises(ApplicationError, match="put back at its configured path"):
            start_login(
                admin_ctx,
                uow,
                ContainerRegistry(),  # type: ignore[arg-type]
                principal="admin-principal",
                harness="codex",
                reason="mkdir rollback",
                replace=True,
            )
        uow.rollback()
    assert live.read_text(encoding="utf-8") == before
    assert not list(credential_root.glob("codex.retired-*"))


def test_the_probe_uses_the_model_of_the_routing_policy_in_force(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """The seeded `default-software` version 2 names `default-routing` version 2, whose
    cheapest enabled codex model is `gpt-5.6-luna`."""
    asyncio.run(live_supervisor.tick())
    response = admin_client.post("/v1/admin/credentials/codex/probe", json={"reason": "onboarding"})
    assert response.status_code == 200, response.text
    assert provider.probe_requests[-1].harness == "codex"
    assert "gpt-5.6-luna" in provider.probe_requests[-1].argv


def test_the_probe_refuses_rather_than_falling_back_to_a_retired_model(
    admin_ctx: AdminContext,
    live_supervisor: Supervisor,
    provider: FakeProvider,
) -> None:
    """The probe took its model from the routing policy the policy in force names and
    then from the seeded `default-routing` versions 2 and 1, so a policy in force with no
    enabled model for a harness silently ran a model the operator had disabled or
    removed. Only the policy in force decides, and no enabled model for the harness in it
    is a refusal.

    The policy in force is put in place inside a transaction that is rolled back: the
    policy tables are not truncated between tests, and a superseding version with a
    disabled harness would be in force for every test that follows."""
    from crucible.application.admin.credentials import (  # noqa: PLC0415
        _probe_model,
        adapter_for,
    )
    from crucible.domain.entities import Policy, RoutingPolicyRecord  # noqa: PLC0415

    asyncio.run(live_supervisor.tick())
    now = SystemClock().now()
    with admin_ctx.uow_factory() as uow:
        seeded_routing = uow.routing_policies.get("default-routing", 2)
        assert seeded_routing is not None
        routing = copy.deepcopy(seeded_routing.document)
        routing["version"] = 99
        for model in routing["models"]:
            if model["harness"] == "codex":
                model["enabled"] = False
        uow.routing_policies.put(
            RoutingPolicyRecord(
                name="default-routing", version=99, document=routing, created_at=now
            )
        )
        seeded_policy = uow.policies.get("default-software", 2)
        assert seeded_policy is not None
        policy = copy.deepcopy(seeded_policy.document)
        policy["version"] = 99
        policy["routing"]["policy"] = {"name": "default-routing", "version": 99}
        uow.policies.put(
            Policy(name="default-software", version=99, document=policy, created_at=now)
        )
        probes_before = len(provider.probe_requests)
        # Through the service the operator calls, not just the selector inside it.
        with pytest.raises(ApplicationError) as raised:
            asyncio.run(
                credentials_module_probe(
                    admin_ctx, uow, harness="codex", reason="after retiring the codex models"
                )
            )
        detail = str(raised.value.detail)
        assert "default-routing version 99" in detail and "no enabled model" in detail
        assert "does not fall back" in detail
        # No fallback: a seeded model the policy in force does not name is never reached.
        assert "gpt-5.6-luna" not in detail
        # A harness the policy in force still enables takes its model from that policy.
        assert (
            _probe_model(uow, adapter_for(admin_ctx, "claude_code"), "claude_code")
            == "claude-haiku-4-5"
        )
        uow.rollback()
    # The refusal happened before the provider was asked to run anything.
    assert len(provider.probe_requests) == probes_before


def test_a_read_only_credential_directory_is_still_replaceable(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """A replacement renames the directory aside and the CLI creates a fresh one, so only
    the credential root has to be writable. Requiring the directory itself refused an
    operator who deliberately holds a credential directory read-only, and it refused with
    a reason that was not the real precondition.

    The refusal each precondition gives is its own: an existing credential with no
    `replace` is refused for being an existing credential, not for a mode."""
    asyncio.run(live_supervisor.tick())
    live = credential_root / "claude_code"
    old_token = (live / "oauth-token").read_text(encoding="utf-8")
    live.chmod(0o500)
    try:
        refused = admin_client.post(
            "/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "already passes the shape check" in detail
        assert "not writable" not in detail

        started = admin_client.post(
            "/v1/admin/credentials/claude_code/login",
            json={"reason": "onboarding", "replace": True},
        )
        assert started.status_code == 200, started.text
        retained = started.json()["retained_as"]
        assert retained.startswith("claude_code.retired-")
        for _ in range(100):
            state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
            if state["state"] == "waiting_for_code":
                break
            time.sleep(0.05)
        admin_client.post(
            "/v1/admin/credentials/claude_code/login/code",
            json={"code": "ABCD-EFGH", "reason": "complete onboarding"},
        )
        for _ in range(100):
            state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
            if state["state"] in ("finished", "failed"):
                break
            time.sleep(0.05)
        assert state["state"] == "finished", state
        # The credential is at its configured path, and it is the new one.
        new_token = (live / "oauth-token").read_text(encoding="utf-8")
        assert new_token.strip() and new_token != old_token
        assert (credential_root / retained / "oauth-token").read_text(encoding="utf-8") == old_token
        assert admin_client.get("/v1/admin/credentials/claude_code").json()["state"] != "absent"
    finally:
        # The temporary tree has to be removable again whatever the test did.
        for path in (live, *credential_root.glob("claude_code.retired-*")):
            if path.is_dir():
                path.chmod(0o700)


def test_a_login_that_reuses_an_unwritable_directory_says_so(
    admin_client: TestClient,
    live_supervisor: Supervisor,
    credential_root: Path,
) -> None:
    """The directory check still exists for the path that keeps it: nothing at the
    configured path passes the shape check, so the login writes into the directory as it
    stands, and an unwritable one is refused for exactly that."""
    asyncio.run(live_supervisor.tick())
    live = credential_root / "codex"
    (live / "auth.json").unlink()
    live.chmod(0o500)
    try:
        refused = admin_client.post(
            "/v1/admin/credentials/codex/login", json={"reason": "onboarding"}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert "is not writable" in detail and str(live) in detail
        assert "already passes the shape check" not in detail
    finally:
        live.chmod(0o700)
