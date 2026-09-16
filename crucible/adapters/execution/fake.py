"""Fake execution provider (08): deterministic, no model, no containers.

The behavior is selected by the image tag on the launch spec: `<repo>:fake-<behavior>`
optionally followed by `-<n>` observations before the scripted exit. Behaviors:

- succeed            exit 0 with a valid CompletionClaimV1 report
- succeed-noreport   exit 0 with no report (completed_without_report)
- blocked            exit 75 with blocked.md
- blocked-nofile     exit 75 without blocked.md (a plain failure)
- crash              exit 1, no report
- environment        exit 70
- hang               never exits; ignores drain, dies on kill
- vanish             disappears after launch (loss)

A test may also script a behavior per external_id with `script()`, which wins over
the image tag. Handles survive as long as the provider instance does, which is how the
"supervisor restart mid-attempt" test keeps a worker alive across supervisors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from crucible.ports.execution import (
    CleanupPolicy,
    CollectedOutputs,
    Handle,
    IsolationLevel,
    LaunchSpec,
    LogChunk,
    Observation,
    ObservationState,
    ProviderCapabilities,
    ProviderError,
    Workspace,
)

PROVIDER_NAME = "fake"
_TAG = re.compile(r"^.*:fake-(?P<behavior>[a-z]+(?:-[a-z]+)?)(?:-(?P<n>\d+))?$")

Behavior = Literal[
    "succeed",
    "succeed-noreport",
    "blocked",
    "blocked-nofile",
    "crash",
    "environment",
    "hang",
    "vanish",
    "prepare-fails",
]
BEHAVIORS: frozenset[str] = frozenset(
    {
        "succeed",
        "succeed-noreport",
        "blocked",
        "blocked-nofile",
        "crash",
        "environment",
        "hang",
        "vanish",
        "prepare-fails",
    }
)


@dataclass(slots=True)
class _Worker:
    spec: LaunchSpec
    behavior: str
    remaining: int
    state: ObservationState = ObservationState.RUNNING
    exit_code: int | None = None
    killed: bool = False
    observations: int = 0
    drains: int = 0
    kills: int = 0
    logs: list[LogChunk] = field(default_factory=list)


def default_report(spec: LaunchSpec) -> dict[str, Any]:
    """A valid CompletionClaimV1 for the contract the worker was given."""
    contract = spec.contract
    checks = [
        {
            "id": v["id"],
            "command": v["command"],
            "exit": int(v.get("expect_exit", 0)),
            "log": f"{v['id']}.log",
        }
        for v in contract.get("required_verification", [])
        if v.get("kind", "command") == "command"
    ]
    return {
        "schema_version": "1.0",
        "task_external_id": spec.external_id,
        "summary": f"Fake worker completed {spec.external_id}.",
        "changed_files": ["src/example.py"],
        "refs": {
            "branch": contract.get("repository", {}).get("work_branch", "crucible/unknown"),
            "head_sha": "0" * 40,
            "commits": 1,
        },
        "checks": checks,
        "acceptance_mapping": [
            {"id": c["id"], "status": "met", "evidence": "report/evidence.md"}
            for c in contract.get("acceptance_criteria", [])
        ],
        "run_evidence": ["run-evidence.md"],
        "proposed_pull_request": {
            "title": contract.get("title", "Fake change"),
            "body": "Fake worker output.",
            "closes": [],
        },
        "limitations": [],
        "risks": [],
        "blockers": [],
        "follow_ups": [],
    }


class FakeProvider:
    name = PROVIDER_NAME

    def __init__(self) -> None:
        self._workers: dict[str, _Worker] = {}
        self._scripts: dict[str, tuple[str, int]] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        self._workspaces: dict[str, Workspace] = {}
        self.cleaned: list[str] = []

    # test controls
    def script(self, external_id: str, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self._scripts[external_id] = (behavior, after)

    def set_report(self, external_id: str, report: dict[str, Any]) -> None:
        self._reports[external_id] = report

    def worker(self, attempt_id: str) -> _Worker | None:
        return self._workers.get(attempt_id)

    def _behavior_for(self, spec: LaunchSpec) -> tuple[str, int]:
        scripted = self._scripts.get(spec.external_id)
        if scripted is not None:
            return scripted
        match = _TAG.match(spec.image)
        if match is None:
            raise ProviderError(f"fake provider cannot run image {spec.image!r}")
        behavior = match.group("behavior")
        if behavior not in BEHAVIORS:
            raise ProviderError(f"unknown fake behavior {behavior!r} in image {spec.image!r}")
        return behavior, int(match.group("n") or 1)

    # provider contract
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            isolation=IsolationLevel.NONE,
            network_control=False,
            resource_limits=False,
            shared_disk=True,
            supports_harnesses=frozenset({"claude_code", "codex", "agy"}),
            max_concurrency=16,
        )

    async def prepare(self, spec: LaunchSpec) -> Workspace:
        behavior, _ = self._behavior_for(spec)
        if behavior == "prepare-fails":
            raise ProviderError("fake prepare failure")
        root = f"fake:///workspaces/{spec.attempt_id}"
        ws = Workspace(
            attempt_id=spec.attempt_id,
            checkout_path=f"{root}/repo",
            identity_path=f"{root}/identity",
            report_path=f"{root}/report",
        )
        self._workspaces[spec.attempt_id] = ws
        return ws

    async def launch(self, ws: Workspace, spec: LaunchSpec) -> Handle:
        behavior, after = self._behavior_for(spec)
        worker = _Worker(spec=spec, behavior=behavior, remaining=after)
        worker.logs.append(LogChunk("stdout", f"fake worker {behavior} start\n".encode()))
        self._workers[spec.attempt_id] = worker
        return Handle(provider=self.name, ref=f"fake-{spec.attempt_id}", attempt_id=spec.attempt_id)

    async def observe(self, h: Handle) -> Observation:
        worker = self._workers.get(h.attempt_id)
        if worker is None:
            return Observation(ObservationState.LOST, detail="no such worker")
        if worker.state is not ObservationState.RUNNING:
            return Observation(worker.state, exit_code=worker.exit_code)
        worker.observations += 1
        if worker.behavior == "hang":
            return Observation(ObservationState.RUNNING)
        if worker.observations < worker.remaining:
            return Observation(ObservationState.RUNNING)
        if worker.behavior == "vanish":
            del self._workers[h.attempt_id]
            return Observation(ObservationState.LOST, detail="worker vanished")
        worker.state = ObservationState.EXITED
        worker.exit_code = {
            "succeed": 0,
            "succeed-noreport": 0,
            "blocked": 75,
            "blocked-nofile": 75,
            "crash": 1,
            "environment": 70,
        }[worker.behavior]
        worker.logs.append(LogChunk("stdout", f"fake worker exit {worker.exit_code}\n".encode()))
        return Observation(ObservationState.EXITED, exit_code=worker.exit_code)

    async def logs(self, h: Handle, since: int) -> list[LogChunk]:
        worker = self._workers.get(h.attempt_id)
        return [] if worker is None else worker.logs[since:]

    async def collect(self, h: Handle, ws: Workspace) -> CollectedOutputs:
        worker = self._workers.get(h.attempt_id)
        if worker is None:
            return CollectedOutputs(report=None, report_raw=None, blocked_md=None)
        spec = worker.spec
        if worker.behavior == "succeed" and worker.exit_code == 0:
            report = self._reports.get(spec.external_id) or default_report(spec)
            return CollectedOutputs(report=report, report_raw=None, blocked_md=None)
        if worker.behavior == "blocked" and worker.exit_code == 75:
            return CollectedOutputs(
                report=None,
                report_raw=None,
                blocked_md=f"# Blocked\n\nFake worker for {spec.external_id} needs a decision.\n",
            )
        return CollectedOutputs(report=None, report_raw=None, blocked_md=None)

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None:
        worker = self._workers.get(h.attempt_id)
        if worker is None or worker.state is not ObservationState.RUNNING:
            return
        if mode == "drain":
            worker.drains += 1
            if worker.behavior != "hang":
                worker.state = ObservationState.EXITED
                worker.exit_code = 143
                worker.killed = True
            return
        worker.kills += 1
        worker.state = ObservationState.EXITED
        worker.exit_code = 137
        worker.killed = True

    async def cleanup(self, ws: Workspace, policy: CleanupPolicy) -> None:
        self.cleaned.append(ws.attempt_id)
        self._workspaces.pop(ws.attempt_id, None)

    async def reconcile(self) -> list[Handle]:
        return [
            Handle(provider=self.name, ref=f"fake-{attempt_id}", attempt_id=attempt_id)
            for attempt_id, worker in self._workers.items()
            if worker.state is ObservationState.RUNNING
        ]

    def remove_out_of_band(self, attempt_id: str) -> None:
        """Simulate an operator removing the worker behind Crucible's back."""
        self._workers.pop(attempt_id, None)
