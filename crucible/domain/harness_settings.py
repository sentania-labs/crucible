"""A harness's run settings an administrator saves (FDY-0140).

They are kept as the `provider_settings` row `harness.<name>` and reach the adapter
through the launch spec, so a change applies to the next launch in every process. Hermes
is the one harness with any: how many model turns one run may take, and the context
window it is told the gateway model has.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Hermes 0.19 stops `-z` at 90 turns and assumes a 256k window when the gateway does not
# say. A task that reads a repository, edits several files and runs its checks can use
# several hundred turns; 300 leaves room for that without letting a looping model run
# for ever. 131072 is a window every model the lab gateway serves for Hermes has, and
# Hermes refuses anything below 64000.
DEFAULT_HERMES_MAX_TURNS = 300
DEFAULT_HERMES_CONTEXT_LENGTH = 131_072
MAX_TURNS_RANGE = (10, 5000)
# 0 means "let Hermes find the window itself".
CONTEXT_LENGTH_RANGE = (64_000, 2_000_000)


def setting_name(harness: str) -> str:
    """The provider_settings row that holds one harness's run settings."""
    return f"harness.{harness}"


@dataclass(frozen=True, slots=True)
class HermesRunLimits:
    max_turns: int = DEFAULT_HERMES_MAX_TURNS
    context_length: int = DEFAULT_HERMES_CONTEXT_LENGTH

    def as_dict(self) -> dict[str, int]:
        return {"max_turns": self.max_turns, "context_length": self.context_length}


def hermes_run_limits(document: Mapping[str, Any] | None) -> HermesRunLimits:
    """The saved limits, with the defaults for anything absent or not a whole number."""
    values = document or {}

    def whole(name: str, default: int) -> int:
        value = values.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    return HermesRunLimits(
        max_turns=whole("max_turns", DEFAULT_HERMES_MAX_TURNS),
        context_length=whole("context_length", DEFAULT_HERMES_CONTEXT_LENGTH),
    )


def hermes_limit_problems(max_turns: int, context_length: int) -> list[str]:
    """Why a pair of limits cannot be saved, in words; empty when they can."""
    problems = []
    low, high = MAX_TURNS_RANGE
    if not low <= max_turns <= high:
        problems.append(f"max turns must be between {low} and {high}")
    low, high = CONTEXT_LENGTH_RANGE
    if context_length != 0 and not low <= context_length <= high:
        problems.append(
            f"the context length must be 0 (Hermes finds it) or between {low} and {high} tokens"
        )
    return problems
