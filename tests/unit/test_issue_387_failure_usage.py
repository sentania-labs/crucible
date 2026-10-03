"""#387: a failed Hermes run keeps its model, session, tokens and cost in the usage record.

Hermes 0.19 writes `--usage-file` from the agent's result. When the agent raised, the
result is empty and `completed` is null; when it returned failed early, the result has
`api_calls` and an `error` but no tokens, model or session. The wrapper fills what is
missing from the run's session rows, and its bootstrap passes the early return's `error`
on as the record's `failure`. The database here is built with the `sessions` DDL of the
pinned hermes_agent 0.19.0 wheel (hermes_state.py, SCHEMA_VERSION 22) and written the way
SessionDB's create_session, update_token_counts (per-call increments) and end_session
write it, so a column Hermes does not have cannot make a test pass. The adapter half is
driven through HermesAdapter.parse_report and classify_exit, which read the file with
_usage() and _metrics()."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from crucible.adapters.harness.hermes import USAGE_NAME, HermesAdapter
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import ExitInfo, ReportMetrics

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
CHILD = "20261002_112003_c4e1d0"
STARTED = 1790938317.0
ENDED = STARTED + 2496.216
CAUSE = "Error code: 400 - context length exceeded"
# Hermes's -z usage file when the agent raised: the result dict is empty.
RAISED_USAGE: dict[str, Any] = {
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
    "failure": CAUSE,
}
# The issue's record: run_conversation returned failed early (completed false,
# api_calls set), so _write_usage_file had no failure and the result no tokens.
RETURNED_USAGE: dict[str, Any] = {
    **RAISED_USAGE,
    "api_calls": 104,
    "completed": False,
}
del RETURNED_USAGE["failure"]


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Call = tuple[int, int, int, int, int, float]


def _create(db: sqlite3.Connection, session: str, started: float, parent: str | None) -> None:
    db.execute(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session, "cli", "fast", parent, started),
    )


def _calls(db: sqlite3.Connection, session: str, calls: tuple[Call, ...]) -> None:
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
            (i, o, cr, cw, r, cost, "openrouter", "fast", session),
        )


def _end(db: sqlite3.Connection, session: str, ended: float, reason: str) -> None:
    db.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
        (ended, reason, session),
    )


def _session_db(home: Path, *calls: Call, child: tuple[Call, ...] = ()) -> None:
    """A run as Hermes leaves it: the session created, one update_token_counts per model
    call (input, output, cache read, cache write, reasoning, cost), then ended. With
    `child`, context compression ended the first row half way and opened a child row
    (conversation_compression.py), which every later call writes to."""
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state.db") as db:
        db.executescript(SESSIONS_DDL)
        _create(db, SESSION, STARTED, None)
        _calls(db, SESSION, calls)
        current = SESSION
        if child:
            _end(db, SESSION, STARTED + 100, "compression")
            _create(db, CHILD, STARTED + 100, SESSION)
            _calls(db, CHILD, child)
            current = CHILD
        _end(db, current, ENDED, "agent_close")


# The issue's totals, over two calls; one call read from cache.
ISSUE_CALLS: tuple[Call, ...] = (
    (6_000_000, 50_000, 0, 0, 29_000, 1.25),
    (206_631, 667, 1_000, 200, 429, 0.0625),
)
ISSUE_TOTAL = 6_206_631 + 1_000 + 200 + 50_667


def _enrich(tmp_path: Path, usage: dict[str, Any]) -> dict[str, Any]:
    path = tmp_path / USAGE_NAME
    path.write_text(json.dumps(usage), encoding="utf-8")
    _wrapper()._enrich_usage(path, tmp_path / "home")
    written: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return written


def _attempt(tmp_path: Path) -> tuple[ReportMetrics, str | None, ExitClass]:
    """What the adapter records on the attempt from the usage file in tmp_path."""
    adapter = HermesAdapter()
    exit = ExitInfo(exit_code=1)
    parsed = adapter.parse_report(tmp_path, exit)
    return parsed.metrics, parsed.run_evidence_error, adapter.classify_exit(exit, "", "", tmp_path)


def _assert_issue_totals(written: dict[str, Any]) -> None:
    assert written["session_id"] == SESSION
    assert written["model"] == "fast"
    assert written["provider"] == "openrouter"
    assert written["input_tokens"] == 6_206_631
    assert written["output_tokens"] == 50_667
    assert written["reasoning_tokens"] == 29_429
    assert written["cache_read_tokens"] == 1_000
    assert written["cache_write_tokens"] == 200
    # Hermes's session_total_tokens: input, cache read, cache write and output.
    assert written["total_tokens"] == ISSUE_TOTAL
    assert written["estimated_cost_usd"] == pytest.approx(1.3125)
    assert written["duration_ms"] == 2_496_216
    assert written["tool_calls"] == 2


def test_a_raised_run_reaches_the_attempt_with_its_totals_and_failure(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, RAISED_USAGE)
    assert written["failed"] is True
    # Hermes wrote null; the run did not complete.
    assert written["completed"] is False
    assert written["failure"] == CAUSE
    _assert_issue_totals(written)
    metrics, error, outcome = _attempt(tmp_path)
    assert error is None
    assert (metrics.model, metrics.tokens_in, metrics.tokens_out) == ("fast", 6_206_631, 50_667)
    assert metrics.cost_usd == pytest.approx(1.3125)
    assert (metrics.duration_ms, metrics.tool_calls) == (2_496_216, 2)
    assert metrics.source == "hermes_usage"
    assert outcome is ExitClass.CRASHED


def test_the_adapter_reads_a_raised_record_the_wrapper_could_not_enrich(tmp_path: Path) -> None:
    (tmp_path / USAGE_NAME).write_text(json.dumps(RAISED_USAGE), encoding="utf-8")
    metrics, error, outcome = _attempt(tmp_path)
    assert error is None
    assert metrics.source == "hermes_usage"
    assert outcome is ExitClass.CRASHED


def test_the_issue_record_exports_the_session_totals(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, RETURNED_USAGE)
    assert (written["failed"], written["completed"]) == (True, False)
    assert written["api_calls"] == 104
    _assert_issue_totals(written)
    metrics, error, _ = _attempt(tmp_path)
    assert error is None
    assert (metrics.model, metrics.tokens_in, metrics.tokens_out) == ("fast", 6_206_631, 50_667)


def test_tokens_after_compression_are_counted_with_the_first_row(tmp_path: Path) -> None:
    _session_db(
        tmp_path / "home",
        (1_000, 10, 0, 0, 5, 0.25),
        child=((5_000_000, 40_000, 0, 0, 3_000, 2.0),),
    )
    written = _enrich(tmp_path, RAISED_USAGE)
    assert written["session_id"] == SESSION
    assert written["input_tokens"] == 5_001_000
    assert written["output_tokens"] == 40_010
    assert written["reasoning_tokens"] == 3_005
    assert written["total_tokens"] == 5_001_000 + 40_010
    assert written["estimated_cost_usd"] == pytest.approx(2.25)
    assert written["tool_calls"] == 2
    assert written["duration_ms"] == 2_496_216
    metrics, _, _ = _attempt(tmp_path)
    assert (metrics.tokens_in, metrics.tokens_out) == (5_001_000, 40_010)


def test_a_successful_run_keeps_what_hermes_wrote(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    usage = {
        **RAISED_USAGE,
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
        "total_tokens": ISSUE_TOTAL,
        "estimated_cost_usd": 1.3125,
    }
    del usage["failure"]
    written = _enrich(tmp_path, usage)
    assert written == {**usage, "duration_ms": 2_496_216, "tool_calls": 2}


def test_totals_hermes_already_wrote_are_not_counted_again(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    usage = {
        **RAISED_USAGE,
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


def test_tokens_come_from_hermes_alone_when_it_wrote_any(tmp_path: Path) -> None:
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    written = _enrich(tmp_path, {**RAISED_USAGE, "input_tokens": 7, "output_tokens": 3})
    assert (written["input_tokens"], written["output_tokens"]) == (7, 3)
    assert written["cache_read_tokens"] is None
    assert written["reasoning_tokens"] is None
    assert written["total_tokens"] == 10
    assert written["model"] == "fast"
    assert written["failed"] is True


def test_a_missing_session_column_fails_loudly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as db:
        db.executescript(SESSIONS_DDL.replace("reasoning_tokens", "thinking_tokens"))
        _create(db, SESSION, STARTED, None)
    wrapper = _wrapper()
    path = tmp_path / USAGE_NAME
    path.write_text(json.dumps(RAISED_USAGE), encoding="utf-8")
    wrapper._enrich_usage(path, home)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {**RAISED_USAGE, "completed": False}


def test_without_a_session_database_only_completed_is_settled(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    written = _enrich(tmp_path, RAISED_USAGE)
    assert written == {**RAISED_USAGE, "completed": False}
    assert not (tmp_path / "home" / "state.db").exists()


# hermes_cli/oneshot.py of hermes_agent 0.19.0: _write_usage_file verbatim but for its
# docstring and lazy import, and run_oneshot's two calls of it.
_STAND_IN_ONESHOT = """
import json
from pathlib import Path


def _write_usage_file(path, result, failure=None):
    if not path:
        return
    try:
        report = {
            "estimated_cost_usd": result.get("estimated_cost_usd"),
            "cost_status": result.get("cost_status"),
            "cost_source": result.get("cost_source"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "cache_write_tokens": result.get("cache_write_tokens"),
            "reasoning_tokens": result.get("reasoning_tokens"),
            "total_tokens": result.get("total_tokens"),
            "api_calls": result.get("api_calls"),
            "model": result.get("model"),
            "provider": result.get("provider"),
            "session_id": result.get("session_id"),
            "completed": result.get("completed"),
            "failed": bool(result.get("failed")) or failure is not None,
            "service_tier": result.get("service_tier"),
        }
        if failure is not None:
            report["failure"] = failure
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\\n", encoding="utf-8")
    except Exception:
        pass


def run_oneshot(usage_file, result, failure):
    if failure is not None:
        _write_usage_file(usage_file, result, failure=str(failure))
        return 1
    _write_usage_file(usage_file, result)
    return 0
"""
_STAND_IN_MAIN = """
import json
import sys


def main():
    from hermes_cli.oneshot import run_oneshot

    path, result, failure = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3] or None
    return run_oneshot(path, result, failure)
"""
RETURNED_RESULT = {
    "final_response": "",
    "api_calls": 104,
    "completed": False,
    "failed": True,
    "error": "Non-retryable error: HTTP 400 context length exceeded",
}


def _bootstrap(
    tmp_path: Path, result: dict[str, Any], failure: str = "", version: str = "0.19.0"
) -> subprocess.CompletedProcess[str]:
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text(_STAND_IN_MAIN, encoding="utf-8")
    (hermes / "hermes_cli" / "oneshot.py").write_text(_STAND_IN_ONESHOT, encoding="utf-8")
    metadata = hermes / f"hermes_agent-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: hermes-agent\nVersion: {version}\n", encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            _wrapper().BOOTSTRAP,
            str(tmp_path / USAGE_NAME),
            json.dumps(result),
            failure,
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PYTHONPATH": str(hermes)},
    )


def test_an_early_failed_return_keeps_its_cause_in_the_usage_file(tmp_path: Path) -> None:
    run = _bootstrap(tmp_path, RETURNED_RESULT)
    assert run.returncode == 0, run.stderr
    usage = json.loads((tmp_path / USAGE_NAME).read_text(encoding="utf-8"))
    assert usage["failure"] == RETURNED_RESULT["error"]
    assert (usage["failed"], usage["completed"], usage["api_calls"]) == (True, False, 104)
    _session_db(tmp_path / "home", *ISSUE_CALLS)
    _wrapper()._enrich_usage(tmp_path / USAGE_NAME, tmp_path / "home")
    written = json.loads((tmp_path / USAGE_NAME).read_text(encoding="utf-8"))
    assert written["failure"] == RETURNED_RESULT["error"]
    _assert_issue_totals(written)


@pytest.mark.parametrize(
    ("result", "failure", "expected"),
    [
        # The agent raised: Hermes's own cause stands.
        ({}, CAUSE, CAUSE),
        # A failed return without an error, and a successful one: nothing is added.
        ({**RETURNED_RESULT, "error": None}, "", None),
        ({"completed": True, "failed": False, "error": "ignored"}, "", None),
    ],
)
def test_the_usage_patch_changes_nothing_else(
    tmp_path: Path, result: dict[str, Any], failure: str, expected: str | None
) -> None:
    run = _bootstrap(tmp_path, result, failure)
    assert run.returncode == (1 if failure else 0), run.stderr
    usage = json.loads((tmp_path / USAGE_NAME).read_text(encoding="utf-8"))
    assert usage.get("failure") == expected


def test_the_usage_patch_refuses_another_hermes_version(tmp_path: Path) -> None:
    run = _bootstrap(tmp_path, RETURNED_RESULT, version="0.20.0")
    assert run.returncode != 0
    assert "its patches are for hermes-agent 0.19.0, found 0.20.0" in run.stderr
    assert not (tmp_path / USAGE_NAME).exists()

def test_no_sessions_table_through_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text("def main(): return 0\n", encoding="utf-8")
    (hermes / "hermes_cli" / "oneshot.py").write_text(_STAND_IN_ONESHOT, encoding="utf-8")
    (hermes / "tools").mkdir(parents=True)
    (hermes / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "tools" / "file_operations.py").write_text('''
class ShellFileOperations:
    def _search_with_grep(self, pattern, path, file_glob, limit, offset, output_mode, context):
        cmd_parts = ["grep", "-rnH"]
        cmd_parts.append("--exclude-dir='.*'")
        cmd_parts.append(self._escape_shell_arg(path))
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])
        cmd = "set -o pipefail; " + " ".join(cmd_parts)
''', encoding="utf-8")
    metadata = hermes / "hermes_agent-0.19.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: hermes-agent\nVersion: 0.19.0\n", encoding="utf-8")
    
    home = tmp_path / "home"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as db:
        pass
        
    path = tmp_path / USAGE_NAME
    path.write_text(json.dumps(RAISED_USAGE), encoding="utf-8")
    
    wrapper = _wrapper()
    wrapper.HERMES_PYTHON = sys.executable
    
    monkeypatch.setenv("CRUCIBLE_HERMES_USAGE", str(path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(hermes))
    monkeypatch.setenv("CRUCIBLE_HERMES_MAX_TURNS", "0")
    monkeypatch.setattr(sys, "argv", ["crucible-hermes.py"])
    
    assert wrapper.main() == 0
    
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {**RAISED_USAGE, "completed": False}
    
    assert "crucible-hermes: Hermes's state.db has no sessions table" in capsys.readouterr().err
