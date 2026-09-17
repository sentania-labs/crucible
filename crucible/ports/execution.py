"""ExecutionProvider contract (08). Same shape for fake, docker, and kubernetes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

# Where the workspace appears inside every Crucible-created container (06, 08).
REPO_MOUNT = "/crucible/repo"
IDENTITY_MOUNT = "/crucible/identity"
REPORT_MOUNT = "/crucible/report"
OUTPUT_MOUNT = "/crucible/out"
VERIFY_MOUNT = "/crucible/verify"


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
    # The policy snapshot the execution was created with (05b): resources, network
    # allowlist, git author identity, image allowlist, cleanup.
    policy: dict[str, Any] = field(default_factory=dict)
    # Who the work is for, as the `crucible.owner` container label (08).
    owner: str = "crucible"
    # The registered repository's clone url. It lives on the Repository row, not in
    # the contract, so the supervisor puts it here for `prepare` (03, 08).
    repository_url: str = ""


@dataclass(frozen=True, slots=True)
class Workspace:
    attempt_id: str
    checkout_path: str
    identity_path: str
    report_path: str
    checkout_lease_id: str | None = None
    # What the collector writes into, and the hash of the rendered identity bundle (06).
    output_path: str | None = None
    identity_sha256: str | None = None
    # Which branch the checkout started from, and whether it came from the remote
    # work_branch head (a correction or a retry of published work) or from base_ref (08).
    work_branch: str | None = None
    started_from: str | None = None


@dataclass(frozen=True, slots=True)
class Handle:
    provider: str
    ref: str
    attempt_id: str
    # Resolved at launch and recorded on the attempt (08, 13).
    image_digest: str | None = None
    name: str | None = None


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
class LogOffset:
    """Where a log pull resumes (10).

    Docker has no byte offsets, so the position is the last stored line's timestamp
    and its sha256. `--since` is inclusive (S8), so the pull asks for lines at or after
    the timestamp and skips until the hash matches: strict-after, never by timestamp
    alone. `index` is what an in-memory provider counts with."""

    index: int = 0
    timestamp: str | None = None
    line_sha256: str | None = None

    @property
    def is_start(self) -> bool:
        return self.index == 0 and self.timestamp is None


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: Literal["stdout", "stderr"]
    content: bytes
    ts: datetime | None = None
    # The sha256 of the last line in `content`, which together with `ts` is the resume
    # position for the next pull (10).
    line_sha256: str | None = None
    lines: int = 0


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
class VerificationRun:
    """One `required_verification` command Crucible itself re-ran after exit, in a fresh
    verifier container from the collected tree (11). The worker's own log is a claim;
    this is the evidence."""

    id: str
    command: str
    expect_exit: int
    exit_code: int
    log_tail: str
    ran: bool = True
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.ran and self.exit_code == self.expect_exit


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """What `workspace_clean` reads (11): nothing labelled for this attempt is left
    behind once the collector and the verifier have been removed."""

    leftover: tuple[str, ...] = ()
    checked: bool = True
    detail: str = ""


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
    # The full diff against base_ref. The secret scanner needs the content, not only the
    # path list (11), so a collector that cannot produce it leaves this None and the
    # no_secrets gate refuses to report `pass`.
    diff_text: str | None = None
    bundle: BranchBundle | None = None
    artifacts: tuple[CollectedArtifact, ...] = ()
    # The verifier container's re-run of every required_verification command, and the
    # provider's own answer to "is anything of this attempt still running" (11).
    verifications: tuple[VerificationRun, ...] = ()
    workspace_state: WorkspaceState | None = None
    # Rejections the collector made while copying the report directory out: symlinks,
    # hard links, devices, and files above the size cap (08). Each becomes an event.
    copy_rejections: tuple[dict[str, str], ...] = ()


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

    async def logs(self, h: Handle, since: LogOffset) -> list[LogChunk]: ...

    async def collect(
        self, h: Handle, ws: Workspace, spec: LaunchSpec | None = None
    ) -> CollectedOutputs: ...

    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None: ...

    async def cleanup(self, ws: Workspace, policy: CleanupPolicy) -> None: ...

    async def reconcile(self) -> list[Handle]: ...

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove provider-side leavings for attempts that are gone (16). Returns the
        count removed. A provider with nothing to remove returns 0."""
        ...
