"""Configuration: environment (CRUCIBLE_ prefix, `__` nesting) over an optional TOML file
named by CRUCIBLE_CONFIG. Precedence: constructor, environment, TOML, defaults. Sanitized
example in examples/config/."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from crucible.domain.endpoints import validate_endpoint


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
    credential_volume: str = "crucible-credentials"
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


class KubernetesSettings(BaseModel):
    """The Kubernetes execution provider (08, 26).

    Off by default: a deployment that has not had the namespaces, the default-deny
    NetworkPolicy, the storage class and the pod PID limit prepared for it (26's
    checklist) would only fail at the readiness probe, and Docker stays the development
    provider either way.

    `kubeconfig` is a path or None for the in-cluster ServiceAccount. Nothing here is
    ever a credential value: the ServiceAccount token is a file the kubelet rotates and
    the registry credential is the cluster's own image pull Secret (12).
    """

    enabled: bool = False
    namespace: str = "crucible"
    workers_namespace: str = "crucible-workers"
    kubeconfig: str | None = None
    kubeconfig_context: str | None = None
    api_timeout_seconds: float = 30.0
    service_account: str = "crucible-worker"
    storage_class: str = ""
    workspace_size: str = "20Gi"
    image_pull_secret: str | None = None
    # The cluster-side PersistentVolumeClaim holding the git reference cache the
    # preparer clones from. Without one the preparer clones from the remote.
    cache_claim: str | None = None
    # 26: a Pod Pending longer than this is a launch failure with the Pod's conditions
    # as the detail, never a stall.
    launch_timeout_seconds: int = 300
    prepare_timeout_seconds: int = 900
    collector_timeout_seconds: int = 900
    verifier_timeout_seconds: int = 3600
    report_size_cap_bytes: int = 10 * 1024 * 1024
    poll_interval_seconds: float = 2.0
    max_concurrency: int = 3
    # The cluster DNS service address. 26 allows port 53 on this address and nothing
    # else on it, and denies everything else inside the cluster.
    cluster_dns_ip: str = "10.96.0.10"
    # The cluster resolver's pods, allowed on port 53 beside the address above. A CNI
    # that translates a service address to its pods before it evaluates policy (Cilium
    # with kube-proxy replacement) matches only this (crucible#91). An empty namespace
    # leaves the address rule alone. These seed the `kubernetes.egress` admin setting;
    # a value saved through the admin API, CLI or UI wins over them.
    dns_namespace: str = "kube-system"
    dns_pod_labels: dict[str, str] = Field(default_factory=lambda: {"k8s-app": "kube-dns"})
    # An in-cluster local model endpoint (a LiteLLM gateway behind a Service): its
    # namespace, its pods' labels, and the pods' port (0: the endpoint URL's port). An
    # empty namespace means the endpoint is outside the cluster and keeps its resolved
    # address rule. Also seeds of `kubernetes.egress`.
    local_endpoint_namespace: str = ""
    local_endpoint_pod_labels: dict[str, str] = Field(default_factory=dict)
    local_endpoint_port: int = 0
    # What a worker's egress rule excludes: the API server, the node network, other
    # namespaces' pod network, link-local, and the lab's private ranges (26).
    denied_cidrs: list[str] = Field(
        default_factory=lambda: [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
            "100.64.0.0/10",
            "127.0.0.0/8",
        ]
    )
    # Private ranges a named local endpoint may resolve into. Literal IP endpoint URLs
    # are still refused. Keep this narrower than the cluster service and pod ranges.
    local_endpoint_cidrs: list[str] = Field(default_factory=list)
    # The worker image repositories `GET /admin/images` reports the promoted tags of.
    # Bare repositories: the provider appends each tag the registry lists.
    image_repositories: list[str] = Field(default_factory=list)
    # One exact, pullable worker image reference for 26's namespace readiness canary.
    #
    # The canary runs the first image the provider knows of, and until an attempt has
    # resolved one that is the first entry of `image_repositories`, which is a bare
    # repository and therefore means `:latest` to a kubelet. A cluster whose registry has
    # no `latest` then has a canary stuck in ImagePullBackOff until the launch timeout,
    # and a status page that reports the namespace as not ready for a reason that is not
    # about the namespace. Naming the promoted worker image here is what a deployment
    # does about that. It is its own field on the provider and never an entry of
    # `image_repositories`, so the images listing does not take it for a repository.
    probe_image: str = ""
    # The harness credential Secret in the workers namespace, per harness (12, 26).
    credential_secrets: dict[str, str] = Field(default_factory=dict)
    extra_image_allowlist: list[str] = Field(default_factory=list)
    use_reference_cache: bool = True


class CredentialSettings(BaseModel):
    """One harness's credential directory (12). `path` is a directory Crucible reads and
    seeds per-attempt copies from; no value is ever configuration. `mount_mode` may raise
    the adapter's declared minimum to `rw-narrow` and never lowers it (25 step 7)."""

    source: Literal["directory"] = "directory"
    path: str | None = None
    mount_mode: Literal["ro", "rw-narrow"] | None = None


class HarnessSettings(BaseModel):
    """The operator's configuration gate for a harness (25). A harness whose dedicated
    session compatibility is unverified ships disabled with the reason recorded (S1b);
    the runtime enable flag an administrator flips lives in the database beside it, and
    a launch needs both."""

    enabled: bool = True
    reason: str = ""


class GitHubAppSettings(BaseModel):
    """The App's identity and the files Crucible reads to use it (12).

    `app_id` is a public identifier. `private_key_path` and `webhook_secret_path` are
    paths to files; no key, secret, or token is ever a configuration *value*, and the
    paths come from configuration precisely so no operator path is ever hardcoded in
    this repository."""

    app_id: int = 0
    private_key_path: str | None = None
    webhook_secret_path: str | None = None


class GitHubSettings(BaseModel):
    """GitHub delivery (12, 23)."""

    enabled: bool = False
    app: GitHubAppSettings = Field(default_factory=GitHubAppSettings)
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
    # 23 step 3: the publisher runs on an egress network whose allowlist is `github.com`
    # and `api.github.com` and nothing else. That is a narrower list than the workers'
    # proxy permits, so the publisher gets its own network and proxy by default; a
    # deployment that has only one may point this at it deliberately.
    publisher_network: str = "crucible-publish"
    publisher_egress_proxy: str | None = None
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


class AdminSettings(BaseModel):
    """The administrative surface (25)."""

    # How long a rotated-out credential directory is retained before it is shredded.
    credential_retention_hours: int = 24
    # The bounded auth probe's hard timeout (25: 120 s).
    probe_timeout_seconds: int = 120
    # How long an interactive login may wait for the operator in total.
    login_timeout_seconds: int = 900
    # The command each harness's login runs, overriding the adapter's own. Meant for the
    # parity tests, which drive a fake CLI that mimics each flow; never a production knob.
    login_commands: dict[str, list[str]] = Field(default_factory=dict)
    # Shared with the egress proxy in Docker deployments. A UI save rewrites this file
    # from the same renderer as `make proxy-config`.
    proxy_config_path: str | None = None
    proxy_subnet: str = "10.88.0.0/24"
    proxy_reload_timeout_seconds: float = 0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRUCIBLE_", env_nested_delimiter="__", extra="ignore", toml_file=None
    )

    service: ServiceSettings = Field(default_factory=ServiceSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    supervisor: SupervisorSettings = Field(default_factory=SupervisorSettings)
    docker: DockerSettings = Field(default_factory=DockerSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    wake: WakeSettings = Field(default_factory=WakeSettings)
    credentials: dict[str, CredentialSettings] = Field(default_factory=dict)
    harnesses: dict[str, HarnessSettings] = Field(default_factory=dict)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    local_endpoint_url: str | None = None
    # Compatibility seed for deployments created before C10. Database routing state
    # wins after the migration has created a local entry.
    spark_endpoint_url: str | None = None

    @field_validator("local_endpoint_url", "spark_endpoint_url")
    @classmethod
    def _valid_spark_endpoint(cls, value: str | None) -> str | None:
        if value:
            validate_endpoint("local", value)
        return value

    @property
    def endpoint_seed(self) -> str | None:
        return self.local_endpoint_url or self.spark_endpoint_url

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
