"""Acceptance (04, 09, 11). Foundry's semantic verdict on a collected head.

Crucible records it and never computes it. An `artifacts` deliverable reaches `accepted`
here; a `branch` or `pull_request` deliverable records the AcceptanceResult and moves to
`publishing`, where the supervisor pushes the verified head and opens the PR (23)."""

from __future__ import annotations

from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.transitions import move_task, record_event, require_contract
from crucible.application.wakes import create_wake
from crucible.contracts.api import AcceptRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import AcceptanceResult, AcceptanceVerdict, Principal, Role, Task
from crucible.domain.events import EventKind
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

PUBLISHED_DELIVERABLES = frozenset({"pull_request", "branch"})


def deliverable_kinds(uow: UnitOfWork, task: Task) -> list[str]:
    stored = require_contract(uow, task)
    return [str(d.get("kind")) for d in stored.document.get("deliverables", [])]


def record_acceptance(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: AcceptRequest
) -> Task:
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR):
        raise ForbiddenError("only an orchestrator or operator principal records acceptance")
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    if task.state is not TaskState.AWAITING_ACCEPTANCE:
        raise TransitionNotAllowedError(
            f"acceptance is recorded only in awaiting_acceptance; task is {task.state.value}"
        )
    head = task.head_sha or ""
    if request.head_sha and request.head_sha != head:
        raise TransitionNotAllowedError(
            f"the collected head is {head!r}; an AcceptanceResult names the head it is for"
        )
    now = clock.now()
    uow.acceptance.supersede_for_task(task.id, now)
    result = AcceptanceResult(
        id=new_id(),
        task_id=task.id,
        head_sha=head,
        principal_id=principal.id,
        verdict=request.verdict,
        reasoning=request.reasoning,
        created_at=now,
    )
    uow.acceptance.add(result)
    record_event(
        uow,
        clock,
        EventKind.ACCEPTANCE_RECORDED,
        principal=principal.name,
        task_id=task.id,
        payload={
            "acceptance_id": result.id,
            "head_sha": head,
            "verdict": request.verdict.value,
            "reasoning": request.reasoning,
        },
    )
    if request.verdict is AcceptanceVerdict.REJECTED:
        move_task(
            uow,
            clock,
            task,
            TaskState.REJECTED,
            EventKind.TASK_REJECTED,
            principal=principal.name,
            payload={"head_sha": head, "acceptance_id": result.id},
        )
        return task
    if request.verdict is AcceptanceVerdict.NEEDS_MORE_WORK:
        # The task waits here until a correction is attached (09); nothing moves yet.
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.NEEDS_MORE_WORK,
            summary=f"needs_more_work recorded on {head}; attach a correction to continue",
            task=task,
            extra_links={"corrections": f"/v1/tasks/{task.id}/corrections"},
            raised_by=principal.name,
        )
        return task
    kinds = deliverable_kinds(uow, task)
    if PUBLISHED_DELIVERABLES & set(kinds):
        # 09: a `branch` or `pull_request` deliverable goes through `publishing`, where
        # the supervisor mints a token, pushes the bundle head, and opens or updates the
        # PR. Nothing is accepted unpublished, and the API does not touch GitHub: the
        # state change is the request, the supervisor does the work (14).
        move_task(
            uow,
            clock,
            task,
            TaskState.PUBLISHING,
            EventKind.TASK_PUBLISHING,
            principal=principal.name,
            payload={
                "head_sha": head,
                "acceptance_id": result.id,
                "deliverables": kinds,
            },
        )
        return task
    move_task(
        uow,
        clock,
        task,
        TaskState.ACCEPTED,
        EventKind.TASK_ACCEPTED,
        principal=principal.name,
        payload={"head_sha": head, "acceptance_id": result.id, "deliverables": kinds},
    )
    return task


def close_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, note: str
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    move_task(
        uow,
        clock,
        task,
        TaskState.CLOSED,
        EventKind.TASK_CLOSED,
        principal=principal.name,
        payload={"note": note},
    )
    return task
