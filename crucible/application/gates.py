"""Pre-PR gate evaluation and the task transitions it drives (09, 11).

The evaluators are pure functions in `crucible.domain.gates`. This module supplies them
with the contract, the policy, and the evidence rows, persists one GateResult per gate,
and then moves the task: any fail or error goes to `pre_pr_gates_failed`, a pending
internal review goes to `awaiting_internal_review`, everything else to `gates_passed`
and straight on to `awaiting_acceptance`."""

from __future__ import annotations

import logging
from typing import Any

from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    Execution,
    ExecutionRole,
    GateResultRecord,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.gates import (
    DEFERRED_TO_C3,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateOutcome,
    GateResult,
    blocking,
    evaluate_pre_pr,
    waiting_for_review,
)
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.gates")

PHASE_PRE_PR = "pre_pr"


def internal_review_required(
    policy: dict[str, Any], contract: dict[str, Any], role: ExecutionRole
) -> bool:
    """09: a correction needs another internal review only when the correction contract
    asks for one or the policy requires one for corrections."""
    review = policy.get("internal_review", {})
    if not review.get("required", True):
        return False
    if role is not ExecutionRole.CORRECT:
        return True
    if review.get("required_for_corrections", False):
        return True
    correction = contract.get("correction") or {}
    return bool(correction.get("request_internal_review", False))


def evidence_items(uow: UnitOfWork, attempt_id: str, task_id: str) -> tuple[EvidenceItem, ...]:
    """Attempt evidence plus the task-scoped review evidence the review gate reads."""
    rows = list(uow.evidence.list_for_attempt(attempt_id))
    rows.extend(
        row
        for row in uow.evidence.list_for_task(task_id)
        if row.attempt_id != attempt_id and row.kind == "review_received"
    )
    return tuple(
        EvidenceItem(
            id=int(row.id or 0),
            kind=row.kind,
            source=row.source,
            verified=row.verified,
            payload=row.payload,
            artifact_id=row.artifact_id,
        )
        for row in rows
    )


def gate_input(uow: UnitOfWork, *, task: Task, attempt: Attempt, execution: Execution) -> GateInput:
    stored = uow.contracts.get(task.id, execution.contract_version)
    assert stored is not None
    policy = execution.policy_snapshot or {}
    return GateInput(
        contract=stored.document,
        policy=policy,
        head_sha=task.head_sha,
        evidence=evidence_items(uow, attempt.id, task.id),
        internal_review_required=internal_review_required(policy, stored.document, execution.role),
    )


def configured_pre_pr_gates(policy: dict[str, Any]) -> list[str]:
    """The policy names the required set (05b); with no policy document, every pre-PR gate.

    An explicit empty list is an empty set, which is not the same as no policy at all."""
    gates = policy.get("gates", {}).get("pre_pr")
    if gates is None:
        return sorted(PRE_PR_GATES)
    return [str(g) for g in gates]


def persist_outcomes(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt: Attempt,
    outcomes: dict[str, GateOutcome],
) -> None:
    now = clock.now()
    for gate, outcome in outcomes.items():
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id=task.id,
                attempt_id=attempt.id,
                head_sha=task.head_sha or "",
                gate=gate,
                phase=PHASE_PRE_PR,
                result=outcome.result.value,
                detail=outcome.detail,
                evidence_ids=list(outcome.evidence_ids),
                evaluated_at=now,
            )
        )


def summarize(outcomes: dict[str, GateOutcome]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome.result.value] = counts.get(outcome.result.value, 0) + 1
    return {
        "counts": counts,
        "results": {gate: outcome.result.value for gate, outcome in outcomes.items()},
        "failing": blocking(outcomes),
        "deferred": sorted(g for g in outcomes if g in DEFERRED_TO_C3),
    }


def _unchanged(
    uow: UnitOfWork, *, task: Task, attempt: Attempt, outcomes: dict[str, GateOutcome]
) -> bool:
    """True when the stored rows already say exactly this for this head."""
    stored = {
        row.gate: (row.result, row.detail)
        for row in uow.gate_results.list_for_attempt(attempt.id)
        if row.head_sha == (task.head_sha or "")
    }
    if not stored:
        return False
    return stored == {gate: (o.result.value, o.detail) for gate, o in outcomes.items()}


def evaluate_and_advance(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt: Attempt,
    execution: Execution,
) -> dict[str, GateOutcome]:
    """Evaluate every pre-PR gate the policy requires and move the task (09).

    Safe to run again: the gate rows are keyed by (attempt, gate, head) and the task only
    moves when the transition table permits it."""
    gi = gate_input(uow, task=task, attempt=attempt, execution=execution)
    gates = configured_pre_pr_gates(gi.policy)
    outcomes = evaluate_pre_pr(gates, gi)
    summary = summarize(outcomes)
    if _unchanged(uow, task=task, attempt=attempt, outcomes=outcomes):
        # A task waiting for its internal review is re-evaluated on every tick; writing
        # the same answer again would make reconciliation not idempotent (10).
        return outcomes
    persist_outcomes(uow, clock, task=task, attempt=attempt, outcomes=outcomes)
    record_event(
        uow,
        clock,
        EventKind.GATES_EVALUATED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={"head_sha": task.head_sha, "phase": PHASE_PRE_PR, **summary},
    )
    failing = summary["failing"]
    if (
        task.state is not TaskState.REPORTED
        and task.state is not TaskState.AWAITING_INTERNAL_REVIEW
    ):
        return outcomes
    if failing:
        move_task(
            uow,
            clock,
            task,
            TaskState.PRE_PR_GATES_FAILED,
            EventKind.TASK_PRE_PR_GATES_FAILED,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={"head_sha": task.head_sha, "failing": failing},
        )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.PRE_PR_GATES_FAILED,
            summary=f"pre-PR gates failed on {task.head_sha}: {', '.join(failing)}",
            task=task,
            attempt_id=attempt.id,
            extra_links={"gates": f"/v1/attempts/{attempt.id}/gates"},
        )
        return outcomes
    if waiting_for_review(outcomes):
        if task.state is TaskState.REPORTED:
            move_task(
                uow,
                clock,
                task,
                TaskState.AWAITING_INTERNAL_REVIEW,
                EventKind.TASK_AWAITING_INTERNAL_REVIEW,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "head_sha": task.head_sha,
                    "executor": gi.policy.get("internal_review", {}).get("executor"),
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.INTERNAL_REVIEW_NEEDED,
                summary=(
                    f"the mechanical gates pass on {task.head_sha}; "
                    "a non-author internal review is required before acceptance"
                ),
                task=task,
                attempt_id=attempt.id,
                extra_links={"review": f"/v1/tasks/{task.id}/review"},
            )
        return outcomes
    move_task(
        uow,
        clock,
        task,
        TaskState.GATES_PASSED,
        EventKind.TASK_GATES_PASSED,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={"head_sha": task.head_sha, "results": summary["results"]},
    )
    move_task(
        uow,
        clock,
        task,
        TaskState.AWAITING_ACCEPTANCE,
        EventKind.TASK_AWAITING_ACCEPTANCE,
        execution_id=execution.id,
        attempt_id=attempt.id,
        payload={"head_sha": task.head_sha},
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.GATES_PASSED,
        summary=(
            f"every required pre-PR gate passed on {task.head_sha}; "
            "Foundry's AcceptanceResult is what moves this forward"
        ),
        task=task,
        attempt_id=attempt.id,
        extra_links={"accept": f"/v1/tasks/{task.id}/accept"},
    )
    return outcomes


def _review_note(uow: UnitOfWork, task: Task) -> str:
    """A reviewer that asked for changes does not stop the gate, so the wake says so (11)."""
    reports = [
        r for r in uow.review_reports.list_for_task(task.id) if r.head_sha == (task.head_sha or "")
    ]
    if any(r.document.get("verdict") == "request_changes" for r in reports):
        return "the internal review recorded request_changes, which no gate acts on; "
    return ""


def counts_for_metrics(outcomes: dict[str, GateOutcome]) -> tuple[int, int]:
    passed = sum(1 for o in outcomes.values() if o.result is GateResult.PASS)
    failed = sum(1 for o in outcomes.values() if o.result in (GateResult.FAIL, GateResult.ERROR))
    return passed, failed
