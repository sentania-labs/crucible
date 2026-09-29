"""Which models routing tries first, and when it stops trusting one (ADR 0028).

Per tier, `prefer_pools` is the pool order routing tries before the tier's capability
preference; the local pools (Hermes on the gateway) come first for `trivial` and
`standard` by default. `rotation` says when a model is demoted in a project for failing
blocking gates, and when a demoted one gets a probe attempt to recover. A save writes a
new routing version and a delivery policy version naming it, as the local endpoint panel
does; versions a task already references are never rewritten.
"""

from __future__ import annotations

import copy
from typing import Any

from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.admin.routing import active_documents, publish_routing
from crucible.application.errors import ContractValidationError
from crucible.contracts.policy import LOCAL_FIRST_TIERS, RoutingPolicyV1
from crucible.domain.entities import Principal
from crucible.ports.repository import UnitOfWork

ROTATION_FIELDS = (
    "quality_feedback",
    "quality_window",
    "demote_failure_percent",
    "demote_min_sample",
    "probe_after_minutes",
)


def parse_pool_order(text: str) -> list[str] | None:
    """The CLI's and the UI's spelling of a tier's pool order: pool names separated by
    commas or spaces, first tried first; empty for no preference; `default` for the
    default rule."""
    if text.strip().lower() == "default":
        return None
    return [pool for pool in text.replace(",", " ").split() if pool]


def preference_view(uow: UnitOfWork) -> dict[str, Any]:
    policy, record = active_documents(uow)
    routing = RoutingPolicyV1.model_validate(record.document)
    return {
        "policy": {"name": policy.name, "version": policy.version},
        "routing_policy": {"name": record.name, "version": record.version},
        "pools": sorted(routing.pools),
        "local_pools": routing.local_pools(),
        "tiers": {
            name: {
                # What routing uses, and whether it is the version's own list or the
                # default a version without one reads as.
                "prefer_pools": routing.preferred_pools(name),
                "default": rule.prefer_pools is None,
                "prefer": list(rule.prefer),
                "allowed_capability": list(rule.allowed_capability),
            }
            for name, rule in sorted(routing.tiers.items())
        },
        "default_rule": (
            f"the local pools first for {', '.join(sorted(LOCAL_FIRST_TIERS))}; "
            "no pool preference for any other tier"
        ),
        "rotation": {
            field: getattr(routing.rotation, field) for field in ("strategy", *ROTATION_FIELDS)
        },
    }


def _effective(document: dict[str, Any]) -> Any:
    """What routing does with a document: a save that changes none of it is refused, so
    a form sent back unchanged writes no version. An invalid document is compared as
    itself and left for validation to refuse."""
    try:
        routing = RoutingPolicyV1.model_validate(document)
    except ValueError:
        return document
    return (
        {
            name: (routing.preferred_pools(name), rule.prefer_pools is None)
            for name, rule in routing.tiers.items()
        },
        {field: getattr(routing.rotation, field) for field in ROTATION_FIELDS},
    )


def save_preference(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    tiers: dict[str, list[str] | None],
    rotation: dict[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    """`tiers` maps a tier to its pool order, or to None for the default; a tier left out
    keeps what it has. A `rotation` field left out keeps its value."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="routing set-preference"
    )
    policy, record = active_documents(uow)
    document = copy.deepcopy(record.document)
    unknown_tiers = sorted(set(tiers) - set(document.get("tiers") or {}))
    unknown_fields = sorted(set(rotation) - set(ROTATION_FIELDS))
    if unknown_tiers or unknown_fields:
        raise ContractValidationError(
            "the routing preference names what the routing policy does not have",
            errors=[{"path": f"tiers.{name}", "message": "no such tier"} for name in unknown_tiers]
            + [
                {"path": f"rotation.{name}", "message": "not an editable rotation setting"}
                for name in unknown_fields
            ],
        )
    for name, pools in tiers.items():
        document["tiers"][name]["prefer_pools"] = None if pools is None else list(pools)
    document.setdefault("rotation", {}).update(rotation)
    if _effective(document) == _effective(record.document):
        raise ContractValidationError(
            "the routing order and demotion are already these; nothing was saved",
            errors=[{"path": "tiers", "message": "no change"}],
        )
    publish_routing(
        ctx,
        uow,
        principal=principal,
        policy=policy,
        routing=record,
        routing_document=document,
        reason=reason,
        note="Routing preference update",
    )
    return preference_view(uow)


__all__ = ["ROTATION_FIELDS", "parse_pool_order", "preference_view", "save_preference"]
