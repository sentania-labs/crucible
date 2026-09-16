"""POST /tasks/{id}/cancel (16): record the verbatim reason; cancelling while an attempt
runs (the supervisor terminates it), cancelled at once otherwise."""

from __future__ import annotations

from crucible.application.errors import NotFoundError
from crucible.application.transitions import move_task, record_event
from crucible.contracts.api import CancelRequest
from crucible.domain.entities import Principal, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def cancel_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: CancelRequest
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    payload = {
        "reason": request.reason,
        "verbatim": request.verbatim,
        "decided_by": request.decided_by,
    }
    record_event(
        uow,
        clock,
        EventKind.TASK_CANCEL_REQUESTED,
        principal=principal.name,
        task_id=task.id,
        payload=payload,
    )
    if task.state is TaskState.RUNNING:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLING,
            EventKind.TASK_CANCELLING,
            principal=principal.name,
            payload=payload,
        )
    else:
        move_task(
            uow,
            clock,
            task,
            TaskState.CANCELLED,
            EventKind.TASK_CANCELLED,
            principal=principal.name,
            payload=payload,
        )
    return task
