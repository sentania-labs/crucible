"""Which pre-PR gates block and which are advisory (ADR 0024).

`gates.advisory` in the policy lists the pre-PR gates whose failure is carried in front
of the internal reviewer instead of stopping the task. A version without the field takes
the default set. A save here writes a new immutable version of the policy in force with
only that list changed, as the command timeout does; the upload event records the reason
and turning a gate outside the default set advisory is recorded as an operator decision.
"""

from __future__ import annotations

import copy
from typing import Any

from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.admin.routing import active_policy
from crucible.application.errors import ContractValidationError
from crucible.application.gates import configured_pre_pr_gates
from crucible.application.policies import put_policy
from crucible.domain.entities import Principal
from crucible.domain.gates import (
    ALWAYS_ADVISORY_GATES,
    ALWAYS_BLOCKING_GATES,
    DEFAULT_ADVISORY_GATES,
    PRE_PR_GATES,
    advisory_gates,
)
from crucible.ports.repository import UnitOfWork


def gate_classes_view(uow: UnitOfWork) -> dict[str, Any]:
    policy = active_policy(uow)
    advisory = advisory_gates(policy.document)
    gates = configured_pre_pr_gates(policy.document)
    return {
        "policy": {"name": policy.name, "version": policy.version},
        "advisory": sorted(g for g in gates if g in advisory),
        "blocking": sorted(g for g in gates if g not in advisory),
        # True when the version carries no list and the default applies.
        "default": (policy.document.get("gates") or {}).get("advisory") is None,
        "default_advisory": sorted(DEFAULT_ADVISORY_GATES),
        "always_blocking": sorted(ALWAYS_BLOCKING_GATES),
        # FDY-0143: listed as advisory above whatever the policy says.
        "always_advisory": sorted(ALWAYS_ADVISORY_GATES),
        "note": (
            "a path matching the contract's prohibited_paths stops the task even when "
            "scope_contained is advisory"
        ),
    }


def save_gate_classes(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    advisory: list[str],
    reason: str | None,
) -> dict[str, Any]:
    """Write a policy version whose advisory set is exactly `advisory`. A gate that is
    always advisory is accepted and not stored, so the view's own list saves back."""
    advisory = [gate for gate in advisory if gate not in ALWAYS_ADVISORY_GATES]
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="gates set-advisory"
    )
    unknown = sorted(set(advisory) - PRE_PR_GATES - ALWAYS_BLOCKING_GATES)
    fixed = sorted(set(advisory) & ALWAYS_BLOCKING_GATES)
    if unknown or fixed:
        errors = [
            {"path": "gates.advisory", "message": f"{g} is not a pre-PR gate"} for g in unknown
        ] + [{"path": "gates.advisory", "message": f"{g} always blocks"} for g in fixed]
        raise ContractValidationError("the advisory set names gates it may not", errors=errors)
    policy = active_policy(uow)
    document = copy.deepcopy(policy.document)
    next_version = max(item.version for item in uow.policies.list_versions(policy.name)) + 1
    document["version"] = next_version
    document["description"] = (
        f"{policy.document.get('description', 'Software delivery policy')} "
        f"Advisory gates update in version {next_version}."
    )
    document.setdefault("gates", {})["advisory"] = sorted(set(advisory))
    put_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=policy.name,
        version=next_version,
        document=document,
        reason=reason,
    )
    return gate_classes_view(uow)


__all__ = ["gate_classes_view", "save_gate_classes"]
