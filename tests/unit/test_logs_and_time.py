from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from crucible.domain.ids import is_ulid, new_id
from crucible.domain.time import ensure_utc, rfc3339
from crucible.logs import JsonFormatter, log_context


def test_ulids() -> None:
    a, b = new_id(), new_id()
    assert is_ulid(a) and is_ulid(b) and a != b
    assert not is_ulid("nope")
    assert not is_ulid("0" * 25 + "!")


def test_rfc3339_has_offset_not_z() -> None:
    value = datetime(2026, 9, 16, 11, 20, tzinfo=timezone(timedelta(hours=-5)))
    rendered = rfc3339(value)
    assert rendered == "2026-09-16T16:20:00.000000+00:00"
    assert not rendered.endswith("Z")


def test_naive_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 1, 1))


def test_json_log_line_carries_context() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("crucible.test", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    record.__dict__["handle"] = "h1"
    with log_context(task_id="t1", attempt_id="a1", ignored="no"):
        line = formatter.format(record)
    entry = json.loads(line)
    assert entry["message"] == "hello x"
    assert entry["task_id"] == "t1" and entry["attempt_id"] == "a1"
    assert "ignored" not in entry and entry["handle"] == "h1"
    assert entry["ts"].endswith("+00:00")
    assert "execution_id" not in entry
    outside = json.loads(formatter.format(record))
    assert "task_id" not in outside


def test_context_nesting() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", (), None)
    with log_context(task_id="t1"), log_context(execution_id="e1"):
        entry = json.loads(formatter.format(record))
    assert entry["task_id"] == "t1" and entry["execution_id"] == "e1"
    assert datetime.fromisoformat(entry["ts"]).tzinfo == UTC
