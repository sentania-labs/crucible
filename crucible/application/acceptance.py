"""Acceptance (04, 09, 11). Foundry's semantic verdict on a collected head.

Crucible records it and never computes it. An `artifacts` deliverable reaches `accepted`
here; a `branch` or `pull_request` deliverable records the AcceptanceResult, raises the
publish-pending flag, and waits in `awaiting_acceptance` for C4's publisher."""

from __future__ import annotations

from crucible.application.errors import (
    ForbiddenError,
    NotFoundError,
    TransitionNotAllowedError,
)
from crucible.application.transitions import move_task, record_event
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
    stored = uow.contracts.get(task.id, task.contract_version)
    assert stored is not None
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
        # 09 sends these through `publishing`, which is C4. C2 records the acceptance and
        # leaves the task waiting with a flag, so nothing is accepted unpublished.
        task.publish_pending = True
        task.updated_at = now
        uow.tasks.save(task)
        record_event(
            uow,
            clock,
            EventKind.TASK_PUBLISH_PENDING,
            principal=principal.name,
            task_id=task.id,
            payload={
                "head_sha": head,
                "acceptance_id": result.id,
                "deliverables": kinds,
                "note": "publishing needs the GitHub publisher, which arrives in C4 (20)",
            },
        )
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.PUBLISH_PENDING,
            summary=(
                f"accepted at {head}; the {'/'.join(sorted(set(kinds)))} deliverable waits "
                "for the publisher, which arrives in C4"
            ),
            task=task,
            raised_by=principal.name,
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
