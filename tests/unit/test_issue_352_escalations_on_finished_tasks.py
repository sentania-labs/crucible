"""Tests for FDY-0240: cancelling or closing a task closes its open escalations;
reminders skip finished tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.application.acceptance import close_task
from crucible.application.cancel_task import cancel_task
from crucible.application.decisions import repeat_stale_escalation_wakes
from crucible.application.wakes import create_wake
from crucible.contracts.api import CancelRequest, CloseRequest
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Decision,
    Escalation,
    EscalationState,
    Principal,
    Role,
    Task,
    TaskContract,
)
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from tests.fixtures import FakeClock, contract_document


def _contract_v1(doc: dict[str, Any]) -> Any:
    from crucible.contracts.task_contract import TaskContractV1  # noqa: PLC0415

    return TaskContractV1.model_validate(doc)


def _task(state: TaskState = TaskState.SUBMITTED, **overrides: Any) -> Task:
    return Task(
        id="01TASK1234567890ABCDEF01",
        external_id="FDY-0240",
        principal_id="01PRINC1234567890ABCDEF",
        project="example-service",
        title="Test task for FDY-0240",
        state=state,
        contract_version=1,
        policy_name="default-software",
        policy_version=2,
        repository_id="01REPO1234567890ABCDEF",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
        **overrides,
    )


def _principal(name: str = "foundry") -> Principal:
    return Principal(
        id="01PRINC1234567890ABCDEF",
        name=name,
        role=Role.OPERATOR,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _make_stored_contract(task: Task, contract: dict[str, Any]) -> TaskContract:
    return TaskContract(
        id="01CONTRACT1234567890AB",
        task_id=task.id,
        version=task.contract_version,
        document=contract,
        sha256="abc123",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


class _MockEscRepo:
    """Mock for the escalation repository."""

    def __init__(self, escalations: list[Escalation] | None = None) -> None:
        self._escs = list(escalations or [])
        self.saved: list[Escalation] = []

    def add(self, esc: Escalation) -> None:
        self._escs.append(esc)

    def get(self, esc_id: str, for_update: bool = False) -> Escalation | None:
        for e in self._escs:
            if e.id == esc_id:
                return e
        return None

    def save(self, esc: Escalation) -> None:
        self.saved.append(esc)

    def list_for_task(self, task_id: str) -> list[Escalation]:
        return [e for e in self._escs if e.task_id == task_id]

    def list_open(self) -> list[Escalation]:
        return [e for e in self._escs if e.state is EscalationState.OPEN]


def test_cancel_task_closes_open_escalation() -> None:
    """Cancelling a task with an open escalation closes the escalation via a decision."""
    task = _task(state=TaskState.SUBMITTED)
    esc = Escalation(
        id="ESC001",
        task_id=task.id,
        attempt_id="ATT001",
        state=EscalationState.OPEN,
        question="How to reach origin?",
        opened_at=datetime(2026, 9, 1, tzinfo=UTC),
        closed_at=None,
        decision_id=None,
        last_wake_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    contract_doc = contract_document()
    stored = _make_stored_contract(task, contract_doc)

    esc_repo = _MockEscRepo([esc])

    # Track Decision objects added via uow.decisions.add
    added_decisions: list[Decision] = []

    # Build a minimal UoW mock
    uow = MagicMock()
    uow.tasks.get.return_value = task
    uow.tasks.save = MagicMock()
    uow.contracts.get.return_value = stored
    uow.contracts.add = MagicMock()
    uow.escalations.list_for_task = esc_repo.list_for_task
    uow.escalations.get = esc_repo.get
    uow.escalations.save = esc_repo.save
    uow.events = MagicMock()
    uow.events.append = MagicMock(side_effect=lambda evt: evt)
    uow.acceptance = MagicMock()
    uow.acceptance.list_for_task.return_value = []
    uow.retention = MagicMock()
    uow.retention.list_recent.return_value = []
    uow.decisions.add = MagicMock(side_effect=added_decisions.append)

    clock = FakeClock()
    principal = _principal()
    cancel_request = CancelRequest(
        reason="test cancel",
        verbatim="I cancel this task because of testing.",
        decided_by="test",
    )

    result = cancel_task(
        uow,
        clock,
        principal=principal,
        task_id=task.id,
        request=cancel_request,
    )

    assert result.state is TaskState.CANCELLED

    # The escalation should have been saved (ANSWERED + CLOSED)
    assert len(esc_repo.saved) >= 2
    closed_esc = esc_repo.saved[-1]
    assert closed_esc.state is EscalationState.CLOSED
    assert closed_esc.closed_at is not None

    # Events for answered and closed should have been recorded
    call_args_list = uow.events.append.call_args_list
    kinds: list[str] = []
    for args_tuple, _kwargs_dict in call_args_list:
        if args_tuple and len(args_tuple) > 0 and hasattr(args_tuple[0], "kind"):
            kinds.append(args_tuple[0].kind)
    assert EventKind.ESCALATION_ANSWERED.value in kinds
    assert EventKind.ESCALATION_CLOSED.value in kinds

    # The Decision for the cancellation should have been persisted
    assert len(added_decisions) == 1
    decision = added_decisions[0]
    assert decision.kind == "task_cancelled"
    assert decision.verbatim == "I cancel this task because of testing."
    assert decision.resolves == "How to reach origin?"
    assert decision.escalation_id == "ESC001"
    assert decision.task_id == task.id

    # Both answered and closed escalation transitions share the same decision_id
    assert closed_esc.decision_id == decision.id


def test_close_task_closes_open_escalation() -> None:
    """Closing a task with an open escalation closes the escalation via a decision."""
    # We need to start from a state that can transition to CLOSED, e.g., ACCEPTED
    task = _task(state=TaskState.ACCEPTED)
    esc = Escalation(
        id="ESC002",
        task_id=task.id,
        attempt_id="ATT002",
        state=EscalationState.OPEN,
        question="What is the answer to life?",
        opened_at=datetime(2026, 9, 1, tzinfo=UTC),
        closed_at=None,
        decision_id=None,
        last_wake_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    contract_doc = contract_document()
    stored = _make_stored_contract(task, contract_doc)

    esc_repo = _MockEscRepo([esc])

    # Track Decision objects added via uow.decisions.add
    added_decisions: list[Decision] = []

    # Build a minimal UoW mock
    uow = MagicMock()
    uow.tasks.get.return_value = task
    uow.tasks.save = MagicMock()
    uow.contracts.get.return_value = stored
    uow.contracts.add = MagicMock()
    uow.escalations.list_for_task = esc_repo.list_for_task
    uow.escalations.get = esc_repo.get
    uow.escalations.save = esc_repo.save
    uow.events = MagicMock()
    uow.events.append = MagicMock(side_effect=lambda evt: evt)
    uow.acceptance = MagicMock()
    uow.acceptance.list_for_task.return_value = []
    uow.retention = MagicMock()
    uow.retention.list_recent.return_value = []
    uow.decisions.add = MagicMock(side_effect=added_decisions.append)

    clock = FakeClock()
    principal = _principal()
    close_request = CloseRequest(note="Task is done for testing.")

    result = close_task(
        uow,
        clock,
        principal=principal,
        task_id=task.id,
        note=close_request.note,
    )

    assert result.state is TaskState.CLOSED

    # The escalation should have been saved (ANSWERED + CLOSED)
    assert len(esc_repo.saved) >= 2
    closed_esc = esc_repo.saved[-1]
    assert closed_esc.state is EscalationState.CLOSED
    assert closed_esc.closed_at is not None

    # Events for answered and closed should have been recorded
    call_args_list = uow.events.append.call_args_list
    kinds: list[str] = []
    for args_tuple, _kwargs_dict in call_args_list:
        if args_tuple and len(args_tuple) > 0 and hasattr(args_tuple[0], "kind"):
            kinds.append(args_tuple[0].kind)
    assert EventKind.ESCALATION_ANSWERED.value in kinds
    assert EventKind.ESCALATION_CLOSED.value in kinds

    # The Decision for the close should have been persisted
    assert len(added_decisions) == 1
    decision = added_decisions[0]
    assert decision.kind == "task_closed"
    assert decision.verbatim == "Task is done for testing."
    assert decision.resolves == "What is the answer to life?"
    assert decision.escalation_id == "ESC002"
    assert decision.task_id == task.id

    # Both answered and closed escalation transitions share the same decision_id
    assert closed_esc.decision_id == decision.id


def test_repeat_stale_escalation_wakes_skips_terminal_tasks() -> None:
    """Repeat stale escalation wakes should not produce a wake for
    escalations on cancelled or closed tasks."""
    now = datetime(2026, 9, 1, tzinfo=UTC)
    clock = FakeClock(now)

    # Create an escalation that is stale (older than stale_hours)
    stale_hours = 24
    esc = Escalation(
        id="ESC003",
        task_id="task1",
        attempt_id="ATT003",
        state=EscalationState.OPEN,
        question="Stale escalation",
        opened_at=now - timedelta(hours=stale_hours + 1),
        closed_at=None,
        decision_id=None,
        last_wake_at=now - timedelta(hours=stale_hours + 1),  # ensure it's stale
    )

    # Create a task in a terminal state (cancelled)
    task_cancelled = _task(state=TaskState.CANCELLED)
    task_cancelled.id = "task1"

    # Build a minimal UoW mock
    uow = MagicMock()
    uow.tasks.get.return_value = task_cancelled
    uow.escalations.list_open.return_value = [esc]
    uow.escalations.save = MagicMock()

    # Track wakes created
    created_wakes: list[Any] = []

    # Patch the create_wake in the decisions module
    with pytest.MonkeyPatch().context() as mp:
        def fake_create_wake(*args, **kwargs):
            wake = create_wake(*args, **kwargs)
            created_wakes.append(wake)
            return wake
        mp.setattr("crucible.application.decisions.create_wake", fake_create_wake)
        repeated = repeat_stale_escalation_wakes(uow, clock, stale_hours=stale_hours)

    assert repeated == 0
    assert len(created_wakes) == 0

    # Now test with a closed task
    task_closed = _task(state=TaskState.CLOSED)
    task_closed.id = "task1"
    uow.tasks.get.return_value = task_closed

    with pytest.MonkeyPatch().context() as mp:
        def fake_create_wake(*args, **kwargs):
            wake = create_wake(*args, **kwargs)
            created_wakes.append(wake)
            return wake
        mp.setattr("crucible.application.decisions.create_wake", fake_create_wake)
        repeated = repeat_stale_escalation_wakes(uow, clock, stale_hours=stale_hours)

    assert repeated == 0
    assert len(created_wakes) == 0

    # Now test with a rejected task
    task_rejected = _task(state=TaskState.REJECTED)
    task_rejected.id = "task1"
    uow.tasks.get.return_value = task_rejected

    with pytest.MonkeyPatch().context() as mp:
        def fake_create_wake(*args, **kwargs):
            wake = create_wake(*args, **kwargs)
            created_wakes.append(wake)
            return wake
        mp.setattr("crucible.application.decisions.create_wake", fake_create_wake)
        repeated = repeat_stale_escalation_wakes(uow, clock, stale_hours=stale_hours)

    assert repeated == 0

def test_repeat_stale_escalation_wakes_still_wakes_for_blocked_task() -> None:

def test_repeat_stale_escalation_wakes_still_wakes_for_blocked_task() -> None:
    """Repeat stale escalation wakes should still produce a wake for
    an escalation on a blocked (live) task."""
    now = datetime(2026, 9, 1, tzinfo=UTC)
    clock = FakeClock(now)

    # Create an escalation that is stale (older than stale_hours)
    stale_hours = 24
    esc = Escalation(
        id="ESC004",
        task_id="task1",
        attempt_id="ATT004",
        state=EscalationState.OPEN,
        question="Stale escalation on blocked task",
        opened_at=now - timedelta(hours=stale_hours + 1),
        closed_at=None,
        decision_id=None,
        last_wake_at=now - timedelta(hours=stale_hours + 1),  # ensure it's stale
    )

    # Create a task in a non-terminal state (blocked)
    task_blocked = _task(state=TaskState.BLOCKED)
    task_blocked.id = "task1"

    # Build a minimal UoW mock
    uow = MagicMock()
    uow.tasks.get.return_value = task_blocked
    uow.escalations.list_open.return_value = [esc]
    uow.escalations.save = MagicMock()

    # Track wakes created
    created_wakes: list[Any] = []

    # Patch the create_wake in the decisions module
    def fake_create_wake(*args, **kwargs):
        wake = create_wake(*args, **kwargs)
        created_wakes.append(wake)
        return wake
    mp.setattr("crucible.application.decisions.create_wake", fake_create_wake)
        mp.setattr("crucible.application.decisions.create_wake", create_wake)
        repeated = repeat_stale_escalation_wakes(uow, clock, stale_hours=stale_hours)

    assert repeated == 1
    assert len(created_wakes) == 1
    wake = created_wakes[0]
    assert wake.reason == WakeReason.ESCALATION_STALE.value
    # We can check the summary
    assert f"escalation {esc.id} has been open since" in wake.payload["summary"]
    assert wake.task_id == task_blocked.id
    assert wake.principal_id == task_blocked.principal_id
