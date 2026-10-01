"""FDY-0201: frontier caps, conservative unknown adapters, and auth observations."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.application.errors import ContractValidationError
from crucible.application.harnesses import CREDENTIAL_HOLDING_STATES, HarnessRegistry
from crucible.application.policies import validate_policy
from crucible.application.supervisor import Supervisor
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY, HarnessConcurrency
from crucible.domain.lifecycle import AttemptState
from crucible.ports.execution import CollectedOutputs
from crucible.ports.harness import CredentialSource, MountMode
from tests.fixtures import FakeClock
from tests.unit.test_class_routing import NOW
from tests.unit.test_policy_schema import seeded_policy
from tests.unit.test_routing import _routing_setup


@pytest.mark.parametrize("harness", ["claude_code", "codex", "agy", "hermes"])
def test_policy_accepts_parallel_frontier_harnesses(harness: str) -> None:
    document = seeded_policy()
    document["concurrency"]["per_harness"][harness] = 2
    policy = validate_policy(document, name=document["name"], version=document["version"])
    assert policy.concurrency.per_harness[harness] == 2


@pytest.mark.parametrize("declared", [True, False])
def test_policy_refuses_writable_harness_without_parallel_safety(
    monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    if declared:
        monkeypatch.setitem(HARNESS_CONCURRENCY, "unsafe", HarnessConcurrency())
    document = seeded_policy()
    document["concurrency"]["per_harness"]["unsafe"] = 2
    with pytest.raises(ContractValidationError) as caught:
        validate_policy(document, name=document["name"], version=document["version"])
    assert caught.value.errors == [
        {
            "path": "concurrency.per_harness.unsafe",
            "message": "unsafe has no read-only credential declaration or parallel-attempt "
            "safety declaration for rw-narrow copies (12), so concurrency must be 1",
        }
    ]
    document["concurrency"]["per_harness"]["unsafe"] = 1
    validate_policy(document, name=document["name"], version=document["version"])


@pytest.mark.parametrize("harness", ["claude_code", "codex", "agy"])
def test_policy_still_requires_frontier_caps(harness: str) -> None:
    document = seeded_policy()
    del document["concurrency"]["per_harness"][harness]
    with pytest.raises(ContractValidationError, match="policy failed validation"):
        validate_policy(document, name=document["name"], version=document["version"])


def test_agy_and_codex_declare_parallel_writable_copies() -> None:
    for adapter in (AgyAdapter(), CodexAdapter()):
        assert adapter.parallel_attempts_safe is True
        assert adapter.credential_spec().minimum_mode is MountMode.RW_NARROW


@pytest.mark.parametrize("harness", ["claude_code", "codex", "agy"])
@pytest.mark.parametrize("state", CREDENTIAL_HOLDING_STATES)
def test_supervisor_harness_cap_two_counts_until_collection(
    monkeypatch: pytest.MonkeyPatch, harness: str, state: AttemptState
) -> None:
    monkeypatch.setattr("crucible.application.supervisor.load_routing", lambda *_: None)
    supervisor = Supervisor(
        MagicMock(), {}, FakeClock(NOW), holder="test", artifact_store=MagicMock()
    )
    supervisor._harnesses = default_registry()
    # Existing installations may explicitly configure writable Claude Code copies.
    supervisor._credential_sources = {harness: CredentialSource("/credential", MountMode.RW_NARROW)}
    execution: Any = SimpleNamespace(
        harness=harness,
        model="model",
        policy_snapshot={"concurrency": {"per_harness": {harness: 2}}},
    )
    live = [SimpleNamespace(execution_id="first", state=state)]
    uow: Any = MagicMock()
    uow.attempts.list_in_states.side_effect = lambda states: [a for a in live if a.state in states]
    uow.executions.get.side_effect = lambda _: execution
    assert supervisor._harness_busy_in_uow(uow, execution) is None
    live.append(SimpleNamespace(execution_id="second", state=state))
    assert supervisor._harness_busy_in_uow(uow, execution) == (
        f"2 of 2 {harness} worker(s) already running"
    )
    live[0].state = AttemptState.COLLECTED
    assert supervisor._harness_busy_in_uow(uow, execution) is None


def test_supervisor_keeps_unsafe_writable_adapter_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("crucible.application.supervisor.load_routing", lambda *_: None)
    adapter = CodexAdapter()
    monkeypatch.delattr(CodexAdapter, "parallel_attempts_safe")
    supervisor = Supervisor(
        MagicMock(),
        {},
        FakeClock(NOW),
        holder="test",
        artifact_store=MagicMock(),
        harnesses=HarnessRegistry([adapter]),
    )
    execution: Any = SimpleNamespace(
        harness="codex",
        model="model",
        policy_snapshot={"concurrency": {"per_harness": {"codex": 2}}},
    )
    uow: Any = MagicMock()
    uow.attempts.list_in_states.return_value = [SimpleNamespace(execution_id="first")]
    uow.executions.get.return_value = execution
    assert (
        supervisor._harness_busy_in_uow(uow, execution) == "1 of 1 codex worker(s) already running"
    )


@pytest.mark.parametrize("running", [1, 2])
@pytest.mark.parametrize("harness", ["claude_code", "codex"])
async def test_supervisor_cap_two_uses_free_slot_then_falls_through(
    monkeypatch: pytest.MonkeyPatch, running: int, harness: str
) -> None:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False, first_harness=harness)
    supervisor._harnesses = default_registry()
    supervisor._credential_sources = {}
    pending.execution.policy_snapshot["concurrency"] = {"per_harness": {harness: 2, "agy": 2}}
    first = uow.attempts.list_in_states.return_value[0]
    if running == 2:
        uow.attempts.list_in_states.return_value.append(replace(first, id="second"))
    assert await supervisor._begin_launch(pending) is not None
    assert pending.attempt.state is AttemptState.PREPARING
    assert pending.attempt.selected_harness == (harness if running == 1 else "agy")
    if running == 2:
        assert (
            pending.attempt.ordered_candidates[0]["busy"]
            == f"2 of 2 {harness} worker(s) already running"
        )


def test_supervisor_auth_failure_counter_is_per_harness_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    supervisor = Supervisor(
        MagicMock(), {}, FakeClock(NOW), holder="test", artifact_store=MagicMock()
    )
    uow: Any = MagicMock()
    uow.harnesses.get.return_value = None
    outputs = CollectedOutputs(report=None, report_raw=None, blocked_md=None)
    for index, (harness, outcome) in enumerate(
        [
            ("codex", ExitClass.AUTH_FAILURE),
            ("agy", ExitClass.AUTH_FAILURE),
            ("codex", ExitClass.COMPLETED),
            ("codex", ExitClass.AUTH_FAILURE),
            ("claude_code", ExitClass.ENVIRONMENT),
        ]
    ):
        attempt: Any = SimpleNamespace(id=f"attempt-{index}", exit_class=outcome)
        execution: Any = SimpleNamespace(harness=harness)
        supervisor._record_credential_sync(uow, attempt, execution, outputs)
    assert supervisor.auth_failures_by_harness == {"codex": 2, "agy": 1}
    assert [(r.__dict__["harness"], r.__dict__["auth_failure_count"]) for r in caplog.records] == [
        ("codex", 1),
        ("agy", 1),
        ("codex", 2),
    ]
    assert "harness=codex attempt=attempt-3 auth_failure_count=2" in caplog.text
