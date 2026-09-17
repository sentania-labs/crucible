"""Policy and routing-policy upload and read (04, 05b).

A version is immutable once a task references it. `allow_no_ci`, `allow_branch_only`, and
turning off `release.require_operator_approval` may only be uploaded by an operator or
admin principal, and each is recorded as a decision."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from crucible.application.errors import (
    ConflictError,
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
)
from crucible.application.transitions import record_event
from crucible.contracts.common import to_document
from crucible.contracts.policy import PolicyV1, RoutingPolicyV1
from crucible.domain.entities import Decision, Policy, Principal, Role, RoutingPolicyRecord
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

# Every real harness mounts its credential rw-narrow (12, 07): Claude Code and Codex from
# the start, AGY since the C5 live run showed its token rotating.
RW_NARROW_HARNESSES: frozenset[str] = frozenset({"claude_code", "codex", "agy"})


def _problems(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"path": ".".join(str(p) for p in err["loc"]) or "$", "message": err["msg"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


def validate_policy(document: object, *, name: str, version: int) -> PolicyV1:
    try:
        policy = PolicyV1.model_validate(document)
    except ValidationError as exc:
        raise ContractValidationError("policy failed validation", errors=_problems(exc)) from None
    problems: list[dict[str, Any]] = []
    if policy.name != name:
        problems.append({"path": "name", "message": f"the path says {name!r}"})
    if policy.version != version:
        problems.append({"path": "version", "message": f"the path says {version}"})
    # 05b: concurrency must be 1 for any harness whose credential mount is rw-narrow (12).
    for harness in sorted(RW_NARROW_HARNESSES):
        limit = policy.concurrency.per_harness.get(harness)
        if limit is None:
            problems.append(
                {
                    "path": f"concurrency.per_harness.{harness}",
                    "message": "every supported harness needs a concurrency cap",
                }
            )
        elif limit != 1:
            problems.append(
                {
                    "path": f"concurrency.per_harness.{harness}",
                    "message": (
                        f"{harness} mounts its credential rw-narrow (12), so concurrency must be 1"
                    ),
                }
            )
    if problems:
        raise ContractValidationError("policy failed validation", errors=problems)
    return policy


def put_policy(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    name: str,
    version: int,
    document: object,
) -> Policy:
    policy = validate_policy(document, name=name, version=version)
    existing = uow.policies.get(name, version)
    if existing is not None and uow.policies.is_referenced(name, version):
        raise ConflictError(
            f"policy {name}/{version} is referenced by at least one task and is immutable (05b)"
        )
    operator_only = policy.operator_only_settings()
    if operator_only and principal.role not in (Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError(
            "these settings may only be uploaded by an operator or admin principal: "
            + ", ".join(operator_only),
            errors=[{"path": field, "message": "operator-only (05b)"} for field in operator_only],
        )
    routing = uow.routing_policies.get(policy.routing.policy.name, policy.routing.policy.version)
    if routing is None:
        raise ContractValidationError(
            "policy names a routing policy that does not exist",
            errors=[
                {
                    "path": "routing.policy",
                    "message": (
                        f"{policy.routing.policy.name}/{policy.routing.policy.version} "
                        "is not uploaded"
                    ),
                }
            ],
        )
    now = clock.now()
    stored = uow.policies.put(
        Policy(
            name=name,
            version=version,
            document=to_document(policy),
            created_at=existing.created_at if existing else now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.POLICY_UPLOADED,
        principal=principal.name,
        payload={
            "policy": {"name": name, "version": version},
            "replaced": existing is not None,
            "operator_only_settings": operator_only,
        },
    )
    for field in operator_only:
        uow.decisions.add(
            Decision(
                id=new_id(),
                task_id=None,
                escalation_id=None,
                principal_id=principal.id,
                kind="policy_operator_setting",
                verbatim=f"{principal.name} uploaded {name}/{version} with {field} set",
                resolves=field,
                created_at=now,
            )
        )
    return stored


def get_policy(uow: UnitOfWork, *, name: str, version: int) -> Policy:
    policy = uow.policies.get(name, version)
    if policy is None:
        raise NotFoundError(f"policy {name}/{version} does not exist")
    return policy


def validate_routing_policy(document: object, *, name: str, version: int) -> RoutingPolicyV1:
    try:
        routing = RoutingPolicyV1.model_validate(document)
    except ValidationError as exc:
        raise ContractValidationError(
            "routing policy failed validation", errors=_problems(exc)
        ) from None
    problems: list[dict[str, Any]] = []
    if routing.name != name:
        problems.append({"path": "name", "message": f"the path says {name!r}"})
    if routing.version != version:
        problems.append({"path": "version", "message": f"the path says {version}"})
    if problems:
        raise ContractValidationError("routing policy failed validation", errors=problems)
    return routing


def put_routing_policy(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    name: str,
    version: int,
    document: object,
) -> RoutingPolicyRecord:
    routing = validate_routing_policy(document, name=name, version=version)
    existing = uow.routing_policies.get(name, version)
    if existing is not None and uow.routing_policies.is_referenced(name, version):
        raise ConflictError(
            f"routing policy {name}/{version} is referenced by a policy and is immutable (05b)"
        )
    now = clock.now()
    stored = uow.routing_policies.put(
        RoutingPolicyRecord(
            name=name,
            version=version,
            document=to_document(routing),
            created_at=existing.created_at if existing else now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.ROUTING_POLICY_UPLOADED,
        principal=principal.name,
        payload={
            "routing_policy": {"name": name, "version": version},
            "models": len(routing.models),
            "replaced": existing is not None,
        },
    )
    return stored


def get_routing_policy(uow: UnitOfWork, *, name: str, version: int) -> RoutingPolicyRecord:
    routing = uow.routing_policies.get(name, version)
    if routing is None:
        raise NotFoundError(f"routing policy {name}/{version} does not exist")
    return routing
