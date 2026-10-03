"""The worker image's Hermes wrapper (FDY-0140): the instructions in the prompt, the run
limits, the progress line and the usage record's turn-limit mark. The bootstrap is run
against a stand-in `run_agent` and `hermes_cli.main` with the shape Hermes 0.19 has, so
what it changes and what it leaves alone is checked without Hermes installed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_identity_goes_into_the_prompt_ahead_of_the_pointer(tmp_path: Path) -> None:
    wrapper = _wrapper()
    identity = tmp_path / "IDENTITY.md"
    identity.write_text("# Task EX-1\n\nDo the thing.\n", encoding="utf-8")
    argv = ["--model", "coder", "-z", "Read IDENTITY.md and execute the task."]
    prompt = wrapper.inline_identity(argv, str(identity))[-1]
    assert prompt.startswith("# Task EX-1\n\nDo the thing.")
    assert prompt.endswith("Read IDENTITY.md and execute the task.")
    assert wrapper.inline_identity(argv, str(tmp_path / "absent.md")) == argv
    assert wrapper.inline_identity(argv, None) == argv


def test_the_context_length_is_hermes_own_setting_and_zero_writes_nothing(
    tmp_path: Path,
) -> None:
    wrapper = _wrapper()
    wrapper.write_settings(tmp_path / "home", 0)
    assert not (tmp_path / "home" / "config.yaml").exists()
    wrapper.write_settings(tmp_path / "home", 131072)
    assert (tmp_path / "home" / "config.yaml").read_text() == ("model:\n  context_length: 131072\n")


@pytest.mark.parametrize(
    ("usage", "reached"),
    [
        ({"completed": False, "failed": False, "api_calls": 301}, True),
        ({"completed": True, "failed": False, "api_calls": 300}, False),
        ({"completed": False, "failed": True, "api_calls": 12}, False),
    ],
)
def test_the_usage_record_says_whether_the_turn_limit_ended_the_run(
    tmp_path: Path, usage: dict[str, object], reached: bool
) -> None:
    wrapper = _wrapper()
    path = tmp_path / "hermes-usage.json"
    path.write_text(json.dumps(usage), encoding="utf-8")
    wrapper._enrich_usage(path, tmp_path, 300)
    written = json.loads(path.read_text())
    assert written["max_turns"] == 300
    assert written["turn_limit_reached"] is reached


def test_a_progress_line_follows_each_change_to_the_session_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrapper = _wrapper()
    stop = threading.Event()
    watcher = threading.Thread(target=wrapper.watch_progress, args=(tmp_path, stop, 0.05))
    watcher.start()
    try:
        stop.wait(0.2)
        (tmp_path / "state.db").write_bytes(b"turn 1")
        stop.wait(0.3)
    finally:
        stop.set()
        watcher.join()
    lines = capsys.readouterr().err.splitlines()
    assert lines == [wrapper.PROGRESS_LINE]


def stand_in_release(hermes: Path, version: str = "0.19.0") -> None:
    """The installed-package record the bootstrap's version check reads."""
    info = hermes / f"hermes_agent-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: hermes-agent\nVersion: {version}\n", encoding="utf-8"
    )


_STAND_IN_AGENT = """
class AIAgent:
    def __init__(self, base_url=None, api_key=None, provider=None, api_mode=None,
                 acp_command=None, acp_args=None, command=None, args=None, model="",
                 max_iterations=90, tool_delay=1.0):
        self.max_iterations = max_iterations
"""
_STAND_IN_MAIN = """
import os
import sys


def main():
    # Hermes reads its approval mode when its tools are imported; the bootstrap must
    # not have imported run_agent before this runs.
    assert "run_agent" not in sys.modules
    import run_agent

    oneshot = run_agent.AIAgent(model="m")
    explicit = run_agent.AIAgent(model="m", max_iterations=45)
    print(sys.argv[1:], oneshot.max_iterations, explicit.max_iterations)
    return 0
"""


@pytest.mark.parametrize(("limit", "expected"), [("300", "300 45"), ("", "90 45")])
def test_the_bootstrap_sets_the_turn_budget_only_where_none_was_named(
    tmp_path: Path, limit: str, expected: str
) -> None:
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "run_agent.py").write_text(_STAND_IN_AGENT, encoding="utf-8")
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text(_STAND_IN_MAIN, encoding="utf-8")
    stand_in_release(hermes)
    # The bootstrap starts only the Hermes version its patches were written for (#385).
    (hermes / "hermes_agent-0.19.0.dist-info").mkdir(exist_ok=True)
    (hermes / "hermes_agent-0.19.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: hermes-agent\nVersion: 0.19.0\n", encoding="utf-8"
    )
    # The working directory is the task's checkout. Modules there named like Hermes's
    # own must never be imported in their place.
    checkout = tmp_path / "checkout"
    (checkout / "hermes_cli").mkdir(parents=True)
    (checkout / "run_agent.py").write_text("raise SystemExit('checkout module')\n")
    (checkout / "hermes_cli" / "__init__.py").write_text("raise SystemExit('checkout')\n")
    wrapper = _wrapper()
    result = subprocess.run(
        [sys.executable, "-P", "-c", wrapper.BOOTSTRAP, "-z", "prompt"],
        capture_output=True,
        text=True,
        check=False,
        cwd=checkout,
        env={"PYTHONPATH": str(hermes), "CRUCIBLE_HERMES_MAX_TURNS": limit},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"['-z', 'prompt'] {expected}"


def test_hermes_is_started_so_the_checkout_cannot_shadow_its_modules() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert '[HERMES_PYTHON, "-P", "-c", BOOTSTRAP,' in source


def test_another_hermes_release_stops_the_bootstrap(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "run_agent.py").write_text("raise SystemExit('no')\n", encoding="utf-8")
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text("raise SystemExit('no')\n", encoding="utf-8")
    stand_in_release(hermes, "0.20.0")

    wrapper = _wrapper()
    result = subprocess.run(
        [sys.executable, "-P", "-c", wrapper.BOOTSTRAP, "-z", "prompt"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PYTHONPATH": str(hermes), "CRUCIBLE_HERMES_MAX_TURNS": "300"},
    )
    assert result.returncode != 0
    assert (
        "crucible-hermes: its patches are for hermes-agent 0.19.0, found 0.20.0; "
        "refusing to start Hermes unpatched (hades #385)"
    ) in result.stderr
