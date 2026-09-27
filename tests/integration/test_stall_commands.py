"""Issue 152 through the supervisor: a silent command longer than the stall limit but
inside its command timeout is not a stall, and a worker with nothing in flight and no
output still stalls out.

The worker is the fake provider's `hang`, scripted with the lines each harness writes
while a command runs: the log is the only live evidence the supervisor has, on Docker
and Kubernetes alike."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.application.supervisor import Supervisor
from crucible.ports.execution import LogChunk
from tests.fixtures import FakeClock, contract_document
from tests.integration.conftest import event_kinds

pytestmark = pytest.mark.integration

# The seeded policy's stall limits, and a command running longer than the fail limit
# but well inside the 60-minute default command timeout.
STALL_WARN = 300
STALL_FAIL = 1800
COMMAND_SECONDS = 2400
TICK = 50

CLAUDE_BASH = {
    "type": "assistant",
    "message": {
        "id": "msg_1",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "make e2e"}}
        ],
    },
    "parent_tool_use_id": None,
}
CLAUDE_RESULT = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]},
}
CODEX_STARTED = {
    "type": "item.started",
    "item": {"id": "item_1", "type": "command_execution", "command": "make e2e"},
}
CODEX_COMPLETED = {
    "type": "item.completed",
    "item": {"id": "item_1", "type": "command_execution", "command": "make e2e"},
}

# What each harness writes when its command starts and when it ends.
SCRIPTS: dict[str, tuple[LogChunk, LogChunk]] = {
    "claude_code": (
        LogChunk("stdout", (json.dumps(CLAUDE_BASH) + "\n").encode()),
        LogChunk("stdout", (json.dumps(CLAUDE_RESULT) + "\n").encode()),
    ),
    "codex": (
        LogChunk("stdout", (json.dumps(CODEX_STARTED) + "\n").encode()),
        LogChunk("stdout", (json.dumps(CODEX_COMPLETED) + "\n").encode()),
    ),
    "hermes": (
        LogChunk("stderr", b"crucible-launch: commands running: 1\n"),
        LogChunk("stderr", b"crucible-launch: commands running: 0\n"),
    ),
}


# One model each harness serves in the seeded routing policy.
MODELS = {
    "claude_code": "claude-sonnet-5",
    "codex": "gpt-5.6-luna",
    "hermes": "gpt-oss:120b",
    "agy": "gemini-3.8-flash-low",
}


@pytest.fixture
def operator(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    """Pinning a harness is the operator's (05)."""
    with TestClient(
        create_app(ctx), headers={"Authorization": f"Bearer {tokens['operator']}"}
    ) as c:
        yield c


def local_endpoint_policy(client: TestClient, admin: TestClient) -> int:
    """Hermes serves only a local endpoint, which the seeded routing leaves unset; the
    same setup as the local pool cap test, one version on."""
    routing = client.get("/v1/routing/default-routing/4").json()["document"]
    routing["version"] = 152
    hermes = next(model for model in routing["models"] if model["harness"] == "hermes")
    hermes.update(
        {"endpoint_url": "http://192.0.2.41:11434/v1", "enabled": True, "disabled_reason": None}
    )
    assert admin.put("/v1/routing/default-routing/152", json=routing).status_code == 200
    policy = client.get("/v1/policies/default-software/4").json()["document"]
    policy["version"] = 152
    policy["routing"]["policy"]["version"] = 152
    assert admin.put("/v1/policies/default-software/152", json=policy).status_code == 200
    limits = policy["limits"]
    assert (limits["stall_warn_seconds"], limits["stall_fail_seconds"]) == (STALL_WARN, STALL_FAIL)
    return 152


def start(
    client: TestClient, harness: str, external_id: str, policy_version: int | None = None
) -> str:
    doc = contract_document(external_id=external_id)
    if policy_version is not None:
        doc["policy"] = {"name": "default-software", "version": policy_version}
    doc["repository"]["work_branch"] = f"crucible/{external_id}"
    doc["execution_request"]["image"] = "crucible-worker:fake-hang"
    # Long enough that only a stall can end the attempt inside the test's window.
    doc["execution_request"]["timeout_seconds"] = 14400
    doc["execution_request"]["harness"] = harness
    doc["execution_request"]["model"] = MODELS[harness]
    doc["execution_request"]["pin_reason"] = "Issue 152 needs this harness's live evidence."
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 201, r.text
    task_id: str = r.json()["id"]
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "provider": "fake",
            "image": "crucible-worker:fake-hang",
            "policy_version": policy_version or 2,
        },
    )
    assert r.status_code == 200, r.text
    return task_id


async def advance(supervisor: Supervisor, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        await supervisor.tick()


@pytest.mark.parametrize("harness", sorted(SCRIPTS))
async def test_a_silent_command_past_the_stall_limit_is_not_a_stall(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    ctx: AppContext,
    tokens: dict[str, str],
    harness: str,
) -> None:
    client = operator
    policy_version = None
    if harness == "hermes":
        admin_headers = {"Authorization": f"Bearer {tokens['admin']}"}
        with TestClient(create_app(ctx), headers=admin_headers) as admin:
            policy_version = local_endpoint_policy(client, admin)
    external_id = f"EX-0152-{harness.upper().replace('_', '')}"
    task_id = start(client, harness, external_id, policy_version)
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    command_started, command_ended = SCRIPTS[harness]
    worker.logs.append(command_started)

    await advance(supervisor, clock, COMMAND_SECONDS)
    assert worker.drains == 0 and worker.kills == 0
    kinds = event_kinds(client, task_id)
    assert "worker_quiet" not in kinds and "worker_stalled" not in kinds
    attempt = client.get(f"/v1/attempts/{attempt_id}").json()
    assert attempt["state"] == "running"
    assert attempt["heartbeat_summary"]["state"] == "alive"
    with ctx.uow_factory() as uow:
        signals = [h for h in uow.heartbeats.list_for_attempt(attempt_id, limit=10_000)]
    running = [h for h in signals if h.signal == "command_running"]
    # Refreshed about once a minute, not once a tick.
    assert COMMAND_SECONDS // 120 <= len(running) <= COMMAND_SECONDS // 60 + 1
    assert running[0].detail["count"] == 1

    # The command ends and the worker falls silent: the clock runs again from there.
    worker.logs.append(command_ended)
    await advance(supervisor, clock, STALL_WARN + TICK)
    assert event_kinds(client, task_id).count("worker_quiet") == 1
    await advance(supervisor, clock, STALL_FAIL - STALL_WARN)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"
    assert "worker_stalled" in event_kinds(client, task_id)


@pytest.mark.parametrize("harness", ["agy", "claude_code"])
async def test_a_worker_with_nothing_in_flight_and_no_output_still_stalls(
    operator: TestClient,
    supervisor: Supervisor,
    provider: FakeProvider,
    clock: FakeClock,
    harness: str,
) -> None:
    """Claude Code with no command open, and AGY, which gives no live evidence at all:
    both stall at the limit as before (05b)."""
    client = operator
    task_id = start(client, harness, f"EX-0152-IDLE-{harness.upper().replace('_', '')}")
    await supervisor.tick()
    attempt_id = client.get(f"/v1/tasks/{task_id}").json()["latest_attempt"]["id"]
    worker = provider.worker(attempt_id)
    assert worker is not None
    await advance(supervisor, clock, STALL_FAIL - TICK)
    assert worker.drains == 0
    assert event_kinds(client, task_id).count("worker_quiet") == 1
    await advance(supervisor, clock, TICK)
    assert worker.drains == 1
    assert client.get(f"/v1/attempts/{attempt_id}").json()["termination_reason"] == "stall"
