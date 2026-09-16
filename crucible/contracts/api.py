"""API request and response schemas for /v1 (04). Every response carries schema_version."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from crucible.contracts.common import SCHEMA_VERSION, Rfc3339, StrictModel
from crucible.contracts.task_contract import HarnessName, ProviderName
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = SCHEMA_VERSION


class ContractVersionView(Response):
    version: int
    sha256: str
    submitted_at: Rfc3339


class AttemptSummary(Response):
    id: str
    execution_id: str
    number: int
    state: AttemptState
    exit_code: int | None
    exit_class: ExitClass | None
    started_at: Rfc3339 | None
    ended_at: Rfc3339 | None
    handle: str | None


class ExecutionSummary(Response):
    id: str
    role: str
    state: ExecutionState
    harness: str
    model: str
    provider: str
    image: str
    contract_version: int
    max_attempts: int
    created_at: Rfc3339
    ended_at: Rfc3339 | None
    attempts: list[AttemptSummary]


class TaskView(Response):
    id: str
    external_id: str
    title: str
    project: str
    state: TaskState
    principal: str
    repository: str
    policy: dict[str, Any]
    contract_version: int
    created_at: Rfc3339
    updated_at: Rfc3339
    closed_at: Rfc3339 | None
    contract_versions: list[ContractVersionView]
    contract: dict[str, Any]
    executions: list[ExecutionSummary]
    latest_attempt: AttemptSummary | None
    gate_summary: dict[str, Any]
    pull_request: dict[str, Any] | None
    open_escalations: list[dict[str, Any]]


class TaskListItem(Response):
    id: str
    external_id: str
    title: str
    project: str
    state: TaskState
    repository: str
    contract_version: int
    created_at: Rfc3339
    updated_at: Rfc3339


class TaskList(Response):
    items: list[TaskListItem]
    next_cursor: str | None


class StartOverrides(StrictModel):
    """C1 accepts the shape and refuses any override (amendments are C2)."""

    model: str | None = None
    effort: str | None = None
    image: str | None = None
    timeout_seconds: int | None = None


class StartRequest(StrictModel):
    harness: HarnessName
    model: str = Field(min_length=1)
    provider: ProviderName
    image: str = Field(min_length=1)
    policy_version: int = Field(ge=1)
    effort: str | None = None
    overrides: StartOverrides | None = None


class CancelRequest(StrictModel):
    reason: str = Field(min_length=1)
    verbatim: str = Field(min_length=1, description="The deciding principal's own words.")
    decided_by: str = Field(min_length=1)


class EventView(Response):
    seq: int
    ts: Rfc3339
    kind: str
    task_id: str | None
    execution_id: str | None
    attempt_id: str | None
    principal: str
    verified: bool
    payload: dict[str, Any]


class EventList(Response):
    items: list[EventView]
    next_cursor: str | None


class AttemptView(Response):
    id: str
    execution_id: str
    task_id: str
    number: int
    state: AttemptState
    handle: str | None
    workspace_path: str | None
    identity_sha256: str | None
    image_digest: str | None
    started_at: Rfc3339 | None
    ended_at: Rfc3339 | None
    exit_code: int | None
    exit_class: ExitClass | None
    timeout_at: Rfc3339 | None
    termination_reason: str | None
    lease: dict[str, Any] | None
    heartbeat_summary: dict[str, Any]
    report: dict[str, Any] | None


class ExecutionView(Response):
    id: str
    task_id: str
    role: str
    state: ExecutionState
    contract_version: int
    harness: str
    model: str
    effort: str | None
    provider: str
    image: str
    max_attempts: int
    retry_on: list[str]
    timeout_seconds: int
    policy_snapshot: dict[str, Any]
    created_at: Rfc3339
    ended_at: Rfc3339 | None
    attempts: list[AttemptSummary]


class HealthView(Response):
    status: Literal["ok"]
    version: str


class ReadyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    detail: str


class ReadyView(Response):
    ready: bool
    database: ReadyCheck
    migrations: ReadyCheck
    supervisor: ReadyCheck


class SupervisorView(Response):
    lease: dict[str, Any] | None
    last_tick_at: Rfc3339 | None
    last_success_at: Rfc3339 | None
    last_error_at: Rfc3339 | None
    last_error: str | None
    healthy: bool
    tick_ms: int | None
    counts: dict[str, int]
    providers: list[dict[str, Any]]
    github: dict[str, Any] | None


class RepositoryRegistration(StrictModel):
    url: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    policy_name: str = Field(min_length=1)
    installation_id: int | None = None


class RepositoryView(Response):
    id: str
    name: str
    url: str
    default_branch: str
    policy_name: str
    installation_id: int | None
    registered_by: str
    created_at: Rfc3339
