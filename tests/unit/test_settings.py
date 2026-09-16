from __future__ import annotations

from pathlib import Path

import pytest

from crucible.settings import load_settings


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
