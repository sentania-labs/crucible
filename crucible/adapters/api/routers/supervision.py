"""/health, /ready, /supervisor (04)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from crucible import __version__
from crucible.adapters.api.deps import Ctx, Reader, UoW
from crucible.adapters.persistence.migrate import is_current
from crucible.application.queries import supervisor_view
from crucible.contracts.api import HealthView, ReadyCheck, ReadyView, SupervisorView

router = APIRouter()


@router.get("/health", response_model=HealthView)
def health() -> HealthView:
    return HealthView(status="ok", version=__version__)


@router.get("/ready", response_model=ReadyView, responses={503: {"model": ReadyView}})
def ready(ctx: Ctx, response: Response) -> ReadyView:
    database = ReadyCheck(ok=False, detail="not checked")
    migrations = ReadyCheck(ok=False, detail="not checked")
    supervisor = ReadyCheck(ok=False, detail="not checked")
    try:
        with ctx.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database = ReadyCheck(ok=True, detail="reachable")
        ok, detail = is_current(ctx.engine, ctx.database_url)
        migrations = ReadyCheck(ok=ok, detail=detail)
        with ctx.uow_factory() as uow:
            lease = uow.leases.get_supervisor()
        now = ctx.clock.now()
        if lease is None:
            supervisor = ReadyCheck(ok=False, detail="no supervisor lease")
        elif lease.expires_at <= now:
            supervisor = ReadyCheck(
                ok=False,
                detail=f"lease held by {lease.holder} expired {lease.expires_at.isoformat()}",
            )
        else:
            supervisor = ReadyCheck(ok=True, detail=f"held by {lease.holder}")
    except Exception as exc:  # the readiness probe must never raise
        database = ReadyCheck(ok=False, detail=type(exc).__name__)
    view = ReadyView(
        ready=database.ok and migrations.ok and supervisor.ok,
        database=database,
        migrations=migrations,
        supervisor=supervisor,
    )
    if not view.ready:
        response.status_code = 503
    return view


@router.get("/supervisor", response_model=SupervisorView)
def supervisor_status(ctx: Ctx, uow: UoW, _principal: Reader) -> SupervisorView:
    return supervisor_view(uow, ctx.providers)
