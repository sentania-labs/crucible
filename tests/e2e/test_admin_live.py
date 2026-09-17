"""The administrative surface against a live stack on the rootless daemon (25): every
row of the operations table through the API and through `crucible-admin` in local mode,
and the bounded probe run for each of the three harnesses with the dedicated
credentials (a live run per harness, recorded like the live tier's).

Two credential roots are in play, on purpose. The probes run against the dedicated root
(`CRUCIBLE_LIVE_CREDENTIAL_ROOT`), so a token the CLI refreshes during the probe is
written back where it belongs. The destructive operations (rotate, remove) run against
scratch copies of that root inside the artifact root, so nothing here ever moves,
shreds or replaces the operator's dedicated credentials. The login is not run against a
provider (the dedicated credentials exist); the fake CLIs of the integration tier cover
the driver.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.admin.context import AdminContext, GitHubAppInfo
from crucible.application.auth import mint_token
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from crucible.domain.entities import Role
from crucible.domain.secrets import scan_text
from crucible.ports.harness import CredentialSource, MountMode
from tests.e2e import github_live
from tests.e2e.conftest import RUN_ID
from tests.e2e.test_live_harness import (
    ALL_HARNESSES,
    CREDENTIAL_ROOT_ENV,
    REPORT_ENV,
    _secret_values,
)

LOCAL_TZ = ZoneInfo("America/Chicago")


def _why_not() -> str:
    root = os.environ.get(CREDENTIAL_ROOT_ENV, "")
    if not root or not Path(root).is_dir():
        return f"set {CREDENTIAL_ROOT_ENV} to the dedicated Crucible credential root (12)"
    return ""


NOT_CONFIGURED = _why_not()
pytestmark = [
    pytest.mark.e2e_admin,
    pytest.mark.skipif(bool(NOT_CONFIGURED), reason=NOT_CONFIGURED or "configured"),
]


def _local(moment: datetime) -> str:
    return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _record(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, sort_keys=True)
    print(f"admin-live: {line}")
    target = os.environ.get(REPORT_ENV, "").strip()
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@pytest.fixture(scope="session")
def dedicated_root() -> Path:
    return Path(os.environ[CREDENTIAL_ROOT_ENV])


@pytest.fixture
def scratch_root(artifact_root: Path, dedicated_root: Path) -> Iterator[Path]:
    """Copies of the dedicated directories, mode 700, inside the artifact root; what
    rotate and remove act on. Shredded at the end whatever the test did."""
    root = artifact_root / f"scratch-credentials-{RUN_ID}"
    root.mkdir(mode=0o700)
    for harness in ALL_HARNESSES:
        shutil.copytree(dedicated_root / harness, root / harness, symlinks=False)
        os.chmod(root / harness, 0o700)
    yield root
    from crucible.application.admin.credentials import shred_tree  # noqa: PLC0415

    for path in root.iterdir():
        if path.is_dir():
            shred_tree(path, keep_root=False)
    shutil.rmtree(root, ignore_errors=True)


def _provider(docker_config: DockerConfig, sources: dict[str, CredentialSource]) -> DockerProvider:
    registry = default_registry()
    endpoints = {h for a in registry for h in a.capabilities().endpoints}
    config = replace(
        docker_config,
        credentials=sources,
        proxy_allowlist=tuple(sorted(set(docker_config.proxy_allowlist) | endpoints)),
    )
    return DockerProvider(config, harnesses=registry)


def _sources(root: Path) -> dict[str, CredentialSource]:
    return {
        "claude_code": CredentialSource(str(root / "claude_code")),
        "codex": CredentialSource(str(root / "codex")),
        "agy": CredentialSource(str(root / "agy"), MountMode.RW_NARROW),
    }


def _context(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    provider: DockerProvider,
    sources: dict[str, CredentialSource],
) -> AppContext:
    github_env = github_live.why_not_configured() == ""
    app = GitHubAppInfo()
    github = None
    if github_env:
        config = github_live.load_config()
        app = GitHubAppInfo(app_id=config.app_id, private_key_path=config.private_key_path)
        github = github_live.client(config)
    admin = AdminContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers={"docker": provider},
        harnesses=provider.harnesses,
        credential_sources=sources,
        github=github,
        github_app=app,
        artifact_root=str(artifact_root),
        lease_ttl_seconds=600,
        credential_retention_hours=0,
        probe_timeout_seconds=120,
    )
    return AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers=[provider],
        database_url=migrated,
        engine=engine,
        artifact_store=DiskArtifactStore(artifact_root / "store"),
        harnesses=provider.harnesses,
        credential_sources=sources,
        admin=admin,
    )


@pytest.fixture
def probe_ctx(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    docker_config: DockerConfig,
    dedicated_root: Path,
) -> AppContext:
    sources = _sources(dedicated_root)
    return _context(engine, migrated, artifact_root, _provider(docker_config, sources), sources)


@pytest.fixture
def scratch_ctx(
    engine: Engine,
    migrated: str,
    artifact_root: Path,
    docker_config: DockerConfig,
    scratch_root: Path,
) -> AppContext:
    sources = _sources(scratch_root)
    return _context(engine, migrated, artifact_root, _provider(docker_config, sources), sources)


def _tokens(ctx: AppContext) -> dict[str, str]:
    out: dict[str, str] = {}
    with ctx.uow_factory() as uow:
        for role in Role:
            out[role.value] = mint_token(
                uow, ctx.clock, name=f"{role.value}-principal-{RUN_ID}", role=role
            ).token
        uow.commit()
    return out


def _client(ctx: AppContext, token: str) -> TestClient:
    return TestClient(create_app(ctx), headers={"Authorization": f"Bearer {token}"})


def _config_file(
    artifact_root: Path, migrated: str, stack: dict[str, Any], sources: dict[str, CredentialSource]
) -> Path:
    lines = [
        "[database]",
        f'url = "{migrated}"',
        "[supervisor]",
        "lease_ttl_seconds = 600",
        "[docker]",
        "enabled = true",
        f'host = "{stack["docker_host"]}"',
        'mount_kind = "bind"',
        f'artifact_host_root = "{artifact_root}"',
        'artifact_volume = ""',
        f'workers_network = "{stack["workers_network"]}"',
        f'egress_proxy = "{stack["egress_proxy"]}"',
        "workspace_dir_mode = 511",
        "use_reference_cache = false",
        "[service]",
        f'artifact_root = "{artifact_root}"',
        "[admin]",
        "credential_retention_hours = 0",
        "probe_timeout_seconds = 120",
    ]
    for harness, source in sources.items():
        lines += [f"[credentials.{harness}]", f'path = "{source.path}"']
        if source.mount_mode:
            lines.append(f'mount_mode = "{source.mount_mode.value}"')
    path = artifact_root / f"crucible-admin-{RUN_ID}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cli(config: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> Any:
    cli.main(["--config", str(config), *argv])
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


@pytest.fixture(autouse=True)
def _quiet_cli_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)


def _supervisor(ctx: AppContext, provider: DockerProvider) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"docker": provider},
        SystemClock(),
        holder=f"e2e-admin-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=600,
        harnesses=provider.harnesses,
    )


async def test_the_probe_runs_live_for_each_harness_through_api_and_cli(
    probe_ctx: AppContext,
    stack: dict[str, Any],
    artifact_root: Path,
    migrated: str,
    dedicated_root: Path,
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = probe_ctx.providers[0]
    assert isinstance(provider, DockerProvider)
    await _supervisor(probe_ctx, provider).tick()
    tokens = _tokens(probe_ctx)
    assert probe_ctx.admin is not None
    config = _config_file(artifact_root, migrated, stack, probe_ctx.admin.credential_sources)
    # The pinned images must be the only ones the probe can choose, or it refuses.
    for harness in ALL_HARNESSES:
        images = [i for i in await provider.list_images() if i.harness == harness]
        if len({i.reference for i in images}) > 1:
            from tests.e2e import daemon  # noqa: PLC0415

            pinned = daemon.manifest_pins()[harness]
            with _client(probe_ctx, tokens["admin"]) as admin:
                promoted = admin.post(
                    f"/v1/admin/images/{pinned}/promote",
                    json={"reason": "e2e-admin: the manifest pin is the probe's image"},
                )
                assert promoted.status_code == 200, promoted.text
    entries: list[dict[str, Any]] = []
    with _client(probe_ctx, tokens["admin"]) as admin:
        for index, harness in enumerate(ALL_HARNESSES):
            secrets = _secret_values(dedicated_root, harness)
            started = datetime.now(UTC)
            if index % 2 == 0:
                response = admin.post(
                    f"/v1/admin/credentials/{harness}/probe", json={"reason": "e2e-admin probe"}
                )
                assert response.status_code == 200, response.text
                report = response.json()
                entry_point = "api"
            else:
                report = _cli(
                    config,
                    "--reason",
                    "e2e-admin probe",
                    "credentials",
                    "probe",
                    "--harness",
                    harness,
                    capsys=capsys,
                )
                entry_point = "cli"
            probe = report["probe"]
            entry = {
                "harness": harness,
                "entry_point": entry_point,
                "started_local": _local(started),
                "image": probe["image"],
                "image_digest": probe["image_digest"],
                "harness_version": probe["harness_version"],
                "exit_class": probe["exit_class"],
                "exit_code": probe["exit_code"],
                "duration_seconds": probe["duration_seconds"],
                "mount_mode": probe["mount_mode"],
                "auth_files_changed": probe["auth_files_changed"],
                "files": probe["files"],
            }
            entries.append(entry)
            _record(entry)
            assert probe["exit_class"] == "completed", entry
            assert set(probe) == {
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
            }, "the probe records what 25 allows and nothing else"
            blob = json.dumps(report)
            for value in secrets:
                assert value not in blob
            assert scan_text(blob) is None
        # The whole record set, the events, every text column: no credential value.
        haystack = [admin.get("/v1/admin/audit", params={"limit": 200}).text]
        with engine.begin() as connection:
            for table, column in connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type IN "
                    "('text', 'character varying', 'jsonb')"
                )
            ).all():
                values = connection.execute(
                    text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}"')
                ).scalars()
                haystack.extend(str(v) for v in values if v is not None)
        blob = "\n".join(haystack)
        for harness in ALL_HARNESSES:
            for value in _secret_values(dedicated_root, harness):
                assert value not in blob, "a credential value reached a Crucible record"
        assert scan_text(blob) is None
        # No probe workspace or credential copy is left behind.
        leftovers = [p for p in (artifact_root / "workspaces").glob("probe*")]
        assert leftovers == [], leftovers
        kinds = [
            e["kind"] for e in admin.get("/v1/admin/audit", params={"limit": 200}).json()["items"]
        ]
        assert kinds.count("credential_probed") == 3
        status = admin.get("/v1/admin/status").json()
        assert all(
            status["credentials"][h]["last_launch_outcome"] == "probe:completed"
            for h in ALL_HARNESSES
        ), status["credentials"]


async def test_every_other_operation_through_api_and_cli_on_the_live_stack(
    scratch_ctx: AppContext,
    stack: dict[str, Any],
    artifact_root: Path,
    migrated: str,
    scratch_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = scratch_ctx.providers[0]
    assert isinstance(provider, DockerProvider)
    await _supervisor(scratch_ctx, provider).tick()
    tokens = _tokens(scratch_ctx)
    assert scratch_ctx.admin is not None
    config = _config_file(artifact_root, migrated, stack, scratch_ctx.admin.credential_sources)
    with _client(scratch_ctx, tokens["admin"]) as admin:
        # harnesses: list, disable, enable
        listed = admin.get("/v1/admin/harnesses").json()["items"]
        assert {h["name"] for h in listed} == {*ALL_HARNESSES, "script-harness"}
        assert all(h["installed_versions"] for h in listed if h["name"] != "script-harness")
        assert (
            admin.post("/v1/admin/harnesses/codex/disable", json={"reason": "live: pause"}).json()[
                "enabled"
            ]
            is False
        )
        assert (
            _cli(config, "--reason", "live: resume", "harnesses", "enable", "codex", capsys=capsys)[
                "enabled"
            ]
            is True
        )
        # validate (shape plus the bounded probe) on the scratch copy, through the CLI
        validated = _cli(
            config,
            "--reason",
            "live validate",
            "credentials",
            "validate",
            "--harness",
            "claude_code",
            capsys=capsys,
        )
        assert validated["validated"] is True, validated
        _record(
            {
                "harness": "claude_code",
                "entry_point": "cli validate",
                "started_local": _local(datetime.now(UTC)),
                **validated["probe"],
            }
        )
        # rotate the scratch codex copy with a prepared directory (a copy of itself)
        incoming = artifact_root / f"incoming-codex-{RUN_ID}"
        shutil.copytree(scratch_root / "codex", incoming)
        rotated = admin.post(
            "/v1/admin/credentials/codex/rotate",
            json={"reason": "live rotate", "new_path": str(incoming)},
        )
        assert rotated.status_code == 200, rotated.text
        retained = scratch_root / rotated.json()["retained_as"]
        assert retained.is_dir() and (scratch_root / "codex" / "auth.json").is_file()
        assert list(incoming.iterdir()) == [], "the staging copy was shredded"
        from crucible.application.admin.credentials import sweep_retired  # noqa: PLC0415

        time.sleep(1.1)
        with scratch_ctx.uow_factory() as uow:
            assert sweep_retired(scratch_ctx.admin, uow, principal="e2e-admin") == 1
            uow.commit()
        assert not retained.exists()
        # remove the scratch agy copy through the CLI
        removed = _cli(
            config,
            "--reason",
            "live remove",
            "credentials",
            "remove",
            "--harness",
            "agy",
            capsys=capsys,
        )
        assert removed["credential"]["state"] == "absent"
        assert not any(p.is_file() for p in (scratch_root / "agy").rglob("*"))
        # images: list and promote the pinned codex image
        images = admin.get("/v1/admin/images").json()["items"]
        codex_images = [i for i in images if i["harness"] == "codex"]
        assert codex_images
        from tests.e2e import daemon  # noqa: PLC0415

        pin = daemon.manifest_pins()["codex"]
        promoted = _cli(config, "--reason", "live promote", "images", "promote", pin, capsys=capsys)
        assert promoted["promotion_state"] == "default"
        states = {
            i["reference"]: i["promotion_state"]
            for i in admin.get("/v1/admin/images").json()["items"]
        }
        assert states[pin] == "default"
        # providers and github
        health = admin.get("/v1/admin/providers").json()["items"][0]
        assert health["name"] == "docker" and health["health"] == "ok", health
        assert _cli(config, "providers", "status", capsys=capsys)["items"][0]["health"] == "ok"
        github_status = admin.get("/v1/admin/github").json()
        if github_status["configured"]:
            assert github_status["key_fingerprint"].startswith("sha256:")
            with scratch_ctx.uow_factory() as uow:
                from crucible.application.repositories import register_repository  # noqa: PLC0415
                from crucible.contracts.api import (  # noqa: PLC0415
                    ExternalReviewAttestation,
                    RepositoryRegistration,
                )

                live = github_live.load_config()
                register_repository(
                    uow,
                    scratch_ctx.clock,
                    principal_name="e2e-admin",
                    name=live.repository,
                    registration=RepositoryRegistration(
                        url=live.https_url,
                        default_branch="main",
                        policy_name="default-software",
                        installation_id=live.installation_id,
                        external_review=ExternalReviewAttestation(
                            attested_all_prs=True, attested_by="operator"
                        ),
                    ),
                )
                uow.commit()
            checked = admin.post("/v1/admin/github/check", json={"reason": "live check"}).json()
            assert checked["repositories"][0]["ok"] is True, checked
            assert "ghs_" not in json.dumps(checked)
        # audit and status, capabilities for the orchestrator
        tail = _cli(config, "audit", "tail", "--limit", "200", capsys=capsys)
        kinds = {e["kind"] for e in tail["items"]}
        assert {
            "harness_disabled",
            "harness_enabled",
            "credential_validated",
            "credential_rotated",
            "credential_removed",
            "image_promoted",
        } <= kinds
        document = admin.get("/v1/admin/status").json()
        assert document["providers"][0]["health"] == "ok"
        assert scan_text(json.dumps(document)) is None
    with _client(scratch_ctx, tokens["orchestrator"]) as orchestrator:
        view = orchestrator.get("/v1/capabilities").json()
        assert set(view) == {"harnesses", "providers", "github", "workers", "tasks", "wakes"}
        assert orchestrator.get("/v1/admin/status").status_code == 403
