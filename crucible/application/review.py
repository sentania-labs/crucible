"""Internal non-author review (04, 11).

`POST /tasks/{id}/review` either uploads a ReviewReportV1 the orchestrator produced
through its own harness, or asks for a Crucible `review` execution on the collected head.
`reviewer_must_not_be_author` is enforced mechanically: an uploaded report may not name
an attempt of this task, and a review execution never shares an attempt with the
implementing one."""

from __future__ import annotations

from typing import Any

from crucible.application.errors import (
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.transitions import record_event
from crucible.contracts.api import ReviewRequest
from crucible.contracts.evidence import EvidenceKind, EvidenceSource
from crucible.contracts.review_report import parse_review_report
from crucible.domain.entities import (
    Attempt,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    Principal,
    ReviewReportRecord,
    Task,
)
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

REVIEWER_ORCHESTRATOR = "orchestrator"
REVIEWER_EXECUTION = "crucible_review_execution"


def latest_work_attempt(uow: UnitOfWork, task: Task) -> tuple[Attempt, Execution] | None:
    """The implementing or correcting attempt whose head the task is currently bound to."""
    best: tuple[Attempt, Execution] | None = None
    for execution in uow.executions.list_for_task(task.id):
        if execution.role is ExecutionRole.REVIEW:
            continue
        for attempt in uow.attempts.list_for_execution(execution.id):
            if best is None or attempt.id > best[0].id:
                best = (attempt, execution)
    return best


def _author_attempt_ids(uow: UnitOfWork, task: Task) -> set[str]:
    return {
        attempt.id
        for execution in uow.executions.list_for_task(task.id)
        if execution.role is not ExecutionRole.REVIEW
        for attempt in uow.attempts.list_for_execution(execution.id)
    }


def _rejection(
    clock: Clock, task: Task, principal_name: str, errors: list[dict[str, Any]]
) -> Event:
    """Recorded on its own after the request transaction rolls back (09)."""
    return Event(
        seq=None,
        ts=clock.now(),
        kind=EventKind.REVIEW_REPORT_REJECTED.value,
        principal=principal_name,
        verified=True,
        payload={"errors": errors},
        task_id=task.id,
    )


def record_review_report(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    document: dict[str, Any],
    reviewer_kind: str,
    reviewer_attempt_id: str | None,
    reviewer_principal_id: str | None,
    principal_name: str,
    artifact_id: str | None = None,
) -> ReviewReportRecord:
    """Store the report and the `review_received` evidence the gate reads."""
    report, errors = parse_review_report(document)
    if report is None:
        raise ContractValidationError(
            "review report failed validation",
            errors=errors,
            event=_rejection(clock, task, principal_name, errors),
        )
    if task.head_sha and report.reviewed_head_sha != task.head_sha:
        problems = [
            {
                "path": "reviewed_head_sha",
                "message": f"the collected head is {task.head_sha}; a review is bound to its head",
            }
        ]
        raise ContractValidationError(
            "review report names another head",
            errors=problems,
            event=_rejection(clock, task, principal_name, problems),
        )
    # The reviewer the document names wins: an uploaded report may not claim an attempt
    # of this task, and a review execution names its own attempt.
    reviewer_kind = report.reviewer.kind
    if report.reviewer.attempt_id:
        reviewer_attempt_id = report.reviewer.attempt_id
    authors = _author_attempt_ids(uow, task)
    if reviewer_attempt_id is not None and reviewer_attempt_id in authors:
        problems = [
            {
                "path": "reviewer.attempt_id",
                "message": "reviewer_must_not_be_author: that attempt wrote the change",
            }
        ]
        raise ForbiddenError(
            "the reviewer may not be the author",
            errors=problems,
            event=_rejection(clock, task, principal_name, problems),
        )
    record = ReviewReportRecord(
        id=new_id(),
        task_id=task.id,
        head_sha=report.reviewed_head_sha,
        reviewer_kind=reviewer_kind,
        reviewer_attempt_id=reviewer_attempt_id,
        reviewer_principal_id=reviewer_principal_id,
        document=document,
        created_at=clock.now(),
        artifact_id=artifact_id,
    )
    uow.review_reports.add(record)
    uow.evidence.add(
        EvidenceRecord(
            id=None,
            attempt_id=reviewer_attempt_id,
            task_id=task.id,
            kind=EvidenceKind.REVIEW_RECEIVED.value,
            observed_at=record.created_at,
            source=EvidenceSource.CRUCIBLE.value,
            verified=True,
            payload={
                "review_report_id": record.id,
                "reviewed_head_sha": record.head_sha,
                "reviewer_kind": reviewer_kind,
                "reviewer_attempt_id": reviewer_attempt_id,
                "reviewer_principal_id": reviewer_principal_id,
                "reviewer_is_author": False,
                "verdict": report.verdict,
                "findings": len(report.findings),
            },
        )
    )
    record_event(
        uow,
        clock,
        EventKind.REVIEW_REPORT_RECORDED,
        principal=principal_name,
        task_id=task.id,
        attempt_id=reviewer_attempt_id,
        payload={
            "review_report_id": record.id,
            "head_sha": record.head_sha,
            "reviewer_kind": reviewer_kind,
            "verdict": report.verdict,
            "findings": len(report.findings),
            "note": "a request_changes verdict does not move the task; Foundry decides (11)",
        },
    )
    return record


def request_review(
    uow: UnitOfWork,
    clock: Clock,
    *,
    principal: Principal,
    task_id: str,
    request: ReviewRequest,
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if task.state is not TaskState.AWAITING_INTERNAL_REVIEW:
        raise TransitionNotAllowedError(
            f"a review is accepted only in awaiting_internal_review; task is {task.state.value}"
        )
    work = latest_work_attempt(uow, task)
    if work is None:
        raise TransitionNotAllowedError("the task has no collected attempt to review")
    attempt, execution = work
    policy = execution.policy_snapshot or {}
    executor = str(policy.get("internal_review", {}).get("executor", "orchestrator_or_crucible"))
    if request.report is not None:
        if executor == "crucible":
            raise ForbiddenError("the policy requires a Crucible review execution")
        record_review_report(
            uow,
            clock,
            task=task,
            document=request.report,
            reviewer_kind=REVIEWER_ORCHESTRATOR,
            reviewer_attempt_id=None,
            reviewer_principal_id=principal.id,
            principal_name=principal.name,
        )
        # gate_results is fenced to the supervisor (14), so the next tick resolves
        # internal_review_recorded and moves the task on.
        return task
    if executor == "orchestrator":
        raise ForbiddenError("the policy requires an uploaded ReviewReportV1")
    assert request.execution is not None
    # Execution and attempt rows are fenced to the supervisor (14), so the API records
    # the request and the next tick materializes the `review` execution.
    record_event(
        uow,
        clock,
        EventKind.REVIEW_EXECUTION_REQUESTED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "head_sha": task.head_sha,
            "contract_version": execution.contract_version,
            "harness": request.execution.harness.value,
            "model": request.execution.model,
            "provider": request.execution.provider.value,
            "image": request.execution.image,
            "effort": request.execution.effort,
            "timeout_seconds": request.execution.timeout_seconds,
            "rationale": request.execution.rationale,
            "author_attempt_id": attempt.id,
        },
    )
    return task
