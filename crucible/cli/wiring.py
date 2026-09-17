"""Compose the application from settings. Used by both CLIs."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from functools import partial

from fastapi import FastAPI

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.fake import FakeProvider
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
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.harnesses import HarnessRegistry
from crucible.application.supervisor import Supervisor
from crucible.domain.ids import new_id
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.execution import ExecutionProvider
from crucible.ports.github import GitHubClient
from crucible.ports.harness import CredentialSource, HarnessGate, MountMode
from crucible.ports.notification import WakeDeliverer
from crucible.ports.publish import Publisher
from crucible.settings import Settings


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


def docker_config(settings: Settings) -> DockerConfig:
    d = settings.docker
    return DockerConfig(
        endpoint=d.host,
        artifact_root=settings.service.artifact_root,
        mount_kind=d.mount_kind,
        artifact_volume=d.artifact_volume,
        artifact_host_root=d.artifact_host_root,
        credential_root=d.credential_root,
        credential_host_root=d.credential_host_root,
        workers_network=d.workers_network,
        egress_proxy=d.egress_proxy,
        proxy_allowlist=tuple(d.egress_allowlist),
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
    registry = default_registry()
    providers: dict[str, ExecutionProvider] = {"fake": FakeProvider()}
    docker: DockerProvider | None = None
    if settings.docker.enabled:
        docker = DockerProvider(docker_config(settings), harnesses=registry)
        providers["docker"] = docker
    artifact_store = DiskArtifactStore(settings.service.artifact_root)
    wake_deliverer = WebhookWakeDeliverer(
        settings.wake.webhook_url,
        settings.wake.secret,
        timeout_seconds=settings.wake.timeout_seconds,
    )
    github = github_client(settings)
    admin = AdminContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
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
    )
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
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
