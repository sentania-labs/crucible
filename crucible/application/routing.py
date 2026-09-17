"""Routing-policy enforcement, pool usage, and per-model history (05b, 03, 14).

Crucible never selects a model. It refuses a contract whose model is absent, disabled,
mismatched with the harness, outside the tier's allowed capability, or whose quota pool is
over its soft limit; and it reports what happened so the next selection is informed.

The quota check runs twice: advisory at submit, authoritative at attempt launch in the
same fenced transaction that moves the attempt to `launching`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from crucible.contracts.policy import RoutingModel, RoutingPolicyV1, window_seconds
from crucible.domain.entities import AttemptMetrics
from crucible.ports.repository import UnitOfWork

Problem = dict[str, Any]


@dataclass(frozen=True, slots=True)
class PoolUsage:
    pool: str
    window: str
    budget_units: str
    soft_limit: int
    used: int
    attempts: int
    fallback_to_attempts: bool
    over_soft_limit: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "window": self.window,
            "budget_units": self.budget_units,
            "soft_limit": self.soft_limit,
            "used": self.used,
            "attempts": self.attempts,
            "counting": "attempts" if self.fallback_to_attempts else self.budget_units,
            "fallback_to_attempts": self.fallback_to_attempts,
            "over_soft_limit": self.over_soft_limit,
        }


def routing_ref(policy_document: dict[str, Any]) -> tuple[str, int] | None:
    ref = (policy_document or {}).get("routing", {}).get("policy", {})
    if not ref:
        return None
    return str(ref.get("name")), int(ref.get("version", 0))


def load_routing(uow: UnitOfWork, policy_document: dict[str, Any]) -> RoutingPolicyV1 | None:
    ref = routing_ref(policy_document)
    if ref is None:
        return None
    record = uow.routing_policies.get(*ref)
    if record is None:
        return None
    return RoutingPolicyV1.model_validate(record.document)


def check_selection(
    routing: RoutingPolicyV1, *, tier: str, harness: str, model_id: str
) -> list[Problem]:
    """05: the contract's model must be an enabled entry whose harness matches and whose
    capability the tier allows."""
    problems: list[Problem] = []
    tier_rule = routing.tiers.get(tier)
    if tier_rule is None:
        problems.append(
            {
                "path": "execution_request.tier",
                "message": f"tier {tier!r} is not in routing policy {routing.name}",
            }
        )
    entry = routing.model(model_id)
    if entry is None:
        problems.append(
            {
                "path": "execution_request.model",
                "message": f"model {model_id!r} is not in routing policy {routing.name}",
            }
        )
        return problems
    if not entry.enabled:
        problems.append(
            {"path": "execution_request.model", "message": f"model {model_id!r} is disabled"}
        )
    if entry.harness != harness:
        problems.append(
            {
                "path": "execution_request.harness",
                "message": f"the routing policy pairs {model_id!r} with harness {entry.harness!r}",
            }
        )
    if tier_rule is not None and entry.capability not in tier_rule.allowed_capability:
        problems.append(
            {
                "path": "execution_request.model",
                "message": (
                    f"tier {tier!r} allows {sorted(tier_rule.allowed_capability)}; "
                    f"{model_id!r} is {entry.capability}"
                ),
            }
        )
    return problems


def pool_usage(uow: UnitOfWork, routing: RoutingPolicyV1, pool: str, now: datetime) -> PoolUsage:
    spec = routing.pools[pool]
    since = now - timedelta(seconds=window_seconds(spec.window))
    models = {m.id for m in routing.models if m.pool == pool}
    rows = [
        m
        for m in uow.attempt_metrics.list_since(since=since, model=None, task_ids=None)
        if m.model in models
    ]
    attempts = len(rows)
    if spec.budget_units == "attempts":
        return PoolUsage(
            pool=pool,
            window=spec.window,
            budget_units=spec.budget_units,
            soft_limit=spec.soft_limit,
            used=attempts,
            attempts=attempts,
            fallback_to_attempts=False,
            over_soft_limit=spec.soft_limit > 0 and attempts >= spec.soft_limit,
        )
    values = [
        _budget_value(row, spec.budget_units)
        for row in rows
        if _budget_value(row, spec.budget_units) is not None
    ]
    # 05b: a harness that reports no token counts records null, and the pool falls back
    # to counting attempts, which GET /routing/usage states.
    fallback = not values and attempts > 0
    used = attempts if fallback else int(sum(v or 0 for v in values))
    return PoolUsage(
        pool=pool,
        window=spec.window,
        budget_units=spec.budget_units,
        soft_limit=spec.soft_limit,
        used=used,
        attempts=attempts,
        fallback_to_attempts=fallback,
        over_soft_limit=spec.soft_limit > 0 and used >= spec.soft_limit,
    )


def _budget_value(metrics: AttemptMetrics, unit: str) -> float | None:
    if unit == "tokens_out":
        return None if metrics.tokens_out is None else float(metrics.tokens_out)
    if unit == "cost_units":
        return metrics.cost_units
    return 1.0


def usage_report(uow: UnitOfWork, routing: RoutingPolicyV1, now: datetime) -> list[dict[str, Any]]:
    return [pool_usage(uow, routing, pool, now).as_dict() for pool in sorted(routing.pools)]


def check_quota(
    uow: UnitOfWork, routing: RoutingPolicyV1, *, model_id: str, now: datetime
) -> Problem | None:
    entry = routing.model(model_id)
    if entry is None:
        return None
    usage = pool_usage(uow, routing, entry.pool, now)
    if usage.over_soft_limit:
        return {
            "path": "execution_request.model",
            "message": (
                f"quota pool {entry.pool} is at {usage.used} of its soft limit "
                f"{usage.soft_limit} for the current {usage.window} window"
            ),
        }
    return None


@dataclass(frozen=True, slots=True)
class Reservation:
    """What the launch-time reservation recorded. The fake provider consumes nothing, so
    the reservation is structural: the row that C3's real launch will decrement."""

    model: str
    harness: str
    endpoint_kind: str
    pool: str
    ok: bool
    detail: str


def reserve(
    uow: UnitOfWork, policy_document: dict[str, Any], *, harness: str, model_id: str, now: datetime
) -> Reservation:
    """The authoritative pool check at attempt launch (05b). Called inside the fenced
    transaction that moves the attempt to `launching`."""
    routing = load_routing(uow, policy_document)
    entry: RoutingModel | None = routing.model(model_id) if routing else None
    if routing is None or entry is None:
        return Reservation(
            model=model_id,
            harness=harness,
            endpoint_kind="unknown",
            pool="unrouted",
            ok=True,
            detail="no routing policy entry; nothing to reserve",
        )
    problem = check_quota(uow, routing, model_id=model_id, now=now)
    if problem is not None:
        return Reservation(
            model=model_id,
            harness=harness,
            endpoint_kind=entry.endpoint,
            pool=entry.pool,
            ok=False,
            detail=str(problem["message"]),
        )
    return Reservation(
        model=model_id,
        harness=harness,
        endpoint_kind=entry.endpoint,
        pool=entry.pool,
        ok=True,
        detail=f"pool {entry.pool} is under its soft limit",
    )


def history(
    uow: UnitOfWork,
    *,
    model: str | None,
    project: str | None,
    since: datetime | None,
) -> list[dict[str, Any]]:
    """Per-model outcomes Foundry reads before selecting (04, 05b)."""
    task_ids: list[str] | None = None
    if project is not None:
        task_ids = [
            t.id
            for t in uow.tasks.search(
                state=None,
                project=project,
                repository_id=None,
                external_id=None,
                updated_since=None,
                after_id=None,
                limit=1000,
            )
        ]
    rows = uow.attempt_metrics.list_since(since=since, model=model, task_ids=task_ids)
    return [
        {
            "attempt_id": m.attempt_id,
            "task_id": m.task_id,
            "model": m.model,
            "harness": m.harness,
            "endpoint_kind": m.endpoint_kind,
            "pool": m.pool,
            "wall_ms": m.wall_ms,
            "tokens_in": m.tokens_in,
            "tokens_out": m.tokens_out,
            "cost_units": m.cost_units,
            "cost_source": m.cost_source,
            "exit_class": m.exit_class,
            "gates_passed": m.gates_passed,
            "gates_failed": m.gates_failed,
            "corrections_after": m.corrections_after,
            "acceptance_verdict": m.acceptance_verdict,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]
