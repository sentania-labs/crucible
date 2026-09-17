"""Configuration: environment (CRUCIBLE_ prefix, `__` nesting) over an optional TOML file
named by CRUCIBLE_CONFIG. Precedence: constructor, environment, TOML, defaults. Sanitized
example in examples/config/."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


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


class WakeSettings(BaseModel):
    """Wake delivery (17). The secret comes from the environment and is never stored."""

    webhook_url: str | None = None
    secret: str | None = None
    timeout_seconds: float = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRUCIBLE_", env_nested_delimiter="__", extra="ignore", toml_file=None
    )

    service: ServiceSettings = Field(default_factory=ServiceSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    supervisor: SupervisorSettings = Field(default_factory=SupervisorSettings)
    wake: WakeSettings = Field(default_factory=WakeSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier sources win: the environment overrides the TOML file.
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


def load_settings(config_path: str | None = None) -> Settings:
    """Settings with the TOML file (argument, else CRUCIBLE_CONFIG) as the lowest source."""
    path = config_path or os.environ.get("CRUCIBLE_CONFIG")

    class ConfiguredSettings(Settings):
        model_config = SettingsConfigDict(**{**Settings.model_config, "toml_file": path})

    return ConfiguredSettings()
