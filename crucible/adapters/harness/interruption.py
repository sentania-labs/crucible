"""Decide whether a failed model call, as a harness adapter read it, interrupts the worker.

Each adapter reads its own CLI's error events (Codex exec and app-server notifications,
Claude Code `api_retry`, AGY's `result`, Hermes's provider exit) and hands over a
`ProviderFailure`. Nothing here reads a transcript line: the shape of an event belongs to
the adapter that knows the CLI, and the rule for what counts as an interruption is the
one place below.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from crucible.domain.infrastructure import Interruption

# A gateway or proxy in front of the model answered for it: the model never ran.
GATEWAY_STATUSES = frozenset({502, 503, 504})
QUOTA_STATUS = 429
# Anthropic's `overloaded_error`; the provider is at capacity for this model.
CAPACITY_STATUS = 529
TAIL_BYTES = 64 * 1024

# An HTTP status line as the CLIs render a status they received: "503 Service
# Unavailable". The reason phrase is required, so a bare number is never a status.
_STATUS_LINE = re.compile(
    r"\b(429 Too Many Requests|502 Bad Gateway|503 Service Unavailable|504 Gateway Timeout)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderFailure:
    """One failed model call, from the harness's own error event.

    `status` is the HTTP status the provider (or the gateway in front of it) answered,
    `transport` says no answer came at all (refused, reset, timed out), `capacity` that
    the provider said the model is at capacity, `quota` that the account's quota or rate
    limit refused the call."""

    message: str
    status: int | None = None
    transport: bool = False
    capacity: bool = False
    quota: bool = False


def status_line(text: str) -> int | None:
    """The HTTP status a CLI wrote into its own error message, or None."""
    match = _STATUS_LINE.search(text)
    return int(match.group(1)[:3]) if match else None


def interruption_from(failure: ProviderFailure | None) -> Interruption | None:
    """The interruption a provider failure is, or None for one that is the worker's own
    (a 400, a 401, a context overflow): those keep the class the exit already has."""
    if failure is None:
        return None
    capacity = failure.capacity or failure.status == CAPACITY_STATUS
    quota = failure.quota or failure.status == QUOTA_STATUS
    if not (capacity or quota or failure.transport or failure.status in GATEWAY_STATUSES):
        return None
    return Interruption(failure.message[:2000], capacity=capacity, quota=quota)


def tail_lines(*tails: str) -> Iterator[str]:
    """The lines of each tail, last first, as an adapter scans for the final failure."""
    for tail in tails:
        yield from reversed(tail[-TAIL_BYTES:].splitlines())


def file_tail(path: Path | None) -> str:
    """The end of a transcript file the harness wrote itself, or "" when there is none."""
    if path is None or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
