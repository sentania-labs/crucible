"""A launch runs beside the tick, and a cancel stops it (hades #189, #190).

On v0.6.3 the supervisor waited on one attempt's cache refresher for four and a half
minutes: its lease expired, readiness failed and every other task waited. A cancel sent
during that wait was honoured only after the worker had started. Here a prepare is held
open by the fake provider the way that refresher was.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import event_kinds, make_supervisor, run_to_settled

pytestmark = pytest.mark.integration

CANCEL = {"reason": "wrong repository", "verbatim": "cancel HT-0003", "decided_by": "scott"}
PINS = {"codex": "gpt-5.6-luna", "claude_code": "claude-sonnet-5"}


@pytest.fixture
def client(client: TestClient, tokens: dict[str, str]) -> TestClient:
    """Pinned tasks are an operator's to submit and cancel (05)."""
    client.headers["Authorization"] = f"Bearer {tokens['operator']}"
    return client


def _submit(client: TestClient, external_id: str, harness: str = "codex") -> str:
    document = contract_document(external_id=external_id)
    document["repository"]["work_branch"] = f"crucible/{external_id}"
    document["execution_request"].update(
        {
            "harness": harness,
            "model": PINS[harness],
            "pin_reason": "launch isolation integration test",
            "image": "crucible-worker:fake-succeed",
        }
    )
    response = client.post("/v1/tasks", json=document)
    assert response.status_code == 201, response.text
    task_id = str(response.json()["id"])
    response = client.post(
        f"/v1/tasks/{task_id}/start",
        json={"provider": "fake", "image": "crucible-worker:fake-succeed", "policy_version": 2},
    )
    assert response.status_code == 200, response.text
    return task_id


def _attempt(client: TestClient, task_id: str) -> dict[str, Any]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return dict(view["executions"][0]["attempts"][-1])


def _collected(client: TestClient, task_id: str) -> dict[str, Any]:
    events = client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()["items"]
    return dict(next(e for e in events if e["kind"] == "attempt_collected")["payload"])


async def test_a_slow_prepare_holds_back_neither_the_lease_nor_another_launch(
    ctx: AppContext, client: TestClient, provider: FakeProvider, clock: FakeClock
) -> None:
    """hades #190: a prepare that hangs past the lease TTL leaves the lease renewed, the
    supervisor healthy, and another task launching beside it."""
    supervisor = make_supervisor(ctx, provider, launch_wait_seconds=0.2)
    slow = _submit(client, "HT-SLOW", "codex")
    other = _submit(client, "HT-OTHER", "claude_code")
    provider.hold_prepare("HT-SLOW")

    started = time.monotonic()
    first = await supervisor.tick()
    assert time.monotonic() - started < 5
    assert first.held and first.launched == 1
    assert _attempt(client, slow)["state"] == "preparing"
    assert _attempt(client, other)["started_at"] is not None

    # Past the lease TTL, twice over, with the prepare still held.
    for _ in range(2):
        clock.advance(31)
        result = await supervisor.tick()
        assert result.held
        status = client.get("/v1/supervisor").json()
        assert status["healthy"] is True, status
        assert status["lease"]["holder"] == "sup-a"
    # Still this process's to finish: not reconciled as stranded.
    assert _attempt(client, slow)["state"] == "preparing"
    assert "attempt_collected" not in event_kinds(client, slow)

    provider.release_prepare("HT-SLOW")
    assert await run_to_settled(supervisor, client, other) == "awaiting_internal_review"
    assert await run_to_settled(supervisor, client, slow) == "awaiting_internal_review"
    assert provider.worker(str(_attempt(client, slow)["id"])) is not None
    await supervisor.stop()


async def test_a_cancel_during_a_slow_prepare_starts_no_worker(
    ctx: AppContext, client: TestClient, provider: FakeProvider
) -> None:
    """hades #189: the task is cancelled in the tick that sees the cancel, the prepare
    stops where it is, and no worker is ever started."""
    supervisor = make_supervisor(ctx, provider, launch_wait_seconds=0.2)
    task_id = _submit(client, "HT-CANCEL")
    provider.hold_prepare("HT-CANCEL")
    await supervisor.tick()
    attempt = _attempt(client, task_id)
    assert attempt["state"] == "preparing"

    response = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
    assert response.status_code == 200 and response.json()["state"] == "cancelling"
    await supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "cancelled"
    attempt = _attempt(client, task_id)
    assert attempt["state"] == "failed" and attempt["exit_class"] == "killed"
    assert provider.worker(str(attempt["id"])) is None
    assert provider.prepare_cancelled == [attempt["id"]]
    collected = _collected(client, task_id)
    assert collected["reason"] == "task cancelled during launch"
    assert collected["stage"] == "prepare"
    assert "attempt_running" not in event_kinds(client, task_id)
    await supervisor.stop()


async def test_a_cancel_after_the_prepare_starts_no_worker(
    ctx: AppContext, client: TestClient, provider: FakeProvider
) -> None:
    """hades #189: the last look is in the transaction that would move the attempt to
    launching, so a cancel that lands after the checkout is built still starts nothing."""
    supervisor = make_supervisor(ctx, provider, launch_wait_seconds=0.2)
    task_id = _submit(client, "HT-CANCEL-LATE")
    original = provider.prepare

    async def cancel_once_prepared(spec: Any, checkout_token: Any = None, cancelled: Any = None):  # type: ignore[no-untyped-def]
        workspace = await original(spec, checkout_token, cancelled)
        response = client.post(f"/v1/tasks/{task_id}/cancel", json=CANCEL)
        assert response.status_code == 200, response.text
        return workspace

    provider.prepare = cancel_once_prepared  # type: ignore[method-assign]
    await supervisor.tick()

    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "cancelled"
    attempt = _attempt(client, task_id)
    assert attempt["state"] == "failed" and attempt["exit_class"] == "killed"
    assert provider.worker(str(attempt["id"])) is None
    assert _collected(client, task_id)["stage"] == "launch"
    assert attempt["id"] in provider.discarded
    await supervisor.stop()


async def test_stopping_the_supervisor_ends_a_launch_in_flight(
    ctx: AppContext, client: TestClient, provider: FakeProvider, clock: FakeClock
) -> None:
    """A supervisor that stops mid-launch cancels its launch, and the next one finds the
    attempt stranded in preparing and retries it, as after a crash (10, 16)."""
    supervisor = make_supervisor(ctx, provider, launch_wait_seconds=0.2)
    task_id = _submit(client, "HT-STOP")
    provider.hold_prepare("HT-STOP")
    await supervisor.tick()
    assert _attempt(client, task_id)["state"] == "preparing"
    await supervisor.stop()
    provider.release_prepare("HT-STOP")
    clock.advance(31)
    successor = make_supervisor(ctx, provider, holder="sup-b")
    assert await run_to_settled(successor, client, task_id) == "awaiting_internal_review"
    assert "task_retry_scheduled" in event_kinds(client, task_id)
    await successor.stop()


async def test_a_supervisor_that_lost_its_lease_abandons_its_launches(
    ctx: AppContext, client: TestClient, provider: FakeProvider, clock: FakeClock
) -> None:
    """Review of hades #190: the usual lease loss is a renewal that fails at the top of
    a tick. The launch begun under the old lease is cancelled then, so it neither keeps
    preparing for an attempt the new holder owns nor fails a later tick of its own."""
    first = make_supervisor(ctx, provider, launch_wait_seconds=0.2)
    task_id = _submit(client, "HT-LEASE")
    provider.hold_prepare("HT-LEASE")
    await first.tick()
    stranded = str(_attempt(client, task_id)["id"])
    assert first._launches, "the prepare should still be held"

    clock.advance(31)
    successor = make_supervisor(ctx, provider, holder="sup-b", launch_wait_seconds=0.2)
    assert (await successor.tick()).held
    assert client.get(f"/v1/attempts/{stranded}").json()["state"] == "failed"
    result = await first.tick()
    assert result.held is False
    assert not first._launches
    provider.release_prepare("HT-LEASE")
    assert provider.worker(stranded) is None
    assert await run_to_settled(successor, client, task_id) == "awaiting_internal_review"
    await successor.stop()
