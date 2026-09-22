"""Compose the application from settings. Used by both CLIs."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sapi import (
    KubernetesApiError,
    KubernetesClient,
    in_cluster_access,
    kubeconfig_access,
)
from crucible.adapters.execution.k8sregistry import HttpRegistryClient
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.transport import RestTransport
from crucible.adapters.harness.registry import default_registry
from crucible.adapters.notification.webhook import WebhookWakeDeliverer
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.admin.context import AdminContext, GitHubAppInfo
from crucible.application.admin.credentials import sweep_retired
from crucible.application.admin.routing import local_endpoint_view
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.errors import NotFoundError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.proxy_config import worker_proxy_config
from crucible.application.supervisor import Supervisor
from crucible.domain.ids import new_id
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.execution import ExecutionProvider
from crucible.ports.github import GitHubClient
from crucible.ports.harness import CredentialSource, HarnessGate, MountMode
from crucible.ports.notification import WakeDeliverer
from crucible.ports.publish import Publisher
from crucible.settings import Settings

log = logging.getLogger("crucible.wiring")


@dataclass(slots=True)
class Wiring:
    settings: Settings
    ctx: AppContext
    providers: dict[str, ExecutionProvider]
    artifact_store: ArtifactStore
    wake_deliverer: WakeDeliverer
    github: GitHubClient | None = None
    publisher: Publisher | None = None
    harnesses: HarnessRegistry | None = None
    admin: AdminContext | None = None

    def supervisor(self) -> Supervisor:
        s = self.settings.supervisor
        return Supervisor(
            self.ctx.uow_factory,
            self.providers,
            self.ctx.clock,
            holder=f"{s.holder or socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}",
            artifact_store=self.artifact_store,
            wake_deliverer=self.wake_deliverer,
            github=self.github,
            publisher=self.publisher,
            delivery_config=DeliveryConfig(
                poll_interval_seconds=self.settings.github.poll_interval_seconds,
                reactions_poll_interval_seconds=(
                    self.settings.github.reactions_poll_interval_seconds
                ),
                ci_log_excerpt_bytes=self.settings.github.ci_log_excerpt_bytes,
                publisher_timeout_seconds=self.settings.github.publisher_timeout_seconds,
                publisher_image=self.settings.github.publisher_image,
            ),
            lease_ttl_seconds=s.lease_ttl_seconds,
            attempt_lease_ttl_seconds=s.attempt_lease_ttl_seconds,
            checkout_lease_ttl_seconds=s.checkout_lease_ttl_seconds,
            grace_seconds=s.grace_seconds,
            harnesses=self.harnesses,
            harness_gates=harness_gates(self.settings),
            credential_sources=credential_sources(self.settings),
            credential_sweep=(
                partial(sweep_retired, self.admin) if self.admin is not None else None
            ),
        )

    def app(self) -> FastAPI:
        return create_app(self.ctx)


def credential_sources(settings: Settings) -> dict[str, CredentialSource]:
    """12: where each harness's credential directory is. Paths, never values."""
    out: dict[str, CredentialSource] = {}
    for name, entry in settings.credentials.items():
        if entry.path:
            out[name] = CredentialSource(
                path=entry.path,
                mount_mode=MountMode(entry.mount_mode) if entry.mount_mode else None,
            )
    return out


def harness_gates(settings: Settings) -> dict[str, HarnessGate]:
    """25: the operator's configuration gate per harness, with its reason."""
    return {
        name: HarnessGate(enabled=entry.enabled, reason=entry.reason)
        for name, entry in settings.harnesses.items()
    }


def docker_config(
    settings: Settings, *, local_endpoint_url: str | None = None, database_value: bool = False
) -> DockerConfig:
    d = settings.docker
    local_endpoints = []
    endpoint = local_endpoint_url if database_value else settings.endpoint_seed
    if endpoint:
        parsed = urlsplit(endpoint)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        local_endpoints.append(f"{parsed.hostname}:{port}")
    return DockerConfig(
        endpoint=d.host,
        artifact_root=settings.service.artifact_root,
        mount_kind=d.mount_kind,
        artifact_volume=d.artifact_volume,
        artifact_host_root=d.artifact_host_root,
        credential_root=d.credential_root,
        credential_host_root=d.credential_host_root,
        credential_volume=d.credential_volume,
        workers_network=d.workers_network,
        egress_proxy=d.egress_proxy,
        proxy_allowlist=tuple([*d.egress_allowlist, *local_endpoints]),
        no_proxy=d.no_proxy,
        api_timeout_seconds=d.api_timeout_seconds,
        collector_timeout_seconds=d.collector_timeout_seconds,
        verifier_timeout_seconds=d.verifier_timeout_seconds,
        report_size_cap_bytes=d.report_size_cap_bytes,
        workspace_dir_mode=d.workspace_dir_mode,
        use_reference_cache=d.use_reference_cache,
        max_concurrency=d.max_concurrency,
        extra_image_allowlist=tuple(d.extra_image_allowlist),
        credentials=credential_sources(settings),
    )


def kubernetes_config(settings: Settings) -> KubernetesConfig:
    k = settings.kubernetes
    return KubernetesConfig(
        namespace=k.workers_namespace,
        service_account=k.service_account,
        storage_class=k.storage_class,
        workspace_size=k.workspace_size,
        image_pull_secret=k.image_pull_secret,
        cache_claim=k.cache_claim,
        launch_timeout_seconds=k.launch_timeout_seconds,
        prepare_timeout_seconds=k.prepare_timeout_seconds,
        collector_timeout_seconds=k.collector_timeout_seconds,
        verifier_timeout_seconds=k.verifier_timeout_seconds,
        report_size_cap_bytes=k.report_size_cap_bytes,
        max_concurrency=k.max_concurrency,
        poll_interval_seconds=k.poll_interval_seconds,
        api_timeout_seconds=k.api_timeout_seconds,
        cluster_dns_ip=k.cluster_dns_ip,
        denied_cidrs=tuple(k.denied_cidrs),
        extra_image_allowlist=tuple(k.extra_image_allowlist),
        credential_secrets=dict(k.credential_secrets),
        # 25 step 7: a configured mount mode may raise the adapter's declared minimum
        # to rw-narrow and never lowers it. The Kubernetes provider reads the same
        # `[credentials.<harness>]` block the Docker provider does; only the source
        # differs, a Secret in the workers namespace rather than a directory (12, 26).
        credential_modes={
            name: MountMode(entry.mount_mode)
            for name, entry in settings.credentials.items()
            if entry.mount_mode
        },
        image_repositories=tuple(k.image_repositories),
        probe_image=k.probe_image,
        use_reference_cache=k.use_reference_cache,
    )


def kubernetes_provider(settings: Settings, registry: HarnessRegistry) -> KubernetesProvider:
    """The provider of 26. The access is either a kubeconfig path or the in-cluster
    ServiceAccount; neither is a credential value in configuration (12)."""
    k = settings.kubernetes
    access = (
        kubeconfig_access(k.kubeconfig, k.kubeconfig_context)
        if k.kubeconfig
        else in_cluster_access()
    )
    client = KubernetesClient(access, k.workers_namespace, timeout=k.api_timeout_seconds)
    return KubernetesProvider(
        kubernetes_config(settings), client, HttpRegistryClient(), harnesses=registry
    )


def github_client(settings: Settings) -> GitHubClient | None:
    """The GitHub adapter, when the App is configured. The key is a path Crucible reads
    to sign a JWT in memory; nothing about it is a configuration value (12)."""
    g = settings.github
    if not g.enabled or not g.app.app_id or not g.app.private_key_path:
        return None
    transport = RestTransport(g.api_base, timeout=g.api_timeout_seconds)
    authenticator = AppAuthenticator(
        AppConfig(
            app_id=g.app.app_id,
            private_key_path=g.app.private_key_path,
            api_base=g.api_base,
        ),
        transport,
    )
    return RestGitHubClient(authenticator, transport, allow_issue_comments=g.allow_issue_comments)


def wire(settings: Settings) -> Wiring:
    engine = make_engine(settings.database.url)
    factory = SqlUnitOfWorkFactory(engine)
    database_endpoint: str | None = None
    routing_document: dict[str, object] | None = None
    database_value = False
    try:
        with factory() as uow:
            local = local_endpoint_view(uow)
            database_value = True
            database_endpoint = local.get("endpoint_url")
            reference = local["routing_policy"]
            record = uow.routing_policies.get(reference["name"], reference["version"])
            routing_document = record.document if record is not None else None
    except (SQLAlchemyError, NotFoundError):
        # `migrate` and first-run commands can wire before the policy tables exist.
        database_value = False
    if settings.admin.proxy_config_path and routing_document is not None:
        path = Path(settings.admin.proxy_config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            worker_proxy_config(
                settings.admin.proxy_subnet,
                list(settings.docker.egress_allowlist),
                [routing_document],
            ),
            encoding="utf-8",
        )
    registry = default_registry()
    providers: dict[str, ExecutionProvider] = {"fake": FakeProvider()}
    docker: DockerProvider | None = None
    if settings.docker.enabled:
        docker = DockerProvider(
            docker_config(
                settings,
                local_endpoint_url=database_endpoint,
                database_value=database_value,
            ),
            harnesses=registry,
        )
        providers["docker"] = docker
    if settings.kubernetes.enabled:
        # 26: the Kubernetes provider is reported by `GET /v1/capabilities` and
        # `GET /v1/admin/providers` exactly when it is wired, with its namespace probe
        # in its health checks. A deployment that turned it on with no kubeconfig and
        # no in-cluster ServiceAccount gets the provider left out and a log line, not a
        # service that will not start: the Docker provider and the API are still the
        # operator's way of finding out what is wrong (25).
        try:
            providers["kubernetes"] = kubernetes_provider(settings, registry)
        except KubernetesApiError as exc:
            log.error("the kubernetes provider is enabled but unreachable: %s", exc)
    artifact_store = DiskArtifactStore(settings.service.artifact_root)
    wake_deliverer = WebhookWakeDeliverer(
        settings.wake.webhook_url,
        settings.wake.secret,
        timeout_seconds=settings.wake.timeout_seconds,
    )
    github = github_client(settings)
    admin = AdminContext(
        uow_factory=factory,
        clock=SystemClock(),
        providers=providers,
        harnesses=registry,
        harness_gates=harness_gates(settings),
        credential_sources=credential_sources(settings),
        github=github,
        github_app=GitHubAppInfo(
            app_id=settings.github.app.app_id,
            private_key_path=settings.github.app.private_key_path,
            webhook_secret_path=settings.github.app.webhook_secret_path,
            webhook_enabled=settings.github.webhook_enabled,
            api_base=settings.github.api_base,
        ),
        artifact_root=settings.service.artifact_root,
        lease_ttl_seconds=settings.supervisor.lease_ttl_seconds,
        credential_retention_hours=settings.admin.credential_retention_hours,
        probe_timeout_seconds=settings.admin.probe_timeout_seconds,
        login_timeout_seconds=settings.admin.login_timeout_seconds,
        login_commands={k: tuple(v) for k, v in settings.admin.login_commands.items()},
        proxy_config_path=settings.admin.proxy_config_path,
        proxy_subnet=settings.admin.proxy_subnet,
        proxy_hosts=tuple(settings.docker.egress_allowlist),
    )
    ctx = AppContext(
        uow_factory=factory,
        clock=SystemClock(),
        providers=list(providers.values()),
        database_url=settings.database.url,
        engine=engine,
        artifact_store=artifact_store,
        lease_ttl_seconds=settings.supervisor.lease_ttl_seconds,
        github_webhook_enabled=settings.github.webhook_enabled,
        github_webhook_secret_path=settings.github.app.webhook_secret_path,
        harnesses=registry,
        harness_gates=harness_gates(settings),
        credential_sources=credential_sources(settings),
        admin=admin,
        settings=settings,
    )
    publisher: Publisher | None = None
    if docker is not None and github is not None:
        publisher = DockerPublisher(
            docker,
            PublisherConfig(
                # 23 step 3: the publisher's own egress network, not the workers'.
                network=settings.github.publisher_network,
                egress_proxy=(
                    settings.github.publisher_egress_proxy or settings.docker.egress_proxy
                ),
                no_proxy=settings.docker.no_proxy,
                credential_host=settings.github.credential_host,
                timeout_seconds=settings.github.publisher_timeout_seconds,
            ),
        )
    return Wiring(
        settings=settings,
        ctx=ctx,
        providers=providers,
        artifact_store=artifact_store,
        wake_deliverer=wake_deliverer,
        github=github,
        publisher=publisher,
        harnesses=registry,
        admin=admin,
    )
