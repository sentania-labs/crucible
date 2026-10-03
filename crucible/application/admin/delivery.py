"""Administer the live global auto-merge switch."""

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.auto_merge import SETTING_NAME, auto_merge_enabled
from crucible.application.errors import ContractValidationError
from crucible.domain.entities import Principal, ProviderSetting
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def auto_merge_view(uow: UnitOfWork) -> dict[str, Any]:
    return {"enabled": auto_merge_enabled(uow)}


def save_auto_merge(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    enabled: Any,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="delivery auto-merge set"
    )
    if type(enabled) is not bool:
        raise ContractValidationError("enabled must be a JSON boolean")
    before = auto_merge_view(uow)
    after = {"enabled": enabled}
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document=after,
            updated_at=ctx.clock.now(),
            updated_by=principal.name,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.AUTO_MERGE_UPDATED,
        principal=principal.name,
        reason=reason,
        before=before,
        after=after,
    )
    return after
