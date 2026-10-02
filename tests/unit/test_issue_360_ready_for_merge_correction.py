"""Regression coverage for issue 360 ready-for-merge corrections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from crucible.application.corrections import CORRECTABLE_STATES, attach_correction
from crucible.application.errors import ContractValidationError
from crucible.contracts.task_contract import TaskContractV1, correction_narrows
from crucible.domain.entities import Principal, Role, Task
from crucible.domain.external_review import Cycle, CycleState, completed_rounds, required_rounds
from crucible.domain.lifecycle import TASK_TRANSITIONS, TaskState
from tests.fixtures import FakeClock, contract_document


class _TaskRows:
    def __init__(self, task: Task) -> None:
        self.task = task

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        del for_update
        return self.task if task_id == self.task.id else None


class _CorrectionStore:
    """The concrete store needed before stale-version validation returns."""

    def __init__(self, task: Task) -> None:
        self.tasks = _TaskRows(task)


def _task() -> Task:
    now = datetime(2026, 10, 2, tzinfo=UTC)
    return Task(
        id="01TASK1234567890ABCDEF01",
        external_id="EX-0001",
        principal_id="01PRINC1234567890ABCDEF",
        project="example-service",
        title="Correct the certified pull request",
        state=TaskState.READY_FOR_MERGE,
        contract_version=2,
        policy_name="default-software",
        policy_version=2,
        repository_id="01REPO1234567890ABCDEF",
        created_at=now,
        updated_at=now,
        head_sha="a" * 40,
    )


def _correction(*, of_version: int = 2) -> dict[str, Any]:
    document = contract_document()
    document["correction"] = {
        "of_version": of_version,
        "reason": "external_review",
        "addresses": [{"kind": "review_comment", "id": "360", "disposition_id": None}],
        "instructions": "Correct the defect Foundry found in the full diff.",
        "resume_from": "remote_branch",
        "request_internal_review": False,
    }
    return document


def test_ready_for_merge_accepts_a_correction_and_schedules_remote_branch_work() -> None:
    assert TaskState.READY_FOR_MERGE in CORRECTABLE_STATES
    assert (TaskState.READY_FOR_MERGE, TaskState.SCHEDULED) in TASK_TRANSITIONS
    assert TaskState.SCHEDULED not in CORRECTABLE_STATES
    assert (TaskState.READY_FOR_MERGE, TaskState.MERGED) in TASK_TRANSITIONS
    correction = TaskContractV1.model_validate(_correction()).correction
    assert correction is not None
    assert correction.resume_from == "remote_branch"


def test_the_completed_external_round_stays_counted_for_the_corrected_head() -> None:
    cycle = Cycle(
        id="cycle-1",
        head_sha="a" * 40,
        components=("review",),
        opened_at=datetime(2026, 10, 2, tzinfo=UTC),
        state=CycleState.COMPLETED,
        completed_components={"review": "github-review-1"},
    )

    assert completed_rounds([cycle]) == 1
    assert completed_rounds([cycle]) >= required_rounds({"external_review": {"required_rounds": 1}})
    assert cycle.head_sha != "b" * 40


def test_a_stale_of_version_is_refused() -> None:
    task = _task()
    principal = Principal(
        id=task.principal_id,
        name="foundry",
        role=Role.ORCHESTRATOR,
        created_at=task.created_at,
    )

    with pytest.raises(ContractValidationError, match="version the task is on"):
        attach_correction(
            _CorrectionStore(task),  # type: ignore[arg-type]
            FakeClock(),
            principal=principal,
            task_id=task.id,
            body=_correction(of_version=1),
        )


def test_a_correction_may_not_shrink_required_verification() -> None:
    previous = TaskContractV1.model_validate(contract_document())
    document = _correction(of_version=1)
    document["required_verification"] = document["required_verification"][:-1]
    correction = TaskContractV1.model_validate(document)

    problems = correction_narrows(previous, correction)

    assert {
        "path": "required_verification",
        "message": "required_verification may not shrink; missing: ['V4']",
    } in problems
