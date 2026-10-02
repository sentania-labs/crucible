import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failure_path_enrichment(tmp_path: Path) -> None:
    wrapper = _wrapper()
    home = tmp_path / "home"
    home.mkdir()
    usage_path = tmp_path / "usage.json"

    # create dummy state.db
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE sessions (
                id TEXT,
                started_at TEXT,
                ended_at TEXT,
                tool_call_count INTEGER,
                model TEXT,
                provider TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                total_tokens INTEGER
            )"""
        )
        db.execute(
            """INSERT INTO sessions
            (id, started_at, ended_at, tool_call_count, model, provider,
             input_tokens, output_tokens, reasoning_tokens, total_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "sess_123",
                "2026-10-02T10:00:00Z",
                "2026-10-02T10:00:01Z",
                10,
                "fast-model",
                "openai",
                100,
                50,
                10,
                150,
            ),
        )

    usage_data = {
        "failed": True,
        "completed": False,
        "error_cause": "Timeout",
        "api_calls": 5,
        "session_id": "sess_123",
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    usage_path.write_text(json.dumps(usage_data))

    wrapper._enrich_usage(usage_path, home)

    result = json.loads(usage_path.read_text())
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["error_cause"] == "Timeout"
    assert result["model"] == "fast-model"
    assert result["provider"] == "openai"
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50
    assert result["reasoning_tokens"] == 10
    assert result["total_tokens"] == 150
    assert result["duration_ms"] == 1000
    assert result["tool_calls"] == 10


def test_success_path_no_double_count(tmp_path: Path) -> None:
    wrapper = _wrapper()
    home = tmp_path / "home"
    home.mkdir()
    usage_path = tmp_path / "usage.json"

    # create dummy state.db
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE sessions (
                id TEXT,
                started_at TEXT,
                ended_at TEXT,
                tool_call_count INTEGER,
                model TEXT,
                provider TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                total_tokens INTEGER
            )"""
        )
        db.execute(
            """INSERT INTO sessions
            (id, started_at, ended_at, tool_call_count, model, provider,
             input_tokens, output_tokens, reasoning_tokens, total_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "sess_123",
                "2026-10-02T10:00:00Z",
                "2026-10-02T10:00:01Z",
                10,
                "fast-model",
                "openai",
                100,
                50,
                10,
                150,
            ),
        )

    usage_data = {
        "failed": False,
        "completed": True,
        "api_calls": 5,
        "session_id": "sess_123",
        "input_tokens": 500,
        "output_tokens": 200,
        "reasoning_tokens": 50,
        "total_tokens": 700,
    }
    usage_path.write_text(json.dumps(usage_data))

    wrapper._enrich_usage(usage_path, home)

    result = json.loads(usage_path.read_text())
    assert result["input_tokens"] == 500
    assert result["output_tokens"] == 200
    assert result["reasoning_tokens"] == 50
    assert result.get("total_tokens") == 700
