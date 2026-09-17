"""/events, /executions/{id}, /attempts/{id}, /repositories/{name} (04)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from crucible.adapters.api.deps import Admin, Ctx, Orchestrator, Reader, UoW
from crucible.application.artifacts import read_artifact, upload_artifact
from crucible.application.errors import NotFoundError
from crucible.application.queries import (
    artifact_view,
    attempt_artifacts,
    attempt_evidence,
    attempt_gates,
    attempt_report,
    attempt_view,
    execution_view,
    global_events,
)
from crucible.application.repositories import register_repository
from crucible.contracts.api import (
    ArtifactList,
    ArtifactView,
    AttemptView,
    CompletionClaimView,
    EventList,
    EvidenceList,
    ExecutionView,
    GateList,
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


@router.get("/attempts/{attempt_id}/gates", response_model=GateList)
def get_gates(attempt_id: str, uow: UoW, _principal: Reader) -> GateList:
    return attempt_gates(uow, attempt_id)


@router.get("/attempts/{attempt_id}/evidence", response_model=EvidenceList)
def get_evidence(attempt_id: str, uow: UoW, _principal: Reader) -> EvidenceList:
    return attempt_evidence(uow, attempt_id)


@router.get("/attempts/{attempt_id}/report", response_model=CompletionClaimView)
def get_report(attempt_id: str, uow: UoW, _principal: Reader) -> CompletionClaimView:
    return attempt_report(uow, attempt_id)


@router.get("/attempts/{attempt_id}/artifacts", response_model=ArtifactList)
def list_attempt_artifacts(attempt_id: str, uow: UoW, _principal: Reader) -> ArtifactList:
    return attempt_artifacts(uow, attempt_id)


@router.post("/attempts/{attempt_id}/artifacts", response_model=ArtifactView, status_code=201)
async def post_attempt_artifact(
    attempt_id: str,
    request: Request,
    ctx: Ctx,
    uow: UoW,
    principal: Orchestrator,
    type: Annotated[str, Query(min_length=1)],
    filename: Annotated[str, Query(min_length=1)],
) -> ArtifactView:
    """Upload an artifact. 04 describes multipart; this takes the bytes as the request
    body with `type` and `filename` as query parameters, because multipart parsing would
    need a dependency Crucible does not carry. Recorded in docs/implementation-notes/c2.md."""
    content = await request.body()
    artifact = upload_artifact(
        uow,
        ctx.clock,
        ctx.artifact_store,
        principal=principal,
        attempt_id=attempt_id,
        artifact_type=type,
        filename=filename,
        content=content,
        content_type=request.headers.get("content-type", "application/octet-stream"),
    )
    uow.commit()
    return artifact_view(uow, artifact.id)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView)
def get_artifact(artifact_id: str, uow: UoW, _principal: Reader) -> ArtifactView:
    return artifact_view(uow, artifact_id)


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(artifact_id: str, ctx: Ctx, uow: UoW, _principal: Reader) -> Response:
    artifact, content = read_artifact(uow, ctx.artifact_store, artifact_id)
    return Response(
        content=content,
        media_type=artifact.content_type,
        headers={"X-Crucible-Artifact-Sha256": artifact.sha256},
    )
