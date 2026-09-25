"""Issue 128 through the supervisor: the launch carries the resolved per-command
timeout, submission holds a contract's value to the policy's bounds, and a harness
that exits with work still in flight is recorded `incomplete`, not completed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.harness.base import TRANSCRIPT_NAME
from crucible.adapters.harness.hermes import PROCESSES_NAME
from crucible.ports.execution import CollectedOutputs, Handle, LaunchSpec, Workspace
from tests.fixtures import contract_document
from tests.integration.conftest import make_supervisor, run_to_settled, submit_and_start

pytestmark = pytest.mark.integration

# What each harness's own tooling leaves behind when it exits with a command running:
# a Claude Code background task that never ended, a Codex command item never completed
# (both are transcript lines), and a Hermes process registry that still lists one. The
# fake provider picks the harness, so the report carries all three.
IN_FLIGHT_TRANSCRIPT = [
    {"type": "system", "subtype": "task_started", "task_id": "b1", "is_backgrounded": True},
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution"}},
    {"type": "result", "subtype": "success"},
]


class InFlightProvider(FakeProvider):
    """A fake whose workspaces are real directories, so the adapter reads the report."""

    def __init__(self, root: Path, only_image: str | None = None) -> None:
        super().__init__()
        self.root = root
        # When set, only attempts launched from this image leave work in flight.
        self.only_image = only_image

    async def prepare(self, spec: LaunchSpec) -> Workspace:
        base = self.root / spec.attempt_id
        (base / "repo").mkdir(parents=True)
        ws = Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=str(base / "repo"),
            identity_path=str(base / "identity"),
            report_path=str(base / "report"),
        )
        self._workspaces[spec.attempt_id] = ws
        return ws

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs:
        if self.only_image is not None and (spec is None or spec.image != self.only_image):
            return await super().collect(h, ws, spec)
        report = Path(ws.checkout_path).parent / "output" / "report"
        report.mkdir(parents=True, exist_ok=True)
        (report / TRANSCRIPT_NAME).write_text(
            "\n".join(json.dumps(line) for line in IN_FLIGHT_TRANSCRIPT) + "\n",
            encoding="utf-8",
        )
        (report / PROCESSES_NAME).write_text(
            json.dumps([{"session_id": "proc_1", "command": "make test"}]), encoding="utf-8"
        )
        return await super().collect(h, ws, spec)


def _attempts(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    view = client.get(f"/v1/tasks/{task_id}").json()
    return [a for e in view["executions"] for a in e["attempts"]]


async def test_the_launch_carries_the_contracts_command_timeout(
    client: TestClient, ctx: AppContext, provider: FakeProvider
) -> None:
    supervisor = make_supervisor(ctx, provider)
    request = {**contract_document()["execution_request"], "command_timeout_ms": 600_000}
    narrowed = submit_and_start(
        client, "crucible-worker:fake-succeed", "EX-0128A", execution_request=request
    )
    default = submit_and_start(client, "crucible-worker:fake-succeed", "EX-0128B")
    await run_to_settled(supervisor, client, narrowed)
    await run_to_settled(supervisor, client, default)
    specs = {
        worker.spec.external_id: worker.spec
        for attempt in (*_attempts(client, narrowed), *_attempts(client, default))
        if (worker := provider.worker(str(attempt["id"]))) is not None
    }
    assert specs["EX-0128A"].command_timeout_ms == 600_000
    # The policy predates the field: the default of 60 minutes, within the 3600 s attempt.
    assert specs["EX-0128B"].command_timeout_ms == 3_600_000


def test_submission_holds_the_command_timeout_to_the_policy_bounds(client: TestClient) -> None:
    doc = contract_document(external_id="EX-0128C")
    doc["repository"]["work_branch"] = "crucible/EX-0128C"
    doc["execution_request"]["command_timeout_ms"] = 500
    refused = client.post("/v1/tasks", json=doc)
    assert refused.status_code == 422
    paths = {e["path"] for e in refused.json()["errors"]}
    assert "execution_request.command_timeout_ms" in paths
    doc["execution_request"]["command_timeout_ms"] = 3_600_001
    over = client.post("/v1/tasks", json=doc)
    assert over.status_code == 422
    assert "must not exceed timeout_seconds" in over.text


async def test_a_harness_that_exits_with_work_in_flight_is_incomplete(
    client: TestClient, ctx: AppContext, tmp_path: Path
) -> None:
    provider = InFlightProvider(tmp_path)
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-0128D")
    await run_to_settled(supervisor, client, task_id)
    (attempt,) = _attempts(client, task_id)
    view = client.get(f"/v1/tasks/{task_id}").json()
    harness = view["executions"][0]["harness"]
    if harness == "agy":
        pytest.skip("AGY leaves no in-flight evidence to read (its adapter says why)")
    assert attempt["exit_class"] == "incomplete", harness
    assert attempt["state"] == "failed"
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    collected = next(e for e in events if e["kind"] == "attempt_collected")
    assert collected["payload"]["work_in_flight"]
    # The report is valid, yet exit_clean must not pass on the zero exit code alone.
    gates = client.get(f"/v1/attempts/{attempt['id']}/gates").json()["items"]
    assert all(g["result"] != "pass" for g in gates if g["gate"] == "exit_clean")


async def test_a_review_that_exits_with_work_in_flight_is_not_recorded(
    client: TestClient, ctx: AppContext, tmp_path: Path
) -> None:
    """A review worker that exits 0 with a valid ReviewReportV1 but a command still
    running did not finish: no review is recorded and the attempt fails."""
    provider = InFlightProvider(tmp_path, only_image="crucible-worker:fake-review")
    supervisor = make_supervisor(ctx, provider)
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", "EX-0128E")
    assert await run_to_settled(supervisor, client, task_id) == "awaiting_internal_review"
    review = {
        "harness": "codex",
        "model": "gpt-5.6-luna",
        "provider": "fake",
        "image": "crucible-worker:fake-review",
        "timeout_seconds": 600,
        "rationale": "A non-author review of the collected head.",
    }
    r = client.post(f"/v1/tasks/{task_id}/review", json={"execution": review})
    assert r.status_code == 200, r.text
    assert await run_to_settled(supervisor, client, task_id, max_ticks=8) == (
        "awaiting_internal_review"
    )
    view = client.get(f"/v1/tasks/{task_id}").json()
    roles = {e["role"]: e for e in view["executions"]}
    assert roles["review"]["state"] == "failed"
    (attempt,) = roles["review"]["attempts"]
    assert attempt["exit_code"] == 0
    assert attempt["exit_class"] == "incomplete"
    assert attempt["state"] == "failed"
    assert view["review_reports"] == []
    events = client.get(f"/v1/tasks/{task_id}/events").json()["items"]
    assert "review_report_recorded" not in {e["kind"] for e in events}
