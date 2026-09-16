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
    # The head SHA the collector produced for the latest implement or correct attempt.
    # Everything after `reported` is bound to it (09).
    head_sha: str | None = None
    # `branch` and `pull_request` deliverables stop at awaiting_acceptance in C2: an
    # accept records the AcceptanceResult and raises this flag for C4's publisher.
    publish_pending: bool = False


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
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None


class EscalationState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class AcceptanceVerdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_MORE_WORK = "needs_more_work"


class DispositionKind(StrEnum):
    FIX = "fix"
    DECLINE = "decline"
    OUT_OF_SCOPE = "out_of_scope"
    ALREADY_ADDRESSED = "already_addressed"
    QUESTION = "question"


@dataclass(slots=True)
class Artifact:
    id: str
    attempt_id: str | None
    task_id: str | None
    type: str
    path: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime
    created_by: str = "crucible"


@dataclass(slots=True)
class EvidenceRecord:
    id: int | None
    attempt_id: str | None
    task_id: str | None
    kind: str
    observed_at: datetime
    source: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None
    pull_request_id: str | None = None


@dataclass(slots=True)
class ReviewReportRecord:
    id: str
    task_id: str
    head_sha: str
    reviewer_kind: str
    reviewer_attempt_id: str | None
    reviewer_principal_id: str | None
    document: dict[str, Any]
    created_at: datetime
    artifact_id: str | None = None
    superseded_at: datetime | None = None


@dataclass(slots=True)
class GateResultRecord:
    id: str
    task_id: str
    attempt_id: str
    head_sha: str | None
    gate: str
    phase: str
    result: str
    detail: str
    evidence_ids: list[int]
    evaluated_at: datetime


@dataclass(slots=True)
class AcceptanceResult:
    id: str
    task_id: str
    head_sha: str
    principal_id: str
    verdict: AcceptanceVerdict
    reasoning: str
    created_at: datetime
    superseded_at: datetime | None = None


@dataclass(slots=True)
class Decision:
    id: str
    task_id: str | None
    escalation_id: str | None
    principal_id: str
    kind: str
    verbatim: str
    resolves: str
    created_at: datetime


@dataclass(slots=True)
class Escalation:
    id: str
    task_id: str
    attempt_id: str | None
    state: EscalationState
    question: str
    opened_at: datetime
    closed_at: datetime | None = None
    decision_id: str | None = None
    last_wake_at: datetime | None = None


@dataclass(slots=True)
class Wake:
    id: str
    principal_id: str
    task_id: str | None
    reason: str
    payload: dict[str, Any]
    created_at: datetime
    attempts: int = 0
    delivered_at: datetime | None = None
    acked_at: datetime | None = None
    ack_note: str | None = None
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    gave_up_at: datetime | None = None


@dataclass(slots=True)
class ReviewDisposition:
    id: str
    review_comment_id: str
    principal_id: str
    disposition: DispositionKind
    reasoning: str
    created_at: datetime


@dataclass(slots=True)
class AttemptMetrics:
    attempt_id: str
    task_id: str
    model: str
    harness: str
    endpoint_kind: str
    pool: str
    wall_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_units: float | None = None
    cost_source: str = "none"
    exit_class: str | None = None
    gates_passed: int = 0
    gates_failed: int = 0
    corrections_after: int = 0
    acceptance_verdict: str | None = None
    created_at: datetime | None = None


@dataclass(slots=True)
class RoutingPolicyRecord:
    name: str
    version: int
    document: dict[str, Any]
    created_at: datetime
    retired_at: datetime | None = None
