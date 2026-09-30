"""A local route requires executable verification specific to the task."""

from contextlib import nullcontext
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.application.queries import task_view
from crucible.application.routing import Selection, select_model
from crucible.application.supervisor import Supervisor, _Pending
from crucible.domain.entities import Attempt, Execution, ExecutionRole, Task
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from tests.fixtures import FakeClock
from tests.unit.test_class_routing import NOW, _model, _routing, _uow


def _select(*, specific: bool = False, frontier: bool = True, pinned: bool = False) -> Selection:
    models = [
        {
            **_model("hermes", harness="hermes", pool="lab-local"),
            "endpoint": "local",
            "endpoint_url": "http://spark:4000/v1",
        },
        {
            **_model("codex-local", pool="lab-local"),
            "endpoint": "local",
            "endpoint_url": "http://spark:4000/v1",
        },
        _model("pool-peer", pool="lab-local"),
    ]
    if frontier:
        models.append(_model("frontier", capability="frontier"))
    routing = _routing(models)
    routing.tiers["standard"].allowed_capability.append("frontier")
    commands = ["make lint", "make test", "make scan"]
    if specific:
        commands.append("python3 -m unittest tests.test_x")
    return select_model(
        _uow(),
        routing,
        tier="standard",
        project="p",
        provider="fake",
        now=NOW,
        contract={"required_verification": [{"command": c} for c in commands]},
        policy_document={"repository": {"required_checks": commands[:3]}},
        pinned_model="codex-local" if pinned else None,
    )


def test_local_candidates_need_a_task_specific_check() -> None:
    result = _select()
    assert result.selected is not None and result.selected.id == "frontier"
    local = [c for c in result.candidates if c["pool"] == "lab-local"]
    assert len(local) == 3
    assert all(not c["eligible"] and c["excluded"] == ["no task-specific check"] for c in local)


def test_a_task_specific_check_allows_the_local_route() -> None:
    result = _select(specific=True)
    assert result.selected is not None and result.selected.id == "codex-local"
    assert all(c["eligible"] for c in result.candidates)


def test_operator_pin_cannot_bypass_task_specific_check() -> None:
    assert _select(pinned=True).selected is None


@pytest.mark.parametrize("quota", [False, True])
def test_no_candidates_blocks_with_the_reason(quota: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _select(frontier=False)
    assert selection.selected is None
    if quota:
        for candidate in selection.candidates:
            candidate["excluded"].append("pool is at its soft limit")
    assert not Supervisor._selection_is_quota_blocked(selection)
    task = Task(
        "task",
        "FDY-0156",
        "foundry",
        "p",
        "Check routing",
        TaskState.SCHEDULED,
        1,
        "policy",
        1,
        "repo",
        NOW,
        NOW,
    )
    execution = Execution(
        "execution",
        task.id,
        ExecutionRole.IMPLEMENT,
        1,
        "",
        "",
        None,
        "fake",
        "",
        {},
        ExecutionState.CREATED,
        1,
        [],
        60,
        NOW,
    )
    attempt = Attempt("attempt", execution.id, task.id, 1, AttemptState.PENDING, NOW)
    uow: Any = MagicMock()
    uow.tasks.get.return_value = task
    uow.executions.get.return_value = execution
    uow.attempts.get.return_value = attempt
    supervisor = object.__new__(Supervisor)
    supervisor._clock = FakeClock()
    monkeypatch.setattr(supervisor, "_fenced", lambda: nullcontext(uow))
    monkeypatch.setattr(supervisor, "_selection_for", lambda *_a, **_kw: selection)
    pending = _Pending(task=task, execution=execution, attempt=attempt, contract={})
    assert supervisor._route_pending(pending) is None
    assert task.state is TaskState.BLOCKED
    assert attempt.state is AttemptState.FAILED
    assert execution.state is ExecutionState.FAILED
    wake = uow.wakes.add.call_args.args[0]
    assert wake.reason == "blocked"
    assert "no task-specific check" in wake.payload["summary"]
    assert uow.escalations.add.call_count == 1
    assert attempt.ordered_candidates == list(selection.candidates)

    # GET /v1/tasks/{id} uses this query and serializes these summaries.
    uow.principals.get.return_value = None
    uow.repositories.get.return_value = None
    uow.contracts.list_for_task.return_value = []
    uow.executions.list_for_task.return_value = [execution]
    uow.attempts.list_for_execution.return_value = [attempt]
    uow.events.list_for_task.return_value = []
    uow.pull_requests.get_for_task.return_value = None
    view = task_view(uow, task.id).model_dump(mode="json")
    candidates = view["executions"][0]["attempts"][0]["ordered_candidates"]
    assert all("no task-specific check" in c["excluded"] for c in candidates)
