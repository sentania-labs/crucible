"""/events, /executions/{id}, /attempts/{id}, /repositories/{name} (04)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from crucible.adapters.api.deps import Admin, Ctx, Reader, UoW
from crucible.application.errors import NotFoundError
from crucible.application.queries import attempt_view, execution_view, global_events
from crucible.application.repositories import register_repository
from crucible.contracts.api import (
    AttemptView,
    EventList,
    ExecutionView,
    RepositoryRegistration,
    RepositoryView,
)
from crucible.domain.entities import Repository

router = APIRouter()


@router.get("/events", response_model=EventList)
def events(
    uow: UoW,
    _principal: Reader,
    cursor: str | None = None,
    kind: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> EventList:
    return global_events(uow, cursor=cursor, kind=kind, since=since, limit=limit)


@router.get("/executions/{execution_id}", response_model=ExecutionView)
def get_execution(execution_id: str, uow: UoW, _principal: Reader) -> ExecutionView:
    return execution_view(uow, execution_id)


@router.get("/attempts/{attempt_id}", response_model=AttemptView)
def get_attempt(attempt_id: str, uow: UoW, _principal: Reader) -> AttemptView:
    return attempt_view(uow, attempt_id)


def _repo_view(repo: Repository) -> RepositoryView:
    return RepositoryView(
        id=repo.id,
        name=repo.name,
        url=repo.url,
        default_branch=repo.default_branch,
        policy_name=repo.policy_name,
        installation_id=repo.installation_id,
        registered_by=repo.registered_by,
        created_at=repo.created_at,
    )


@router.get("/repositories/{name}", response_model=RepositoryView)
def get_repository(name: str, uow: UoW, _principal: Reader) -> RepositoryView:
    repo = uow.repositories.get_by_name(name)
    if repo is None:
        raise NotFoundError(f"repository {name!r} is not registered")
    return _repo_view(repo)


@router.put("/repositories/{name}", response_model=RepositoryView)
def put_repository(
    name: str, body: RepositoryRegistration, ctx: Ctx, uow: UoW, principal: Admin
) -> RepositoryView:
    repo = register_repository(
        uow, ctx.clock, principal_name=principal.name, name=name, registration=body
    )
    uow.commit()
    return _repo_view(repo)
