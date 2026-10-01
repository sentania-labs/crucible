"""Task checks must distinguish the unchanged base before a worker starts."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.domain.entities import ExecutionRole
from crucible.domain.exit_class import ExitClass
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import LaunchSpec
from tests.unit.kubernetes_fixtures import build, created
from tests.unit.test_routing import _routing_setup


def _setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, FakeProvider, LaunchSpec]:
    supervisor, item, uow = _routing_setup(monkeypatch, all_busy=False)
    item.attempt.state = AttemptState.PREPARING
    item.task.state = TaskState.RUNNING
    item.contract["required_verification"] = [
        {"id": "V1", "command": "make lint"},
        {"id": "V4", "command": "test -f made-by-the-worker"},
    ]
    uow.executions.list_for_task.return_value = [item.execution]
    uow.attempts.list_for_execution.return_value = [item.attempt]
    monkeypatch.setattr(supervisor, "_settle_if_cancelled", lambda *_: False)
    monkeypatch.setattr(supervisor, "_cancel_check", lambda *_: AsyncMock(return_value=False))
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    supervisor._github = None
    provider = FakeProvider()
    spec = LaunchSpec(
        attempt_id=item.attempt.id,
        task_id=item.task.id,
        external_id=item.task.external_id,
        role="implement",
        harness="script-harness",
        model="test",
        image="fake:succeed",
        timeout_seconds=60,
        contract=item.contract,
    )
    return supervisor, item, uow, provider, spec


async def test_probe_passes_when_a_check_fails_on_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    assert await supervisor._probe_before_prepare(item, provider, spec)
    row = uow.evidence.add.call_args.args[0]
    assert row.kind == "gate_probe" and row.source == "crucible"
    assert row.payload["id"] == "V4" and row.payload["exit"] == 1
    assert uow.evidence.add.call_count == 1  # Generic policy checks are not probed.
    assert item.task.state is TaskState.RUNNING
    assert provider.gate_probe_calls == [item.attempt.id]


async def test_probe_passes_when_exit_differs_from_nonzero_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, _, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"][1]["expect_exit"] = 1
    provider.gate_probe_exits = {"V4": 0}
    assert await supervisor._probe_before_prepare(item, provider, spec)


async def test_probe_blocks_when_exit_matches_nonzero_expectation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"][1]["expect_exit"] = 1
    provider.gate_probe_exits = {"V4": 1}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    assert item.attempt.termination_reason == "gate_proves_nothing"
    assert "V4 passes on the unchanged repo" in uow.escalations.add.call_args.args[0].question


async def test_all_checks_passing_blocks_as_gate_proves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_exits = {"V4": 0}
    monkeypatch.setattr(supervisor, "_build_spec", AsyncMock(return_value=spec))
    prepare = AsyncMock()
    monkeypatch.setattr(supervisor, "_prepare", prepare)
    assert not await supervisor._finish_launch(item, provider)
    prepare.assert_not_called()
    assert item.task.state is TaskState.BLOCKED
    assert item.attempt.termination_reason == "gate_proves_nothing"
    assert item.attempt.started_at is None
    assert not provider.prepares_started and not provider._workers
    assert "V4 passes on the unchanged repo" in uow.escalations.add.call_args.args[0].question
    assert uow.wakes.add.call_count == 1
    supervisor._release_checkout_leases.assert_called_once()


async def test_a_missing_program_blocks_as_check_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_exits = {"V4": 127}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    assert item.attempt.termination_reason == "check_cannot_run"
    question = uow.escalations.add.call_args.args[0].question
    assert "V4: exit 127" in question and "not found" in question
    assert uow.evidence.add.call_args.args[0].payload["exit"] == 127
    assert uow.wakes.add.call_count == 1
    assert item.attempt.started_at is None


@pytest.mark.parametrize("changed", [False, True])
async def test_a_correction_that_keeps_the_checks_is_not_probed(
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    previous_contract = deepcopy(item.contract)
    previous_attempt = replace(item.attempt, id="previous", started_at=item.attempt.created_at)
    uow.attempts.list_for_execution.return_value = [previous_attempt, item.attempt]
    uow.contracts.get.return_value = SimpleNamespace(document=previous_contract)
    item.execution.role = ExecutionRole.CORRECT
    if changed:
        item.contract["required_verification"][1]["command"] = "test -f new-file"
    assert await supervisor._probe_before_prepare(item, provider, spec)
    assert bool(provider.gate_probe_calls) is changed


async def test_failed_probe_job_blocks_as_check_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_error = "probe Job exit 1: checkout failed"
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    assert item.attempt.termination_reason == "check_cannot_run"
    assert "V4: probe Job exit 1" in uow.escalations.add.call_args.args[0].question
    assert uow.evidence.add.call_args.args[0].payload["exit"] is None


async def test_no_task_checks_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"] = [{"id": "V1", "command": "make lint"}]
    assert await supervisor._probe_before_prepare(item, provider, spec)
    assert not provider.gate_probe_calls
    uow.evidence.add.assert_not_called()


async def test_no_task_checks_creates_no_kubernetes_probe_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, _, _, spec = _setup(monkeypatch)
    api, _, provider = build()
    item.contract["required_verification"] = [{"id": "V1", "command": "make lint"}]
    assert await supervisor._probe_before_prepare(item, provider, spec)
    assert not created(api, "jobs", "gate-probe")


async def test_docker_unsupported_probe_has_explicit_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    monkeypatch.setattr(provider, "probe_checks", AsyncMock(return_value=None))
    assert await supervisor._probe_before_prepare(item, provider, spec)
    assert uow.evidence.add.call_args.args[0].payload["detail"] == "probe not supported, skipped"


async def test_probe_refusal_does_not_consume_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, item, uow, provider, spec = _setup(monkeypatch)
    provider.gate_probe_exits = {"V4": 0}
    assert not await supervisor._probe_before_prepare(item, provider, spec)
    blocked = replace(item.attempt)
    # The operator reschedules, and the first actual worker hits an environment
    # failure. With a two-attempt budget it must still get its one ordinary retry.
    item.attempt.id = "first-worker"
    item.attempt.number = 2
    item.attempt.state = AttemptState.COLLECTED
    item.attempt.termination_reason = None
    item.attempt.exit_class = ExitClass.ENVIRONMENT
    item.task.state = TaskState.RUNNING
    item.execution.max_attempts = 2
    item.execution.retry_on = ["environment"]
    uow.attempts.list_for_execution.return_value = [blocked, item.attempt]
    monkeypatch.setattr(supervisor, "_local_cap", lambda *_: None)
    supervisor._classify_and_finish(uow, item.attempt, None)
    assert item.task.state is TaskState.SCHEDULED
    retry = uow.attempts.add.call_args.args[0]
    assert retry.state is AttemptState.PENDING


@pytest.mark.parametrize("missing", [False, True])
async def test_mixed_probe_exits_require_every_command_to_run(
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    supervisor, item, _, provider, spec = _setup(monkeypatch)
    item.contract["required_verification"].append({"id": "V5", "command": "true"})
    provider.gate_probe_exits = {"V4": 1, "V5": 127 if missing else 0}
    assert await supervisor._probe_before_prepare(item, provider, spec) is not missing
    if missing:
        assert item.attempt.termination_reason == "check_cannot_run"
