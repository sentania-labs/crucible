"""Persistence ports. Repositories return domain entities; the unit of work owns
the transaction. Every state change and its event commit together."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol

from crucible.domain.entities import (
    Attempt,
    CompletionClaimRecord,
    Event,
    Execution,
    Lease,
    Policy,
    Principal,
    Repository,
    SupervisorStatus,
    Task,
    TaskContract,
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


class PolicyRepository(Protocol):
    def get(self, name: str, version: int) -> Policy | None: ...


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
    ) -> Sequence[Attempt]: ...


class EventRepository(Protocol):
    def append(self, event: Event) -> Event: ...

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


class ClaimRepository(Protocol):
    def put(self, record: CompletionClaimRecord) -> None: ...

    def get(self, attempt_id: str) -> CompletionClaimRecord | None: ...


class SupervisorStatusRepository(Protocol):
    def get(self) -> SupervisorStatus: ...

    def write(self, status: SupervisorStatus) -> None: ...


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
    supervisor_status: SupervisorStatusRepository
    idempotency: IdempotencyRepository

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
