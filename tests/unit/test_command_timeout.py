"""Issue 128: the per-command timeout comes from the launch, and a harness that exits
with a command it was waiting on cut off is never a clean completion. Issue 153: a
background process the worker left running is not unfinished work.

The transcripts under tests/fixtures_data/transcripts are the real harnesses' own
output, captured on 2026-09-25 from the pinned worker image against the stub model
server (tests/e2e/stub_model.py) under `--network none`, with a scripted command longer
than a small configured timeout:

- claude-code-2.1.280-background-killed: without the fix, a 40 s command moved to the
  background at 3 s; the CLI ended its turn and exited 0 at 9 s, killing it.
- claude-code-2.1.280-background-completed: the same with a 6 s command, which ended
  on its own before the CLI exited.
- claude-code-2.1.280-background-requested: captured 2026-09-27 the same way, without
  the fix, the model asking for `run_in_background` on a 20 s command; the CLI exited 0
  and killed it. The `init` line is dropped, as in the others.
- codex-0.156.0-session-abandoned: unified exec yielded a 40 s command after 10 s; the
  turn ended and Codex exited 0 with the command never completed.
- codex-0.156.0-session-polled: the same command, the model polling with write_stdin;
  Codex waited and the command completed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.harness import base
from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.claude_code import in_flight as claude_in_flight
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import USAGE_NAME, HermesAdapter
from crucible.contracts.policy import Bounds, PolicyV1
from crucible.contracts.task_contract import ExecutionRequest
from crucible.domain.command_timeout import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    effective_command_timeout_ms,
    policy_bounds,
)
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import ExitInfo, LaunchContext
from tests.unit.test_policy_schema import seeded_policy_v3

FIXTURES = Path(__file__).parent.parent / "fixtures_data" / "transcripts"
CLEAN = ExitInfo(exit_code=0, report_present=True)


def context(**overrides: Any) -> LaunchContext:
    values: dict[str, Any] = {
        "attempt_id": "01ATTEMPT0000000000000000A",
        "model": "model-x",
        "effort": None,
        "timeout_seconds": 7200,
        "identity_mount": "/crucible/identity",
        "report_mount": "/crucible/report",
        "repo_mount": "/crucible/repo",
        "credential_mounted": True,
    }
    values.update(overrides)
    return LaunchContext(**values)


def report_dir(tmp_path: Path, transcript: str | None = None) -> Path:
    directory = tmp_path / "report"
    directory.mkdir(parents=True)
    if transcript is not None:
        (directory / base.TRANSCRIPT_NAME).write_bytes((FIXTURES / transcript).read_bytes())
    return directory


# ----- where the value comes from --------------------------------------------------


def policy(bounds: dict[str, int] | None = None) -> dict[str, Any]:
    document = seeded_policy_v3()
    if bounds is not None:
        document["limits"]["command_timeout_ms"] = bounds
    return document


def contract(command_timeout_ms: int | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"timeout_seconds": 3600}
    if command_timeout_ms is not None:
        request["command_timeout_ms"] = command_timeout_ms
    return {"execution_request": request}


def test_a_policy_version_without_the_field_takes_the_operators_default() -> None:
    parsed = PolicyV1.model_validate(policy())
    assert parsed.limits.command_timeout_ms.default == DEFAULT_COMMAND_TIMEOUT_MS == 3_600_000
    assert policy_bounds(policy())["default"] == 3_600_000
    assert effective_command_timeout_ms(policy(), contract(), 7200) == 3_600_000


def test_the_policy_default_applies_when_the_contract_sets_none() -> None:
    bounds = {"min": 1000, "max": 7_200_000, "default": 1_800_000}
    assert effective_command_timeout_ms(policy(bounds), contract(), 7200) == 1_800_000


def test_a_contract_narrows_the_policy_default() -> None:
    bounds = {"min": 1000, "max": 7_200_000, "default": 1_800_000}
    assert effective_command_timeout_ms(policy(bounds), contract(600_000), 7200) == 600_000


def test_the_launch_never_exceeds_the_attempts_own_timeout() -> None:
    # A 60-minute default on a 5-minute attempt launches with 5 minutes.
    assert effective_command_timeout_ms(policy(), contract(), 300) == 300_000
    assert context(timeout_seconds=300, command_timeout_ms=None).command_timeout == 300_000
    assert context(timeout_seconds=300, command_timeout_ms=900_000).command_timeout == 300_000


def test_policy_bounds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="min <= default <= max"):
        Bounds(min=1000, max=2000, default=3000)
    document = policy({"min": 5000, "max": 4000, "default": 4500})
    with pytest.raises(ValidationError):
        PolicyV1.model_validate(document)


def execution_request(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "tier": "standard",
        "provider": "docker",
        "timeout_seconds": 600,
        "rationale": "tests",
    }
    values.update(overrides)
    return values


def test_a_contract_may_not_set_a_command_timeout_above_its_own_timeout() -> None:
    ExecutionRequest.model_validate(execution_request(command_timeout_ms=600_000))
    with pytest.raises(ValidationError, match="must not exceed timeout_seconds"):
        ExecutionRequest.model_validate(execution_request(command_timeout_ms=600_001))
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(execution_request(command_timeout_ms=0))


# ----- what each harness is launched with ------------------------------------------


def test_claude_code_turns_off_backgrounding_and_takes_the_launch_timeout() -> None:
    launch = ClaudeCodeAdapter().build_launch(context(command_timeout_ms=1_234_000))
    assert launch.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert launch.env["BASH_DEFAULT_TIMEOUT_MS"] == "1234000"
    assert launch.env["BASH_MAX_TIMEOUT_MS"] == "1234000"
    # Set with or without a credential: a probe or an unauthenticated run is the same CLI.
    bare = ClaudeCodeAdapter().build_launch(
        context(command_timeout_ms=1_234_000, credential_mounted=False)
    )
    assert bare.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_codex_polls_as_long_as_the_launch_timeout() -> None:
    argv = CodexAdapter().build_launch(context(command_timeout_ms=1_234_000)).argv
    assert "background_terminal_max_timeout=1234000" in argv
    assert argv[argv.index("background_terminal_max_timeout=1234000") - 1] == "-c"


def test_hermes_takes_the_launch_timeout_in_whole_seconds() -> None:
    launch = HermesAdapter().build_launch(
        context(
            command_timeout_ms=1_234_567,
            endpoint="local",
            endpoint_url="http://spark.example.internal:11434/v1",
        )
    )
    # Rounded up: Hermes never gets less than the launch asked for.
    assert launch.env["TERMINAL_TIMEOUT"] == "1235"
    assert launch.env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] == "1235"
    # Issue 153: nothing is copied out after exit; the registry is only counted live.
    assert "CRUCIBLE_AFTER_EXIT" not in launch.env


def test_agy_has_no_command_timeout_to_set_so_the_prompt_asks_for_blocking() -> None:
    # 1.2.8 offers no setting (the adapter's docstring cites the evidence); its print
    # timeout stays the attempt's own, and the prompt carries the instruction instead.
    launch = AgyAdapter().build_launch(context(command_timeout_ms=90_000, timeout_seconds=1200))
    assert launch.argv[launch.argv.index("--print-timeout") + 1] == "1200s"
    assert launch.env == {}
    prompt = launch.argv[2]
    assert prompt.startswith(base.POINTER_PROMPT)
    assert "blocking" in prompt and "up to 2 minutes" in prompt
    assert len(prompt) < 1024


# ----- a command cut off at exit, and a background process left running --------------


def test_claude_code_backgrounded_command_killed_at_exit_is_incomplete(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "claude-code-2.1.280-background-killed.jsonl")
    pending = claude_in_flight(directory / base.TRANSCRIPT_NAME)
    assert pending == ("background task bkw1w8e0h: the scripted command",)
    adapter = ClaudeCodeAdapter()
    assert adapter.classify_exit(CLEAN, "", "", directory) is ExitClass.INCOMPLETE
    assert adapter.parse_report(directory, CLEAN).in_flight == pending


def test_claude_code_backgrounded_command_that_finished_is_a_completion(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "claude-code-2.1.280-background-completed.jsonl")
    assert claude_in_flight(directory / base.TRANSCRIPT_NAME) == ()
    assert ClaudeCodeAdapter().classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED


def test_claude_code_task_the_model_stopped_during_the_run_is_not_in_flight(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / base.TRANSCRIPT_NAME
    events = [
        {"type": "system", "subtype": "task_started", "task_id": "t1", "is_backgrounded": True},
        {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "t1",
            "patch": {"status": "killed"},
        },
        {"type": "result", "subtype": "success"},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    assert claude_in_flight(transcript) == ()
    # The same kill after the final result is the exit taking the command down.
    transcript.write_text(
        "\n".join(json.dumps(e) for e in (events[0], events[2], events[1])) + "\n",
        encoding="utf-8",
    )
    assert claude_in_flight(transcript) == ("background task t1: t1",)


def test_claude_code_background_task_the_model_asked_for_is_a_completion(
    tmp_path: Path,
) -> None:
    """Issue 153: the same kill at exit, but the model asked for the background with
    `run_in_background`: a process it chose to leave, not a blocking call cut off."""
    directory = report_dir(tmp_path, "claude-code-2.1.280-background-requested.jsonl")
    assert claude_in_flight(directory / base.TRANSCRIPT_NAME) == ()
    adapter = ClaudeCodeAdapter()
    assert adapter.classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED
    assert adapter.parse_report(directory, CLEAN).in_flight == ()


def test_claude_code_counts_only_the_tasks_it_backgrounded_on_its_own(tmp_path: Path) -> None:
    def tool_use(tool_id: str, **extra: Any) -> dict[str, Any]:
        arguments = {"command": "make build", **extra}
        block = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": arguments}
        return {"type": "assistant", "message": {"id": tool_id, "content": [block]}}

    def started(task_id: str, tool_id: str) -> dict[str, Any]:
        return {
            "type": "system",
            "subtype": "task_started",
            "task_id": task_id,
            "tool_use_id": tool_id,
            "is_backgrounded": True,
            "description": task_id,
        }

    events = [
        tool_use("u1", run_in_background=True),
        started("chosen", "u1"),
        tool_use("u2"),
        started("moved", "u2"),
        # Only a literal true is the model's request.
        tool_use("u3", run_in_background="yes"),
        started("unclear", "u3"),
        {"type": "result", "subtype": "success"},
    ]
    transcript = tmp_path / base.TRANSCRIPT_NAME
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    assert claude_in_flight(transcript) == (
        "background task moved: moved",
        "background task unclear: unclear",
    )


def test_codex_session_left_open_at_exit_is_a_completion(tmp_path: Path) -> None:
    """Issue 153: a unified-exec session the model ended its turn on is a background
    process; Codex only exits once the turn is over, never mid-call."""
    directory = report_dir(tmp_path, "codex-0.156.0-session-abandoned.jsonl")
    adapter = CodexAdapter()
    assert adapter.classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED
    assert adapter.parse_report(directory, CLEAN).in_flight == ()


def test_codex_session_polled_to_its_end_is_a_completion(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "codex-0.156.0-session-polled.jsonl")
    assert CodexAdapter().classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED


def test_codex_failures_keep_their_class(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "codex-0.156.0-session-abandoned.jsonl")
    crashed = ExitInfo(exit_code=1, report_present=True)
    assert CodexAdapter().classify_exit(crashed, "", "", directory) is ExitClass.CRASHED


def test_hermes_background_process_still_running_at_exit_is_a_completion(
    tmp_path: Path,
) -> None:
    """Issue 153: the launch wrapper's last count said a background process was running
    when Hermes exited; that does not change the class, and nothing is recorded."""
    directory = report_dir(tmp_path)
    (directory / USAGE_NAME).write_text(
        json.dumps({"completed": True, "failed": False}), encoding="utf-8"
    )
    adapter = HermesAdapter()
    stderr = "crucible-launch: commands running: 1\n"
    assert adapter.classify_exit(CLEAN, "", stderr, directory) is ExitClass.COMPLETED
    assert adapter.parse_report(directory, CLEAN).in_flight == ()


def test_work_in_flight_never_overrides_a_failure_or_a_termination() -> None:
    pending = ("background task t1: build",)
    for exit_class in (ExitClass.CRASHED, ExitClass.TIMEOUT, ExitClass.KILLED, ExitClass.BLOCKED):
        assert base.with_in_flight(exit_class, pending) is exit_class
    assert base.with_in_flight(ExitClass.COMPLETED_WITHOUT_REPORT, pending) is (
        ExitClass.INCOMPLETE
    )
    assert base.with_in_flight(ExitClass.COMPLETED, ()) is ExitClass.COMPLETED
