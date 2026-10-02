#!/usr/bin/python3
"""Run Hermes, capture plain output, and enrich its usage record from per-run state.

FDY-0140:
- The task's IDENTITY.md is put in the prompt itself, ahead of the pointer, so the model
  starts from the instructions rather than having to decide to read them.
- Hermes 0.19's `-z` builds its agent with a fixed 90-turn budget and reads no turn
  setting. The limit Crucible passes is applied by running Hermes's own entry point
  under a small bootstrap that sets that budget when the agent is built. The context
  window is Hermes's own `model.context_length` setting, written to its home.
- Nothing is added to PATH. Hermes is started by the virtual environment's own Python,
  by path, so every command the model runs sees the image's toolchain, not the venv's.
  (Hermes puts the directory its `hermes` command is found in first on each subshell's
  PATH; the image links `/usr/local/bin/hermes`, already on PATH, so that is a no-op.)
- While Hermes works, a line goes to stderr each time its session store changes: `-z`
  writes nothing else until it ends, and a quiet worker is otherwise indistinguishable
  from a stuck one.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERMES_PYTHON = "/opt/hermes/bin/python"
# The Hermes the image pins (images/worker/Dockerfile). The usage patch in BOOTSTRAP and
# the session columns below are written against it; another version fails loudly.
HERMES_VERSION = "0.19.0"
# How often the session store is looked at for progress.
PROGRESS_SECONDS = 15.0
PROGRESS_LINE = "crucible-hermes: working, session updated"

# Run inside the Hermes virtual environment. It changes nothing but the turn budget an
# agent is built with when the caller named none, and only once `run_agent` is imported
# the ordinary way, so Hermes's own import order (its approval mode is read at import)
# is untouched. #387: it also gives Hermes's usage file the failure cause an early
# return from the agent reports only in its result's `error`.
BOOTSTRAP = r"""
import importlib.abc
import importlib.metadata
import importlib.util
import os
import sys

HERMES_VERSION = "@HERMES_VERSION@"

LIMIT = int(os.environ.get("CRUCIBLE_HERMES_MAX_TURNS") or 0)
# `max_iterations` is the tenth parameter of AIAgent.__init__ after self (0.19).
POSITION = 9


class _TurnBudget(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "run_agent":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            original = module.AIAgent.__init__

            def __init__(self, *args, **kwargs):
                if len(args) <= POSITION and "max_iterations" not in kwargs:
                    kwargs["max_iterations"] = LIMIT
                original(self, *args, **kwargs)

            module.AIAgent.__init__ = __init__

        loader.exec_module = exec_module
        return spec


class _FailureCause(importlib.abc.MetaPathFinder):
    # #387: when the agent returns failed rather than raising (a provider error it gave
    # up on, for example), hermes_cli.oneshot writes the usage file with no `failure`
    # and the cause is lost with the result's `error`. Pass that error on as the
    # failure. Written against 0.19.0's _write_usage_file(path, result, failure=None).
    def find_spec(self, name, path, target=None):
        if name != "hermes_cli.oneshot":
            return None
        sys.meta_path.remove(self)
        installed = importlib.metadata.version("hermes-agent")
        if installed != HERMES_VERSION:
            raise RuntimeError(
                f"crucible-hermes: the usage-file patch is for hermes-agent "
                f"{HERMES_VERSION}, and {installed} is installed"
            )
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            original = module._write_usage_file

            def _write_usage_file(path, result, failure=None):
                error = result.get("error") if isinstance(result, dict) else None
                if failure is None and result.get("failed") and isinstance(error, str) and error:
                    failure = error
                original(path, result, failure)

            module._write_usage_file = _write_usage_file

        loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _FailureCause())
if LIMIT > 0:
    sys.meta_path.insert(0, _TurnBudget())
sys.argv = ["hermes", *sys.argv[1:]]
from hermes_cli.main import main

sys.exit(main())
""".replace("@HERMES_VERSION@", HERMES_VERSION)


def _milliseconds(started: object, ended: object) -> int | None:
    if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
        return max(0, int((ended - started) * 1000))
    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        left = datetime.fromisoformat(started.replace("Z", "+00:00"))
        right = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((right - left).total_seconds() * 1000))


def _limit(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name) or 0))
    except ValueError:
        return 0


class SessionSchemaChanged(RuntimeError):
    """Hermes's session table lacks a column the usage enrichment reads (#387)."""


# usage field -> sessions column (hermes_state.py SCHEMA_VERSION 22 in HERMES_VERSION).
SESSION_FIELDS = (
    ("model", "model"),
    ("provider", "billing_provider"),
    ("estimated_cost_usd", "estimated_cost_usd"),
)
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
# Hermes's own session_total_tokens: prompt (input, cache read and cache write) plus
# output. The row has no total column.
TOTAL_PARTS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
SESSION_COLUMNS = frozenset(
    {"id", "parent_session_id", "started_at", "ended_at", "tool_call_count"}
    | {column for _, column in SESSION_FIELDS}
    | set(TOKEN_FIELDS)
)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_columns(database: sqlite3.Connection) -> None:
    present = {row[1] for row in database.execute("PRAGMA table_info(sessions)")}
    missing = sorted(SESSION_COLUMNS - present)
    if missing:
        raise SessionSchemaChanged(
            f"crucible-hermes: Hermes's sessions table has no {', '.join(missing)} "
            f"column; the usage enrichment is written against hermes-agent {HERMES_VERSION}"
        )


def _run_sessions(database: sqlite3.Connection) -> dict[str, object] | None:
    """#387: the whole run's session state when Hermes wrote no session id.

    The home is fresh for every launch, so every row in it is this run's. After context
    compression Hermes ends the row and opens a child (parent_session_id), and every
    later call's tokens go to the child, so the run's totals are the sum of all rows.
    The session id is the run's top-level row; model and provider are the newest row's.
    """
    root = database.execute(
        "SELECT id FROM sessions WHERE parent_session_id IS NULL "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if root is None:
        return None
    newest = database.execute(
        "SELECT model, billing_provider FROM sessions ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    totals = database.execute(
        "SELECT MIN(started_at), MAX(ended_at), SUM(tool_call_count), "
        "SUM(estimated_cost_usd), "
        + ", ".join(f"SUM({column})" for column in TOKEN_FIELDS)
        + " FROM sessions"
    ).fetchone()
    started, ended, calls, cost, *tokens = totals
    return {
        "id": root[0],
        "model": newest[0],
        "billing_provider": newest[1],
        "started_at": started,
        "ended_at": ended,
        "tool_call_count": calls,
        "estimated_cost_usd": cost,
        **dict(zip(TOKEN_FIELDS, tokens, strict=True)),
    }


def _one_session(database: sqlite3.Connection, session_id: str) -> dict[str, object] | None:
    database.row_factory = sqlite3.Row
    row = database.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row is not None else None


def _fill_from_session(usage: dict[str, object], session: dict[str, object]) -> None:
    """#387: fill what the usage record lacks from Hermes's saved session state.

    Hermes 0.19 writes its usage file from the agent's result, which is empty when the
    agent raised and carries no tokens on an early failed return, so a failed run
    reports no model, session or tokens though the session rows hold them. A value
    Hermes wrote is never replaced or added to, and the tokens come from one source:
    when Hermes wrote any token count, all of them and the total are Hermes's.
    `failed` and `failure` are left alone; a `completed` Hermes left null on a failed
    run is false, so the adapter can read the record.
    """
    usage["duration_ms"] = _milliseconds(session["started_at"], session["ended_at"])
    usage["tool_calls"] = _integer(session["tool_call_count"])
    if usage.get("session_id") is None and session["id"] is not None:
        usage["session_id"] = session["id"]
    for field, column in SESSION_FIELDS:
        if usage.get(field) is None and session[column] is not None:
            usage[field] = session[column]
    if all(usage.get(field) is None for field in TOKEN_FIELDS):
        for field in TOKEN_FIELDS:
            usage[field] = _integer(session[field])
    if usage.get("total_tokens") is None:
        parts = [_integer(usage.get(field)) for field in TOTAL_PARTS]
        if any(part is not None for part in parts):
            usage["total_tokens"] = sum(part for part in parts if part is not None)


def _session(home: Path, session_id: object) -> dict[str, object] | None:
    path = home / "state.db"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as database:
            _check_columns(database)
            if isinstance(session_id, str):
                return _one_session(database, session_id)
            return _run_sessions(database)
    except sqlite3.Error:
        return None


def _enrich_usage(usage_path: Path, home: Path, max_turns: int = 0) -> None:
    """Add the run's duration, tool calls and, where Hermes left them out, its session,
    model and tokens to the usage record. A session table without the columns this was
    written against raises SessionSchemaChanged rather than writing silent nulls."""
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        if max_turns > 0:
            # FDY-0140: whether the run ended on its turn budget. Hermes reports it as
            # not completed after asking the model for a summary, one call past it.
            calls = usage.get("api_calls")
            usage["max_turns"] = max_turns
            usage["turn_limit_reached"] = (
                isinstance(calls, int) and calls >= max_turns and usage.get("completed") is not True
            )
        if usage.get("failed") is True and usage.get("completed") is None:
            # #387: Hermes 0.19 writes `completed: null` when its agent raised.
            usage["completed"] = False
        session = _session(home, usage.get("session_id"))
        if session is not None:
            _fill_from_session(usage, session)
        temporary = usage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(usage, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(usage_path)
    except (OSError, ValueError, AttributeError, TypeError):
        # The adapter records the original usage file or its parse anomaly. Enrichment
        # is secondary evidence and must not hide Hermes's own outcome.
        return


def inline_identity(argv: list[str], identity: str | None) -> list[str]:
    """The prompt after `-z`, with the identity file's text ahead of it. Unchanged when
    there is no identity file (a harness test has none) or no `-z`."""
    if not identity or "-z" not in argv:
        return argv
    try:
        text = Path(identity).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return argv
    if not text:
        return argv
    index = argv.index("-z") + 1
    if index >= len(argv):
        return argv
    prompt = f"{text}\n\n---\n\nThe task above is {identity}. {argv[index]}"
    return [*argv[:index], prompt, *argv[index + 1 :]]


def write_settings(home: Path, context_length: int) -> None:
    """Hermes's own `model.context_length`, in the per-run home it reads config from.
    Only written when a limit was given; the home starts empty on every launch."""
    if context_length <= 0:
        return
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    config.write_text(f"model:\n  context_length: {context_length}\n", encoding="utf-8")


def _session_stamp(home: Path) -> tuple[int, ...]:
    stamps = []
    for name in ("state.db", "state.db-wal"):
        try:
            stamps.append((home / name).stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    return tuple(stamps)


def watch_progress(
    home: Path, stop: threading.Event, interval: float = PROGRESS_SECONDS
) -> None:
    """Write PROGRESS_LINE to stderr whenever Hermes's session store has changed since
    the last look. Hermes writes it after every model turn and tool call."""
    last = _session_stamp(home)
    while not stop.wait(interval):
        current = _session_stamp(home)
        if current != last:
            last = current
            print(PROGRESS_LINE, file=sys.stderr, flush=True)


def main() -> int:
    usage_path = Path(os.environ["CRUCIBLE_HERMES_USAGE"])
    home = Path(os.environ["HERMES_HOME"])
    max_turns = _limit("CRUCIBLE_HERMES_MAX_TURNS")
    write_settings(home, _limit("CRUCIBLE_HERMES_CONTEXT_LENGTH"))
    argv = inline_identity(sys.argv[1:], os.environ.get("CRUCIBLE_HERMES_IDENTITY"))
    # Stdout stays inherited. Crucible's launch wrapper is the sole transcript writer.
    # -P: the working directory is the task's checkout, and a module there named like
    # one of Hermes's own (`cli`, `tools`, `agent`) must never be imported in its place.
    try:
        child = subprocess.Popen([HERMES_PYTHON, "-P", "-c", BOOTSTRAP, *argv])
    except OSError:
        # An identity too long for one argument (E2BIG): the pointer alone still works.
        child = subprocess.Popen([HERMES_PYTHON, "-P", "-c", BOOTSTRAP, *sys.argv[1:]])

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    stop = threading.Event()
    watcher = threading.Thread(target=watch_progress, args=(home, stop), daemon=True)
    watcher.start()
    code = child.wait()
    stop.set()
    _enrich_usage(usage_path, home, max_turns)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
