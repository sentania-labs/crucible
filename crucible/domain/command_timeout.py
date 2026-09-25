"""The per-command timeout a harness is launched with (issue 128).

A harness runs each shell command under its own timeout, and some of them move a
command that outlives it to the background, where a headless run can end with the work
half done. Crucible sets that timeout from the launch: the policy's
`limits.command_timeout_ms` default, narrowed by the contract's
`execution_request.command_timeout_ms`, and never above the attempt's own
`timeout_seconds`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The operator's default (2026-09-25): 60 minutes, used when a policy version predates
# the field.
DEFAULT_COMMAND_TIMEOUT_MS = 3_600_000
DEFAULT_COMMAND_TIMEOUT_BOUNDS: dict[str, int] = {
    "min": 1_000,
    "max": 14_400_000,
    "default": DEFAULT_COMMAND_TIMEOUT_MS,
}


def policy_bounds(policy: Mapping[str, Any] | None) -> dict[str, int]:
    """The policy's command timeout bounds, or the default bounds for an older version."""
    limits = (policy or {}).get("limits") or {}
    bounds = limits.get("command_timeout_ms") if isinstance(limits, Mapping) else None
    if not isinstance(bounds, Mapping):
        return dict(DEFAULT_COMMAND_TIMEOUT_BOUNDS)
    return {
        key: int(bounds.get(key, fallback))
        for key, fallback in DEFAULT_COMMAND_TIMEOUT_BOUNDS.items()
    }


def effective_command_timeout_ms(
    policy: Mapping[str, Any] | None,
    contract: Mapping[str, Any] | None,
    timeout_seconds: int,
) -> int:
    """The contract's value if it set one, else the policy default, capped at the
    attempt's timeout. Submission already refused a contract value outside the policy
    bounds; the cap here is what keeps a policy default above a short attempt honest."""
    request = ((contract or {}).get("execution_request") or {}) if contract else {}
    requested = request.get("command_timeout_ms") if isinstance(request, Mapping) else None
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        requested = policy_bounds(policy)["default"]
    return max(1, min(int(requested), max(int(timeout_seconds), 1) * 1000))
