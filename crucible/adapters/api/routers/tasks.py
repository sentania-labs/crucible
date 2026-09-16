"""/tasks (04): submit, list, get, start, cancel, events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from crucible.adapters.api.deps import Ctx, Mutator, Reader, UoW
from crucible.adapters.api.idempotency import with_idempotency
from crucible.application.cancel_task import cancel_task
from crucible.application.queries import task_events, task_list, task_view
from crucible.application.start_task import start_task
from crucible.application.submit_task import submit_task
from crucible.contracts.api import CancelRequest, EventList, StartRequest, TaskList, TaskView
from crucible.domain.lifecycle import TaskState
from crucible.ports.repository import UnitOfWork

router = APIRouter(prefix="/tasks")
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


@router.post("", response_model=TaskView, status_code=201)
async def submit(
    request: Request, ctx: Ctx, principal: Mutator, idempotency_key: IdemKey = None
) -> JSONResponse:
    body = await request.body()
    document: Any = await request.json() if body else {}

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task, _ = submit_task(uow, ctx.clock, principal=principal, body=document)
        return 201, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=body,
        scope=str(request.url.path),
        produce=produce,
    )


@router.get("", response_model=TaskList)
def list_tasks(
    uow: UoW,
    _principal: Reader,
    state: TaskState | None = None,
    project: str | None = None,
    repository: str | None = None,
    external_id: str | None = None,
    updated_since: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> TaskList:
    return task_list(
        uow,
        state=state,
        project=project,
        repository=repository,
        external_id=external_id,
        updated_since=updated_since,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{task_id}", response_model=TaskView)
def get_task(task_id: str, uow: UoW, _principal: Reader) -> TaskView:
    return task_view(uow, task_id)


@router.post("/{task_id}/start", response_model=TaskView)
async def start(
    task_id: str,
    body: StartRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = start_task(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/cancel", response_model=TaskView)
async def cancel(
    task_id: str,
    body: CancelRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = cancel_task(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 200, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.get("/{task_id}/events", response_model=EventList)
def events(
    task_id: str,
    uow: UoW,
    _principal: Reader,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> EventList:
    return task_events(uow, task_id, cursor=cursor, limit=limit)
