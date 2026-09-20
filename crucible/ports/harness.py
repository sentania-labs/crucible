"""HarnessAdapter contract (07): launch shape, credential needs, report parsing, and exit
classification for one harness. Adapters contain no lifecycle logic.

The port carries no secret. A credential is named here (which files, where they mount,
how they refresh); its value is read by the provider from a configured path and reaches
the worker only as a per-attempt copy the provider seeds (12).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from crucible.domain.exit_class import ExitClass

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

__all__ = [
    "AdapterLaunch",
    "AuthFile",
    "CredentialSource",
    "CredentialSpec",
    "ExitClass",
    "ExitInfo",
    "HarnessAdapter",
    "HarnessCapabilities",
    "HarnessGate",
    "HarnessUnavailableError",
    "LaunchContext",
    "MountMode",
    "ParsedReport",
    "ReportMetrics",
    "SessionCompatibility",
    "TranscriptFormat",
    "VersionRange",
    "parse_version",
]


class MountMode(StrEnum):
    """How the per-attempt credential copy is mounted (12)."""

    RO = "ro"
    RW_NARROW = "rw-narrow"


class SessionCompatibility(StrEnum):
    """Whether the dedicated Crucible session has been shown to leave the operator's own
    session valid (25, S1b)."""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


class TranscriptFormat(StrEnum):
    STREAM_JSON = "stream-json"
    JSON_EVENTS = "json-events"
    NONE = "none"


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.match(value.strip())
    if match is None:
        raise ValueError(f"not a version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass(frozen=True, slots=True)
class VersionRange:
    """The harness versions an adapter was tested with: inclusive lower bound, exclusive
    upper bound. A launch outside it is refused, never warned about (07, 13)."""

    min_version: str
    max_version_exclusive: str

    def supports(self, version: str) -> bool:
        try:
            found = parse_version(version)
        except ValueError:
            return False
        return parse_version(self.min_version) <= found < parse_version(self.max_version_exclusive)

    @property
    def text(self) -> str:
        return f">={self.min_version},<{self.max_version_exclusive}"


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """What the provider and the routing policy may rely on for this harness."""

    prompt_on_stdin: bool
    model_flag: bool
    effort_flag: bool
    transcript_format: TranscriptFormat
    # The model and auth hostnames this harness must reach (S6). The worker's allowlist
    # is the union of these and the policy's egress_allowlist (13).
    endpoints: tuple[str, ...]
    # The instruction file the harness reads from the checkout; the preparer writes an
    # untracked one only when the checkout has none (06, 07).
    shim: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_on_stdin": self.prompt_on_stdin,
            "model_flag": self.model_flag,
            "effort_flag": self.effort_flag,
            "transcript_format": self.transcript_format.value,
            "endpoints": list(self.endpoints),
            "shim": self.shim,
        }


@dataclass(frozen=True, slots=True)
class AuthFile:
    """One file that is the credential, named relative to the mounted directory.

    `json` says the file is a JSON object and `json_keys` are the top-level keys a
    valid one carries; an opaque file is never parsed. `issued_at` is the path to the
    field that orders two versions of the file (a refresh timestamp or a token expiry);
    a file without one is never synced back, because "newest" would have no meaning
    (12). `env_var` is the one exception 07 allows to file-only delivery: the file's
    content reaches the harness through that variable at container start, resolved by
    the provider from the mounted copy, never placed in the create request."""

    name: str
    # Whether the file is a JSON object (validated on sync-back) or opaque bytes.
    json: bool = False
    json_keys: tuple[str, ...] = ()
    issued_at: tuple[str, ...] | None = None
    required: bool = True
    env_var: str | None = None
    # A file the CLI rewrites as state rather than as a credential: it is seeded so the
    # CLI finds what it expects, and never written back (12).
    sync_back: bool = True


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    """Where a harness expects its credential and which files that is (07, 12)."""

    harness: str
    # The container path the seeded copy is mounted at.
    mount_target: str
    auth_files: tuple[AuthFile, ...]
    # The adapter's declared minimum (07). A probe may raise it to rw-narrow; it is
    # never lowered (25 step 7).
    minimum_mode: MountMode
    # Which subdirectory of the configured credential path maps onto `mount_target`.
    source_subdir: str = ""
    # The environment variable that points the CLI at `mount_target`, if it has one.
    config_dir_env: str | None = None
    # Files mounted read-only on top of the copy from a Crucible-owned template, so the
    # CLI's settings, hooks and server definitions never come from a worker (12).
    templates: Mapping[str, str] = field(default_factory=dict)
    # How the operator logs in to this directory (25 step 2), for the onboarding command.
    login_hint: str = ""

    def env(self) -> dict[str, str]:
        return {self.config_dir_env: self.mount_target} if self.config_dir_env else {}

    def env_from_files(self) -> dict[str, str]:
        return {f.env_var: f"{self.mount_target}/{f.name}" for f in self.auth_files if f.env_var}

    def source_path(self, root: str, name: str) -> Path:
        base = Path(root)
        if self.source_subdir:
            base = base / self.source_subdir
        return base / name


@dataclass(frozen=True, slots=True)
class HarnessGate:
    """The operator's configuration gate for one harness (25): a harness ships disabled
    until its dedicated credential session and daily-session compatibility are verified
    (S1b), and the reason travels with the flag."""

    enabled: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CredentialSource:
    """A configured credential directory for one harness (12): a path and the mount
    mode the operator chose, or None to take the adapter's minimum."""

    path: str
    mount_mode: MountMode | None = None


@dataclass(frozen=True, slots=True)
class LaunchContext:
    """What an adapter needs to build a launch (07). Paths are container paths."""

    attempt_id: str
    model: str
    effort: str | None
    timeout_seconds: int
    identity_mount: str
    report_mount: str
    repo_mount: str
    credential_mounted: bool = False


@dataclass(frozen=True, slots=True)
class AdapterLaunch:
    """The harness-specific half of a LaunchSpec (07).

    `env` carries names and non-secret values only. `env_from_files` maps a variable to a
    container path the provider resolves at container start; it is the only way a value
    from a credential file reaches the harness's environment, and it never appears in the
    create request. The prompt is a short pointer (S3); the identity and the contract
    are files."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    env_from_files: Mapping[str, str] = field(default_factory=dict)
    # What the harness reads on stdin: these files, in order, then this text.
    stdin_files: tuple[str, ...] = ()
    stdin_text: str = ""
    # Where the provider tees the harness's stdout so the transcript becomes an artifact.
    transcript_path: str | None = None
    workdir: str | None = None


@dataclass(frozen=True, slots=True)
class ExitInfo:
    exit_code: int | None
    report_present: bool = False
    blocked_present: bool = False
    oom_killed: bool = False
    timed_out: bool = False
    killed: bool = False
    lost: bool = False


@dataclass(frozen=True, slots=True)
class ReportMetrics:
    """What the transcript said about cost (05b): tokens and the model that answered.
    Null where a harness reports nothing; the pool then counts attempts."""

    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    source: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ParsedReport:
    """The report directory as an adapter read it (07). The claim is still a claim."""

    claim: dict[str, Any] | None
    raw: str | None
    errors: list[dict[str, Any]]
    blocked_md: str | None
    report_present: bool
    # Progress lines the worker wrote, ingested as unverified events (07).
    progress: tuple[dict[str, Any], ...] = ()
    metrics: ReportMetrics = field(default_factory=ReportMetrics)
    transcript_lines: int = 0
    transcript_name: str | None = None


class HarnessUnavailableError(Exception):
    """The registry refused a harness name: unknown, or disabled (07, 25)."""

    def __init__(self, harness: str, reason: str) -> None:
        super().__init__(f"harness {harness!r} is unavailable: {reason}")
        self.harness = harness
        self.reason = reason


class HarnessAdapter(Protocol):
    name: str
    supported_versions: VersionRange

    def capabilities(self) -> HarnessCapabilities: ...

    def credential_spec(self) -> CredentialSpec | None: ...

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch: ...

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport: ...

    def classify_exit(self, exit: ExitInfo, stdout_tail: str, stderr_tail: str) -> ExitClass: ...

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None: ...
