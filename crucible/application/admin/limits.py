"""The per-command timeout, as an administered policy limit (issue 128).

`limits.command_timeout_ms` is the timeout every harness runs a shell command under.
The policy carries its bounds and the deployment's default; a task contract may narrow
it within those bounds, and the launch never exceeds the attempt's own timeout. A save
here writes a new immutable version of the policy in force with only this limit
changed, as the local endpoint panel does; tasks whose contracts name that version
launch with it.
"""

from __future__ import annotations

import copy
from typing import Any

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.admin.routing import active_policy
from crucible.application.errors import ContractValidationError
from crucible.application.policies import put_policy
from crucible.domain.command_timeout import policy_bounds
from crucible.domain.entities import Principal
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def command_timeout_view(uow: UnitOfWork) -> dict[str, Any]:
    policy = active_policy(uow)
    limits = policy.document.get("limits") or {}
    return {
        "policy": {"name": policy.name, "version": policy.version},
        # A version uploaded before issue 128 has no field and takes the default bounds.
        "command_timeout_ms": policy_bounds(policy.document),
        "timeout_seconds": limits.get("timeout_seconds"),
    }


def save_command_timeout(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    minimum: int | None,
    maximum: int | None,
    default: int | None,
    reason: str | None,
) -> dict[str, Any]:
    """A bound left out keeps the value in force."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="limits set-command-timeout"
    )
    before = command_timeout_view(uow)
    current = before["command_timeout_ms"]
    minimum = current["min"] if minimum is None else minimum
    maximum = current["max"] if maximum is None else maximum
    default = current["default"] if default is None else default
    if not 1 <= minimum <= default <= maximum:
        raise ContractValidationError(
            "the command timeout must satisfy 1 <= min <= default <= max",
            errors=[{"path": "limits.command_timeout_ms", "message": "min <= default <= max"}],
        )
    policy = active_policy(uow)
    document = copy.deepcopy(policy.document)
    next_version = max(item.version for item in uow.policies.list_versions(policy.name)) + 1
    document["version"] = next_version
    document["description"] = (
        f"{policy.document.get('description', 'Software delivery policy')} "
        f"Command timeout update in version {next_version}."
    )
    document.setdefault("limits", {})["command_timeout_ms"] = {
        "min": minimum,
        "max": maximum,
        "default": default,
    }
    put_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=policy.name,
        version=next_version,
        document=document,
        reason=reason,
    )
    after = command_timeout_view(uow)
    admin_event(
        uow,
        ctx,
        EventKind.COMMAND_TIMEOUT_UPDATED,
        principal=principal.name,
        reason=reason,
        before={"policy_version": policy.version, **before["command_timeout_ms"]},
        after={"policy_version": next_version, **after["command_timeout_ms"]},
    )
    return after


__all__ = ["command_timeout_view", "save_command_timeout"]
