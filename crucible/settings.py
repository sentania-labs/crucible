"""Configuration: environment (CRUCIBLE_ prefix, `__` nesting) over an optional TOML file
named by CRUCIBLE_CONFIG. Precedence: constructor, environment, TOML, defaults. Sanitized
example in examples/config/."""

from __future__ import annotations

import os
from typing import Literal

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
    checkout_lease_ttl_seconds: int = 21600
    grace_seconds: int = 60
    holder: str | None = None


class DockerSettings(BaseModel):
    """The Docker execution provider (08, 13).

    `host` is the socket proxy, never the raw socket. Under the rootless arrangement
    (S9) the Crucible service runs as uid 1000 and the artifact root is a named volume
    it shares with every container it creates, so `mount_kind` stays `volume`;
    developer mode uses `bind` with the daemon-visible path of the artifact root.
    """

    enabled: bool = False
    host: str = "tcp://docker-socket-proxy:2375"
    api_timeout_seconds: float = 30.0
    mount_kind: Literal["volume", "bind"] = "volume"
    artifact_volume: str = "crucible-artifacts"
    artifact_host_root: str | None = None
    credential_root: str | None = None
    credential_host_root: str | None = None
    workers_network: str = "crucible-workers"
    egress_proxy: str | None = "http://egress-proxy:3128"
    # What the egress proxy is configured to permit. An attempt whose allowlist is not
    # a subset of this is refused at launch rather than quietly running with less
    # network than the policy promised.
    egress_allowlist: list[str] = Field(default_factory=list)
    no_proxy: str = "localhost,127.0.0.1"
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    report_size_cap_bytes: int = 10 * 1024 * 1024
    workspace_dir_mode: int = 0o755
    use_reference_cache: bool = True
    max_concurrency: int = 3
    extra_image_allowlist: list[str] = Field(default_factory=list)


class GitHubSettings(BaseModel):
    """GitHub delivery (12, 23).

    `private_key_path` and `webhook_secret_path` are paths to files Crucible reads; no
    key, secret, or token is ever a configuration *value*. The paths come from
    configuration precisely so that no operator path is ever hardcoded in the
    repository."""

    enabled: bool = False
    app_id: int = 0
    private_key_path: str | None = None
    webhook_secret_path: str | None = None
    api_base: str = "https://api.github.com"
    api_timeout_seconds: float = 20.0
    # 23 defaults. The reaction poll is part of every cycle, not an extra: GitHub emits
    # no webhook for a reaction and the reviewer's clean verdict is one.
    poll_interval_seconds: int = 120
    reactions_poll_interval_seconds: int = 60
    # The optional accelerator, off by default on a workstation (Q13).
    webhook_enabled: bool = False
    # The publisher container. The image is the attempt's own worker image unless a
    # deployment pins one; the network is the egress network with github.com allowed.
    publisher_image: str | None = None
    publisher_network: str | None = None
    publisher_timeout_seconds: int = 600
    credential_host: str = "github.com"
    # 23: posting a comment under Crucible's App identity is refused by the provider and
    # is not Crucible's act anyway. Off unless a deployment deliberately turns it on.
    allow_issue_comments: bool = False
    ci_log_excerpt_bytes: int = 64 * 1024


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
    docker: DockerSettings = Field(default_factory=DockerSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
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
