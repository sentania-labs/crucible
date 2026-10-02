"""Failures of the model transport or container runtime, outside worker control."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from crucible.domain.exit_class import ExitClass

START_FAILURES = frozenset(
    {
        "StartError",
        "CreateContainerError",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
    }
)


@dataclass(frozen=True)
class Interruption:
    message: str
    capacity: bool = False
    quota: bool = False

    @property
    def exit_class(self) -> ExitClass:
        return ExitClass.QUOTA_EXHAUSTED if self.quota else ExitClass.INFRASTRUCTURE


def model_interruption(exit_code: int | None, *tails: str) -> Interruption | None:
    """Recognize a failed model request, never a successful worker's quoted output."""
    if exit_code in (None, 0):
        return None
    for tail in reversed(tails):
        for line in reversed(tail[-65536:].splitlines()):
            lower = line.lower()
            # Tool output can contain the very HTTP failure the worker is testing.
            # It is not a refusal from the harness's model provider.
            if line.lstrip().startswith("{"):
                try:
                    document = json.loads(line)
                except (ValueError, RecursionError):
                    document = None
                if isinstance(document, dict):
                    kind = str(document.get("type", document.get("event")))
                    item = document.get("item")
                    if kind in {"tool_result", "tool_use", "user", "assistant"} or (
                        isinstance(item, dict)
                        and str(item.get("type"))
                        in {"command_execution", "tool_call", "tool_result"}
                    ):
                        continue
                    if kind in {"turn.completed", "response.completed"}:
                        break
            elif re.search(r"(?:^FAILED\s+.*test|\b\d+ failed\b|AssertionError)", line):
                break
            capacity = "at capacity" in lower or "overloaded_error" in lower
            quota = bool(
                re.search(
                    r"(?:http(?:/\d(?:\.\d)?)?\s+|status(?:[_ ]code)?[\s\"':=]*"
                    r"|error(?: code)?[\s\"':=]*)429\b|429\s+too many requests",
                    lower,
                )
            )
            gateway = bool(
                re.search(
                    r"(?:http(?:/\d(?:\.\d)?)?\s+|status(?:[_ ]code)?[\s\"':=]*"
                    r"|error(?: code)?[\s\"':=]*)50[234]\b"
                    r"|50[234]\s+(?:service unavailable|bad gateway|gateway timeout)",
                    lower,
                )
            )
            transport = any(
                word in lower
                for word in (
                    "connection refused",
                    "connection reset",
                    "econnrefused",
                    "econnreset",
                    "connecttimeout",
                    "readtimeout",
                    "request timed out",
                    "connection timed out",
                )
            )
            if capacity or quota or gateway or transport:
                return Interruption(line[:2000], capacity=capacity, quota=quota)
    return None
