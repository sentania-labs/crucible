"""/tasks (04): submit, list, get, start, cancel, events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from crucible.adapters.api.deps import Ctx, Mutator, Orchestrator, Reader, UoW
from crucible.adapters.api.idempotency import with_idempotency
from crucible.application.acceptance import close_task, record_acceptance
from crucible.application.cancel_task import cancel_task
from crucible.application.corrections import amend_task, attach_correction
from crucible.application.decisions import record_decision, record_disposition
from crucible.application.queries import task_events, task_list, task_view
from crucible.application.review import request_review
from crucible.application.start_task import start_task
from crucible.application.submit_task import submit_task
from crucible.contracts.api import (
    AcceptRequest,
    AmendRequest,
    CancelRequest,
    CloseRequest,
    DecisionRequest,
    DispositionRequest,
    EventList,
    ReviewRequest,
    StartRequest,
    TaskList,
    TaskView,
)
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


@router.post("/{task_id}/review", response_model=TaskView)
async def review(
    task_id: str,
    body: ReviewRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = request_review(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
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


@router.post("/{task_id}/accept", response_model=TaskView)
async def accept(
    task_id: str,
    body: AcceptRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_acceptance(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
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


@router.post("/{task_id}/corrections", response_model=TaskView)
async def corrections(
    task_id: str, request: Request, ctx: Ctx, principal: Mutator, idempotency_key: IdemKey = None
) -> JSONResponse:
    raw = await request.body()
    document: Any = await request.json() if raw else {}

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = attach_correction(
            uow, ctx.clock, principal=principal, task_id=task_id, body=document
        )
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


@router.post("/{task_id}/amend", response_model=TaskView)
async def amend(
    task_id: str,
    body: AmendRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = amend_task(
            uow,
            ctx.clock,
            principal=principal,
            task_id=task_id,
            body=body.contract,
            reason=body.reason,
        )
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


@router.post("/{task_id}/decisions", response_model=TaskView)
async def decisions(
    task_id: str,
    body: DecisionRequest,
    request: Request,
    ctx: Ctx,
    principal: Mutator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = record_decision(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
        return 201, task_view(uow, task.id).model_dump(mode="json")

    return await with_idempotency(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        principal=principal,
        key=idempotency_key,
        body=raw,
        scope=str(request.url.path),
        produce=produce,
    )


@router.post("/{task_id}/dispositions", response_model=TaskView)
async def dispositions(
    task_id: str, body: DispositionRequest, ctx: Ctx, uow: UoW, principal: Orchestrator
) -> TaskView:
    record_disposition(uow, ctx.clock, principal=principal, task_id=task_id, request=body)
    uow.commit()
    return task_view(uow, task_id)


@router.post("/{task_id}/close", response_model=TaskView)
async def close(
    task_id: str,
    body: CloseRequest,
    request: Request,
    ctx: Ctx,
    principal: Orchestrator,
    idempotency_key: IdemKey = None,
) -> JSONResponse:
    raw = await request.body()

    async def produce(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        task = close_task(uow, ctx.clock, principal=principal, task_id=task_id, note=body.note)
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
