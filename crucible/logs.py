"""Structured JSON logging (01): one event per line on stdout, RFC 3339 timestamps with
an offset, and task_id, execution_id, attempt_id from a context where present."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "crucible_log_context", default=None
)
CONTEXT_KEYS = ("task_id", "execution_id", "attempt_id")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


@contextmanager
def log_context(**ids: str | None) -> Iterator[None]:
    """Bind entity ids for every log line emitted inside the block."""
    current = dict(_context.get() or {})
    current.update({k: v for k, v in ids.items() if v is not None and k in CONTEXT_KEYS})
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(_context.get() or {})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers[:] = []
        logger.propagate = True
