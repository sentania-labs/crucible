"""Every failure class the fake provider can produce (16), with the retry rule."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from tests.fixtures import FakeClock
from tests.integration.conftest import event_kinds, run_until, submit_and_start

pytestmark = pytest.mark.integration


def _attempts(client: TestClient, task_id: str) -> list[dict[str, object]]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return [a for e in view["executions"] for a in e["attempts"]]


async def test_crash_no_retry(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-crash")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    attempts = _attempts(client, task_id)
    assert len(attempts) == 1
    assert attempts[0]["exit_class"] == "crashed" and attempts[0]["state"] == "failed"
    kinds = event_kinds(client, task_id)
    assert "execution_failed" in kinds and "task_retry_scheduled" not in kinds


async def test_completed_without_report(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed-noreport")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "completed_without_report" and attempt["state"] == "failed"
    assert "report_parsed" not in event_kinds(client, task_id)


async def test_blocked_exit_75(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked")
    assert await run_until(supervisor, client, task_id, {"blocked"}) == "blocked"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "blocked" and attempt["state"] == "blocked"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    blocked = next(e for e in events if e["kind"] == "task_blocked")
    assert "needs a decision" in blocked["payload"]["blocked_md"]
    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["executions"][0]["state"] == "active"


async def test_exit_75_without_blocked_md_is_failure(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-blocked-nofile")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "crashed"


async def test_environment_retries_then_reports(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-environment")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    attempts = _attempts(client, task_id)
    assert [a["number"] for a in attempts] == [1, 2]
    assert all(a["exit_class"] == "environment" for a in attempts)
    kinds = event_kinds(client, task_id)
    assert kinds.count("task_retry_scheduled") == 1
    assert kinds.count("attempt_created") == 2
    assert kinds.index("task_retry_scheduled") < kinds.index("execution_failed")


async def test_lost_retries_when_contract_allows(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-vanish-2")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    attempts = _attempts(client, task_id)
    assert len(attempts) == 2 and all(a["exit_class"] == "lost" for a in attempts)
    assert event_kinds(client, task_id).count("attempt_lost") == 2


async def test_lost_no_retry_when_contract_excludes_it(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-vanish-2",
        lifecycle={"max_attempts": 3, "retry_on": ["environment"], "cleanup": "policy"},
    )
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    assert len(_attempts(client, task_id)) == 1


async def test_prepare_failure_is_environment(client: TestClient, supervisor: Supervisor) -> None:
    task_id = submit_and_start(
        client,
        "crucible-worker:fake-prepare-fails",
        lifecycle={"max_attempts": 1, "retry_on": [], "cleanup": "policy"},
    )
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "environment"
    kinds = event_kinds(client, task_id)
    assert "attempt_preparing" in kinds and "attempt_launching" not in kinds
    assert "task_running" in kinds


async def test_timeout_drains_then_kills(
    client: TestClient, supervisor: Supervisor, clock: FakeClock, provider: FakeProvider
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-hang")
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "running"
    await supervisor.tick()
    (attempt,) = _attempts(client, task_id)
    worker = provider.worker(str(attempt["id"]))
    assert worker is not None and worker.drains == 0
    clock.advance(3600)
    await supervisor.tick()
    assert worker.drains == 1 and worker.kills == 0
    await supervisor.tick()
    assert worker.kills == 0, "still inside the grace window"
    clock.advance(60)
    await supervisor.tick()
    assert worker.kills == 1
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    (attempt,) = _attempts(client, task_id)
    assert attempt["exit_class"] == "timeout" and attempt["exit_code"] == 137
    kinds = event_kinds(client, task_id)
    assert (
        kinds.index("attempt_timeout_drain")
        < kinds.index("attempt_timeout_kill")
        < kinds.index("attempt_exited")
    )
    assert "task_retry_scheduled" not in kinds


async def test_report_with_secret_is_redacted(
    client: TestClient, supervisor: Supervisor, provider: FakeProvider
) -> None:
    from crucible.adapters.execution.fake import default_report  # noqa: PLC0415
    from crucible.ports.execution import LaunchSpec  # noqa: PLC0415
    from tests.fixtures import contract_document  # noqa: PLC0415

    spec = LaunchSpec(
        attempt_id="x",
        task_id="t",
        external_id="EX-0001",
        harness="codex",
        model="m",
        image="i",
        timeout_seconds=1,
        contract=contract_document(),
    )
    report = default_report(spec)
    report["summary"] = "pushed with ghp_" + "k" * 36
    provider.set_report("EX-0001", report)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    assert await run_until(supervisor, client, task_id, {"reported"}) == "reported"
    (attempt,) = _attempts(client, task_id)
    stored = client.get(f"/v1/attempts/{attempt['id']}").json()["report"]
    assert stored["parsed_ok"] is False and stored["document"] == {"redacted": True}
    assert "kkkk" not in client.get(f"/v1/attempts/{attempt['id']}").text
    assert attempt["exit_class"] == "completed_without_report"
