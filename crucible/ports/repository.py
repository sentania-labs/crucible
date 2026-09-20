"""Persistence ports. Repositories return domain entities; the unit of work owns
the transaction. Every state change and its event commit together."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol

from crucible.domain.entities import (
    AcceptanceResult,
    Artifact,
    Attempt,
    AttemptMetrics,
    BootstrapImport,
    CICertification,
    CIDecision,
    CompletionClaimRecord,
    Decision,
    Escalation,
    Event,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    ExternalReview,
    ExternalReviewCycle,
    GateResultRecord,
    GitHubDelivery,
    HarnessState,
    Heartbeat,
    ImagePromotion,
    Lease,
    LogChunkRecord,
    Policy,
    PoolExhaustion,
    Principal,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    Reaction,
    Repository,
    RetentionAction,
    ReviewComment,
    ReviewDisposition,
    ReviewReportRecord,
    RoutingPolicyRecord,
    SupervisorStatus,
    Task,
    TaskContract,
    Wake,
)
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState


class PrincipalRepository(Protocol):
    def get(self, principal_id: str) -> Principal | None: ...

    def get_by_name(self, name: str) -> Principal | None: ...

    def add(self, principal: Principal, token_salt: bytes, token_hash: bytes) -> None: ...

    def credentials(self, principal_id: str) -> tuple[bytes, bytes] | None: ...

    def rotate(self, principal_id: str, token_salt: bytes, token_hash: bytes) -> None: ...

    def list_all(self) -> Sequence[Principal]: ...


class RepositoryRegistry(Protocol):
    def get_by_name(self, name: str) -> Repository | None: ...

    def get(self, repository_id: str) -> Repository | None: ...

    def upsert(self, repository: Repository) -> Repository: ...

    def list_all(self) -> Sequence[Repository]: ...


class PolicyRepository(Protocol):
    def get(self, name: str, version: int) -> Policy | None: ...

    def put(self, policy: Policy) -> Policy: ...

    def list_versions(self, name: str) -> Sequence[Policy]: ...

    def is_referenced(self, name: str, version: int) -> bool:
        """True once a task names this version; a referenced version is immutable (05b)."""
        ...


class RoutingPolicyRepository(Protocol):
    def get(self, name: str, version: int) -> RoutingPolicyRecord | None: ...

    def put(self, policy: RoutingPolicyRecord) -> RoutingPolicyRecord: ...

    def is_referenced(self, name: str, version: int) -> bool: ...


class PoolExhaustionRepository(Protocol):
    def get(self, pool: str, *, for_update: bool = False) -> PoolExhaustion | None: ...
    def put(self, mark: PoolExhaustion) -> PoolExhaustion: ...
    def list_all(self) -> Sequence[PoolExhaustion]: ...
    def clear(
        self, pool: str, *, at: datetime, principal: str, reason: str
    ) -> PoolExhaustion | None: ...


class TaskRepository(Protocol):
    def add(self, task: Task) -> None: ...

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None: ...

    def get_by_external_id(self, principal_id: str, external_id: str) -> Task | None: ...

    def save(self, task: Task) -> None: ...

    def list_by_state(self, state: TaskState, *, for_update: bool = False) -> Sequence[Task]: ...

    def search(
        self,
        *,
        state: TaskState | None,
        project: str | None,
        repository_id: str | None,
        external_id: str | None,
        updated_since: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> Sequence[Task]: ...


class ContractRepository(Protocol):
    def add(self, contract: TaskContract) -> None: ...

    def get(self, task_id: str, version: int) -> TaskContract | None: ...

    def list_for_task(self, task_id: str) -> Sequence[TaskContract]: ...


class ExecutionRepository(Protocol):
    def add(self, execution: Execution) -> None: ...

    def list_for_task_by_role(self, task_id: str, role: ExecutionRole) -> Sequence[Execution]: ...

    def get(self, execution_id: str, *, for_update: bool = False) -> Execution | None: ...

    def save(self, execution: Execution) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Execution]: ...

    def list_by_state(self, state: ExecutionState) -> Sequence[Execution]: ...


class AttemptRepository(Protocol):
    def add(self, attempt: Attempt) -> None: ...

    def get(self, attempt_id: str, *, for_update: bool = False) -> Attempt | None: ...

    def save(self, attempt: Attempt) -> None: ...

    def list_for_execution(self, execution_id: str) -> Sequence[Attempt]: ...

    def list_for_task(self, task_id: str) -> Sequence[Attempt]: ...

    def list_in_states(
        self, states: Sequence[AttemptState], *, for_update: bool = False
    ) -> Sequence[Attempt]:
        """The attempts the supervisor and the status views scan. An unsupervised attempt
        (15: imported from the bootstrap ledger, no worker behind it) is never among
        them; `list_for_task` and `list_for_execution` still return it."""
        ...


class EventRepository(Protocol):
    def append(self, event: Event) -> Event: ...

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        """The most recent event of one kind, so a busy task's request is still found."""
        ...

    def list_for_task(self, task_id: str, *, after_seq: int, limit: int) -> Sequence[Event]: ...

    def list_global(
        self, *, after_seq: int, kind: str | None, since: datetime | None, limit: int
    ) -> Sequence[Event]: ...


class LeaseRepository(Protocol):
    def acquire_supervisor(self, holder: str, now: datetime, ttl_seconds: int) -> Lease | None: ...

    def renew_supervisor(
        self, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None: ...

    def verify_supervisor(self, holder: str, fenced_token: int) -> bool: ...

    def release_supervisor(self, holder: str, fenced_token: int) -> bool: ...

    def get_supervisor(self) -> Lease | None: ...

    def upsert_attempt_lease(
        self, attempt_id: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease: ...

    def get_attempt_lease(self, attempt_id: str) -> Lease | None: ...

    def release_attempt_lease(self, attempt_id: str) -> None: ...

    def acquire_checkout_lease(
        self, key: str, holder: str, fenced_token: int, now: datetime, ttl_seconds: int
    ) -> Lease | None:
        """One attempt at a time per repository url and work branch (10). Returns None
        when a different, unexpired holder has it."""
        ...

    def get_checkout_lease(self, key: str) -> Lease | None: ...

    def release_checkout_lease(self, key: str, holder: str) -> bool: ...

    def list_checkout_leases(self) -> Sequence[Lease]: ...


class LogRepository(Protocol):
    def append(self, chunk: LogChunkRecord) -> LogChunkRecord: ...

    def last_offset(self, attempt_id: str) -> int: ...

    def count_for_attempt(self, attempt_id: str) -> int: ...

    def list_for_attempt(
        self, attempt_id: str, *, after_id: int = 0, limit: int = 500
    ) -> Sequence[LogChunkRecord]: ...

    def list_from_offset(
        self, attempt_id: str, *, offset: int, stream: str | None = None, limit: int = 500
    ) -> Sequence[LogChunkRecord]: ...

    def delete_for_attempts(self, attempt_ids: Sequence[str]) -> int: ...

    def attempts_with_logs_before(self, cutoff: datetime, limit: int) -> Sequence[str]:
        """Attempts whose newest stored chunk is older than the cutoff (16)."""
        ...


class HeartbeatRepository(Protocol):
    def append(self, heartbeat: Heartbeat) -> Heartbeat: ...

    def list_for_attempt(self, attempt_id: str, *, limit: int = 500) -> Sequence[Heartbeat]: ...

    def latest_activity(self, attempt_id: str) -> Heartbeat | None: ...


class RetentionRepository(Protocol):
    def record(self, action: RetentionAction) -> RetentionAction | None:
        """Write the action, or return None when this subject was already acted on:
        retention is idempotent and running it twice changes nothing (10)."""
        ...

    def list_recent(self, limit: int) -> Sequence[RetentionAction]: ...


class ClaimRepository(Protocol):
    def put(self, record: CompletionClaimRecord) -> None: ...

    def get(self, attempt_id: str) -> CompletionClaimRecord | None: ...


class ArtifactRepository(Protocol):
    def add(self, artifact: Artifact) -> None: ...

    def get(self, artifact_id: str) -> Artifact | None: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[Artifact]: ...

    def find_by_sha256(self, sha256: str, attempt_id: str | None) -> Artifact | None: ...


class EvidenceRepository(Protocol):
    def add(self, evidence: EvidenceRecord) -> EvidenceRecord: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[EvidenceRecord]: ...

    def list_for_task(self, task_id: str) -> Sequence[EvidenceRecord]: ...


class ReviewReportRepository(Protocol):
    def add(self, report: ReviewReportRecord) -> None: ...

    def supersede(self, report_id: str, at: datetime) -> None:
        """Mark one report superseded. The row is kept: 09 says a superseded result is
        history, not a deletion."""
        ...

    def get(self, report_id: str) -> ReviewReportRecord | None: ...

    def list_for_task(self, task_id: str) -> Sequence[ReviewReportRecord]: ...


class GateResultRepository(Protocol):
    def put(self, result: GateResultRecord) -> None: ...

    def list_for_attempt(self, attempt_id: str) -> Sequence[GateResultRecord]: ...

    def list_for_task(self, task_id: str) -> Sequence[GateResultRecord]: ...


class AcceptanceRepository(Protocol):
    def add(self, result: AcceptanceResult) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[AcceptanceResult]: ...

    def supersede_for_task(self, task_id: str, at: datetime) -> None: ...


class DecisionRepository(Protocol):
    def add(self, decision: Decision) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Decision]: ...


class EscalationRepository(Protocol):
    def add(self, escalation: Escalation) -> None: ...

    def get(self, escalation_id: str, *, for_update: bool = False) -> Escalation | None: ...

    def save(self, escalation: Escalation) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[Escalation]: ...

    def list_open(self) -> Sequence[Escalation]: ...


class DispositionRepository(Protocol):
    def add(self, disposition: ReviewDisposition) -> None: ...

    def get_by_comment(self, review_comment_id: str) -> ReviewDisposition | None: ...

    def list_for_comments(self, comment_ids: Sequence[str]) -> Sequence[ReviewDisposition]: ...


# ----- C4: GitHub delivery (14, 23) --------------------------------------


class PullRequestRepository(Protocol):
    def add(self, pull_request: PullRequest) -> None: ...

    def get(self, pull_request_id: str, *, for_update: bool = False) -> PullRequest | None: ...

    def get_for_task(self, task_id: str, *, for_update: bool = False) -> PullRequest | None: ...

    def save(self, pull_request: PullRequest) -> None: ...

    def list_in_states(self, states: Sequence[PullRequestState]) -> Sequence[PullRequest]: ...


class PullRequestHeadRepository(Protocol):
    def add(self, head: PullRequestHead) -> bool:
        """True when this (pull request, sha) is new; False when it is already recorded."""
        ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[PullRequestHead]: ...


class ExternalReviewCycleRepository(Protocol):
    def add(self, cycle: ExternalReviewCycle) -> None: ...

    def save(self, cycle: ExternalReviewCycle) -> None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReviewCycle]: ...


class ExternalReviewRepository(Protocol):
    def add(self, review: ExternalReview) -> bool:
        """True when stored; False when this signal was already recorded."""
        ...

    def get_by_github(
        self, pull_request_id: str, signal: str, github_id: str
    ) -> ExternalReview | None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ExternalReview]: ...


class ReviewCommentRepository(Protocol):
    def add(self, comment: ReviewComment) -> bool:
        """True when stored; False when this comment was already recorded."""
        ...

    def save(self, comment: ReviewComment) -> None: ...

    def get(self, comment_id: str) -> ReviewComment | None: ...

    def get_by_github(
        self, pull_request_id: str, kind: str, github_id: str
    ) -> ReviewComment | None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[ReviewComment]: ...


class ReactionRepository(Protocol):
    def add(self, reaction: Reaction) -> bool:
        """True when stored; False when this reaction was already recorded."""
        ...

    def save(self, reaction: Reaction) -> None: ...

    def list_for_pull_request(self, pull_request_id: str) -> Sequence[Reaction]: ...


class CICertificationRepository(Protocol):
    def put(self, certification: CICertification) -> CICertification: ...

    def get_for_head(self, pull_request_id: str, head_sha: str) -> CICertification | None: ...

    def list_for_task(self, task_id: str) -> Sequence[CICertification]: ...


class CIDecisionRepository(Protocol):
    def add(self, decision: CIDecision) -> None: ...

    def list_for_task(self, task_id: str) -> Sequence[CIDecision]: ...


class GitHubDeliveryRepository(Protocol):
    def add(self, delivery: GitHubDelivery) -> bool:
        """True when stored; False when this delivery id was already seen (04)."""
        ...

    def get(self, delivery_id: str) -> GitHubDelivery | None: ...

    def list_unprocessed(self, limit: int = 100) -> Sequence[GitHubDelivery]: ...

    def count_unprocessed(self) -> int: ...

    def mark_processed(self, delivery_id: str, at: datetime) -> None: ...


class WakeRepository(Protocol):
    def add(self, wake: Wake) -> None: ...

    def get(self, wake_id: str, *, for_update: bool = False) -> Wake | None: ...

    def save(self, wake: Wake) -> None: ...

    def list_for_principal(
        self, principal_id: str, *, since: datetime | None, include_acked: bool, limit: int
    ) -> Sequence[Wake]: ...

    def list_undelivered(self, now: datetime) -> Sequence[Wake]: ...

    def count_unacked(self) -> int: ...

    def list_acked_before(self, cutoff: datetime, limit: int) -> Sequence[Wake]:
        """Wakes acked before the cutoff, oldest first. The caller applies each one's
        own policy window and records a RetentionAction per deletion (16)."""
        ...

    def delete(self, wake_id: str) -> bool: ...


class AttemptMetricsRepository(Protocol):
    def put(self, metrics: AttemptMetrics) -> None: ...

    def get(self, attempt_id: str) -> AttemptMetrics | None: ...

    def list_since(
        self, *, since: datetime | None, model: str | None, task_ids: Sequence[str] | None
    ) -> Sequence[AttemptMetrics]: ...

    def recent_for_project(
        self, *, project: str, models: Sequence[str], limit_per_model: int
    ) -> Sequence[AttemptMetrics]:
        """The newest metrics per model for one project, without paging through tasks."""
        ...


class SupervisorStatusRepository(Protocol):
    def get(self) -> SupervisorStatus: ...

    def write(self, status: SupervisorStatus) -> None: ...


class HarnessStateRepository(Protocol):
    """The runtime record per harness (25): enable flag, compatibility, observations."""

    def get(self, name: str) -> HarnessState | None: ...

    def list_all(self) -> Sequence[HarnessState]: ...

    def put(self, state: HarnessState) -> HarnessState: ...


class ImagePromotionRepository(Protocol):
    def get(self, digest: str) -> ImagePromotion | None: ...

    def list_all(self) -> Sequence[ImagePromotion]: ...

    def put(self, promotion: ImagePromotion) -> ImagePromotion: ...


class BootstrapImportRepository(Protocol):
    """The imports of 15, newest first when listed."""

    def add(self, record: BootstrapImport) -> None: ...

    def get(self, import_id: str, *, for_update: bool = False) -> BootstrapImport | None: ...

    def get_by_content(self, content_sha256: str) -> BootstrapImport | None: ...

    def save(self, record: BootstrapImport) -> None: ...

    def list_all(self) -> Sequence[BootstrapImport]: ...

    def authoritative(self, *, for_update: bool = False) -> BootstrapImport | None: ...


class IdempotencyKeyTakenError(Exception):
    """The (principal, key) row already exists; read it back in a fresh transaction."""


class IdempotencyRepository(Protocol):
    def get(
        self, principal_id: str, key: str
    ) -> tuple[str, int | None, dict[str, Any] | None] | None:
        """(request_sha256, response_status, response_body); status is None while reserved."""
        ...

    def reserve(self, principal_id: str, key: str, *, request_sha256: str, now: datetime) -> None:
        """Insert the key row inside the current transaction; raises IdempotencyKeyTakenError
        once the conflicting row's transaction has committed."""
        ...

    def complete(
        self, principal_id: str, key: str, *, status: int, body: dict[str, Any]
    ) -> None: ...


class UnitOfWork(Protocol):
    principals: PrincipalRepository
    repositories: RepositoryRegistry
    policies: PolicyRepository
    tasks: TaskRepository
    contracts: ContractRepository
    executions: ExecutionRepository
    attempts: AttemptRepository
    events: EventRepository
    leases: LeaseRepository
    claims: ClaimRepository
    logs: LogRepository
    heartbeats: HeartbeatRepository
    retention: RetentionRepository
    supervisor_status: SupervisorStatusRepository
    idempotency: IdempotencyRepository
    routing_policies: RoutingPolicyRepository
    pool_exhaustions: PoolExhaustionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceRepository
    review_reports: ReviewReportRepository
    gate_results: GateResultRepository
    acceptance: AcceptanceRepository
    decisions: DecisionRepository
    escalations: EscalationRepository
    dispositions: DispositionRepository
    wakes: WakeRepository
    attempt_metrics: AttemptMetricsRepository
    pull_requests: PullRequestRepository
    pull_request_heads: PullRequestHeadRepository
    review_cycles: ExternalReviewCycleRepository
    external_reviews: ExternalReviewRepository
    review_comments: ReviewCommentRepository
    reactions: ReactionRepository
    ci_certifications: CICertificationRepository
    ci_decisions: CIDecisionRepository
    github_deliveries: GitHubDeliveryRepository
    harnesses: HarnessStateRepository
    image_promotions: ImagePromotionRepository
    bootstrap_imports: BootstrapImportRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def set_fenced_token(self, fenced_token: int) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class FencedTokenRejectedError(Exception):
    """The database refused a supervisor write because the fenced token is stale (10)."""


class AppendOnlyViolationError(Exception):
    """An UPDATE or DELETE hit an append-only table (14)."""
