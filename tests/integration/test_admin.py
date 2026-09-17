"""The administrative surface (25) on the fake provider: every row of the operations
table through the API and through `crucible-admin` in local mode, both landing in the
same audit trail; a mutation refused without a live supervisor lease; the orchestrator's
read-only view. The fake CLIs stand in for the three logins; every secret-shaped value
is built at runtime.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from crucible.domain.entities import Role
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


def fake_login_cli(root: Path) -> dict[str, tuple[str, ...]]:
    path = root / "fake-login"
    path.write_text(
        "#!/bin/bash\n"
        'echo "Visit https://example.invalid/device and enter code ABCD-EFGH"\n'
        'printf "Paste the code: "\n'
        "read -t 30 -r code\n"
        f'echo "token: {_token("sk-ant-oat01-")}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return {name: (str(path),) for name in ("claude_code", "codex", "agy")}


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


# ----- the guard --------------------------------------------------------------------


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
    # The staging copy was shredded once the swap landed.
    assert list(incoming.iterdir()) == []
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
    started = admin_client.post(
        "/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"}
    ).json()
    assert "captured to oauth-token" in started["window"]
    for _ in range(100):
        state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
        if state["state"] == "waiting_for_code":
            break
        time.sleep(0.05)
    assert state["url"] == "https://example.invalid/device", state
    admin_client.post("/v1/admin/credentials/claude_code/login/code", json={"code": "ABCD-EFGH"})
    for _ in range(100):
        state = admin_client.get("/v1/admin/credentials/claude_code/login").json()
        if state["state"] in ("finished", "failed"):
            break
        time.sleep(0.05)
    assert state["state"] == "finished", state
    assert state["token_written"] is True
    finished = admin_client.post("/v1/admin/credentials/claude_code/login/finish").json()
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

    registered = admin_client.put(
        "/v1/admin/repositories/second",
        json={"url": "https://github.com/example-org/second", "attested_all_prs": True},
    ).json()
    assert registered["repository"] == "second"
    run_cli(
        config_file,
        "repositories",
        "register",
        "--name",
        "third",
        "--url",
        "https://github.com/example-org/third",
        "--attest-external-review-all-prs",
        capsys=capsys,
    )

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
        "audit",
    }
    assert document["supervisor"]["healthy"] is True
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
    assert calls == [
        ("POST", "/v1/admin/harnesses/agy/disable", {"reason": "r"}),
        ("POST", "/v1/admin/credentials/codex/probe", {"reason": "r"}),
        ("GET", "/v1/admin/audit?limit=10&cursor=5", None),
    ]
    assert Role.ADMIN.value == "admin"
