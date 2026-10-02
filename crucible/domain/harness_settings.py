"""A harness's run settings an administrator saves (FDY-0140).

They are kept as the `provider_settings` row `harness.<name>` and reach the adapter
through the launch spec, so a change applies to the next launch in every process. Hermes
is the one harness with any: how many model turns one run may take, the context
window it is told the gateway model has, and the response allowance (max output tokens)
its requests carry (issue 388).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Hermes 0.19 stops `-z` at 90 turns and assumes a 256k window when the gateway does not
# say. A task that reads a repository, edits several files and runs its checks can use
# several hundred turns; 300 leaves room for that without letting a looping model run
# for ever. 131072 is a common window for the coding models a local gateway serves and
# above Hermes's 64000 floor; it is not read from the gateway, so a model with a smaller
# window needs its real figure saved, or 0 to let Hermes probe for it.
DEFAULT_HERMES_MAX_TURNS = 300
DEFAULT_HERMES_CONTEXT_LENGTH = 131_072
# Issue 388: the response allowance the local gateway applies to a request that names
# none. Hermes reserves it out of the window when it decides when to compress, and its
# requests carry it as max_tokens, so both sides budget with the same reservation.
DEFAULT_HERMES_MAX_OUTPUT_TOKENS = 32_000
MAX_TURNS_RANGE = (10, 5000)
# 0 means "let Hermes find the window itself".
CONTEXT_LENGTH_RANGE = (64_000, 2_000_000)
MAX_OUTPUT_TOKENS_RANGE = (1_024, 1_000_000)
# The launch-only key the supervisor adds from the routing entry's thinking setting
# (`chat_template_kwargs.enable_thinking`); it is never part of the saved document.
THINKING_KEY = "enable_thinking"


def setting_name(harness: str) -> str:
    """The provider_settings row that holds one harness's run settings."""
    return f"harness.{harness}"


@dataclass(frozen=True, slots=True)
class HermesRunLimits:
    max_turns: int = DEFAULT_HERMES_MAX_TURNS
    context_length: int = DEFAULT_HERMES_CONTEXT_LENGTH
    max_output_tokens: int = DEFAULT_HERMES_MAX_OUTPUT_TOKENS
    # From the routing entry at launch, not saved with the limits.
    enable_thinking: bool = False

    def as_dict(self) -> dict[str, int]:
        """The saved document: the limits an administrator sets."""
        return {
            "max_turns": self.max_turns,
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
        }

    def effective(self) -> dict[str, Any]:
        """Issue 388: what one launch was given, recorded on its attempt."""
        return {**self.as_dict(), THINKING_KEY: self.enable_thinking}


def hermes_run_limits(document: Mapping[str, Any] | None) -> HermesRunLimits:
    """The saved limits, with the defaults for anything absent or not a whole number."""
    values = document or {}

    def whole(name: str, default: int) -> int:
        value = values.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    return HermesRunLimits(
        max_turns=whole("max_turns", DEFAULT_HERMES_MAX_TURNS),
        context_length=whole("context_length", DEFAULT_HERMES_CONTEXT_LENGTH),
        max_output_tokens=whole("max_output_tokens", DEFAULT_HERMES_MAX_OUTPUT_TOKENS),
        enable_thinking=values.get(THINKING_KEY) is True,
    )


def hermes_limit_problems(
    max_turns: int,
    context_length: int,
    max_output_tokens: int = DEFAULT_HERMES_MAX_OUTPUT_TOKENS,
) -> list[str]:
    """Why a set of limits cannot be saved, in words; empty when it can."""
    problems = []
    low, high = MAX_TURNS_RANGE
    if not low <= max_turns <= high:
        problems.append(f"max turns must be between {low} and {high}")
    low, high = CONTEXT_LENGTH_RANGE
    if context_length != 0 and not low <= context_length <= high:
        problems.append(
            f"the context length must be 0 (Hermes finds it) or between {low} and {high} tokens"
        )
    low, high = MAX_OUTPUT_TOKENS_RANGE
    if not low <= max_output_tokens <= high:
        problems.append(f"max output tokens must be between {low} and {high}")
    elif context_length and max_output_tokens * 2 > context_length:
        # Hermes reserves the allowance out of the window; past half of it the input
        # budget left is too small to work in.
        problems.append("max output tokens must be at most half the context length")
    return problems
