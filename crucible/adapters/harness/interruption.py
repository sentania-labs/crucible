"""Translate structured harness provider events into interruption causes."""

from __future__ import annotations

import json

from crucible.domain.infrastructure import Interruption


def model_interruption(exit_code: int | None, *tails: str) -> Interruption | None:
    """Accept provider event fields only, never text from commands or tracebacks."""
    if exit_code in (None, 0):
        return None
    for tail in reversed(tails):
        for line in reversed(tail[-65536:].splitlines()):
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in {"turn.completed", "response.completed"}:
                break
            if kind not in {"provider_error", "turn.failed", "quota_exhausted"}:
                continue
            error = event.get("error", event)
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            status = error.get("status_code", error.get("status"))
            capacity = code in {"overloaded_error", "at_capacity"}
            quota = kind == "quota_exhausted" or status == 429 or code in {
                "usage_limit_reached", "rate_limit_exceeded", "insufficient_quota"
            }
            transport = code in {"ECONNREFUSED", "ECONNRESET", "ETIMEDOUT"}
            if capacity or quota or transport or status in {502, 503, 504}:
                return Interruption(json.dumps(event)[:2000], capacity=capacity, quota=quota)
    return None
