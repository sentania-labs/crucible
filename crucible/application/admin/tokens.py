"""Administrative principal tokens, with one-time display and revocation."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.auth import MintedToken, mint_token
from crucible.application.errors import ConflictError
from crucible.application.first_run import FIRST_RUN_PREFIX, discard_after_use, is_first_run
from crucible.domain.entities import Role
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def list_principals(uow: UnitOfWork) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "role": item.role.value,
            "created_at": item.created_at.isoformat(),
            "disabled_at": item.disabled_at.isoformat() if item.disabled_at else None,
        }
        for item in uow.principals.list_all()
    ]


def create(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    name: str,
    role: str,
    reason: str | None,
) -> MintedToken:
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="tokens create")
    if is_first_run(name):
        # Reserved, so that a name with this prefix is always the migration's first-run
        # principal, whose delivered token a sign-in removes (ADR 0016).
        raise ConflictError(f"principal names starting {FIRST_RUN_PREFIX!r} are reserved")
    try:
        selected = Role(role)
        minted = mint_token(uow, ctx.clock, name=name, role=selected)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    admin_event(
        uow,
        ctx,
        EventKind.PRINCIPAL_CREATED,
        principal=principal,
        reason=reason,
        before=None,
        after={"name": minted.principal.name, "role": minted.principal.role.value},
    )
    return minted


def after_revoke(ctx: AdminContext, result: dict[str, Any]) -> None:
    """ADR 0016: once a revoke of the first-run principal is committed, its token has
    nothing left to open and is not left lying in its Secret or file. Blocking: an
    async caller runs it on a thread."""
    discard_after_use(ctx.first_run, str(result.get("name", "")))


def revoke(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    principal_id: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="tokens revoke", reason_required=True
    )
    target = uow.principals.get(principal_id)
    if target is None or target.disabled_at is not None:
        raise ConflictError("the principal does not exist or is already revoked")
    if target.name == principal:
        raise ConflictError("an administrator cannot revoke the token in use")
    uow.principals.disable(principal_id, ctx.clock.now())
    admin_event(
        uow,
        ctx,
        EventKind.PRINCIPAL_REVOKED,
        principal=principal,
        reason=reason,
        before={"name": target.name, "role": target.role.value, "enabled": True},
        after={"name": target.name, "role": target.role.value, "enabled": False},
    )
    return {"id": target.id, "name": target.name, "revoked": True}
