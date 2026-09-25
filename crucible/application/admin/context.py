"""What every administrative service needs, and the two rules they all obey (25): a
mutation requires a reason and a live supervisor lease, and it is an event with the
principal and a before-and-after summary that never carries a value."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from crucible.application.errors import ContractValidationError, SupervisorNotLiveError
from crucible.application.harnesses import HarnessRegistry
from crucible.application.queries import supervisor_health
from crucible.application.transitions import record_event
from crucible.domain.entities import Event
from crucible.domain.events import EventKind
from crucible.domain.secrets import scan_text
from crucible.ports.clock import Clock
from crucible.ports.execution import ExecutionProvider
from crucible.ports.first_run import FirstRunDelivery
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
    proxy_config_path: str | None = None
    proxy_subnet: str = "10.88.0.0/24"
    proxy_hosts: tuple[str, ...] = ()
    proxy_reload_timeout_seconds: float = 0
    # The command each harness's login runs, overridable for the fake-CLI tests.
    login_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Login flows beyond the three harnesses' (`login.FLOWS`), by harness name: the
    # stand-in login the end-to-end tiers drive. Never set by a deployment.
    login_flows: dict[str, Any] = field(default_factory=dict)
    # The settings file's `kubernetes.egress` values, shown until a save replaces them,
    # and the namespaces no selector may name (crucible#91).
    kubernetes_egress_seed: dict[str, Any] = field(default_factory=dict)
    kubernetes_protected_namespaces: tuple[str, ...] = ()
    # Where the first-run administrator token was delivered; a revoke of that principal
    # removes it (ADR 0016).
    first_run: FirstRunDelivery | None = None


def record_refusal(ctx: AdminContext, *, principal: str, operation: str, detail: str) -> None:
    """25: "the refusal itself is recorded, so an audit shows the attempt". The refusal
    ends the caller's transaction, so it is written through a unit of work of its own and
    committed there. Best effort: a refusal that cannot be recorded never becomes a
    second failure on top of the first."""
    if not operation:
        return
    try:
        with ctx.uow_factory() as uow:
            record_event(
                uow,
                ctx.clock,
                EventKind.ADMIN_REFUSED,
                principal=principal or "unknown",
                payload={"operation": operation, "detail": detail},
            )
            uow.commit()
    except Exception:  # a refusal is never made worse by a failure to record it
        return


def refuse_secret_shaped(value: str, *, field: str) -> None:
    """25: no event payload ever carries a credential value, and the audit serves payloads
    back. A secret-shaped input is refused rather than redacted, so the operator knows the
    value did not land anywhere."""
    hit = scan_text(value)
    if hit is not None:
        raise ContractValidationError(
            f"the {field} looks like a credential and was not recorded",
            errors=[
                {
                    "path": field,
                    "message": (
                        f"matched the {hit} pattern; an administrative record never "
                        "carries a credential value (25)"
                    ),
                }
            ],
        )


def require_reason(
    reason: str | None,
    ctx: AdminContext | None = None,
    *,
    principal: str = "",
    operation: str = "",
) -> str:
    """Every mutation requires a reason string (25), and the string is recorded, so it
    may not be a credential."""
    if reason is None or not reason.strip():
        if ctx is not None:
            record_refusal(
                ctx, principal=principal, operation=operation, detail="no reason was given"
            )
        raise ContractValidationError(
            "a reason is required", errors=[{"path": "reason", "message": "must not be empty"}]
        )
    cleaned = reason.strip()
    try:
        refuse_secret_shaped(cleaned, field="reason")
    except ContractValidationError:
        if ctx is not None:
            record_refusal(
                ctx,
                principal=principal,
                operation=operation,
                detail="the reason was secret-shaped",
            )
        raise
    return cleaned


def require_live_supervisor(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str = "", operation: str = ""
) -> None:
    """25: a mutation is refused when the supervisor lease is not held by a live
    instance. The refusal itself is recorded, so an audit shows the attempt."""
    ok, detail = supervisor_health(uow, ctx.clock.now(), ctx.lease_ttl_seconds)
    if not ok:
        record_refusal(ctx, principal=principal, operation=operation, detail=detail)
        raise SupervisorNotLiveError(f"refused: {detail}")


def guard_mutation(
    ctx: AdminContext, uow: UnitOfWork, reason: str | None, *, principal: str, operation: str
) -> str:
    """The two rules every mutation obeys (25), in one call so no operation can carry
    only one of them: a reason that is a reason and not a credential, and a live
    supervisor lease. Either refusal is recorded as `admin_refused`."""
    cleaned = require_reason(reason, ctx, principal=principal, operation=operation)
    require_live_supervisor(ctx, uow, principal=principal, operation=operation)
    return cleaned


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
    """One event per mutation: principal, reason, before-and-after summary (25). The
    whole assembled payload is scanned before it is written, because `GET /admin/audit`
    serves it back and the event log is append-only."""
    payload: dict[str, Any] = {
        "reason": reason,
        "before": dict(before or {}),
        "after": dict(after or {}),
    }
    payload.update(extra)
    refuse_secret_shaped(json.dumps(payload, default=str), field="payload")
    return record_event(uow, ctx.clock, kind, principal=principal, payload=payload)
