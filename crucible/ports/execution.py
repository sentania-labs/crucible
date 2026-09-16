"""ExecutionProvider contract (08). Same shape for fake, docker, and kubernetes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class IsolationLevel(StrEnum):
    NONE = "none"
    PROCESS = "process"
    CONTAINER = "container"
    POD = "pod"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    isolation: IsolationLevel
    network_control: bool
    resource_limits: bool
    shared_disk: bool
    supports_harnesses: frozenset[str]
    max_concurrency: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "isolation": self.isolation.value,
            "network_control": self.network_control,
            "resource_limits": self.resource_limits,
            "shared_disk": self.shared_disk,
            "supports_harnesses": sorted(self.supports_harnesses),
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """What a provider runs. Env carries names only, never secret values (07)."""

    attempt_id: str
    task_id: str
    external_id: str
    role: str
    harness: str
    model: str
    image: str
    timeout_seconds: int
    contract: dict[str, Any]
    env: dict[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    network: Literal["policy", "none"] = "policy"


@dataclass(frozen=True, slots=True)
class Workspace:
    attempt_id: str
    checkout_path: str
    identity_path: str
    report_path: str
    checkout_lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class Handle:
    provider: str
    ref: str
    attempt_id: str


class ObservationState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class Observation:
    state: ObservationState
    exit_code: int | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: Literal["stdout", "stderr"]
    content: bytes


@dataclass(frozen=True, slots=True)
class CollectedArtifact:
    """One file the collector copied out of the workspace. Bytes are data, never code."""

    name: str
    type: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class BranchBundle:
    """What `git bundle create base_ref..work_branch` plus `git bundle verify` produced.

    The Docker collector builds this in C3; the fake provider synthesizes it so the
    gate path is exercised end to end (08)."""

    head_sha: str
    base_ref: str
    work_branch: str
    commits: int
    verified: bool
    commit_paths: tuple[str, ...] = ()
    commit_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectedOutputs:
    """What the collector produced (08): the report directory, the diff path list, the
    branch bundle summary, and the artifacts copied out of the workspace."""

    report: dict[str, Any] | None
    report_raw: str | None
    blocked_md: str | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    diff_paths: tuple[str, ...] = ()
    bundle: BranchBundle | None = None
    artifacts: tuple[CollectedArtifact, ...] = ()


class CleanupPolicy(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    KEEP_DIFF_ONLY = "keep_diff_only"


class ProviderError(Exception):
    """A provider failed before or while the harness ran (exit class environment)."""


class ExecutionProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    async def prepare(self, spec: LaunchSpec) -> Workspace: ...

    async def launch(self, ws: Workspace, spec: LaunchSpec) -> Handle: ...

    async def observe(self, h: Handle) -> Observation: ...

    async def logs(self, h: Handle, since: int) -> list[LogChunk]: ...

    async def collect(self, h: Handle, ws: Workspace) -> CollectedOutputs: ...

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None: ...

    async def cleanup(self, ws: Workspace, policy: CleanupPolicy) -> None: ...

    async def reconcile(self) -> list[Handle]: ...
