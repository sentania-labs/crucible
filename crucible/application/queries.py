"""Read side: task, execution, attempt, event, and supervisor views."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from crucible.application.errors import NotFoundError
from crucible.contracts.api import (
    AttemptSummary,
    AttemptView,
    ContractVersionView,
    EventList,
    EventView,
    ExecutionSummary,
    ExecutionView,
    SupervisorView,
    TaskList,
    TaskListItem,
    TaskView,
)
from crucible.domain.entities import Attempt, Event, Execution
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import ExecutionProvider
from crucible.ports.repository import UnitOfWork

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        raise NotFoundError("cursor is not valid") from None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _attempt_summary(a: Attempt) -> AttemptSummary:
    return AttemptSummary(
        id=a.id,
        execution_id=a.execution_id,
        number=a.number,
        state=a.state,
        exit_code=a.exit_code,
        exit_class=a.exit_class,
        started_at=a.started_at,
        ended_at=a.ended_at,
        handle=a.handle,
    )


def _execution_summary(e: Execution, attempts: list[Attempt]) -> ExecutionSummary:
    return ExecutionSummary(
        id=e.id,
        role=e.role.value,
        state=e.state,
        harness=e.harness,
        model=e.model,
        provider=e.provider,
        image=e.image,
        contract_version=e.contract_version,
        max_attempts=e.max_attempts,
        created_at=e.created_at,
        ended_at=e.ended_at,
        attempts=[_attempt_summary(a) for a in attempts],
    )


def task_view(uow: UnitOfWork, task_id: str) -> TaskView:
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    principal = uow.principals.get(task.principal_id)
    repository = uow.repositories.get(task.repository_id)
    versions = uow.contracts.list_for_task(task.id)
    current = next((v for v in versions if v.version == task.contract_version), None)
    executions = uow.executions.list_for_task(task.id)
    summaries = [
        _execution_summary(e, list(uow.attempts.list_for_execution(e.id))) for e in executions
    ]
    all_attempts = [a for s in summaries for a in s.attempts]
    latest = max(all_attempts, key=lambda a: a.id) if all_attempts else None
    return TaskView(
        id=task.id,
        external_id=task.external_id,
        title=task.title,
        project=task.project,
        state=task.state,
        principal=principal.name if principal else task.principal_id,
        repository=repository.name if repository else task.repository_id,
        policy={"name": task.policy_name, "version": task.policy_version},
        contract_version=task.contract_version,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        contract_versions=[
            ContractVersionView(version=v.version, sha256=v.sha256, submitted_at=v.submitted_at)
            for v in versions
        ],
        contract=current.document if current else {},
        executions=summaries,
        latest_attempt=latest,
        gate_summary={},
        pull_request=None,
        open_escalations=[],
    )


def task_list(
    uow: UnitOfWork,
    *,
    state: TaskState | None,
    project: str | None,
    repository: str | None,
    external_id: str | None,
    updated_since: datetime | None,
    cursor: str | None,
    limit: int | None,
) -> TaskList:
    size = clamp_limit(limit)
    repository_id: str | None = None
    if repository is not None:
        repo = uow.repositories.get_by_name(repository)
        if repo is None:
            return TaskList(items=[], next_cursor=None)
        repository_id = repo.id
    rows = uow.tasks.search(
        state=state,
        project=project,
        repository_id=repository_id,
        external_id=external_id,
        updated_since=updated_since,
        after_id=decode_cursor(cursor),
        limit=size + 1,
    )
    page = list(rows[:size])
    repo_names: dict[str, str] = {}
    items = []
    for t in page:
        if t.repository_id not in repo_names:
            repo = uow.repositories.get(t.repository_id)
            repo_names[t.repository_id] = repo.name if repo else t.repository_id
        items.append(
            TaskListItem(
                id=t.id,
                external_id=t.external_id,
                title=t.title,
                project=t.project,
                state=t.state,
                repository=repo_names[t.repository_id],
                contract_version=t.contract_version,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
        )
    next_cursor = encode_cursor(page[-1].id) if len(rows) > size and page else None
    return TaskList(items=items, next_cursor=next_cursor)


def _event_view(e: Event) -> EventView:
    assert e.seq is not None
    return EventView(
        seq=e.seq,
        ts=e.ts,
        kind=e.kind,
        task_id=e.task_id,
        execution_id=e.execution_id,
        attempt_id=e.attempt_id,
        principal=e.principal,
        verified=e.verified,
        payload=e.payload,
    )


def _seq_cursor(cursor: str | None) -> int:
    raw = decode_cursor(cursor)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        raise NotFoundError("cursor is not valid") from None


def _page_events(events: list[Event], size: int) -> EventList:
    page = events[:size]
    next_cursor = None
    if len(events) > size and page and page[-1].seq is not None:
        next_cursor = encode_cursor(str(page[-1].seq))
    return EventList(items=[_event_view(e) for e in page], next_cursor=next_cursor)


def task_events(
    uow: UnitOfWork, task_id: str, *, cursor: str | None, limit: int | None
) -> EventList:
    if uow.tasks.get(task_id) is None:
        raise NotFoundError(f"task {task_id} not found")
    size = clamp_limit(limit)
    events = list(uow.events.list_for_task(task_id, after_seq=_seq_cursor(cursor), limit=size + 1))
    return _page_events(events, size)


def global_events(
    uow: UnitOfWork,
    *,
    cursor: str | None,
    kind: str | None,
    since: datetime | None,
    limit: int | None,
) -> EventList:
    size = clamp_limit(limit)
    events = list(
        uow.events.list_global(
            after_seq=_seq_cursor(cursor), kind=kind, since=since, limit=size + 1
        )
    )
    return _page_events(events, size)


def execution_view(uow: UnitOfWork, execution_id: str) -> ExecutionView:
    e = uow.executions.get(execution_id)
    if e is None:
        raise NotFoundError(f"execution {execution_id} not found")
    attempts = list(uow.attempts.list_for_execution(e.id))
    return ExecutionView(
        id=e.id,
        task_id=e.task_id,
        role=e.role.value,
        state=e.state,
        contract_version=e.contract_version,
        harness=e.harness,
        model=e.model,
        effort=e.effort,
        provider=e.provider,
        image=e.image,
        max_attempts=e.max_attempts,
        retry_on=list(e.retry_on),
        timeout_seconds=e.timeout_seconds,
        policy_snapshot=e.policy_snapshot,
        created_at=e.created_at,
        ended_at=e.ended_at,
        attempts=[_attempt_summary(a) for a in attempts],
    )


def attempt_view(uow: UnitOfWork, attempt_id: str) -> AttemptView:
    a = uow.attempts.get(attempt_id)
    if a is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    lease = uow.leases.get_attempt_lease(a.id)
    claim = uow.claims.get(a.id)
    report: dict[str, Any] | None = None
    if claim is not None:
        report = {
            "parsed_ok": claim.parsed_ok,
            "parse_errors": claim.parse_errors,
            "document": claim.document,
        }
    return AttemptView(
        id=a.id,
        execution_id=a.execution_id,
        task_id=a.task_id,
        number=a.number,
        state=a.state,
        handle=a.handle,
        workspace_path=a.workspace_path,
        identity_sha256=a.identity_sha256,
        image_digest=a.image_digest,
        started_at=a.started_at,
        ended_at=a.ended_at,
        exit_code=a.exit_code,
        exit_class=a.exit_class,
        timeout_at=a.timeout_at,
        termination_reason=a.termination_reason,
        lease=(
            {
                "holder": lease.holder,
                "fenced_token": lease.fenced_token,
                "expires_at": lease.expires_at.isoformat(),
            }
            if lease
            else None
        ),
        heartbeat_summary={"signals": 0, "note": "heartbeats are C3"},
        report=report,
    )


def supervisor_health(uow: UnitOfWork, now: datetime, lease_ttl_seconds: int) -> tuple[bool, str]:
    """Supervisor is healthy only when the lease is held and the last tick inside the lease
    window succeeded. A held lease next to failing ticks is not healthy (10, 19)."""
    lease = uow.leases.get_supervisor()
    status = uow.supervisor_status.get()
    if lease is None:
        return False, "no supervisor lease"
    if lease.expires_at <= now:
        return False, f"lease held by {lease.holder} expired {lease.expires_at.isoformat()}"
    window_start = now - timedelta(seconds=lease_ttl_seconds)
    last_ok = status.last_success_at
    if last_ok is None or last_ok < window_start:
        detail = "no successful tick within the lease window"
        if status.last_error is not None:
            detail += f"; last error: {status.last_error}"
        return False, detail
    if status.last_error_at is not None and status.last_error_at > last_ok:
        return False, f"last tick failed: {status.last_error}"
    return True, f"held by {lease.holder}, last successful tick {last_ok.isoformat()}"


def supervisor_view(
    uow: UnitOfWork, providers: list[ExecutionProvider], now: datetime, lease_ttl_seconds: int
) -> SupervisorView:
    lease = uow.leases.get_supervisor()
    status = uow.supervisor_status.get()
    healthy, _ = supervisor_health(uow, now, lease_ttl_seconds)
    return SupervisorView(
        lease=(
            {
                "holder": lease.holder,
                "fenced_token": lease.fenced_token,
                "expires_at": lease.expires_at.isoformat(),
            }
            if lease
            else None
        ),
        last_tick_at=status.last_tick_at,
        last_success_at=status.last_success_at,
        last_error_at=status.last_error_at,
        last_error=status.last_error,
        healthy=healthy,
        tick_ms=status.tick_ms,
        counts=status.counts,
        providers=[{"name": p.name, **p.capabilities().as_dict()} for p in providers],
        github=None,
    )
