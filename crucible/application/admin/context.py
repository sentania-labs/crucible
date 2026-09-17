"""What every administrative service needs, and the two rules they all obey (25): a
mutation requires a reason and a live supervisor lease, and it is an event with the
principal and a before-and-after summary that never carries a value."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from crucible.application.errors import ContractValidationError, SupervisorNotLiveError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.queries import supervisor_health
from crucible.application.transitions import record_event
from crucible.domain.entities import Event
from crucible.domain.events import EventKind
from crucible.ports.clock import Clock
from crucible.ports.execution import ExecutionProvider
from crucible.ports.github import GitHubClient
from crucible.ports.harness import CredentialSource, HarnessGate
from crucible.ports.repository import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class GitHubAppInfo:
    """The App's public identity and where its files are (12). Paths, never values."""

    app_id: int = 0
    private_key_path: str | None = None
    webhook_secret_path: str | None = None
    webhook_enabled: bool = False
    api_base: str = "https://api.github.com"


@dataclass(slots=True)
class AdminContext:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    providers: dict[str, ExecutionProvider]
    harnesses: HarnessRegistry
    harness_gates: Mapping[str, HarnessGate] = field(default_factory=dict)
    credential_sources: dict[str, CredentialSource] = field(default_factory=dict)
    github: GitHubClient | None = None
    github_app: GitHubAppInfo = field(default_factory=GitHubAppInfo)
    artifact_root: str = ""
    lease_ttl_seconds: int = 30
    credential_retention_hours: int = 24
    probe_timeout_seconds: int = 120
    login_timeout_seconds: int = 900
    # The command each harness's login runs, overridable for the fake-CLI tests.
    login_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)


def require_reason(reason: str | None) -> str:
    """Every mutation requires a reason string (25)."""
    if reason is None or not reason.strip():
        raise ContractValidationError(
            "a reason is required", errors=[{"path": "reason", "message": "must not be empty"}]
        )
    return reason.strip()


def require_live_supervisor(ctx: AdminContext, uow: UnitOfWork) -> None:
    """25: a mutation is refused when the supervisor lease is not held by a live
    instance. The refusal itself is recorded, so an audit shows the attempt."""
    ok, detail = supervisor_health(uow, ctx.clock.now(), ctx.lease_ttl_seconds)
    if not ok:
        raise SupervisorNotLiveError(f"refused: {detail}")


def admin_event(
    uow: UnitOfWork,
    ctx: AdminContext,
    kind: EventKind,
    *,
    principal: str,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    **extra: Any,
) -> Event:
    """One event per mutation: principal, reason, before-and-after summary (25)."""
    payload: dict[str, Any] = {
        "reason": reason,
        "before": dict(before or {}),
        "after": dict(after or {}),
    }
    payload.update(extra)
    return record_event(uow, ctx.clock, kind, principal=principal, payload=payload)
