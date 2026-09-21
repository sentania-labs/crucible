#!/usr/bin/python3
"""Run Hermes, capture plain output, and enrich its usage record from per-run state."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


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


def _enrich_usage(usage_path: Path, home: Path) -> None:
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
        session_id = usage.get("session_id")
        with sqlite3.connect(home / "state.db") as database:
            if isinstance(session_id, str):
                row = database.execute(
                    "SELECT started_at, ended_at, tool_call_count FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            else:
                row = database.execute(
                    "SELECT started_at, ended_at, tool_call_count FROM sessions "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
        if row is not None:
            usage["duration_ms"] = _milliseconds(row[0], row[1])
            usage["tool_calls"] = row[2] if isinstance(row[2], int) else None
            temporary = usage_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(usage, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(usage_path)
    except (OSError, ValueError, sqlite3.Error):
        # The adapter records the original usage file or its parse anomaly. Enrichment
        # is secondary evidence and must not hide Hermes's own outcome.
        return


def main() -> int:
    usage_path = Path(os.environ["CRUCIBLE_HERMES_USAGE"])
    home = Path(os.environ["HERMES_HOME"])
    transcript_path = usage_path.parent / "transcript.jsonl"
    child = subprocess.Popen(
        ["/opt/hermes/bin/hermes", *sys.argv[1:]],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def forward(signum: int, _frame: object) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    assert child.stdout is not None
    with transcript_path.open("w", encoding="utf-8") as transcript:
        for line in child.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            transcript.write(line)
            transcript.flush()
    code = child.wait()
    _enrich_usage(usage_path, home)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
