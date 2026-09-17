"""Compose the application from settings. Used by both CLIs."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from fastapi import FastAPI

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.notification.webhook import WebhookWakeDeliverer
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.application.supervisor import Supervisor
from crucible.domain.ids import new_id
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.execution import ExecutionProvider
from crucible.ports.notification import WakeDeliverer
from crucible.settings import Settings


@dataclass(slots=True)
class Wiring:
    settings: Settings
    ctx: AppContext
    providers: dict[str, ExecutionProvider]
    artifact_store: ArtifactStore
    wake_deliverer: WakeDeliverer

    def supervisor(self) -> Supervisor:
        s = self.settings.supervisor
        return Supervisor(
            self.ctx.uow_factory,
            self.providers,
            self.ctx.clock,
            holder=f"{s.holder or socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}",
            artifact_store=self.artifact_store,
            wake_deliverer=self.wake_deliverer,
            lease_ttl_seconds=s.lease_ttl_seconds,
            attempt_lease_ttl_seconds=s.attempt_lease_ttl_seconds,
            checkout_lease_ttl_seconds=s.checkout_lease_ttl_seconds,
            grace_seconds=s.grace_seconds,
        )

    def app(self) -> FastAPI:
        return create_app(self.ctx)


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
    )


def wire(settings: Settings) -> Wiring:
    engine = make_engine(settings.database.url)
    providers: dict[str, ExecutionProvider] = {"fake": FakeProvider()}
    if settings.docker.enabled:
        providers["docker"] = DockerProvider(docker_config(settings))
    artifact_store = DiskArtifactStore(settings.service.artifact_root)
    wake_deliverer = WebhookWakeDeliverer(
        settings.wake.webhook_url,
        settings.wake.secret,
        timeout_seconds=settings.wake.timeout_seconds,
    )
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers=list(providers.values()),
        database_url=settings.database.url,
        engine=engine,
        artifact_store=artifact_store,
        lease_ttl_seconds=settings.supervisor.lease_ttl_seconds,
    )
    return Wiring(
        settings=settings,
        ctx=ctx,
        providers=providers,
        artifact_store=artifact_store,
        wake_deliverer=wake_deliverer,
    )
