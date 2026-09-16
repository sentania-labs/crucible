"""Configuration: environment (CRUCIBLE_ prefix, `__` nesting) plus an optional TOML file
named by CRUCIBLE_CONFIG. Sanitized example in examples/config/."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseModel):
    bind: str = "0.0.0.0:8080"
    render_timezone: str = "UTC"
    artifact_root: str = "/var/lib/crucible/artifacts"
    log_level: str = "INFO"

    @property
    def host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])


class DatabaseSettings(BaseModel):
    url: str = "postgresql+psycopg://crucible:CHANGE_ME@localhost:5432/crucible"


class SupervisorSettings(BaseModel):
    tick_seconds: float = 5.0
    lease_ttl_seconds: int = 30
    reconcile_interval_seconds: int = 60
    attempt_lease_ttl_seconds: int = 60
    grace_seconds: int = 60
    holder: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRUCIBLE_", env_nested_delimiter="__", extra="ignore"
    )

    service: ServiceSettings = Field(default_factory=ServiceSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    supervisor: SupervisorSettings = Field(default_factory=SupervisorSettings)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(config_path: str | None = None) -> Settings:
    """TOML values are the base; environment variables override them."""
    path = config_path or os.environ.get("CRUCIBLE_CONFIG")
    base: dict[str, Any] = {}
    if path:
        base = _load_toml(Path(path))
    known = {"service", "database", "supervisor"}
    return Settings(**{k: v for k, v in base.items() if k in known})
