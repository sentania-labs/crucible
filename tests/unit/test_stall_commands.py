"""Issue 152: a command in flight counts as activity for the stall clock.

The operator's decision, 2026-09-27: "a running command counts as activity. While a
harness reports a command in flight, the stall clock pauses, so the stall limit applies
to a worker doing nothing and the command timeout applies to a command running long."

The live trackers read the same evidence issue 128 reads after exit, from the log as it
arrives. The Claude Code and Codex cases replay the real transcripts in
tests/fixtures_data/transcripts (see test_command_timeout.py for how they were captured).
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crucible.adapters.execution.docker import LAUNCH_WRAPPER
from crucible.adapters.harness import base
from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.supervisor import command_activity_due, worker_stall_action
from crucible.ports.harness import CommandTracker
from tests.unit.test_command_timeout import context

FIXTURES = Path(__file__).parent.parent / "fixtures_data" / "transcripts"
START = datetime(2026, 9, 27, 13, tzinfo=UTC)
WARN = 300
FAIL = 1800


def running_now(live: CommandTracker) -> tuple[str, ...]:
    return live.running


def lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines(keepends=True)


def tracker(adapter: ClaudeCodeAdapter | CodexAdapter | HermesAdapter) -> CommandTracker:
    found = adapter.command_tracker()
    assert found is not None
    return found


# ----- the stall clock with a command in flight ---------------------------------------


def stall_timeline(*, command_until: int, end: int, tick: int = 5) -> tuple[int | None, int | None]:
    """Walk the supervisor's per-tick rule from START: the worker writes nothing after
    its first line, a command is in flight until `command_until` seconds, and each tick
    writes a command_running signal when one is due. Returns when the first warning and
    the stall came, in seconds, or None."""
    activity = START
    warned_at: datetime | None = None
    first_warn: int | None = None
    for second in range(tick, end + 1, tick):
        now = START + timedelta(seconds=second)
        if second < command_until and command_activity_due(now=now, last_activity=activity):
            activity = now
        action = worker_stall_action(
            now=now,
            last_activity=activity,
            last_signal=activity,
            warn_seconds=WARN,
            fail_seconds=FAIL,
            warned_at=warned_at,
        )
        if action == "fail":
            return first_warn, second
        if action == "warn":
            warned_at = now
            first_warn = first_warn if first_warn is not None else second
    return first_warn, None


def test_a_silent_command_longer_than_the_stall_limit_is_not_a_stall() -> None:
    # 50 minutes of silence inside a 60-minute command timeout: no warning, no stall.
    assert stall_timeline(command_until=3000, end=3000) == (None, None)


def test_the_stall_clock_resumes_when_the_command_ends() -> None:
    warned, stalled = stall_timeline(command_until=3000, end=6000)
    assert warned is not None and 3000 + WARN - 60 <= warned <= 3000 + WARN
    assert stalled is not None and 3000 + FAIL - 60 <= stalled <= 3000 + FAIL


def test_a_worker_with_no_command_in_flight_and_no_output_still_stalls() -> None:
    assert stall_timeline(command_until=0, end=6000) == (WARN, FAIL)


def test_a_command_signal_is_written_at_most_once_a_minute() -> None:
    assert command_activity_due(now=START, last_activity=None)
    assert not command_activity_due(now=START + timedelta(seconds=59), last_activity=START)
    assert command_activity_due(now=START + timedelta(seconds=60), last_activity=START)


# ----- Claude Code ---------------------------------------------------------------------


def test_claude_code_tool_use_is_in_flight_until_its_result() -> None:
    transcript = lines("claude-code-2.1.280-background-completed.jsonl")
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", transcript[0])
    (running,) = running_now(live)
    assert running.startswith("tool Bash: python3 -c")
    live.feed("stdout", "".join(transcript[1:3]))
    # The result has not come back and the CLI reports the command backgrounded.
    assert len(running_now(live)) == 2
    live.feed("stdout", transcript[3])
    (background,) = running_now(live)
    assert background.startswith("background task")
    live.feed("stdout", "".join(transcript[4:]))
    assert running_now(live) == ()


def test_claude_code_background_task_killed_at_exit_was_running_until_then() -> None:
    transcript = lines("claude-code-2.1.280-background-killed.jsonl")
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", "".join(transcript[:6]))
    (running,) = running_now(live)
    assert running.startswith("background task")
    live.feed("stdout", "".join(transcript[6:]))
    assert running_now(live) == ()


def test_claude_code_a_later_message_from_the_same_agent_closes_its_tools() -> None:
    """A result line lost (too long to hold) cannot pin the clock: the model is only
    asked again once every result is back."""
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", assistant("msg_1", tool("toolu_1", "make test")) + "\n")
    live.feed("stdout", assistant("msg_1", tool("toolu_2", "make lint")) + "\n")
    assert len(running_now(live)) == 2
    live.feed("stdout", assistant("msg_2", '{"type": "text", "text": "done"}') + "\n")
    assert running_now(live) == ()


def test_claude_code_a_subagent_message_does_not_close_its_parents_tool() -> None:
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", assistant("msg_1", tool("toolu_task", "")) + "\n")
    live.feed("stdout", assistant("msg_s1", tool("toolu_sub", "pytest"), parent="toolu_task"))
    live.feed("stdout", "\n")
    live.feed("stdout", assistant("msg_s2", '{"type": "text", "text": "ok"}', "toolu_task"))
    live.feed("stdout", "\n")
    (running,) = running_now(live)
    assert "toolu_task" in running


def test_a_line_split_across_chunks_is_read_whole_and_streams_stay_apart() -> None:
    line = assistant("msg_1", tool("toolu_1", "sleep 900"))
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", line[:20])
    live.feed("stderr", "a warning on the other stream\n")
    assert running_now(live) == ()
    live.feed("stdout", line[20:] + "\n")
    assert len(running_now(live)) == 1


def test_a_line_too_long_to_hold_is_dropped_and_reading_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(base, "LIVE_LINE_LIMIT", 64)
    live = tracker(ClaudeCodeAdapter())
    live.feed("stdout", "x" * 100)
    live.feed("stdout", "y" * 100)
    live.feed("stdout", '"tail of the long line"}\n')
    live.feed("stdout", assistant("m", tool("t", "ls"))[:60])
    assert running_now(live) == ()
    monkeypatch.setattr(base, "LIVE_LINE_LIMIT", 1024)
    live.feed("stdout", assistant("m", tool("t", "ls"))[60:] + "\n")
    assert len(running_now(live)) == 1


# ----- Codex ---------------------------------------------------------------------------


def test_codex_command_item_is_in_flight_until_it_completes() -> None:
    transcript = lines("codex-0.156.0-session-polled.jsonl")
    live = tracker(CodexAdapter())
    live.feed("stdout", "".join(transcript[:3]))
    assert running_now(live) == ()
    live.feed("stdout", transcript[3])
    (running,) = running_now(live)
    assert running.startswith("command item_1")
    live.feed("stdout", "".join(transcript[4:]))
    assert running_now(live) == ()


def test_codex_items_other_than_commands_do_not_count() -> None:
    live = tracker(CodexAdapter())
    live.feed("stdout", '{"type": "item.started", "item": {"id": "i", "type": "todo_list"}}\n')
    assert running_now(live) == ()


# ----- Hermes --------------------------------------------------------------------------


def test_hermes_follows_the_wrappers_count_of_its_process_registry() -> None:
    live = tracker(HermesAdapter())
    live.feed("stderr", "crucible-launch: commands running: 2\n")
    (running,) = running_now(live)
    assert running == "process registry: 2 running"
    live.feed("stdout", "some command said crucible-launch: commands running: 9\n")
    assert running_now(live) == (running,)
    live.feed("stderr", "crucible-launch: commands running: 0\n")
    assert running_now(live) == ()


def test_hermes_launch_names_its_registry_for_the_wrapper() -> None:
    launch = HermesAdapter().build_launch(
        context(endpoint="local", endpoint_url="http://gateway/v1")
    )
    assert launch.env["CRUCIBLE_IN_FLIGHT_FILE"] == "/home/worker/.hermes/processes.json"


# ----- no live evidence ----------------------------------------------------------------


def test_agy_and_the_script_harness_have_no_live_evidence() -> None:
    assert AgyAdapter().command_tracker() is None
    assert ScriptHarnessAdapter().command_tracker() is None


# ----- the launch wrapper's registry count ---------------------------------------------


def run_wrapper(tmp_path: Path, script: str, env: dict[str, str]) -> tuple[int, str, float]:
    harness = tmp_path / "harness.sh"
    harness.write_text(script, encoding="utf-8")
    harness.chmod(0o755)
    started = time.monotonic()
    completed = subprocess.run(
        ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", str(harness)],
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode, completed.stderr, time.monotonic() - started


def test_the_wrapper_reports_the_registry_count_while_the_harness_runs(tmp_path: Path) -> None:
    registry = tmp_path / "processes.json"
    registry.write_text(
        '[\n  {"session_id": "proc_1", "command": "make test"},\n'
        '  {"session_id": "proc_2", "command": "make e2e"}\n]',
        encoding="utf-8",
    )
    code, stderr, elapsed = run_wrapper(
        tmp_path, "sleep 1\nexit 4\n", {"CRUCIBLE_IN_FLIGHT_FILE": str(registry)}
    )
    # The exit is still the harness's, and the wrapper does not wait out its probe.
    assert code == 4 and elapsed < 8
    assert stderr.splitlines() == ["crucible-launch: commands running: 2"]


def test_the_wrapper_says_nothing_for_an_empty_missing_or_linked_registry(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    target = tmp_path / "target.json"
    target.write_text('[{"session_id": "p"}]', encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    for path in (empty, tmp_path / "missing.json", link):
        code, stderr, _ = run_wrapper(tmp_path, "sleep 1\n", {"CRUCIBLE_IN_FLIGHT_FILE": str(path)})
        assert code == 0 and stderr == "", path


def test_the_wrapper_starts_no_probe_without_a_registry(tmp_path: Path) -> None:
    code, stderr, elapsed = run_wrapper(tmp_path, "exit 0\n", {})
    assert code == 0 and stderr == "" and elapsed < 5


# ----- helpers -------------------------------------------------------------------------


def tool(tool_id: str, command: str) -> str:
    return (
        '{"type": "tool_use", "id": "' + tool_id + '", "name": "Bash", '
        '"input": {"command": "' + command + '"}}'
    )


def assistant(message_id: str, block: str, parent: str | None = None) -> str:
    owner = "null" if parent is None else f'"{parent}"'
    return (
        '{"type": "assistant", "message": {"id": "'
        + message_id
        + '", "content": ['
        + block
        + ']}, "parent_tool_use_id": '
        + owner
        + "}"
    )
