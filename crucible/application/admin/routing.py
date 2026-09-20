"""Administrative read and reasoned clear of reactive pool exhaustion marks."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.errors import NotFoundError
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def list_exhaustions(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    now = ctx.clock.now()
    return {
        "items": [
            {
                "pool": mark.pool,
                "exhausted_at": mark.exhausted_at.isoformat(),
                "reset_at": mark.reset_at.isoformat(),
                "active": mark.cleared_at is None and mark.reset_at > now,
                "task_id": mark.task_id,
                "attempt_id": mark.attempt_id,
                "reason": mark.reason,
                "cleared_at": mark.cleared_at.isoformat() if mark.cleared_at else None,
                "cleared_by": mark.cleared_by,
                "clear_reason": mark.clear_reason,
            }
            for mark in uow.pool_exhaustions.list_all()
        ]
    }


def clear_exhaustion(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    pool: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="routing clear-exhaustion"
    )
    before = uow.pool_exhaustions.get(pool, for_update=True)
    if before is None:
        raise NotFoundError(f"quota pool {pool!r} has no exhaustion mark")
    cleared = uow.pool_exhaustions.clear(
        pool, at=ctx.clock.now(), principal=principal, reason=reason
    )
    assert cleared is not None
    admin_event(
        uow,
        ctx,
        EventKind.POOL_EXHAUSTION_CLEARED,
        principal=principal,
        reason=reason,
        before={"pool": pool, "reset_at": before.reset_at.isoformat(), "active": True},
        after={"pool": pool, "active": False},
    )
    return {"pool": pool, "active": False, "cleared_at": cleared.cleared_at}
