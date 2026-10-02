"""#387: a failed Hermes run keeps its model, session, tokens and cost in the usage record.

Hermes 0.19 writes `--usage-file` from the agent's result, which is empty when the agent
raised, so the record says only `failed` and the cause. The wrapper fills what is missing
from the run's session row. The database here is built with the `sessions` DDL of the
pinned hermes_agent 0.19.0 wheel (hermes_state.py, SCHEMA_VERSION 22) and written the way
SessionDB's create_session, update_token_counts (per-call increments) and end_session
write it, so a column Hermes does not have cannot make a test pass."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from crucible.adapters.harness.hermes import _metrics

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"

# Verbatim from hermes_state.py in hermes_agent 0.19.0 (line 762).
SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
"""

SESSION = "20261002_105157_a052b9"
STARTED = 1790938317.0
ENDED = STARTED + 2496.216
# Hermes's -z usage file when the agent raised: the result dict is empty.
FAILED_USAGE: dict[str, Any] = {
    "estimated_cost_usd": None,
    "cost_status": None,
    "cost_source": None,
    "input_tokens": None,
    "output_tokens": None,
    "cache_read_tokens": None,
    "cache_write_tokens": None,
    "reasoning_tokens": None,
    "total_tokens": None,
    "api_calls": None,
    "model": None,
    "provider": None,
    "session_id": None,
    "completed": None,
    "failed": True,
    "service_tier": None,
    "failure": "Error code: 400 - context length exceeded",
}


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session_db(home: Path, *calls: tuple[int, int, int, int, int, float]) -> None:
    """A session as Hermes leaves it: created, one update_token_counts per model call
    (input, output, cache read, cache write, reasoning, cost), then ended."""
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as db:
        db.executescript(SESSIONS_DDL)
        db.execute(
            "INSERT INTO sessions (id, source, model, started_at) VALUES (?, ?, ?, ?)",
            (SESSION, "cli", "fast", STARTED),
        )
        # An older child session (compression split) must not be picked.
        db.execute(
            "INSERT INTO sessions (id, source, model, parent_session_id, started_at, "
            "input_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            ("child", "cli", "other", SESSION, STARTED + 10, 1),
        )
        for i, o, cr, cw, r, cost in calls:
            db.execute(
                """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   billing_provider = COALESCE(billing_provider, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + 1,
                   tool_call_count = tool_call_count + 1
                   WHERE id = ?""",
                (i, o, cr, cw, r, cost, "openrouter", "fast", SESSION),
            )
        db.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
            (ENDED, "agent_close", SESSION),
        )


# The issue's totals, over two calls; one call read from cache.
ISSUE_CALLS = (
    (6_000_000, 50_000, 0, 0, 29_000, 1.25),
    (206_631, 667, 1_000, 200, 429, 0.0625),
)


def _enrich(tmp_path: Path, usage: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / "hermes-usage.json"
    path.write_text(json.dumps(usage), encoding="utf-8")
    _wrapper()._enrich_usage(path, tmp_path / "home")
    written: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return written


def test_a_failed_run_exports_the_session_totals_and_keeps_its_failure(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, FAILED_USAGE)
    assert written["failed"] is True
    assert written["completed"] is None
    assert written["failure"] == "Error code: 400 - context length exceeded"
    assert written["session_id"] == SESSION
    assert written["model"] == "fast"
    assert written["provider"] == "openrouter"
    assert written["input_tokens"] == 6_206_631
    assert written["output_tokens"] == 50_667
    assert written["reasoning_tokens"] == 29_429
    assert written["cache_read_tokens"] == 1_000
    assert written["cache_write_tokens"] == 200
    # Hermes's session_total_tokens: input, cache read, cache write and output.
    assert written["total_tokens"] == 6_206_631 + 1_000 + 200 + 50_667
    assert written["estimated_cost_usd"] == pytest.approx(1.3125)
    assert written["duration_ms"] == 2_496_216
    assert written["tool_calls"] == 2
    metrics = _metrics(written)
    assert (metrics.model, metrics.tokens_in, metrics.tokens_out) == ("fast", 6_206_631, 50_667)
    assert metrics.cost_usd == pytest.approx(1.3125)
    assert metrics.duration_ms == 2_496_216


def test_a_failed_run_with_completed_false_keeps_it(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, {**FAILED_USAGE, "completed": False})
    assert (written["failed"], written["completed"]) == (True, False)
    assert written["total_tokens"] == 6_258_498


def test_a_successful_run_keeps_what_hermes_wrote(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    usage = {
        **FAILED_USAGE,
        "failed": False,
        "completed": True,
        "api_calls": 2,
        "model": "fast",
        "provider": "custom",
        "session_id": SESSION,
        "input_tokens": 6_206_631,
        "output_tokens": 50_667,
        "cache_read_tokens": 1_000,
        "cache_write_tokens": 200,
        "reasoning_tokens": 29_429,
        "total_tokens": 6_258_498,
        "estimated_cost_usd": 1.3125,
    }
    del usage["failure"]
    written = _enrich(tmp_path, usage)
    expected = {**usage, "duration_ms": 2_496_216, "tool_calls": 2}
    assert written == expected


def test_totals_hermes_already_wrote_are_not_counted_again(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    usage = {
        **FAILED_USAGE,
        "session_id": SESSION,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 10,
        "total_tokens": 150,
        "estimated_cost_usd": 0.5,
    }
    written = _enrich(tmp_path, usage)
    for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        assert written[key] == usage[key]
    assert written["estimated_cost_usd"] == 0.5
    assert written["model"] == "fast"


def test_only_input_tokens_set_fills_the_rest_without_replacing_it(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, {**FAILED_USAGE, "input_tokens": 7})
    assert written["input_tokens"] == 7
    assert written["output_tokens"] == 50_667
    assert written["reasoning_tokens"] == 29_429
    assert written["total_tokens"] == 7 + 1_000 + 200 + 50_667
    assert written["failed"] is True


def test_a_row_missing_columns_never_breaks_the_record(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as db:
        # No ended_at, tool_call_count or token columns.
        db.execute(
            "CREATE TABLE sessions (id TEXT, parent_session_id TEXT, model TEXT, started_at REAL)"
        )
        db.execute("INSERT INTO sessions VALUES (?, NULL, ?, ?)", (SESSION, "fast", STARTED))
    written = _enrich(tmp_path, FAILED_USAGE)
    assert written["model"] == "fast"
    assert written["session_id"] == SESSION
    assert written["duration_ms"] is None
    assert written["tool_calls"] is None
    assert written["total_tokens"] is None
    assert written["failed"] is True


def test_without_a_session_database_the_record_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    written = _enrich(tmp_path, FAILED_USAGE)
    assert written == FAILED_USAGE
