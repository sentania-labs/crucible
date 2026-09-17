"""Artifact upload and read (04, 14). Bytes are content-addressed on disk; the row holds
the type, size, and digest. Every upload passes the secret scanner before storage (12)."""

from __future__ import annotations

from crucible.application.errors import (
    ContractValidationError,
    ForbiddenError,
    NotFoundError,
)
from crucible.application.evidence import store_artifact
from crucible.domain.entities import Artifact, Event, Principal, Role
from crucible.domain.events import EventKind
from crucible.ports.artifacts import ArtifactStore, SecretInArtifactError
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
UPLOADABLE_TYPES = frozenset(
    {"run_evidence", "transcript", "screenshot", "diff", "test_log", "review_report", "other"}
)


def upload_artifact(
    uow: UnitOfWork,
    clock: Clock,
    store: ArtifactStore,
    *,
    principal: Principal,
    attempt_id: str,
    artifact_type: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> Artifact:
    if principal.role not in (Role.ORCHESTRATOR, Role.OPERATOR, Role.ADMIN):
        raise ForbiddenError("only an orchestrator, operator, or admin principal uploads artifacts")
    attempt = uow.attempts.get(attempt_id)
    if attempt is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    if artifact_type not in UPLOADABLE_TYPES:
        raise ContractValidationError(
            f"artifact type {artifact_type!r} is not uploadable",
            errors=[{"path": "type", "message": f"one of {sorted(UPLOADABLE_TYPES)}"}],
        )
    if len(content) > MAX_UPLOAD_BYTES:
        raise ContractValidationError(
            "artifact exceeds the upload size cap",
            errors=[{"path": "file", "message": f"at most {MAX_UPLOAD_BYTES} bytes"}],
        )
    try:
        artifact = store_artifact(
            uow,
            clock,
            store,
            attempt=attempt,
            name=filename,
            artifact_type=artifact_type,
            content=content,
            content_type=content_type,
            created_by=principal.name,
        )
    except SecretInArtifactError as exc:
        # The request transaction rolls back, so the rejection is recorded on its own.
        rejection = Event(
            seq=None,
            ts=clock.now(),
            kind=EventKind.ARTIFACT_REJECTED.value,
            principal=principal.name,
            verified=True,
            payload={"name": filename, "pattern": exc.pattern, "where": exc.where},
            task_id=attempt.task_id,
            attempt_id=attempt.id,
        )
        raise ContractValidationError(
            "the artifact matches a secret pattern and was not stored",
            errors=[{"path": "file", "message": f"secret pattern {exc.pattern} at {exc.where}"}],
            event=rejection,
        ) from None
    # `evidence` is fenced to the supervisor (14): an uploaded artifact becomes evidence
    # on the next tick, so a request can never manufacture a row a gate will consume.
    return artifact


def read_artifact(
    uow: UnitOfWork, store: ArtifactStore, artifact_id: str
) -> tuple[Artifact, bytes]:
    artifact = uow.artifacts.get(artifact_id)
    if artifact is None:
        raise NotFoundError(f"artifact {artifact_id} not found")
    if not store.exists(artifact.path):
        raise NotFoundError(f"artifact {artifact_id} has no stored content")
    return artifact, store.get(artifact.path)
