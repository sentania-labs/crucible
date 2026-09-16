"""Compose the application from settings. Used by both CLIs."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from fastapi import FastAPI

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory, make_engine
from crucible.application.supervisor import Supervisor
from crucible.domain.ids import new_id
from crucible.ports.execution import ExecutionProvider
from crucible.settings import Settings


@dataclass(slots=True)
class Wiring:
    settings: Settings
    ctx: AppContext
    providers: dict[str, ExecutionProvider]

    def supervisor(self) -> Supervisor:
        s = self.settings.supervisor
        return Supervisor(
            self.ctx.uow_factory,
            self.providers,
            self.ctx.clock,
            holder=f"{s.holder or socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}",
            lease_ttl_seconds=s.lease_ttl_seconds,
            attempt_lease_ttl_seconds=s.attempt_lease_ttl_seconds,
            grace_seconds=s.grace_seconds,
        )

    def app(self) -> FastAPI:
        return create_app(self.ctx)


def wire(settings: Settings) -> Wiring:
    engine = make_engine(settings.database.url)
    providers: dict[str, ExecutionProvider] = {"fake": FakeProvider()}
    ctx = AppContext(
        uow_factory=SqlUnitOfWorkFactory(engine),
        clock=SystemClock(),
        providers=list(providers.values()),
        database_url=settings.database.url,
        engine=engine,
    )
    return Wiring(settings=settings, ctx=ctx, providers=providers)
