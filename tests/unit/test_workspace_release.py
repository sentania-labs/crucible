"""When a kept workspace may go (16, the lab findings of 2026-09-29): once its task is
terminal or its work was published, or once the policy's window has passed since
cleanup, whichever is first, and never while an attempt, a publication or a
correction may still read it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from crucible.application.supervisor import workspace_release_reason
from crucible.domain.entities import Attempt, Event, Execution, ExecutionRole, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
DAYS = 14


def _task(state: TaskState) -> Task:
    return Task(
        id="T1",
        external_id="EX-1",
        principal_id="P",
        project="p",
        title="t",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=2,
        repository_id="R",
        created_at=NOW,
        updated_at=NOW,
    )


def _execution(execution_id: str, role: ExecutionRole = ExecutionRole.IMPLEMENT) -> Execution:
    return Execution(
        id=execution_id,
        task_id="T1",
        role=role,
        contract_version=1,
        harness="codex",
        model="m",
        effort=None,
        provider="kubernetes",
        image="i",
        policy_snapshot={},
        state=ExecutionState.SUCCEEDED,
        max_attempts=3,
        retry_on=[],
        timeout_seconds=3600,
        created_at=NOW,
    )


def _attempt(attempt_id: str, execution_id: str, cleaned_days_ago: float) -> Attempt:
    return Attempt(
        id=attempt_id,
        execution_id=execution_id,
        task_id="T1",
        number=1,
        state=AttemptState.SUCCEEDED,
        created_at=NOW,
        cleaned_up_at=NOW - timedelta(days=cleaned_days_ago),
    )


@dataclass
class _Events:
    items: list[Event] = field(default_factory=list)

    def latest_for_task_kind(self, task_id: str, kind: str) -> Event | None:
        found = [e for e in self.items if e.task_id == task_id and e.kind == kind]
        return found[-1] if found else None

    def list_for_task(self, task_id: str, *, after_seq: int, limit: int) -> list[Event]:
        return [e for e in self.items if e.task_id == task_id][after_seq : after_seq + limit]


@dataclass
class _Rows:
    rows: list[Any]

    def list_for_task(self, task_id: str) -> list[Any]:
        return list(self.rows)

    def list_for_execution(self, execution_id: str) -> list[Any]:
        return [row for row in self.rows if row.execution_id == execution_id]


@dataclass
class _Uow:
    executions: _Rows
    attempts: _Rows
    events: _Events


def _uow(attempts: list[Attempt], events: list[Event] | None = None) -> _Uow:
    executions = {a.execution_id for a in attempts}
    return _Uow(
        executions=_Rows(
            [
                _execution(
                    e, ExecutionRole.REVIEW if e.startswith("R") else ExecutionRole.IMPLEMENT
                )
                for e in sorted(executions)
            ]
        ),
        attempts=_Rows(attempts),
        events=_Events(events or []),
    )


def _event(kind: EventKind, attempt_id: str, **payload: Any) -> Event:
    return Event(
        seq=None,
        ts=NOW,
        kind=kind.value,
        principal="crucible",
        verified=True,
        payload=payload,
        task_id="T1",
        attempt_id=attempt_id,
    )


def _reason(state: TaskState, attempts: list[Attempt], target: Attempt, **kw: Any) -> str | None:
    uow = _uow(attempts, kw.get("events"))
    return workspace_release_reason(uow, _task(state), target, NOW, DAYS)  # type: ignore[arg-type]


@pytest.mark.parametrize("state", [TaskState.CLOSED, TaskState.REJECTED, TaskState.CANCELLED])
def test_a_terminal_tasks_workspaces_all_go_at_once(state: TaskState) -> None:
    latest = _attempt("A2", "E1", 0)
    assert _reason(state, [_attempt("A1", "E1", 0), latest], latest) == f"task_{state.value}"


@pytest.mark.parametrize(
    "state",
    [
        TaskState.REPORTED,
        TaskState.AWAITING_INTERNAL_REVIEW,
        TaskState.AWAITING_ACCEPTANCE,
        TaskState.PUBLISHING,
        TaskState.PUBLISH_FAILED,
        TaskState.BLOCKED,
    ],
)
def test_the_latest_work_attempt_stays_until_it_is_published_even_past_the_window(
    state: TaskState,
) -> None:
    """Acceptance, the publisher and a republish all read its bundle."""
    latest = _attempt("A2", "E1", 30)
    assert _reason(state, [_attempt("A1", "E1", 30), latest], latest) is None


def test_an_earlier_attempt_goes_when_the_window_has_passed() -> None:
    earlier = _attempt("A1", "E1", DAYS + 1)
    latest = _attempt("A2", "E1", DAYS + 1)
    assert _reason(TaskState.AWAITING_ACCEPTANCE, [earlier, latest], earlier) == (
        "retention_window"
    )


def test_an_earlier_attempt_inside_the_window_stays() -> None:
    earlier = _attempt("A1", "E1", DAYS - 1)
    latest = _attempt("A2", "E1", 0)
    assert _reason(TaskState.AWAITING_ACCEPTANCE, [earlier, latest], earlier) is None


def test_once_published_the_published_attempt_and_its_predecessors_go() -> None:
    earlier = _attempt("A1", "E1", 0)
    latest = _attempt("A2", "E1", 0)
    events = [_event(EventKind.PUBLISH_COMPLETED, "A2")]
    for target in (earlier, latest):
        assert (
            _reason(TaskState.AWAITING_EXTERNAL_REVIEW, [earlier, latest], target, events=events)
            == "published"
        )


def test_a_correction_after_publication_keeps_its_own_workspace() -> None:
    """The correction's bundle is the next one published; the published one is not."""
    published = _attempt("A1", "E1", 0)
    correction = _attempt("A2", "E2", 0)
    events = [_event(EventKind.PUBLISH_COMPLETED, "A1")]
    attempts = [published, correction]
    assert (
        _reason(TaskState.EXTERNAL_FEEDBACK_RECEIVED, attempts, correction, events=events) is None
    )
    assert (
        _reason(TaskState.EXTERNAL_FEEDBACK_RECEIVED, attempts, published, events=events)
        == "published"
    )


def test_a_quota_checkpoint_that_never_reached_the_remote_is_kept_while_the_task_is_open() -> None:
    stranded = _attempt("A1", "E1", DAYS + 5)
    latest = _attempt("A2", "E1", 0)
    events = [
        _event(EventKind.TASK_PUBLISH_FAILED, "A1", step="quota_checkpoint"),
        _event(EventKind.PUBLISH_COMPLETED, "A2"),
    ]
    attempts = [stranded, latest]
    assert _reason(TaskState.AWAITING_EXTERNAL_REVIEW, attempts, stranded, events=events) is None
    assert _reason(TaskState.CLOSED, attempts, stranded, events=events) == "task_closed"


def test_a_review_attempt_is_never_the_work_a_publication_needs() -> None:
    work = _attempt("A1", "E1", 0)
    review = _attempt("A2", "R1", DAYS + 1)
    assert _reason(TaskState.AWAITING_ACCEPTANCE, [work, review], review) == "retention_window"
    assert _reason(TaskState.AWAITING_ACCEPTANCE, [work, review], work) is None


def test_an_attempt_not_yet_cleaned_is_never_released() -> None:
    attempt = _attempt("A1", "E1", 0)
    attempt.cleaned_up_at = None
    assert _reason(TaskState.CLOSED, [attempt], attempt) is None


def test_a_task_that_is_gone_releases_its_workspaces() -> None:
    attempt = _attempt("A1", "E1", 0)
    uow = _uow([attempt])
    assert workspace_release_reason(uow, None, attempt, NOW, DAYS) == "task_gone"  # type: ignore[arg-type]
