"""The Kubernetes provider's cluster egress selectors, as an administered setting (25).

`kubernetes.egress` names the cluster resolver's pods and, when the local model
endpoint runs inside the cluster, the gateway's pods and port (crucible#91). The
settings file seeds it; a save writes the database row every process reads back, so
the supervisor follows an edit made here without a restart, and the readiness canary
runs again under the new rules before the next launch.
"""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.errors import ContractValidationError
from crucible.domain.cluster_egress import SETTING_NAME, ClusterEgress, parse_cluster_egress
from crucible.domain.entities import ProviderSetting
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def _reload(ctx: AdminContext) -> None:
    provider = ctx.providers.get("kubernetes")
    reload = getattr(provider, "reload_settings", None)
    if callable(reload):
        reload()


def egress_view(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    """What is in force and where it came from. `source` is `database` once an
    administrator has saved it, and `settings` (the file's values) until then."""
    row = uow.provider_settings.get(SETTING_NAME)
    seed = ctx.kubernetes_egress_seed or ClusterEgress().as_document()
    provider = ctx.providers.get("kubernetes")
    return {
        "setting": SETTING_NAME,
        "source": "database" if row is not None else "settings",
        "document": row.document if row is not None else seed,
        "settings_file": seed,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
        "reason": row.reason if row is not None else None,
        "provider_enabled": provider is not None,
        "protected_namespaces": list(ctx.kubernetes_protected_namespaces),
    }


def save_egress(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    document: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="kubernetes set-egress"
    )
    try:
        egress = parse_cluster_egress(
            document, protected_namespaces=ctx.kubernetes_protected_namespaces
        )
    except ValueError as exc:
        raise ContractValidationError(
            f"the {SETTING_NAME} setting is not valid: {exc}",
            errors=[{"path": "document", "message": str(exc)}],
        ) from exc
    before = egress_view(ctx, uow)
    uow.provider_settings.put(
        ProviderSetting(
            name=SETTING_NAME,
            document=egress.as_document(),
            updated_at=ctx.clock.now(),
            updated_by=principal,
            reason=reason,
        )
    )
    admin_event(
        uow,
        ctx,
        EventKind.KUBERNETES_EGRESS_UPDATED,
        principal=principal,
        reason=reason,
        before={"source": before["source"], **before["document"]},
        after={"source": "database", **egress.as_document()},
    )
    _reload(ctx)
    return egress_view(ctx, uow)


__all__ = ["egress_view", "save_egress"]
