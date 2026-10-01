"""Credential ownership rules that constrain harness concurrency."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HarnessConcurrency:
    """How one harness may share its credential between attempts."""

    parallel_attempts_safe: bool = False
    renewer_held: bool = False

    @property
    def allows_parallel(self) -> bool:
        return self.parallel_attempts_safe or self.renewer_held
