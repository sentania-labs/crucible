"""Hades #353 and #346: infrastructure interruptions and start failures are retried
without evaluating partial work, and a real worker failure is gated as before.

The transcripts under tests/fixtures_data/interruptions follow the event shapes the
pinned CLIs write, as captured in tests/fixtures_data/transcripts (Codex exec 0.156.0,
Claude Code 2.1.280) and as the Codex app-server host keeps its notifications
(images/worker/crucible-codex-host.py adds `type` from `method`):

- codex-app-server-error-then-failed-turn: `error` notifications whose `codexErrorInfo`
  carries `httpStatusCode` 503, then `turn/completed` with status `failed` and an error
  that names no status. The host exits 0 after it.
- codex-app-server-turn-failed: `turn/failed` with the error under `params`.
- codex-exec-turn-failed-503: `codex exec --json`, a retry error item, an `error` event
  and `turn.failed` whose error has only `message`, as on 2026-10-01 at 6:00 PM.
- codex-exec-turn-failed-429, codex-exec-at-capacity, codex-exec-connection-refused:
  the same shapes for a rate limit, a capacity refusal and a refused connection.
- claude-code-api-retry-503: `api_retry` events with `error_status` 503, then the CLI's
  synthetic error message and error result.
- agy-result-unavailable: the final `result` line with status `ERROR`, UNAVAILABLE.
- hermes-503.stderr: Hermes's client error for the failed call.
- *-pytest-failure: a worker whose tests failed, with 503 in the test output.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import timedelta
from http.client import BadStatusLine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution import endpoint_health, k8sspec
from crucible.adapters.execution.dockerapi import DockerApiError
from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import USAGE_NAME, HermesAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.application.corrections import (
    PREVIOUS_BUNDLE_GONE,
    PREVIOUS_BUNDLE_OTHER_PROVIDER,
    _unpublished_bundle_problem,
)
from crucible.application.supervisor import _Pending
from crucible.domain.entities import Event, EvidenceRecord
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.gates import GateName, GateResult, evaluate_gate
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import (
    BranchBundle,
    CollectedOutputs,
    Observation,
    ObservationState,
    WorkerStartError,
)
from crucible.ports.harness import ExitInfo, HarnessAdapter
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_class_routing import NOW
from tests.unit.test_docker_provider import StubClient, workspace_for
from tests.unit.test_docker_provider import provider as docker_provider
from tests.unit.test_docker_provider import spec as docker_spec
from tests.unit.test_gates import _ev, _gi, _passing_evidence
from tests.unit.test_routing import _routing_setup

FIXTURES = Path(__file__).parent.parent / "fixtures_data" / "interruptions"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _running(
    monkeypatch: pytest.MonkeyPatch, harness: str = "codex"
) -> tuple[Any, Any, Any, list[Any]]:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    # The real adapters classify: nothing below hands the supervisor a verdict.
    supervisor._harnesses = default_registry()
    pending.execution.harness = harness
    pending.task.state = TaskState.RUNNING
    pending.execution.state = ExecutionState.ACTIVE
    pending.execution.model = "a-first"
    pending.attempt.state = AttemptState.RUNNING
    pending.attempt.started_at = supervisor._clock.now()
    pending.attempt.selected_model = "a-first"
    pending.attempt.selected_pool = "pool-a-first"
    attempts = [pending.attempt]
    events: list[Any] = []
    evidence: list[Any] = []

    def append_event(event: Any) -> Any:
        events.append(event)
        return event

    def append_evidence(row: Any) -> Any:
        evidence.append(row)
        return row

    uow.events.append.side_effect = append_event
    uow.events.list_for_task.side_effect = lambda *_, **__: list(events)
    uow.events.latest_for_task_kind.side_effect = lambda task_id, kind: next(
        (event for event in reversed(events) if event.kind == kind), None
    )
    uow.attempts.list_for_task.side_effect = lambda *_: list(attempts)
    uow.attempts.list_for_execution.side_effect = lambda *_: list(attempts)
    uow.attempts.add.side_effect = attempts.append
    uow.attempts.list_in_states.side_effect = lambda states: [
        attempt for attempt in attempts if attempt.state in states
    ]
    uow.executions.list_for_task.return_value = [pending.execution]
    uow.tasks.list_by_state.side_effect = lambda state, **_: (
        [pending.task] if pending.task.state is state else []
    )
    uow.contracts.get.return_value = MagicMock(
        document=pending.contract, submitted_at=pending.task.created_at
    )
    uow.pull_requests.get_for_task.return_value = None
    uow.evidence.add.side_effect = append_evidence
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: [
        row for row in evidence if row.attempt_id == attempt_id
    ]
    uow.attempt_metrics.get.return_value = None
    supervisor._artifacts = MagicMock()
    supervisor.attempt_lease_ttl_seconds = 60
    monkeypatch.setattr(supervisor, "_record_credential_sync", MagicMock())
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    return supervisor, pending, uow, attempts


def _events(uow: Any) -> list[Any]:
    return [call.args[0] for call in uow.events.append.call_args_list]


def _finish(supervisor: Any, attempt: Any, tail: str, exit_code: int = 1, **kwargs: Any) -> None:
    """The worker's own output as the provider collected it: no classification given."""
    supervisor._finish_exited(
        attempt.id,
        exit_code,
        CollectedOutputs(report=None, report_raw=None, blocked_md=None, stdout_tail=tail),
        defer_quota=True,
        **kwargs,
    )


def _infrastructure(attempt: Any, count: int, exit_class: ExitClass) -> list[Any]:
    return [
        replace(attempt, id=f"0prior{i}", exit_class=exit_class, state=AttemptState.FAILED)
        for i in range(count)
    ]


def _wakes(uow: Any) -> int:
    return sum(event.kind == EventKind.WAKE_CREATED.value for event in _events(uow))


# ----- item 1: each adapter reads its own CLI's error events ----------------------


def _hermes_dir(tmp_path: Path, *, failed: bool) -> Path:
    (tmp_path / USAGE_NAME).write_text(
        json.dumps({"completed": not failed, "failed": failed}), encoding="utf-8"
    )
    return tmp_path


def _codex_host_dir(tmp_path: Path, name: str) -> Path:
    (tmp_path / "transcript.jsonl").write_text(_fixture(name), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("adapter", "exit_code", "fixture", "stderr", "report_dir"),
    [
        # The app-server host writes its transcript itself and exits 0 after a failed turn.
        (CodexAdapter(), 0, "", "", "codex-app-server-error-then-failed-turn.jsonl"),
        (CodexAdapter(), 1, "", "", "codex-app-server-turn-failed.jsonl"),
        (CodexAdapter(), 1, "codex-exec-turn-failed-503.jsonl", "", None),
        (CodexAdapter(), 1, "codex-exec-connection-refused.jsonl", "", None),
        (ClaudeCodeAdapter(), 1, "claude-code-api-retry-503.jsonl", "", None),
        (AgyAdapter(), 1, "agy-result-unavailable.jsonl", "", None),
        (HermesAdapter(), 1, "", "hermes-503.stderr", "hermes-failed"),
    ],
)
def test_each_harness_classifies_its_gateway_failure_as_infrastructure(
    tmp_path: Path,
    adapter: HarnessAdapter,
    exit_code: int,
    fixture: str,
    stderr: str,
    report_dir: str | None,
) -> None:
    directory: Path | None = None
    if report_dir == "hermes-failed":
        directory = _hermes_dir(tmp_path, failed=True)
    elif report_dir is not None:
        directory = _codex_host_dir(tmp_path, report_dir)
    stdout = _fixture(fixture) if fixture else ""
    error = _fixture(stderr) if stderr else ""
    exit = ExitInfo(exit_code=exit_code)
    assert adapter.classify_exit(exit, stdout, error, directory) is ExitClass.INFRASTRUCTURE
    interruption = adapter.interruption(exit, stdout, error, directory)
    assert interruption is not None and not interruption.quota and not interruption.capacity


@pytest.mark.parametrize(
    ("adapter", "fixture", "stderr", "hermes"),
    [
        (CodexAdapter(), "codex-exec-pytest-failure.jsonl", "", False),
        (ClaudeCodeAdapter(), "claude-code-pytest-failure.jsonl", "", False),
        (AgyAdapter(), "agy-pytest-failure.jsonl", "", False),
        (HermesAdapter(), "", "hermes-pytest-failure.stderr", True),
    ],
)
def test_a_pytest_failure_with_503_in_its_output_is_not_infrastructure(
    tmp_path: Path, adapter: HarnessAdapter, fixture: str, stderr: str, hermes: bool
) -> None:
    directory = _hermes_dir(tmp_path, failed=False) if hermes else None
    stdout = _fixture(fixture) if fixture else ""
    error = _fixture(stderr) if stderr else ""
    for exit in (ExitInfo(exit_code=1), ExitInfo(exit_code=0, report_present=True)):
        assert adapter.interruption(exit, stdout, error, directory) is None
        assert adapter.classify_exit(exit, stdout, error, directory) is not (
            ExitClass.INFRASTRUCTURE
        )


def test_codex_quota_and_capacity_events_keep_their_own_meaning() -> None:
    adapter = CodexAdapter()
    exit = ExitInfo(exit_code=1)
    quota = _fixture("codex-exec-turn-failed-429.jsonl")
    assert adapter.classify_exit(exit, quota, "") is ExitClass.QUOTA_EXHAUSTED
    capacity = adapter.interruption(exit, _fixture("codex-exec-at-capacity.jsonl"), "")
    assert capacity is not None and capacity.capacity
    assert capacity.exit_class is ExitClass.INFRASTRUCTURE


def test_a_retried_call_that_recovered_does_not_end_the_run() -> None:
    """The 503 retries came before a turn that completed: the exit is the worker's."""
    recovered = (
        _fixture("codex-exec-turn-failed-503.jsonl").rsplit("\n", 2)[0]
        + '\n{"type":"item.completed","item":{"id":"item_9","type":"agent_message",'
        '"text":"done"}}\n{"type":"turn.completed","usage":{"input_tokens":1,'
        '"output_tokens":1}}\n'
    )
    assert CodexAdapter().interruption(ExitInfo(exit_code=1), recovered, "") is None
    assert CodexAdapter().classify_exit(ExitInfo(exit_code=1), recovered, "") is ExitClass.CRASHED


def test_hermes_gateway_failure_reaches_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor, pending, uow, _attempts = _running(monkeypatch, harness="hermes")
    pending.attempt.workspace_path = str(tmp_path)
    report = tmp_path / "output" / "report"
    report.mkdir(parents=True)
    _hermes_dir(report, failed=True)
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None, report_raw=None, blocked_md=None, stderr_tail=_fixture("hermes-503.stderr")
        ),
    )
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert pending.task.state is TaskState.SCHEDULED
    assert len(uow.attempts.list_for_task(pending.task.id)) == 2


def test_codex_host_failed_turn_after_503_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #353 replay on the app-server path: the host exits 0 and writes no stdout."""
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    pending.attempt.workspace_path = str(tmp_path)
    report = tmp_path / "output" / "report"
    report.mkdir(parents=True)
    _codex_host_dir(report, "codex-app-server-error-then-failed-turn.jsonl")
    _finish(supervisor, pending.attempt, "", exit_code=0)
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert pending.task.state is TaskState.SCHEDULED
    uow.gate_results.add.assert_not_called()


@pytest.mark.parametrize(
    "fixture",
    ["codex-exec-turn-failed-503.jsonl", "codex-exec-connection-refused.jsonl"],
)
def test_503_mid_run_retries_without_gates(monkeypatch: pytest.MonkeyPatch, fixture: str) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    _finish(supervisor, pending.attempt, _fixture(fixture))
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.task.resume_at == supervisor._clock.now() + timedelta(minutes=3)
    assert pending.execution.max_attempts == 1
    assert len(attempts) == 2
    assert supervisor._list_pending() == []
    supervisor._evaluate_pending_gates()
    uow.gate_results.add.assert_not_called()
    exited = next(e for e in _events(uow) if e.kind == EventKind.ATTEMPT_EXITED.value)
    assert exited.payload["exit_class"] == "infrastructure"
    assert exited.payload["interruption_message"]


def test_429_without_commits_skips_checkpoint_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    routing = uow.routing_policies.get.return_value.document
    routing["pools"]["pool-a-first"]["default_cooldown_seconds"] = 900
    routing["models"][1]["enabled"] = False
    marks: dict[str, Any] = {}

    def put(mark: Any) -> Any:
        marks[mark.pool] = mark
        return mark

    uow.pool_exhaustions.put.side_effect = put
    uow.pool_exhaustions.get.side_effect = marks.get
    uow.pool_exhaustions.list_all.side_effect = lambda: list(marks.values())
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-429.jsonl"))
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    assert not supervisor._quota_checkpoint_pending(pending.attempt.id)
    assert pending.task.state is TaskState.AWAITING_QUOTA
    reset = supervisor._clock.now() + timedelta(seconds=900)
    assert marks["pool-a-first"].reset_at == reset
    assert pending.task.resume_at == reset
    assert supervisor._list_pending() == []


def test_capacity_refusal_excludes_model_and_reroutes_within_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-at-capacity.jsonl"))
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    nxt = attempts[-1]
    # Routing as in tests/unit/test_routing.py: no credential gate on the candidates.
    supervisor._harnesses = None
    result = supervisor._selection_for(
        uow, _Pending(nxt, pending.execution, pending.task, pending.contract)
    )
    assert result.selected.id == "b-second"
    uow.attempts.get.return_value = nxt
    routed = supervisor._route_pending(
        _Pending(nxt, pending.execution, pending.task, pending.contract)
    )
    assert routed is not None
    assert routed.attempt.selected_model == "b-second"


def test_real_test_failure_still_fails_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, attempts = _running(monkeypatch)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-pytest-failure.jsonl"))
    assert pending.attempt.exit_class is ExitClass.CRASHED
    assert pending.task.state is TaskState.REPORTED
    assert len(attempts) == 1
    evidence = [
        row
        for row in _passing_evidence()
        if not (row.kind == "verification_run" and row.payload.get("id") == "V2")
    ]
    evidence.append(_ev("verification_run", {"id": "V2", "exit_code": 1, "ran": True}, ident=21))
    assert evaluate_gate(GateName.VERIFICATION_RAN.value, _gi(evidence)).result is GateResult.FAIL


def test_quota_with_commits_still_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, _attempts = _running(monkeypatch)
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stdout_tail=_fixture("codex-exec-turn-failed-429.jsonl"),
            bundle=BranchBundle("abc", "main", "work", 1, True, sha256="sealed"),
        ),
        defer_quota=True,
    )
    assert supervisor._quota_checkpoint_pending(pending.attempt.id)
    assert pending.task.head_sha == "abc"


def test_worker_question_survives_transport_words(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    supervisor._finish_exited(
        pending.attempt.id,
        75,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md="Which endpoint? connection refused",
            stdout_tail=_fixture("codex-exec-connection-refused.jsonl"),
        ),
    )
    assert pending.attempt.exit_class is ExitClass.BLOCKED
    assert pending.task.state is TaskState.BLOCKED
    assert uow.escalations.add.call_args.args[0].question == "Which endpoint? connection refused"


@pytest.mark.parametrize(
    "classification",
    [ExitClass.BLOCKED, ExitClass.PROVIDER_ERROR, ExitClass.AUTH_FAILURE],
)
def test_specific_adapter_classification_wins(
    monkeypatch: pytest.MonkeyPatch, classification: ExitClass
) -> None:
    supervisor, pending, _uow, _attempts = _running(monkeypatch)
    adapter = MagicMock()
    adapter.classify_exit.return_value = classification
    adapter.interruption.return_value = None
    adapter.provider_quota_event.return_value = None
    supervisor._harnesses = MagicMock()
    supervisor._harnesses.get.return_value = adapter
    mark_down = MagicMock()
    monkeypatch.setattr(supervisor, "_mark_local_endpoint_down", mark_down)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.attempt.exit_class is classification
    if classification is ExitClass.PROVIDER_ERROR:
        mark_down.assert_called_once()
    if classification is ExitClass.BLOCKED:
        assert pending.task.state is TaskState.BLOCKED


def test_quota_reroutes_without_infrastructure_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    uow.pool_exhaustions.put.side_effect = lambda mark: mark
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-429.jsonl"))
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.task.resume_at is None
    assert any(row.kind == EventKind.TASK_REROUTED.value for row in _events(uow))


def test_terminal_quota_is_reported_with_quota_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _attempts = _running(monkeypatch)
    uow.routing_policies.get.return_value.document["reroute"]["reroute_max"] = 0
    uow.pool_exhaustions.put.side_effect = lambda mark: mark
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-429.jsonl"))
    assert pending.task.state is TaskState.REPORTED
    assert not any(row.kind == EventKind.TASK_BLOCKED.value for row in _events(uow))
    assert _wakes(uow) == 1


async def test_503_collection_does_not_run_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    monkeypatch.setattr(
        provider, "observe", AsyncMock(return_value=Observation(ObservationState.EXITED, 1))
    )
    monkeypatch.setattr(
        provider,
        "_worker_tails",
        AsyncMock(return_value=(_fixture("codex-exec-turn-failed-503.jsonl"), "")),
    )
    verifier = AsyncMock()
    monkeypatch.setattr(provider, "_run_verifier", verifier)
    outputs = await provider.collect(handle, workspace, replace(launch, harness="codex"))
    verifier.assert_not_called()
    assert outputs.interruption is not None


async def test_pytest_failure_collection_runs_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    monkeypatch.setattr(
        provider, "observe", AsyncMock(return_value=Observation(ObservationState.EXITED, 1))
    )
    monkeypatch.setattr(
        provider,
        "_worker_tails",
        AsyncMock(return_value=(_fixture("codex-exec-pytest-failure.jsonl"), "")),
    )
    verifier = AsyncMock(return_value=())
    monkeypatch.setattr(provider, "_run_verifier", verifier)
    outputs = await provider.collect(handle, workspace, replace(launch, harness="codex"))
    verifier.assert_awaited_once()
    assert outputs.interruption is None


async def test_retry_spec_resumes_sealed_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, attempts = _running(monkeypatch)
    pending.attempt.id = "000previous"
    pending.attempt.workspace_path = "kubernetes://workspace/previous"
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stdout_tail=_fixture("codex-exec-turn-failed-503.jsonl"),
            bundle=BranchBundle("abc", "main", "work", 1, True, sha256="sealed"),
        ),
        defer_quota=True,
    )
    nxt = attempts[-1]
    assert not nxt.resume_from_remote
    supervisor._harnesses = None
    launch = await supervisor._build_spec(nxt, pending.execution, pending.task, pending.contract)
    assert launch.resume_bundle_attempt_id == "000previous"
    assert launch.resume_bundle_head == "abc"
    assert launch.resume_bundle_sha256 == "sealed"
    assert launch.resume_bundle_path == "kubernetes://workspace/previous/output/work_branch.bundle"


# ----- item 2: the first block wakes and may resume; the second escalates ---------


def test_budget_blocks_once_and_health_recovery_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    attempts[:0] = _infrastructure(pending.attempt, 2, ExitClass.INFRASTRUCTURE)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.task.state is TaskState.BLOCKED
    blocked = next(e for e in _events(uow) if e.kind == EventKind.TASK_BLOCKED.value)
    assert blocked.payload["reason"] == "model_endpoint_unavailable"
    assert blocked.payload["health_retry_allowed"] is True
    # One wake naming the cause, and no escalation: this block may resume on its own.
    assert _wakes(uow) == 1
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    assert "model_endpoint_unavailable" in wake.payload["summary"]
    assert "503 Service Unavailable" in wake.payload["summary"]
    uow.escalations.add.assert_not_called()
    before = len(_events(uow))
    supervisor._record_endpoint_health(pending.task.id, False)
    assert len(_events(uow)) == before
    supervisor._record_endpoint_health(pending.task.id, True)
    assert str(pending.task.state) == TaskState.SCHEDULED.value
    assert pending.task.resume_at is None
    uow.escalations.add.assert_not_called()
    assert sum(e.kind == EventKind.TASK_BLOCKED.value for e in _events(uow)) == 1


def test_second_block_opens_the_one_escalation_and_stays_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    attempts[:0] = _infrastructure(pending.attempt, 2, ExitClass.INFRASTRUCTURE)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    supervisor._record_endpoint_health(pending.task.id, True)
    supervisor._materialize_scheduled()
    fourth = attempts[-1]
    assert fourth.state is AttemptState.PENDING
    pending.task.state = TaskState.RUNNING
    fourth.state = AttemptState.RUNNING
    uow.attempts.get.return_value = fourth
    _finish(supervisor, fourth, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.task.state is TaskState.BLOCKED
    blocks = [e for e in _events(uow) if e.kind == EventKind.TASK_BLOCKED.value]
    assert [b.payload["health_retry_allowed"] for b in blocks] == [True, False]
    uow.escalations.add.assert_called_once()
    assert _wakes(uow) == 2
    supervisor._record_endpoint_health(pending.task.id, True)
    assert pending.task.state is TaskState.BLOCKED
    assert supervisor._infrastructure_waits() == []


def test_new_contract_gets_fresh_interruption_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    attempts[:0] = _infrastructure(pending.attempt, 3, ExitClass.INFRASTRUCTURE)
    attached = pending.task.created_at + timedelta(seconds=1)
    uow.contracts.get.return_value.submitted_at = attached
    pending.execution.contract_version = 2
    pending.task.contract_version = 2
    pending.attempt.created_at = attached
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.task.state is TaskState.SCHEDULED
    uow.attempts.add.assert_called_once()
    uow.escalations.add.assert_not_called()


# ----- item 3: quota reroutes do not spend the infrastructure budget --------------


def test_quota_reroutes_do_not_count_toward_the_infrastructure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, attempts = _running(monkeypatch)
    attempts[:0] = _infrastructure(pending.attempt, 2, ExitClass.QUOTA_EXHAUSTED)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert pending.task.state is TaskState.SCHEDULED
    assert not any(e.kind == EventKind.TASK_BLOCKED.value for e in _events(uow))


# ----- item 4: a Docker worker the runtime could not start ------------------------


class StartRefusingClient(StubClient):
    """The daemon's answer to `/start` when runc cannot set up the process: the
    container exists, never started, and its state carries the runtime's error."""

    MESSAGE = (
        "failed to create task for container: failed to create shim task: OCI runtime "
        "create failed: runc create failed: unable to start container process: error "
        'during container init: error mounting "/srv/crucible/workspaces/a/report" to '
        'rootfs at "/crucible/report": not a directory: unknown'
    )

    def start_container(self, container_id: str) -> None:
        raise DockerApiError(400, self.MESSAGE, path=f"/containers/{container_id}/start")

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        return {
            "State": {
                "Status": "created",
                "Running": False,
                "ExitCode": 128,
                "Error": self.MESSAGE,
                "StartedAt": "0001-01-01T00:00:00Z",
            }
        }


async def test_docker_start_failure_is_a_never_started_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = StartRefusingClient()
    docker = docker_provider(tmp_path, client)
    launch = docker_spec()
    with pytest.raises(WorkerStartError) as raised:
        await docker.launch(workspace_for(tmp_path, launch.attempt_id), launch)
    observation = raised.value.observation
    assert observation.never_started
    assert observation.detail == "StartError"
    assert observation.exit_code == 128
    assert "not a directory" in (observation.container_message or "")
    assert client.removed == ["container-1"]

    supervisor, pending, uow, attempts = _running(monkeypatch)
    pending.attempt.state = AttemptState.LAUNCHING
    supervisor._start_failure(pending.attempt.id, observation)
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    exited = next(e for e in _events(uow) if e.kind == EventKind.ATTEMPT_EXITED.value)
    assert exited.payload["never_started"] is True
    assert "not a directory" in exited.payload["interruption_message"]
    assert exited.payload["interruption_message"].startswith("the worker never started: ")
    rows = uow.evidence.list_for_attempt(pending.attempt.id)
    assert any("not a directory" in row.payload.get("container_message", "") for row in rows)
    assert pending.task.state is TaskState.SCHEDULED
    assert len(attempts) == 2
    uow.gate_results.add.assert_not_called()


async def test_docker_daemon_refusal_before_start_stays_an_environment_failure(
    tmp_path: Path,
) -> None:
    class CreateRefusingClient(StubClient):
        def create_container(self, name: str, body: dict[str, Any]) -> str:
            raise DockerApiError(500, "no space left on device", path="/containers/create")

    docker = docker_provider(tmp_path, CreateRefusingClient())
    launch = docker_spec()
    with pytest.raises(Exception) as raised:
        await docker.launch(workspace_for(tmp_path, launch.attempt_id), launch)
    assert not isinstance(raised.value, WorkerStartError)


# ----- item 5: a retry after a pushed checkpoint resumes from it ------------------


async def test_retry_after_a_checkpoint_resumes_from_it(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, attempts = _running(monkeypatch)
    # A quota checkpoint was pushed earlier; any further attempt resumes from it.
    pending.execution.resume_from_remote = True
    pending.attempt.workspace_path = "kubernetes://workspace/interrupted"
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stdout_tail=_fixture("codex-exec-turn-failed-503.jsonl"),
            bundle=BranchBundle("abc", "main", "work", 1, True, sha256="sealed"),
        ),
        defer_quota=True,
    )
    nxt = attempts[-1]
    assert nxt.resume_from_remote is True
    supervisor._harnesses = None
    launch = await supervisor._build_spec(nxt, pending.execution, pending.task, pending.contract)
    assert launch.contract["repository"]["resume_from_work_branch"] is True
    assert launch.resume_bundle_attempt_id is None


def test_health_recovered_retry_uses_the_same_resume_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, _uow, attempts = _running(monkeypatch)
    pending.execution.resume_from_remote = True
    attempts[:0] = _infrastructure(pending.attempt, 2, ExitClass.INFRASTRUCTURE)
    _finish(supervisor, pending.attempt, _fixture("codex-exec-turn-failed-503.jsonl"))
    assert pending.task.state is TaskState.BLOCKED
    supervisor._record_endpoint_health(pending.task.id, True)
    supervisor._materialize_scheduled()
    assert attempts[-1].state is AttemptState.PENDING
    assert attempts[-1].resume_from_remote is True


# ----- item 6: a never-started head still checks the provider of the sealed work --


def _bundle_row(attempt_id: str, head: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=None,
        attempt_id=attempt_id,
        task_id="task",
        kind="bundle_head",
        observed_at=NOW,
        source="crucible",
        verified=True,
        payload={"bundle_verified": True, "bundle_sha256": "sealed", "head_sha": head},
    )


def _correction_uow(tmp_path: Path, *, sealed_on: str | None, head: str | None) -> tuple[Any, Any]:
    uow: Any = MagicMock()
    task = MagicMock(id="task", head_sha=head)
    docker_execution = MagicMock(id="docker-exec", provider="docker", role="implement")
    k8s_execution = MagicMock(id="k8s-exec", provider="kubernetes", role="implement")
    sealed = MagicMock(id="01A", execution_id="docker-exec", workspace_path=str(tmp_path))
    never = MagicMock(id="01B", execution_id="k8s-exec", workspace_path=None)
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "output" / "work_branch.bundle").write_bytes(b"bundle")
    executions = {"docker-exec": docker_execution, "k8s-exec": k8s_execution}
    attempts = {"docker-exec": [sealed], "k8s-exec": [never]}
    uow.executions.list_for_task.return_value = list(executions.values())
    uow.executions.get.side_effect = executions.get
    uow.attempts.list_for_execution.side_effect = lambda execution_id: attempts[execution_id]
    uow.attempts.list_for_task.return_value = [sealed, never]
    rows = {"01A": [_bundle_row("01A", head or "")] if sealed_on else [], "01B": []}
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: rows[attempt_id]
    uow.retention.list_recent.return_value = []
    exited = Event(
        seq=None,
        ts=NOW,
        kind=EventKind.ATTEMPT_EXITED.value,
        principal="crucible",
        verified=True,
        payload={"exit_class": "infrastructure", "never_started": True},
        task_id="task",
        attempt_id="01B",
    )
    uow.events.latest_for_task_kind.side_effect = lambda task_id, kind: (
        exited if kind == EventKind.ATTEMPT_EXITED.value else None
    )
    return uow, task


def test_never_started_head_still_checks_the_sealed_provider(tmp_path: Path) -> None:
    uow, task = _correction_uow(tmp_path, sealed_on="docker", head="abc")
    assert _unpublished_bundle_problem(uow, task, "docker") is None
    problem = _unpublished_bundle_problem(uow, task, "kubernetes")
    assert problem == {
        "path": "execution_request.provider",
        "message": PREVIOUS_BUNDLE_OTHER_PROVIDER,
    }


def test_never_started_head_without_any_bundle_is_not_correctable_in_place(
    tmp_path: Path,
) -> None:
    uow, task = _correction_uow(tmp_path, sealed_on=None, head="abc")
    assert _unpublished_bundle_problem(uow, task, "kubernetes") == {
        "path": "correction",
        "message": PREVIOUS_BUNDLE_GONE,
    }
    # Nothing ever sealed and no head: the correction starts from the base.
    uow, task = _correction_uow(tmp_path, sealed_on=None, head=None)
    assert _unpublished_bundle_problem(uow, task, "kubernetes") is None


# ----- start failures on Kubernetes (hades #346) ----------------------------------


async def test_start_error_records_message_events_and_remains_correctable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _registry, provider = build()
    api.events.append(
        {
            "involvedObject": {"uid": "pod-uid"},
            "reason": "Failed",
            "message": "bad subPath",
            "count": 2,
        }
    )
    observation = await provider._start_failure_observation(
        {"metadata": {"uid": "pod-uid"}}, 128, "StartError", "mount target is not a directory"
    )
    # The captured status travels through the normal finish transaction before cleanup.
    document = json.loads(provider._launch_evidence(spec(), observation).content)
    assert document["final_observation"]["container_message"] == "mount target is not a directory"
    assert document["final_observation"]["pod_events"][0]["count"] == 2
    supervisor, pending, uow, attempts = _running(monkeypatch)
    attempts[:0] = _infrastructure(pending.attempt, 2, ExitClass.INFRASTRUCTURE)
    _finish(supervisor, pending.attempt, "", final_observation=observation)
    assert pending.task.state is TaskState.BLOCKED
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert _unpublished_bundle_problem(uow, pending.task, "fake") is None
    wake = next(e for e in _events(uow) if e.kind == EventKind.WAKE_CREATED.value)
    assert "the worker never started: mount target is not a directory" in wake.payload["summary"]
    assert any(
        row.payload.get("pod_events") for row in uow.evidence.list_for_attempt(pending.attempt.id)
    )
    pending.attempt.logs_drained_at = supervisor._clock.now()
    assert supervisor._list_cleanup_due() == []


async def test_start_failure_collection_does_not_run_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    monkeypatch.setattr(
        provider,
        "observe",
        AsyncMock(
            return_value=Observation(
                ObservationState.EXITED,
                exit_code=128,
                never_started=True,
                detail="StartError",
                container_message="exec format error",
            )
        ),
    )
    verifier = AsyncMock()
    monkeypatch.setattr(provider, "_run_verifier", verifier)
    outputs = await provider.collect(handle, workspace, launch)
    verifier.assert_not_called()
    assert outputs.bundle is None


@pytest.mark.parametrize(
    "reason,terminated",
    [
        ("StartError", True),
        ("RunContainerError", True),
        ("ContainerCannotRun", True),
        ("InvalidImageName", False),
        ("ErrImageNeverPull", False),
        ("ImageInspectError", False),
        ("PostStartHookError", False),
        ("CreateContainerError", False),
        ("CreateContainerConfigError", False),
        ("ImagePullBackOff", False),
    ],
)
async def test_runtime_status_is_classified_before_collection(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    terminated: bool,
) -> None:
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    status = {"reason": reason, "message": "runtime could not start the image", "exitCode": 128}
    pod = {
        "metadata": {"uid": "runtime-pod", "name": "worker"},
        "status": {
            "phase": "Failed" if terminated else "Pending",
            "containerStatuses": [
                {
                    "name": k8sspec.CONTAINER_NAME,
                    "state": {"terminated" if terminated else "waiting": status},
                }
            ],
        },
    }
    api.events.append(
        {
            "involvedObject": {"uid": "runtime-pod"},
            "reason": "Failed",
            "message": "runtime event",
            "count": 3,
        }
    )
    monkeypatch.setattr(provider, "_pod_of", AsyncMock(return_value=pod))
    monkeypatch.setattr(provider, "_pending_too_long", lambda _: False)
    if reason == "ImagePullBackOff":
        assert (await provider.observe(handle)).state is ObservationState.RUNNING
    monkeypatch.setattr(provider, "_pending_too_long", lambda _: True)
    observation = await provider.observe(handle)
    assert observation.never_started
    assert observation.container_message == "runtime could not start the image"
    assert observation.pod_events == ({"reason": "Failed", "message": "runtime event", "count": 3},)


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("status", [200, 204, 302, 429, 503])
async def test_readiness_probe_status_and_timeout(
    monkeypatch: pytest.MonkeyPatch, scheme: str, status: int
) -> None:
    event_loop_thread = threading.get_ident()
    connection = MagicMock()
    connection.getresponse.return_value.__enter__.return_value.status = status

    def connect(host: str, port: int, *, timeout: int) -> Any:
        assert threading.get_ident() != event_loop_thread
        assert (host, port, timeout) == ("gateway.example", 4000, 5)
        return connection

    monkeypatch.setattr(
        endpoint_health, "HTTPSConnection" if scheme == "https" else "HTTPConnection", connect
    )
    assert await endpoint_health.probe_model_endpoint(
        f"{scheme}://gateway.example:4000/v1?ignored=true#fragment"
    ) is (status == 200)
    connection.request.assert_called_once_with("GET", "/health/readiness")
    connection.getresponse.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize(
    "error", [ConnectionRefusedError(), ConnectionResetError(), TimeoutError()]
)
async def test_readiness_probe_connection_failure(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    connection = MagicMock()
    connection.request.side_effect = error
    monkeypatch.setattr(endpoint_health, "HTTPConnection", MagicMock(return_value=connection))
    assert not await endpoint_health.probe_model_endpoint("http://gateway.example/v1")
    connection.close.assert_called_once_with()


async def test_readiness_probe_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.getresponse.side_effect = BadStatusLine("invalid response")
    monkeypatch.setattr(endpoint_health, "HTTPConnection", MagicMock(return_value=connection))
    assert not await endpoint_health.probe_model_endpoint("http://gateway.example/v1")
    connection.close.assert_called_once_with()


async def test_endpoint_health_retains_proxy_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    connection.getresponse.return_value.__enter__.return_value.status = 200
    monkeypatch.setattr(endpoint_health, "HTTPConnection", MagicMock(return_value=connection))
    assert await endpoint_health.probe_model_endpoint("http://gateway.example/proxy/v1")
    connection.request.assert_called_once_with("GET", "/proxy/health/readiness")


async def test_endpoint_health_deduplicates_waiting_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, _pending, _uow, _events = _running(monkeypatch)
    monkeypatch.setattr(
        supervisor,
        "_infrastructure_waits",
        lambda: [("one", "fake", "http://gateway"), ("two", "fake", "http://gateway")],
    )
    provider = MagicMock()
    provider.probe_model_endpoint = AsyncMock(return_value=True)
    monkeypatch.setattr(supervisor, "_provider", lambda _: provider)
    recorded = MagicMock()
    monkeypatch.setattr(supervisor, "_record_endpoint_health", recorded)

    async def db(call: Any) -> Any:
        return call()

    monkeypatch.setattr(supervisor, "_db", db)
    await supervisor._resume_infrastructure_waits()
    provider.probe_model_endpoint.assert_awaited_once_with("http://gateway")
    assert recorded.call_count == 2
