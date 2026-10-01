"""Shared adapter declarations used by policy validation (05b, 12).

Keeping these in the domain lets uploads validate the same declarations as adapters
without importing adapters into the application layer.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HarnessConcurrency:
    minimum_mode: Literal["ro", "rw-narrow"] = "rw-narrow"
    parallel_attempts_safe: bool = False

    @property
    def allows_parallel(self) -> bool:
        return self.minimum_mode == "ro" or self.parallel_attempts_safe


HARNESS_CONCURRENCY: dict[str, HarnessConcurrency] = {
    # The setup token never refreshes; neither auth file syncs back.
    "claude_code": HarnessConcurrency("ro", True),
    # OpenAI rotates refresh tokens on use, and two attempts syncing their refreshed
    # auth.json back concurrently can let an older or revoked token win the last write
    # (Codex review of PR 307). Codex stays serial until the brokered renewer lands
    # (spec "Many Workers, One Login"); workers then hold access tokens only.
    "codex": HarnessConcurrency("rw-narrow", False),
    # Google does not rotate refresh tokens on ordinary access-token renewal.
    "agy": HarnessConcurrency("rw-narrow", True),
    "hermes": HarnessConcurrency("ro"),
    "script-harness": HarnessConcurrency("ro"),
}
