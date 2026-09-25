"""Issue 128: the per-command timeout comes from the launch, and a harness that exits
with work still in flight is never a clean completion.

The transcripts under tests/fixtures_data/transcripts are the real harnesses' own
output, captured on 2026-09-25 from the pinned worker image against the stub model
server (tests/e2e/stub_model.py) under `--network none`, with a scripted command longer
than a small configured timeout:

- claude-code-2.1.280-background-killed: without the fix, a 40 s command moved to the
  background at 3 s; the CLI ended its turn and exited 0 at 9 s, killing it.
- claude-code-2.1.280-background-completed: the same with a 6 s command, which ended
  on its own before the CLI exited.
- codex-0.156.0-session-abandoned: unified exec yielded a 40 s command after 10 s; the
  turn ended and Codex exited 0 with the command never completed.
- codex-0.156.0-session-polled: the same command, the model polling with write_stdin;
  Codex waited and the command completed.
- hermes-0.19.0-processes-at-exit.json: Hermes's process registry at exit after a
  model-requested `background=true` command under `-z`. Its random `session_key` is
  replaced by a placeholder, which the secret scan would otherwise read as a key.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from crucible.adapters.execution.docker import LAUNCH_WRAPPER
from crucible.adapters.harness import base
from crucible.adapters.harness.agy import AgyAdapter
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.claude_code import in_flight as claude_in_flight
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.codex import in_flight as codex_in_flight
from crucible.adapters.harness.hermes import PROCESSES_NAME, USAGE_NAME, HermesAdapter
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
    assert launch.env["CRUCIBLE_AFTER_EXIT"] == (
        f"/home/worker/.hermes/processes.json={PROCESSES_NAME}"
    )


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


# ----- work in flight at exit ------------------------------------------------------


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


def test_codex_session_abandoned_at_exit_is_incomplete(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "codex-0.156.0-session-abandoned.jsonl")
    pending = codex_in_flight(directory / base.TRANSCRIPT_NAME)
    assert len(pending) == 1 and pending[0].startswith("command item_1: /bin/bash -lc")
    assert CodexAdapter().classify_exit(CLEAN, "", "", directory) is ExitClass.INCOMPLETE


def test_codex_session_polled_to_its_end_is_a_completion(tmp_path: Path) -> None:
    directory = report_dir(tmp_path, "codex-0.156.0-session-polled.jsonl")
    assert codex_in_flight(directory / base.TRANSCRIPT_NAME) == ()
    assert CodexAdapter().classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED


def hermes_report(tmp_path: Path, processes: bytes | None) -> Path:
    directory = report_dir(tmp_path)
    (directory / USAGE_NAME).write_text(
        json.dumps({"completed": True, "failed": False}), encoding="utf-8"
    )
    if processes is not None:
        (directory / PROCESSES_NAME).write_bytes(processes)
    return directory


def test_hermes_background_process_still_running_at_exit_is_incomplete(tmp_path: Path) -> None:
    fixture = (FIXTURES / "hermes-0.19.0-processes-at-exit.json").read_bytes()
    directory = hermes_report(tmp_path, fixture)
    adapter = HermesAdapter()
    assert adapter.classify_exit(CLEAN, "", "", directory) is ExitClass.INCOMPLETE
    (entry,) = adapter.parse_report(directory, CLEAN).in_flight
    assert entry.startswith("background process proc_5104debafe7b: python3 -c")


def test_hermes_with_nothing_running_at_exit_is_a_completion(tmp_path: Path) -> None:
    for processes in (None, b"[]"):
        directory = hermes_report(tmp_path / str(processes), processes)
        assert HermesAdapter().classify_exit(CLEAN, "", "", directory) is ExitClass.COMPLETED


def test_work_in_flight_never_overrides_a_failure_or_a_termination() -> None:
    pending = ("background task t1: build",)
    for exit_class in (ExitClass.CRASHED, ExitClass.TIMEOUT, ExitClass.KILLED, ExitClass.BLOCKED):
        assert base.with_in_flight(exit_class, pending) is exit_class
    assert base.with_in_flight(ExitClass.COMPLETED_WITHOUT_REPORT, pending) is (
        ExitClass.INCOMPLETE
    )
    assert base.with_in_flight(ExitClass.COMPLETED, ()) is ExitClass.COMPLETED


# ----- the launch wrapper's after-exit copy, under the host's bash --------------------


def run_wrapper(
    tmp_path: Path, script: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "harness.sh"
    harness.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    harness.chmod(0o755)
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", LAUNCH_WRAPPER, "crucible-launch", str(harness)],
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_wrapper_copies_the_named_state_after_the_harness_exits(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    state = tmp_path / "processes.json"
    # The harness writes its state as it exits; the copy is taken after, and the
    # wrapper's exit is still the harness's.
    completed = run_wrapper(
        tmp_path,
        f'echo \'[{{"session_id": "p1"}}]\' > {state}\nexit 3\n',
        {
            "CRUCIBLE_REPORT_DIR": str(report),
            "CRUCIBLE_TRANSCRIPT": str(report / "transcript.jsonl"),
            "CRUCIBLE_AFTER_EXIT": f"{state}=hermes-processes.json",
        },
    )
    assert completed.returncode == 3, completed.stderr
    assert json.loads((report / "hermes-processes.json").read_text()) == [{"session_id": "p1"}]


def test_the_wrapper_never_follows_a_link_or_writes_outside_the_report(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    secret = tmp_path / "credential"
    secret.write_text("not for the report", encoding="utf-8")
    link = tmp_path / "processes.json"
    link.symlink_to(secret)
    completed = run_wrapper(
        tmp_path,
        "exit 0\n",
        {
            "CRUCIBLE_REPORT_DIR": str(report),
            "CRUCIBLE_TRANSCRIPT": str(report / "transcript.jsonl"),
            "CRUCIBLE_AFTER_EXIT": f"{link}=hermes-processes.json {secret}=../escaped",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert sorted(p.name for p in report.iterdir()) == ["transcript.jsonl"]
    assert not (tmp_path / "escaped").exists()


def test_the_wrapper_replaces_a_link_planted_at_the_destination(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("untouched", encoding="utf-8")
    (report / "hermes-processes.json").symlink_to(elsewhere)
    state = tmp_path / "processes.json"
    state.write_text('[{"session_id": "p1"}]', encoding="utf-8")
    completed = run_wrapper(
        tmp_path,
        "exit 0\n",
        {
            "CRUCIBLE_REPORT_DIR": str(report),
            "CRUCIBLE_TRANSCRIPT": str(report / "transcript.jsonl"),
            "CRUCIBLE_AFTER_EXIT": f"{state}=hermes-processes.json",
        },
    )
    assert completed.returncode == 0, completed.stderr
    copied = report / "hermes-processes.json"
    assert not copied.is_symlink() and json.loads(copied.read_text()) == [{"session_id": "p1"}]
    assert elsewhere.read_text() == "untouched"
    assert sorted(p.name for p in report.iterdir()) == ["hermes-processes.json", "transcript.jsonl"]
