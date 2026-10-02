"""Local launches share a pool slot and retain the routing decision that owns it."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.harnesses import CREDENTIAL_HOLDING_STATES
from crucible.domain.entities import ExecutionRole, PoolExhaustion
from crucible.domain.lifecycle import AttemptState, TaskState
from tests.unit.test_class_routing import NOW, _model, _routing
from tests.unit.test_routing import _routing_setup


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    route = _routing(
        [
            {
                **_model("codex-local", pool="lab-local"),
                "endpoint": "local",
                "endpoint_url": "http://gateway.test/v1",
            },
            {
                **_model("hermes-local", harness="hermes", pool="lab-local"),
                "endpoint": "local",
                "endpoint_url": "http://gateway.test/v1",
            },
            _model("frontier", capability="frontier", pool="subscription"),
        ]
    )
    route.version = 26
    route.pools["lab-local"].max_concurrency = 2
    route.tiers["complex"] = route.tiers["standard"].model_copy(
        update={"allowed_capability": ["frontier"], "prefer": ["frontier"]}
    )
    item.execution.policy_snapshot["routing"]["policy"]["version"] = route.version
    uow.routing_policies.get.return_value.document = route.model_dump(mode="json")
    uow.attempts.list_in_states.return_value = []
    item.contract["required_verification"].append(
        {"id": "V5", "command": "uv run pytest tests/unit/test_issue_359_local_pool_cap.py"}
    )
    supervisor._harnesses = default_registry()
    supervisor._credential_sources = {}
    return supervisor, item, uow


@pytest.mark.parametrize("harness", ["codex", "hermes"])
@pytest.mark.parametrize("review", [False, True])
async def test_local_attempt_records_pool_and_routing_version(
    monkeypatch: pytest.MonkeyPatch, harness: str, review: bool
) -> None:
    supervisor, item, uow = _setup(monkeypatch)
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: {harness})
    if review:
        item.execution.role = ExecutionRole.REVIEW
        item.execution.harness = harness
        item.execution.model = f"{harness}-local"
        item.execution.image = "review-image"
        item.task.state = TaskState.AWAITING_INTERNAL_REVIEW
    assert await supervisor._begin_launch(item) is not None
    saved = uow.attempts.save.call_args.args[0]
    assert saved.selected_pool == "lab-local"
    assert saved.routing_version == 26
    assert saved.selected_harness == harness
    assert saved.selected_model == f"{harness}-local"
    assert saved.state is AttemptState.PREPARING


@pytest.mark.parametrize("holding_state", CREDENTIAL_HOLDING_STATES)
@pytest.mark.parametrize("review", [False, True])
async def test_third_local_launch_waits_until_a_pool_slot_is_released(
    monkeypatch: pytest.MonkeyPatch, holding_state: AttemptState, review: bool
) -> None:
    supervisor, first, uow = _setup(monkeypatch)
    items = [
        replace(
            first,
            attempt=replace(
                first.attempt, id=f"attempt-{i}", execution_id=f"execution-{i}", task_id=f"task-{i}"
            ),
            execution=replace(first.execution, id=f"execution-{i}", task_id=f"task-{i}"),
            task=replace(first.task, id=f"task-{i}"),
        )
        for i in range(3)
    ]
    if review:
        for item, harness in zip(items, ["codex", "hermes", "codex"], strict=True):
            item.execution.role = ExecutionRole.REVIEW
            item.execution.harness = harness
            item.execution.model = f"{harness}-local"
            item.execution.image = "review-image"
            item.task.state = TaskState.AWAITING_INTERNAL_REVIEW
    uow.tasks.get.side_effect = lambda task_id, **_: next(
        item.task for item in items if item.task.id == task_id
    )
    uow.executions.get.side_effect = lambda execution_id, **_: next(
        item.execution for item in items if item.execution.id == execution_id
    )
    uow.attempts.get.side_effect = lambda attempt_id, **_: next(
        item.attempt for item in items if item.attempt.id == attempt_id
    )
    uow.attempts.list_in_states.side_effect = lambda states: [
        item.attempt for item in items if item.attempt.state in states
    ]
    for item, harness in zip(items[:2], ["codex", "hermes"], strict=True):
        monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda h=harness, **_: {h})
        assert await supervisor._begin_launch(item) is not None
        item.attempt.state = holding_state
    monkeypatch.setattr(supervisor, "_eligible_harnesses", lambda **_: {"codex", "hermes"})
    assert await supervisor._begin_launch(items[2]) is None
    assert items[2].attempt.state is AttemptState.PENDING
    assert all(
        c["busy"] == "2 of 2 lab-local pool worker(s) already running"
        for c in items[2].attempt.ordered_candidates
        if c["eligible"]
    )
    items[0].attempt.state = AttemptState.COLLECTED
    assert await supervisor._begin_launch(items[2]) is not None
    assert items[2].attempt.selected_pool == "lab-local"
    assert items[2].attempt.routing_version == 26


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("frontier_busy", [False, True])
async def test_complex_task_never_falls_through_to_local(
    monkeypatch: pytest.MonkeyPatch, correction: bool, frontier_busy: bool
) -> None:
    supervisor, item, uow = _setup(monkeypatch)
    item.contract["execution_request"]["tier"] = "complex"
    if correction:
        item.execution.role = ExecutionRole.CORRECT
        item.execution.contract_version = 2
        item.task.contract_version = 2
        # Stale local metadata from an earlier decision cannot bypass tier filtering.
        item.attempt.ordered_candidates = [{"model": "codex-local", "eligible": True}]
        item.execution.model = "codex-local"
        item.execution.harness = "codex"
    if frontier_busy:
        live_execution = replace(item.execution, id="live", model="frontier", harness="codex")
        uow.executions.get.side_effect = lambda execution_id, **_: (
            live_execution if execution_id == "live" else item.execution
        )
        uow.attempts.list_in_states.return_value = [
            replace(item.attempt, id="live", execution_id="live", state=AttemptState.RUNNING)
        ]
    result = await supervisor._begin_launch(item)
    if frontier_busy:
        assert result is None
        assert item.attempt.state is AttemptState.PENDING
    else:
        assert result is not None
        assert item.attempt.selected_pool == "subscription"
    assert all(
        not candidate["eligible"]
        for candidate in item.attempt.ordered_candidates
        if candidate["pool"] == "lab-local"
    )


@pytest.mark.parametrize("excluded", [False, True])
async def test_pool_exclusion_and_quota_exhaustion_still_prevent_local_launch(
    monkeypatch: pytest.MonkeyPatch, excluded: bool
) -> None:
    supervisor, item, uow = _setup(monkeypatch)
    if excluded:
        item.attempt.routing_excluded_pools = ["lab-local"]
    else:
        uow.pool_exhaustions.get.side_effect = lambda pool: (
            PoolExhaustion(pool, NOW, NOW + timedelta(seconds=60), "task", "attempt", "quota")
            if pool == "lab-local"
            else None
        )
    item.contract["execution_request"]["tier"] = "complex"
    # Permit either capability so only pool exclusion, rather than tier, blocks local.
    route = uow.routing_policies.get.return_value.document
    route["tiers"]["complex"]["allowed_capability"] = ["mid", "frontier"]
    assert await supervisor._begin_launch(item) is not None
    assert item.attempt.selected_pool == "subscription"
