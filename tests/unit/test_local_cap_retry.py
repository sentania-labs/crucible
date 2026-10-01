from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Task
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock


def _finish_at_cap(*, local: bool, turns: bool) -> tuple[Task, Execution, Attempt, MagicMock]:
    clock = FakeClock()
    task = Task(
        id="task",
        external_id="EX-0001",
        principal_id="foundry",
        project="p",
        title="cap",
        state=TaskState.RUNNING,
        contract_version=1,
        policy_name="policy",
        policy_version=1,
        repository_id="repo",
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    execution = Execution(
        id="execution",
        task_id=task.id,
        role=ExecutionRole.IMPLEMENT,
        contract_version=1,
        harness="hermes",
        model="model",
        effort=None,
        provider="fake",
        image="fake-hang",
        policy_snapshot={},
        state=ExecutionState.ACTIVE,
        max_attempts=3,
        retry_on=["timeout"],
        timeout_seconds=60,
        created_at=clock.now(),
    )
    attempt = Attempt(
        id="attempt",
        execution_id=execution.id,
        task_id=task.id,
        number=1,
        state=AttemptState.COLLECTED,
        created_at=clock.now(),
        exit_class=ExitClass.COMPLETED_WITHOUT_REPORT if turns else ExitClass.TIMEOUT,
    )
    uow = MagicMock()
    uow.tasks.get.return_value = task
    uow.executions.get.return_value = execution
    uow.attempts.list_for_execution.return_value = [attempt]
    uow.leases.list_checkout_leases.return_value = []
    supervisor = Supervisor(
        MagicMock(),
        {"fake": FakeProvider()},
        clock,
        holder="test",
        artifact_store=MagicMock(),
    )
    routing = MagicMock()
    routing.model.return_value = SimpleNamespace(endpoint="local" if local else "subscription")
    with patch("crucible.application.supervisor.load_routing", return_value=routing):
        supervisor._classify_and_finish(uow, attempt, None, turn_cap_reached=turns)
    return task, execution, attempt, uow


def test_a_local_turn_cap_blocks_instead_of_retrying() -> None:
    task, execution, attempt, uow = _finish_at_cap(local=True, turns=True)
    assert task.state is TaskState.BLOCKED
    assert execution.state is ExecutionState.ACTIVE
    assert attempt.state is AttemptState.FAILED
    uow.attempts.add.assert_not_called()
    assert uow.escalations.add.call_args.args[0].question == "too_big_for_local:turns"


def test_a_local_time_cap_blocks_instead_of_retrying() -> None:
    task, execution, attempt, uow = _finish_at_cap(local=True, turns=False)
    assert task.state is TaskState.BLOCKED
    assert execution.state is ExecutionState.ACTIVE
    assert attempt.state is AttemptState.FAILED
    uow.attempts.add.assert_not_called()
    assert uow.escalations.add.call_args.args[0].question == "too_big_for_local:time"


def test_a_frontier_time_cap_still_retries() -> None:
    task, _, attempt, uow = _finish_at_cap(local=False, turns=False)
    assert task.state is TaskState.SCHEDULED
    uow.attempts.add.assert_called_once()
    retry = uow.attempts.add.call_args.args[0]
    assert retry.number == attempt.number + 1
    assert retry.state is AttemptState.PENDING
    uow.wakes.add.assert_not_called()


@pytest.mark.parametrize(("turns", "cap"), [(True, "turn"), (False, "time")])
def test_the_too_big_wake_names_the_cap(turns: bool, cap: str) -> None:
    task, _, attempt, uow = _finish_at_cap(local=True, turns=turns)
    uow.wakes.add.assert_called_once()
    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "blocked"
    assert wake.task_id == task.id
    assert wake.payload["attempt_id"] == attempt.id
    assert wake.payload["summary"] == f"split the task: the local attempt hit its {cap} cap"
