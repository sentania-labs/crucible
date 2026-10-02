"""Regression test for FDY-0227: a blocked task accepts a correction and moves to scheduled;
the open escalation is closed with the correction as its answer; the worker identity text
contains the origin limitation note."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from crucible.adapters.execution.identity import render_identity_md
from crucible.application.corrections import CORRECTABLE_STATES, attach_correction
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


def _task(state: TaskState = TaskState.BLOCKED, **overrides: Any) -> Task:
    return Task(
        id="01TASK1234567890ABCDEF01",
        external_id="FDY-0227",
        principal_id="01PRINC1234567890ABCDEF",
        project="example-service",
        title="Fix origin fetch issue",
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
    """Stub for the escalation repository."""

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


def test_blockeds_in_correctable_states() -> None:
    """FDY-0227: TaskState.BLOCKED is in CORRECTABLE_STATES."""
    assert TaskState.BLOCKED in CORRECTABLE_STATES


def test_blocked_to_scheduled_transition_exists_in_lifecycle() -> None:
    """The task lifecycle permits blocked -> scheduled."""
    from crucible.domain.lifecycle import TASK_TRANSITIONS  # noqa: PLC0415

    assert (TaskState.BLOCKED, TaskState.SCHEDULED) in TASK_TRANSITIONS


def test_rendered_identity_contains_origin_limitation_note() -> None:
    """The worker identity text contains the origin limitation note."""
    contract = contract_document()
    text = render_identity_md(
        contract=contract,
        policy={
            "limits": {"timeout_seconds": 3600, "grace_seconds": 30, "stall_fail_seconds": 600},
            "git": {"work_branch_pattern": "crucible/*", "commit_trailer": "Crucible-Attempt"},
            "gates": {},
        },
        external_id="FDY-0227",
        owner="example-org",
        work_branch="crucible/FDY-0227",
        network_mode="policy",
    )
    assert "origin" in text.lower()
    assert "fetch origin" in text.lower() or "cannot be fetched" in text.lower()


def test_a_blocked_correction_closes_the_open_escalation() -> None:
    """FDY-0227: recording a correction on a blocked task closes the open escalation.

    We patch the heavy validation so only the escalation-closing path is exercised.
    """
    task = _task(state=TaskState.BLOCKED)
    esc = Escalation(
        id="ESC001",
        task_id=task.id,
        attempt_id="ATT001",
        state=EscalationState.OPEN,
        question="How to reach origin/main?",
        opened_at=datetime(2026, 9, 1, tzinfo=UTC),
        closed_at=None,
        decision_id=None,
        last_wake_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    contract_doc = contract_document()
    contract_doc["correction"] = {
        "of_version": 1,
        "reason": "pre_pr_gates",
        "addresses": [{"kind": "internal_review", "id": "1", "disposition_id": None}],
        "instructions": "Merge origin/main instead of fetching it.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
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

    with (
        patch(
            "crucible.application.corrections.parse_contract",
            side_effect=lambda x: _contract_v1(contract_doc),
        ),
        patch("crucible.application.corrections.require_task_principal"),
        patch("crucible.application.corrections.require_operator_for_pin"),
        patch("crucible.application.corrections.eligible_harness_names", return_value=set()),
        patch("crucible.application.corrections.validate_against_registry", return_value=[]),
        patch("crucible.application.corrections.unwired_provider_problems", return_value=[]),
        patch("crucible.application.corrections.correction_narrows", return_value=[]),
        patch("crucible.application.corrections._unpublished_bundle_problem", return_value=None),
        patch(
            "crucible.application.corrections.move_task",
            side_effect=lambda *args, **kwargs: setattr(args[2], "state", args[3]),
        ),
    ):
        result = attach_correction(
            uow,
            clock,
            principal=principal,
            task_id=task.id,
            body=contract_doc,
        )

    assert result.state is TaskState.SCHEDULED

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

    # The Decision for the correction should have been persisted
    assert len(added_decisions) == 1
    decision = added_decisions[0]
    assert decision.kind == "correction"
    assert decision.verbatim == "Merge origin/main instead of fetching it."
    assert decision.resolves == "How to reach origin/main?"
    assert decision.escalation_id == "ESC001"
    assert decision.task_id == task.id

    # Both answered and closed escalation transitions share the same decision_id
    assert closed_esc.decision_id == decision.id
