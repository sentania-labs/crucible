"""Infrastructure exits preserve work and wait without evaluating partial work."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import timedelta
from http.client import BadStatusLine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from crucible.adapters.execution import endpoint_health, k8sspec
from crucible.application.corrections import _unpublished_bundle_problem
from crucible.application.supervisor import _Pending
from crucible.domain.events import EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.gates import GateName, GateResult, evaluate_gate
from crucible.domain.infrastructure import model_interruption
from crucible.domain.lifecycle import AttemptState, ExecutionState, TaskState
from crucible.ports.execution import BranchBundle, CollectedOutputs, Observation, ObservationState
from tests.unit.kubernetes_fixtures import build, spec
from tests.unit.test_gates import _ev, _gi, _passing_evidence
from tests.unit.test_routing import _routing_setup


def _running(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, list[Any]]:
    supervisor, pending, uow = _routing_setup(monkeypatch, all_busy=False)
    pending.task.state = TaskState.RUNNING
    pending.execution.state = ExecutionState.ACTIVE
    pending.execution.model = "a-first"
    pending.attempt.state = AttemptState.RUNNING
    pending.attempt.started_at = supervisor._clock.now()
    pending.attempt.selected_model = "a-first"
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
    uow.contracts.get.return_value = MagicMock(document=pending.contract, submitted_at=pending.task.created_at)
    uow.evidence.add.side_effect = append_evidence
    uow.evidence.list_for_attempt.side_effect = lambda attempt_id: [
        row for row in evidence if row.attempt_id == attempt_id
    ]
    uow.attempt_metrics.get.return_value = None
    supervisor._artifacts = MagicMock()
    supervisor.attempt_lease_ttl_seconds = 60
    monkeypatch.setattr(supervisor, "_record_credential_sync", MagicMock())
    monkeypatch.setattr(supervisor, "_release_checkout_leases", MagicMock())
    return supervisor, pending, uow, events


def _finish(supervisor: Any, attempt: Any, message: str, **kwargs: Any) -> None:
    supervisor._finish_exited(
        attempt.id,
        1,
        CollectedOutputs(report=None, report_raw=None, blocked_md=None, stderr_tail=message),
        defer_quota=True,
        **kwargs,
    )


def test_503_mid_run_retries_without_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, events = _running(monkeypatch)
    _finish(supervisor, pending.attempt, "HTTP 503 Service Unavailable from http://gateway/v1")
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert pending.task.state is TaskState.SCHEDULED
    assert pending.task.resume_at == supervisor._clock.now() + timedelta(minutes=3)
    assert pending.execution.max_attempts == 1
    assert len(uow.attempts.list_for_task(pending.task.id)) == 2
    assert supervisor._list_pending() == []
    supervisor._evaluate_pending_gates()
    uow.gate_results.add.assert_not_called()
    assert any("http://gateway/v1" in str(event.payload) for event in events)


def test_429_without_commits_skips_checkpoint_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, _events = _running(monkeypatch)
    _finish(supervisor, pending.attempt, "HTTP 429 Too Many Requests")
    assert pending.attempt.exit_class is ExitClass.QUOTA_EXHAUSTED
    assert not supervisor._quota_checkpoint_pending(pending.attempt.id)
    assert pending.task.state is TaskState.SCHEDULED
    assert supervisor._list_pending() == []


def test_capacity_refusal_excludes_model_and_reroutes_within_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, _events = _running(monkeypatch)
    _finish(supervisor, pending.attempt, "provider error: model is at capacity")
    nxt = uow.attempts.list_for_task(pending.task.id)[-1]
    result = supervisor._selection_for(
        uow, _Pending(nxt, pending.execution, pending.task, pending.contract)
    )
    assert result.selected.id == "b-second"
    assert result.candidates[0]["model"] != "a-first" or not result.candidates[0]["eligible"]


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
    supervisor, pending, uow, events = _running(monkeypatch)
    prior = [
        replace(pending.attempt, id=str(i), exit_class=ExitClass.INFRASTRUCTURE) for i in range(2)
    ]
    uow.attempts.list_for_task.side_effect = lambda *_: [*prior, pending.attempt]
    _finish(supervisor, pending.attempt, "", final_observation=observation)
    assert pending.task.state is TaskState.BLOCKED
    assert pending.attempt.exit_class is ExitClass.INFRASTRUCTURE
    assert _unpublished_bundle_problem(uow, pending.task, "fake") is None
    assert any("mount target is not a directory" in str(event.payload) for event in events)
    assert any(
        row.payload.get("pod_events") for row in uow.evidence.list_for_attempt(pending.attempt.id)
    )
    pending.attempt.logs_drained_at = supervisor._clock.now()
    assert supervisor._list_cleanup_due() == []


def test_real_test_failure_still_fails_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, _events = _running(monkeypatch)
    _finish(supervisor, pending.attempt, "FAILED tests/test_feature.py: AssertionError")
    assert pending.attempt.exit_class is ExitClass.CRASHED
    assert pending.task.state is TaskState.REPORTED
    evidence = [
        row
        for row in _passing_evidence()
        if not (row.kind == "verification_run" and row.payload.get("id") == "V2")
    ]
    evidence.append(_ev("verification_run", {"id": "V2", "exit_code": 1, "ran": True}, ident=21))
    assert evaluate_gate(GateName.VERIFICATION_RAN.value, _gi(evidence)).result is GateResult.FAIL


@pytest.mark.parametrize(
    "message",
    ["HTTP 502 Bad Gateway", "HTTP 504 Gateway Timeout", "connection reset", "connection refused"],
)
def test_transport_interruptions(message: str) -> None:
    assert model_interruption(1, message) is not None
    assert model_interruption(0, message) is None


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


def test_quota_with_commits_still_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, _uow, _events = _running(monkeypatch)
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stderr_tail="HTTP 429 Too Many Requests",
            bundle=BranchBundle("abc", "main", "work", 1, True, sha256="sealed"),
        ),
        defer_quota=True,
    )
    assert supervisor._quota_checkpoint_pending(pending.attempt.id)
    assert pending.task.head_sha == "abc"


async def test_retry_spec_resumes_sealed_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _events = _running(monkeypatch)
    pending.attempt.id = "000previous"
    pending.attempt.workspace_path = "kubernetes://workspace/previous"
    supervisor._finish_exited(
        pending.attempt.id,
        1,
        CollectedOutputs(
            report=None,
            report_raw=None,
            blocked_md=None,
            stderr_tail="HTTP 503 Service Unavailable",
            bundle=BranchBundle("abc", "main", "work", 1, True, sha256="sealed"),
        ),
        defer_quota=True,
    )
    nxt = uow.attempts.list_for_task(pending.task.id)[-1]
    launch = await supervisor._build_spec(nxt, pending.execution, pending.task, pending.contract)
    assert launch.resume_bundle_attempt_id == "000previous"
    assert launch.resume_bundle_head == "abc"
    assert launch.resume_bundle_sha256 == "sealed"
    assert launch.resume_bundle_path == "kubernetes://workspace/previous/output/work_branch.bundle"


async def test_503_collection_does_not_run_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
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
                exit_code=1,
            )
        ),
    )
    monkeypatch.setattr(
        provider, "_worker_tails", AsyncMock(return_value=("", "HTTP 503 Service Unavailable"))
    )
    verifier = AsyncMock()
    monkeypatch.setattr(provider, "_run_verifier", verifier)
    await provider.collect(handle, workspace, launch)
    verifier.assert_not_called()


def test_budget_blocks_once_and_health_recovery_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, pending, uow, events = _running(monkeypatch)
    prior = [
        replace(pending.attempt, id=str(i), exit_class=ExitClass.INFRASTRUCTURE) for i in range(2)
    ]
    uow.attempts.list_for_task.side_effect = lambda *_: [*prior, pending.attempt]
    _finish(supervisor, pending.attempt, "HTTP 503 Service Unavailable")
    assert pending.task.state is TaskState.BLOCKED
    before = len(events)
    supervisor._record_endpoint_health(pending.task.id, False)
    assert len(events) == before
    supervisor._record_endpoint_health(pending.task.id, True)
    assert str(pending.task.state) == TaskState.SCHEDULED.value
    assert pending.task.resume_at is None
    assert sum(event.kind == EventKind.TASK_BLOCKED.value for event in events) == 1


@pytest.mark.parametrize(
    "reason,terminated",
    [
        ("StartError", True),
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


@pytest.mark.parametrize(
    "tail",
    [
        "HTTP 503 Service Unavailable\nFAILED tests/test_feature.py: AssertionError",
        '{"type":"item.completed","item":{"type":"command_execution",'
        '"aggregated_output":"HTTP 503 Service Unavailable"}}',
        'HTTP 503 Service Unavailable\n{"type":"turn.completed"}',
    ],
)
def test_tool_output_and_recovered_calls_do_not_mask_worker_failure(tail: str) -> None:
    assert model_interruption(1, tail) is None


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


def test_fourth_interruption_after_recovery_stays_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, events = _running(monkeypatch)
    attempts = [replace(pending.attempt, id=str(i), exit_class=ExitClass.INFRASTRUCTURE) for i in range(2)]
    attempts.append(pending.attempt)
    uow.attempts.list_for_task.side_effect = lambda *_: attempts
    _finish(supervisor, pending.attempt, "HTTP 503 Service Unavailable")
    supervisor._record_endpoint_health(pending.task.id, True)
    pending.task.state = TaskState.RUNNING
    fourth = replace(pending.attempt, id="fourth", state=AttemptState.RUNNING, exit_class=None)
    attempts.append(fourth)
    uow.attempts.get.return_value = fourth
    _finish(supervisor, fourth, "HTTP 503 Service Unavailable")
    assert pending.task.state is TaskState.BLOCKED
    supervisor._record_endpoint_health(pending.task.id, True)
    assert pending.task.state is TaskState.BLOCKED
    assert supervisor._infrastructure_waits() == []
    assert sum(row.kind == EventKind.WAKE_CREATED.value for row in events) == 1
    uow.escalations.add.assert_called_once()


def test_new_contract_gets_fresh_interruption_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor, pending, uow, _events = _running(monkeypatch)
    old = [replace(pending.attempt, id=str(i), exit_class=ExitClass.INFRASTRUCTURE) for i in range(3)]
    attached = pending.task.created_at + timedelta(seconds=1)
    uow.contracts.get.return_value.submitted_at = attached
    pending.execution.contract_version = 2
    pending.task.contract_version = 2
    pending.attempt.created_at = attached
    uow.attempts.list_for_task.side_effect = lambda *_: [*old, pending.attempt]
    _finish(supervisor, pending.attempt, "HTTP 503 Service Unavailable")
    assert pending.task.state is TaskState.SCHEDULED
    uow.attempts.add.assert_called_once()
    uow.escalations.add.assert_not_called()
