"""ExecutionProvider contract (08). Same shape for fake, docker, and kubernetes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from crucible.ports.endpoints import validate_endpoint

# Where the workspace appears inside every Crucible-created container (06, 08).
REPO_MOUNT = "/crucible/repo"
IDENTITY_MOUNT = "/crucible/identity"
REPORT_MOUNT = "/crucible/report"
OUTPUT_MOUNT = "/crucible/out"
# The whole workspace, mounted only into the preparer so git itself creates the
# checkout directory and owns it (S9 Test E: the uid the daemon gives a container is
# not the uid a bind source on the host already has).
WORK_MOUNT = "/crucible/work"
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
    # The harness adapter's launch shape (07). `env_from_files` maps a variable name to
    # a container path the provider resolves at container start: the one way a value
    # from a credential file reaches the harness environment, never through `Env`.
    env_from_files: dict[str, str] = field(default_factory=dict)
    # What the harness reads on stdin: these files, in order, then this text.
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    # Where the provider tees the harness's stdout, so the transcript is an artifact.
    transcript_path: str | None = None
    effort: str | None = None
    endpoint: Literal["subscription", "local"] = "subscription"
    endpoint_url: str | None = None

    def __post_init__(self) -> None:
        validate_endpoint(self.endpoint, self.endpoint_url)


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
    # S5: 137 with the kernel's OOM kill is an environment failure, not a crash and not
    # a kill Crucible sent. Carried as a flag so classification never parses `detail`.
    oom_killed: bool = False


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
    # Which line at that timestamp the boundary is, counting from 0. Lines can repeat
    # inside one instant, so the hash alone does not say which one was last seen.
    occurrence: int = 0

    @property
    def is_start(self) -> bool:
        return self.index == 0 and self.timestamp is None


@dataclass(frozen=True, slots=True)
class LogChunk:
    stream: Literal["stdout", "stderr"]
    content: bytes
    ts: datetime | None = None
    # The sha256 of the last line in `content` and which line at `ts` it is. The three
    # together are the resume position for the next pull (10).
    line_sha256: str | None = None
    occurrence: int = 0
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
    # What the sync-back of the per-attempt credential copy did, when there was one (12).
    credential_sync: CredentialSync | None = None
    # A quota checkpoint collector can refuse worker-controlled Git metadata before it
    # runs Git. This survives collection so the unsafe-checkpoint wake names the cause.
    checkpoint_refusal: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialFileSync:
    """One named auth file after the run (12): present in the copy, changed against the
    source, valid in shape, and written back or not, with the reason. Never a value."""

    name: str
    present: bool
    changed: bool
    valid: bool
    synced: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "changed": self.changed,
            "valid": self.valid,
            "synced": self.synced,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CredentialSync:
    """What the sync-back of a per-attempt credential copy did (12)."""

    harness: str
    mount_mode: str
    files: tuple[CredentialFileSync, ...]
    removed: bool
    detail: str = ""

    @property
    def changed(self) -> bool:
        return any(f.changed for f in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "mount_mode": self.mount_mode,
            "changed": self.changed,
            "removed": self.removed,
            "files": [f.as_dict() for f in self.files],
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """A worker image the provider can see (13): its reference, its digest, and the
    harness and version its labels declare."""

    reference: str
    digest: str
    harness: str | None
    harness_version: str | None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """The bounded auth probe (25): the hardened worker image, the credential mounted,
    a one-line prompt, a hard timeout. The launch shape comes from the adapter; the
    provider records only what 25 allows."""

    harness: str
    image: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    env_from_files: dict[str, str] = field(default_factory=dict)
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    identity_text: str = ""
    timeout_seconds: int = 120
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the probe recorded: exit facts, the image, and whether the named auth files
    changed. Tails stay with the caller for classification; nothing here is a value."""

    exit_code: int | None
    image_digest: str
    harness_version: str | None
    duration_seconds: float
    timed_out: bool = False
    oom_killed: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    credential_sync: CredentialSync | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """`ok`, `degraded` or `unavailable` with what was checked (25)."""

    state: str
    checks: dict[str, Any] = field(default_factory=dict)


class CleanupPolicy(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    KEEP_DIFF_ONLY = "keep_diff_only"


class ProviderError(Exception):
    """A provider failed before or while the harness ran (exit class environment)."""


class LaunchRefusedError(ProviderError):
    """A launch the provider refused on purpose (07, 13): the image's harness version is
    outside the adapter's tested range, or the harness has no credential to run with.
    The supervisor turns this into a wake, not a retry."""


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

    async def cleanup(
        self, ws: Workspace, policy: CleanupPolicy, spec: LaunchSpec | None = None
    ) -> None: ...

    async def reconcile(self) -> list[Handle]: ...

    async def retention(self, keep: Sequence[str]) -> int:
        """Remove provider-side leavings for attempts that are gone (16). Returns the
        count removed. A provider with nothing to remove returns 0."""
        ...

    async def list_images(self) -> list[ImageInfo]:
        """The worker images this provider can run, with their labels (13, 25)."""
        ...

    async def discard(self, ws: Workspace, spec: LaunchSpec | None = None) -> None:
        """Remove anything secret the provider placed for an attempt that will never be
        collected: a launch that failed after the credential was seeded, or a worker
        that was lost (12). Cleanup is separate and may never run for such an attempt."""
        ...

    async def probe_credential(self, request: ProbeRequest) -> ProbeResult:
        """25: run the hardened image with the credential mounted for one prompt under a
        hard timeout, sync the named auth files back, remove everything, and report
        the exit facts and whether the files changed."""
        ...

    async def health(self) -> ProviderHealth:
        """25: daemon reachable, network present, disk headroom, as one state."""
        ...
