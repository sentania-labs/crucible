"""Domain entities as plain dataclasses. Repositories return these, never ORM rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    OPERATOR = "operator"
    OBSERVER = "observer"
    ADMIN = "admin"

    @property
    def may_mutate(self) -> bool:
        return self is not Role.OBSERVER


class ExecutionRole(StrEnum):
    IMPLEMENT = "implement"
    CORRECT = "correct"
    REVIEW = "review"


@dataclass(slots=True)
class Principal:
    id: str
    name: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None = None


@dataclass(slots=True)
class Repository:
    id: str
    name: str
    url: str
    default_branch: str
    policy_name: str
    installation_id: int | None
    registered_by: str
    created_at: datetime


@dataclass(slots=True)
class Policy:
    name: str
    version: int
    document: dict[str, Any]
    created_at: datetime
    retired_at: datetime | None = None


@dataclass(slots=True)
class Task:
    id: str
    external_id: str
    principal_id: str
    project: str
    title: str
    state: TaskState
    contract_version: int
    policy_name: str
    policy_version: int
    repository_id: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None


@dataclass(slots=True)
class TaskContract:
    id: str
    task_id: str
    version: int
    document: dict[str, Any]
    sha256: str
    submitted_at: datetime


@dataclass(slots=True)
class Execution:
    id: str
    task_id: str
    role: ExecutionRole
    contract_version: int
    harness: str
    model: str
    effort: str | None
    provider: str
    image: str
    policy_snapshot: dict[str, Any]
    state: ExecutionState
    max_attempts: int
    retry_on: list[str]
    timeout_seconds: int
    created_at: datetime
    ended_at: datetime | None = None


@dataclass(slots=True)
class Attempt:
    id: str
    execution_id: str
    task_id: str
    number: int
    state: AttemptState
    created_at: datetime
    workspace_path: str | None = None
    handle: str | None = None
    identity_sha256: str | None = None
    image_digest: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: int | None = None
    exit_class: ExitClass | None = None
    timeout_at: datetime | None = None
    drain_deadline: datetime | None = None
    killed_at: datetime | None = None
    termination_reason: str | None = None


@dataclass(slots=True)
class Event:
    seq: int | None
    ts: datetime
    kind: str
    principal: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    execution_id: str | None = None
    attempt_id: str | None = None


@dataclass(slots=True)
class Lease:
    id: str
    kind: str
    key: str
    holder: str
    fenced_token: int
    expires_at: datetime


@dataclass(slots=True)
class CompletionClaimRecord:
    attempt_id: str
    document: dict[str, Any]
    parsed_ok: bool
    parse_errors: list[dict[str, Any]]


@dataclass(slots=True)
class SupervisorStatus:
    holder: str | None
    last_tick_at: datetime | None
    tick_ms: int | None
    counts: dict[str, int]
