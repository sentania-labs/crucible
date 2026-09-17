"""Harness declarations: tested version range, egress endpoints, and launch argv (07).

The adapters themselves go live in C5. What C3 needs from them is the part the Docker
provider consults at launch: which harness versions an image may carry, which hostnames
that harness must reach, and what to run. A combination outside the range is a
launch-time refusal, never a warning (07, 13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from crucible.ports.execution import IDENTITY_MOUNT, REPORT_MOUNT

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class UnsupportedHarnessVersionError(Exception):
    """The image's harness version is outside the adapter's tested range (07)."""


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION.match(value.strip())
    if match is None:
        raise ValueError(f"not a version: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One harness as the provider needs it."""

    name: str
    # Tested range, inclusive lower bound and exclusive upper bound (07).
    min_version: str
    max_version_exclusive: str
    # The model and auth hostnames this harness must reach, from S6. The worker's
    # allowlist is the union of these and the policy's egress_allowlist (13).
    endpoints: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    # Argv carries only a short pointer; the bundle travels as files (07, S3).
    prompt: str = f"Read {IDENTITY_MOUNT}/IDENTITY.md and execute the task."

    def supports(self, version: str) -> bool:
        try:
            found = parse_version(version)
        except ValueError:
            return False
        return parse_version(self.min_version) <= found < parse_version(self.max_version_exclusive)

    def range_text(self) -> str:
        return f">={self.min_version},<{self.max_version_exclusive}"


REGISTRY: dict[str, HarnessSpec] = {
    "claude_code": HarnessSpec(
        name="claude_code",
        min_version="2.1.0",
        max_version_exclusive="2.2.0",
        endpoints=("api.anthropic.com",),
        command=(
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt-file",
            f"{IDENTITY_MOUNT}/IDENTITY.md",
            "--output-format",
            "stream-json",
            "--verbose",
        ),
    ),
    "codex": HarnessSpec(
        name="codex",
        min_version="0.153.0",
        max_version_exclusive="0.154.0",
        endpoints=("api.openai.com", "auth.openai.com"),
        command=(
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable",
            "plugins",
            "-c",
            "check_for_update_on_startup=false",
            "--json",
            "-o",
            f"{REPORT_MOUNT}/codex-last-message.md",
        ),
    ),
    "agy": HarnessSpec(
        name="agy",
        min_version="1.2.0",
        max_version_exclusive="1.3.0",
        endpoints=("daily-cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
        command=(
            "agy",
            "-p",
            "--dangerously-skip-permissions",
            "--add-dir",
            IDENTITY_MOUNT,
            "--output-format",
            "stream-json",
        ),
    ),
    # The e2e harness (18): a script that reads the identity bundle, writes a report,
    # and exits with a requested code. No model, no credential, no subscription.
    "script": HarnessSpec(
        name="script",
        min_version="1.0.0",
        max_version_exclusive="2.0.0",
        endpoints=(),
        command=("crucible-script-harness",),
    ),
}


@dataclass(frozen=True, slots=True)
class HarnessCheck:
    ok: bool
    detail: str
    installed: str | None = None
    supported: str = ""
    endpoints: tuple[str, ...] = field(default=())


def check_image_version(harness: str, labels: dict[str, str]) -> HarnessCheck:
    """Compare the image's `crucible.harness_version` label with the tested range (13)."""
    spec = REGISTRY.get(harness)
    if spec is None:
        return HarnessCheck(False, f"no adapter declares harness {harness!r}")
    labelled = labels.get("crucible.harness")
    installed = labels.get("crucible.harness_version")
    if labelled is not None and labelled != harness:
        return HarnessCheck(
            False,
            f"the image declares harness {labelled!r}, the execution asks for {harness!r}",
            installed,
            spec.range_text(),
            spec.endpoints,
        )
    if not installed:
        return HarnessCheck(
            False,
            "the image carries no crucible.harness_version label",
            None,
            spec.range_text(),
            spec.endpoints,
        )
    if not spec.supports(installed):
        return HarnessCheck(
            False,
            f"harness {harness} {installed} is outside the tested range {spec.range_text()}",
            installed,
            spec.range_text(),
            spec.endpoints,
        )
    return HarnessCheck(
        True,
        f"harness {harness} {installed} is inside {spec.range_text()}",
        installed,
        spec.range_text(),
        spec.endpoints,
    )


def egress_allowlist(harness: str, policy_hosts: list[str], extra: list[str]) -> tuple[str, ...]:
    """The union of the policy's allowlist and the adapter's declared endpoints (13)."""
    spec = REGISTRY.get(harness)
    hosts = set(policy_hosts) | set(extra)
    if spec is not None:
        hosts |= set(spec.endpoints)
    return tuple(sorted(h for h in hosts if h))
