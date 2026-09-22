from __future__ import annotations

from pathlib import Path

import pytest

from crucible.cli.wiring import kubernetes_config
from crucible.settings import Settings, load_settings


def test_defaults_without_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUCIBLE_CONFIG", raising=False)
    monkeypatch.delenv("CRUCIBLE_DATABASE__URL", raising=False)
    settings = load_settings()
    assert settings.database.url.startswith("postgresql+psycopg://crucible:CHANGE_ME@localhost")
    assert settings.supervisor.tick_seconds == 5.0


def test_toml_values_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUCIBLE_DATABASE__URL", raising=False)
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        '[database]\nurl = "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"\n'
        "[supervisor]\ntick_seconds = 2\n[docker]\nendpoint = 'ignored-in-c1'\n"
    )
    settings = load_settings(str(cfg))
    assert settings.database.url == "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"
    assert settings.supervisor.tick_seconds == 2.0


def test_environment_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        '[database]\nurl = "postgresql+psycopg://file:CHANGE_ME@filehost:5432/crucible"\n'
        "[supervisor]\ntick_seconds = 2\nlease_ttl_seconds = 45\n"
    )
    monkeypatch.setenv(
        "CRUCIBLE_DATABASE__URL", "postgresql+psycopg://env:CHANGE_ME@envhost:5432/crucible"
    )
    monkeypatch.setenv("CRUCIBLE_SUPERVISOR__TICK_SECONDS", "1")
    settings = load_settings(str(cfg))
    assert settings.database.url == "postgresql+psycopg://env:CHANGE_ME@envhost:5432/crucible"
    assert settings.supervisor.tick_seconds == 1.0
    assert settings.supervisor.lease_ttl_seconds == 45, "file values not overridden still apply"


def test_config_env_var_names_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[service]\nbind = "127.0.0.1:9090"\n')
    monkeypatch.setenv("CRUCIBLE_CONFIG", str(cfg))
    monkeypatch.delenv("CRUCIBLE_SERVICE__BIND", raising=False)
    assert load_settings().service.port == 9090


def test_credentials_and_harness_gates_from_toml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """12 and 25: a credential is a path and a mount mode, a harness gate is a flag with
    its reason; both come from the file and either can be overridden from the environment.
    Nothing here is a value."""
    cfg = tmp_path / "crucible.toml"
    cfg.write_text(
        "[credentials.codex]\n"
        'path = "/var/lib/crucible/credentials/codex"\n'
        'mount_mode = "rw-narrow"\n'
        "[credentials.agy]\n"
        'path = "/var/lib/crucible/credentials/agy"\n'
        "[harnesses.codex]\n"
        "enabled = false\n"
        'reason = "unverified (S1b)"\n'
    )
    monkeypatch.setenv("CRUCIBLE_CREDENTIALS__AGY__MOUNT_MODE", "rw-narrow")
    monkeypatch.setenv("CRUCIBLE_HARNESSES__AGY__ENABLED", "false")
    monkeypatch.setenv("CRUCIBLE_HARNESSES__AGY__REASON", "probe pending")
    settings = load_settings(str(cfg))
    assert settings.credentials["codex"].path == "/var/lib/crucible/credentials/codex"
    assert settings.credentials["codex"].mount_mode == "rw-narrow"
    assert settings.credentials["agy"].mount_mode == "rw-narrow", "the environment overrides"
    assert settings.harnesses["codex"].enabled is False
    assert settings.harnesses["codex"].reason == "unverified (S1b)"
    assert settings.harnesses["agy"].enabled is False
    assert settings.harnesses["agy"].reason == "probe pending"
    # A harness with no entry is not gated by configuration.
    assert "claude_code" not in settings.harnesses


def test_probe_image_leads_the_image_repositories_the_provider_is_given() -> None:
    """26's readiness canary runs the first reference the provider knows of, and a bare
    repository means `:latest` to a kubelet. `probe_image` is how a deployment names an
    exact one, and the wiring is what puts it first."""
    settings = Settings(
        kubernetes={
            "enabled": True,
            "image_repositories": ["registry.example/crucible-worker"],
            "probe_image": "registry.example/crucible-worker:script-harness-1.0.0",
        }
    )
    config = kubernetes_config(settings)
    assert config.image_repositories == (
        "registry.example/crucible-worker:script-harness-1.0.0",
        "registry.example/crucible-worker",
    )


def test_without_a_probe_image_the_repositories_are_passed_through() -> None:
    settings = Settings(kubernetes={"image_repositories": ["registry.example/w"]})
    assert kubernetes_config(settings).image_repositories == ("registry.example/w",)
