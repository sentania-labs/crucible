"""The command-timeout tier (issue 128): each real harness in the pinned worker image,
driven by the stub model server, with a scripted command longer than a small
configured timeout. Local and CI-safe in what it touches: no login, no credential, no
subscription, and `--network none`, so the stub on loopback is the only model the
harness can reach. It needs the worker image (`make images`) and a Docker daemon.

Each harness is launched with the adapter's own argv and environment, through the
provider's launch wrapper, and its report directory is classified by the adapter, so
what is proven is the launch Crucible actually sends and the class it would record.
For each: the trap reproduced without the setting, then the setting making the harness
end or wait for the command instead of exiting with it running. AGY cannot run without a
Google login and is not in this tier (its adapter documents what is and is not known).

    make e2e-command-timeout
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.docker import LAUNCH_WRAPPER
from crucible.adapters.harness.base import TRANSCRIPT_NAME
from crucible.adapters.harness.claude_code import ClaudeCodeAdapter
from crucible.adapters.harness.codex import CodexAdapter
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.domain.exit_class import ExitClass
from crucible.ports.execution import IDENTITY_MOUNT, REPO_MOUNT, REPORT_MOUNT
from crucible.ports.harness import AdapterLaunch, ExitInfo, HarnessAdapter, LaunchContext

pytestmark = [
    pytest.mark.e2e_command_timeout,
    pytest.mark.skipif(
        not os.environ.get("CRUCIBLE_E2E_COMMAND_TIMEOUT"), reason="needs make e2e-command-timeout"
    ),
]

ROOT = Path(__file__).resolve().parents[2]
STUB = Path(__file__).with_name("stub_model.py")
STUB_URL = "http://127.0.0.1:8765"
DOCKER = shlex.split(os.environ.get("CRUCIBLE_E2E_DOCKER", "docker"))
MARKER = "/tmp/command-finished"


def worker_image() -> str:
    for line in (ROOT / "images" / "manifest.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("WORKER="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("images/manifest.env names no WORKER image")


def long_command(seconds: int) -> str:
    # Not `sleep`: Claude Code never auto-backgrounds a command that starts with it.
    return f'python3 -c "import time; time.sleep({seconds})"; echo finished > {MARKER}'


@dataclass
class Run:
    exit_code: int
    elapsed: float
    marker: bool
    report: Path
    stub_log: list[dict[str, Any]]

    def classify(self, adapter: HarnessAdapter) -> ExitClass:
        # The stub writes no CompletionClaimV1; what is under test is the exit and the
        # harness's own record of what was running, so the report is taken as present.
        exit = ExitInfo(exit_code=self.exit_code, report_present=True)
        return adapter.classify_exit(exit, "", "", self.report)

    def tool_outputs(self) -> str:
        return json.dumps(
            [entry["body"].get("messages") or entry["body"].get("input") for entry in self.stub_log]
        )


def run_harness(
    tmp_path: Path,
    launch: AdapterLaunch,
    *,
    command: str,
    extra_env: dict[str, str],
    extra_argv: tuple[str, ...] = (),
    drop_env: tuple[str, ...] = (),
    linger: int = 0,
) -> Run:
    """One harness run in the worker image, through Crucible's launch wrapper."""
    report = tmp_path / "report"
    identity = tmp_path / "identity"
    stub = tmp_path / "stub"
    repo = tmp_path / "repo"
    for directory in (report, identity, stub, repo):
        directory.mkdir()
        directory.chmod(0o777)
    (identity / "IDENTITY.md").write_text("Run the command you are given.\n", encoding="utf-8")
    (stub / "stub_model.py").write_bytes(STUB.read_bytes())
    (stub / "wrapper.sh").write_text(LAUNCH_WRAPPER, encoding="utf-8")
    env = {k: v for k, v in launch.env.items() if k not in drop_env}
    env.update(
        {
            "CRUCIBLE_REPORT_DIR": REPORT_MOUNT,
            "CRUCIBLE_PROMPT": launch.stdin_text,
            "CRUCIBLE_STDIN_FILES": " ".join(launch.stdin_files),
            "CRUCIBLE_TRANSCRIPT": launch.transcript_path or f"{REPORT_MOUNT}/{TRANSCRIPT_NAME}",
            "STUB_COMMAND": command,
            "STUB_LOG": f"{REPORT_MOUNT}/stub-model.jsonl",
            **extra_env,
        }
    )
    # The stub runs beside the harness on loopback; the harness then runs exactly as a
    # worker's does, and the marker says whether the command ever finished. `linger`
    # keeps the container up after the harness exits, so a command it left running
    # would still finish and show the marker; the marker is read before that wait.
    runner = (
        "set -u\n"
        "mkdir -p /home/worker/.hermes /tmp/cfg /tmp/codex\n"
        "python3 /stub/stub_model.py 8765 >/dev/null 2>&1 &\n"
        "sleep 1\n"
        f"cd {REPO_MOUNT}\n"
        'bash -o pipefail -c "$(cat /stub/wrapper.sh)" crucible-launch "$@" '
        f">{REPORT_MOUNT}/stdout.txt 2>{REPORT_MOUNT}/stderr.txt\n"
        "status=$?\n"
        f"if [ -f {MARKER} ]; then echo yes > {REPORT_MOUNT}/marker; fi\n"
        f"sleep {linger}\n"
        'exit "$status"\n'
    )
    (stub / "runner.sh").write_text(runner, encoding="utf-8")
    argv = [
        *DOCKER,
        "run",
        "--rm",
        "--network",
        "none",
        "-v",
        f"{report}:{REPORT_MOUNT}",
        "-v",
        f"{identity}:{IDENTITY_MOUNT}:ro",
        "-v",
        f"{stub}:/stub:ro",
        "-v",
        f"{repo}:{REPO_MOUNT}",
        *(arg for key, value in sorted(env.items()) for arg in ("-e", f"{key}={value}")),
        "--entrypoint",
        "bash",
        worker_image(),
        "/stub/runner.sh",
        *launch.argv,
        *extra_argv,
    ]
    started = time.monotonic()
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    elapsed = time.monotonic() - started - linger
    log = report / "stub-model.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert entries, f"the harness never reached the stub: {completed.stderr[-2000:]}"
    return Run(completed.returncode, elapsed, (report / "marker").exists(), report, entries)


def context(timeout_ms: int, **overrides: Any) -> LaunchContext:
    values: dict[str, Any] = {
        "attempt_id": "01ATTEMPT0000000000000000A",
        "model": "stub-model",
        "effort": None,
        "timeout_seconds": 3600,
        "identity_mount": IDENTITY_MOUNT,
        "report_mount": REPORT_MOUNT,
        "repo_mount": REPO_MOUNT,
        "credential_mounted": False,
        "command_timeout_ms": timeout_ms,
    }
    values.update(overrides)
    return LaunchContext(**values)


# ----- Claude Code ---------------------------------------------------------------------

CLAUDE_ENV = {
    "ANTHROPIC_BASE_URL": STUB_URL,
    "ANTHROPIC_API_KEY": "stub-key-not-a-credential",
    "CLAUDE_CONFIG_DIR": "/tmp/cfg",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def claude(timeout_ms: int) -> AdapterLaunch:
    return ClaudeCodeAdapter().build_launch(context(timeout_ms, model="claude-sonnet-5"))


def test_claude_code_trap_without_the_setting_is_classified_incomplete(tmp_path: Path) -> None:
    run = run_harness(
        tmp_path,
        claude(3000),
        command=long_command(40),
        extra_env=CLAUDE_ENV,
        drop_env=("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",),
    )
    assert run.exit_code == 0 and not run.marker and run.elapsed < 35
    assert "moved to the background" in run.tool_outputs()
    assert run.classify(ClaudeCodeAdapter()) is ExitClass.INCOMPLETE


def test_claude_code_ends_a_command_at_the_launch_timeout_instead(tmp_path: Path) -> None:
    run = run_harness(tmp_path, claude(3000), command=long_command(40), extra_env=CLAUDE_ENV)
    assert run.exit_code == 0 and not run.marker and run.elapsed < 35
    outputs = run.tool_outputs()
    assert "Command timed out after 3s" in outputs and "moved to the background" not in outputs
    assert run.classify(ClaudeCodeAdapter()) is ExitClass.COMPLETED


def test_claude_code_waits_for_a_command_within_the_launch_timeout(tmp_path: Path) -> None:
    run = run_harness(tmp_path, claude(60_000), command=long_command(8), extra_env=CLAUDE_ENV)
    assert run.exit_code == 0 and run.marker and run.elapsed >= 8
    assert run.classify(ClaudeCodeAdapter()) is ExitClass.COMPLETED


# ----- Codex ---------------------------------------------------------------------------

CODEX_ENV = {"CODEX_HOME": "/tmp/codex", "STUB_KEY": "stub-key-not-a-credential"}
CODEX_PROVIDER = (
    "-c",
    "model_provider=stub",
    "-c",
    'model_providers.stub={name="stub",base_url="' + STUB_URL + '/v1",'
    'wire_api="responses",env_key="STUB_KEY"}',
)


def codex(timeout_ms: int) -> AdapterLaunch:
    return CodexAdapter().build_launch(context(timeout_ms))


def test_codex_trap_a_session_left_running_is_classified_incomplete(tmp_path: Path) -> None:
    run = run_harness(
        tmp_path,
        codex(60_000),
        command=long_command(40),
        extra_env=CODEX_ENV,
        extra_argv=CODEX_PROVIDER,
    )
    assert run.exit_code == 0 and not run.marker and run.elapsed < 35
    assert "Process running with session ID" in run.tool_outputs()
    assert run.classify(CodexAdapter()) is ExitClass.INCOMPLETE


def test_codex_polls_a_long_command_to_its_end_within_the_launch_window(tmp_path: Path) -> None:
    run = run_harness(
        tmp_path,
        codex(8000),
        command=long_command(30),
        extra_env={**CODEX_ENV, "STUB_POLL": "1"},
        extra_argv=CODEX_PROVIDER,
    )
    assert run.exit_code == 0 and run.marker
    # Each empty poll asked for 300 s and waited the launch's 8 s at most.
    polls = [e for e in run.stub_log if "Process running with session ID" in json.dumps(e["body"])]
    assert polls, "the harness never answered with a running session"
    walls = [
        float(part.split()[0])
        for entry in run.stub_log
        for part in json.dumps(entry["body"].get("input", [])[-1:]).split("Wall time: ")[1:]
    ]
    assert walls and max(walls) < 10.5
    assert run.classify(CodexAdapter()) is ExitClass.COMPLETED


# ----- Hermes --------------------------------------------------------------------------


def hermes(timeout_ms: int) -> AdapterLaunch:
    return HermesAdapter().build_launch(
        context(timeout_ms, endpoint="local", endpoint_url=f"{STUB_URL}/v1")
    )


def test_hermes_trap_a_background_process_left_running_is_classified_incomplete(
    tmp_path: Path,
) -> None:
    run = run_harness(
        tmp_path,
        hermes(60_000),
        command=long_command(20),
        extra_env={"STUB_BACKGROUND": "1"},
    )
    assert run.exit_code == 0 and not run.marker
    assert "notify_unsupported" in run.tool_outputs()
    assert (run.report / "hermes-processes.json").exists()
    assert run.classify(HermesAdapter()) is ExitClass.INCOMPLETE


def test_hermes_ends_a_command_at_the_launch_timeout(tmp_path: Path) -> None:
    run = run_harness(tmp_path, hermes(3000), command=long_command(40), extra_env={})
    assert run.exit_code == 0 and not run.marker and run.elapsed < 35
    assert "Command timed out after 3s" in run.tool_outputs()
    assert run.classify(HermesAdapter()) is ExitClass.COMPLETED


def test_hermes_waits_for_a_command_within_the_launch_timeout(tmp_path: Path) -> None:
    run = run_harness(tmp_path, hermes(60_000), command=long_command(8), extra_env={})
    assert run.exit_code == 0 and run.marker and run.elapsed >= 8
    assert run.classify(HermesAdapter()) is ExitClass.COMPLETED
