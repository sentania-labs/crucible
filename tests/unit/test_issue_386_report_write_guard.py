"""Hades #386: writing the report outside the checkout must not make the checkout's passed
verification stale. Hermes 0.19's guard resolves an edit with no project of its own to the
session's workspace root, so the wrapper's bootstrap narrows
`agent.verification_evidence.mark_workspace_edited` to paths under the root it resolves.

The stand-in Hermes below keeps 0.19.0's attribute paths and call shapes:
`tools.file_tools.write_file_tool` and `tools.file_tools._mark_verification_stale` (which
falls back to `_authoritative_workspace_root`, here `$TERMINAL_CWD`),
`agent.verification_evidence.record_terminal_result`, `.mark_workspace_edited` and
`.verification_status`, `agent.coding_context.project_facts_for`, and
`agent.verification_stop.build_verify_on_stop_nudge`. Its `hermes_cli.main.main` plays one
attempt's sequence and prints what the guard saw."""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.unit.test_issue_385_hermes_search import _FALLBACK_0_19_0

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"

_CODING_CONTEXT = """
from pathlib import Path


def project_facts_for(cwd=None):
    resolved = Path(cwd or ".").resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "pyproject.toml").is_file():
            return {"root": str(candidate), "verifyCommands": ["make lint"]}
    return None
"""

# The ledger of agent/verification_evidence.py, in memory, with a counter for the clock.
_VERIFICATION_EVIDENCE = """
import itertools
from pathlib import Path

_CLOCK = itertools.count(1)
_EVENTS = {}
_STATE = {}


def _root(cwd):
    from agent.coding_context import project_facts_for

    facts = project_facts_for(cwd)
    if not facts:
        return None
    return str(facts.get("root") or Path(cwd or ".").resolve())


def record_terminal_result(*, command, cwd, session_id, exit_code, output=""):
    root = _root(cwd)
    if root is None:
        return None
    sid = str(session_id or "default")
    event = {"id": len(_EVENTS) + 1, "created_at": next(_CLOCK), "command": command,
             "status": "passed" if int(exit_code) == 0 else "failed"}
    _EVENTS[event["id"]] = event
    _STATE[(sid, root)] = {"last_event_id": event["id"], "last_edit_at": None,
                           "changed_paths": []}
    return event


def mark_workspace_edited(*, session_id, cwd, paths=None):
    root = _root(cwd)
    if root is None:
        return None
    sid = str(session_id or "default")
    changed_paths = sorted({str(p) for p in (paths or []) if p})
    state = _STATE.setdefault((sid, root), {"last_event_id": None, "last_edit_at": None,
                                            "changed_paths": []})
    state["last_edit_at"] = next(_CLOCK)
    state["changed_paths"] = sorted(set(state["changed_paths"]) | set(changed_paths))
    return {"session_id": sid, "root": root, "changed_paths": changed_paths}


def verification_status(*, session_id, cwd):
    root = _root(cwd)
    if root is None:
        return {"status": "not_applicable", "evidence": None}
    state = _STATE.get((str(session_id or "default"), root))
    if state is None or state["last_event_id"] is None:
        return {"status": "unverified", "evidence": None, "root": root,
                "changed_paths": [] if state is None else state["changed_paths"]}
    evidence = _EVENTS[state["last_event_id"]]
    if state["last_edit_at"] and state["last_edit_at"] > evidence["created_at"]:
        status = "stale"
    else:
        status = evidence["status"]
    return {"status": status, "evidence": evidence, "root": root,
            "changed_paths": state["changed_paths"]}
"""

_VERIFICATION_STOP = """
from pathlib import Path


def build_verify_on_stop_nudge(*, session_id, changed_paths, attempts=0, max_attempts=2):
    from agent.coding_context import project_facts_for
    from agent.verification_evidence import verification_status

    paths = sorted({str(p) for p in changed_paths if p})
    if not paths or attempts >= max_attempts:
        return None
    for cwd in sorted({str(Path(p).parent) for p in paths}):
        if not project_facts_for(cwd):
            continue
        status = verification_status(session_id=session_id, cwd=cwd)
        if status["status"] != "passed":
            return f"[System: verification status {status['status']}]"
    return None
"""

# tools/file_tools.py: the write tool and its stale marker, as 0.19.0 has them.
_FILE_TOOLS = """
import os
from pathlib import Path


def _authoritative_workspace_root(task_id="default"):
    return os.environ.get("TERMINAL_CWD")


def _mark_verification_stale(task_id, resolved_paths, session_id=None):
    paths = [p for p in resolved_paths if p]
    if not paths:
        return
    try:
        from agent.coding_context import project_facts_for
        from agent.verification_evidence import mark_workspace_edited

        cwd = None
        for path in paths:
            candidate = str(Path(path).parent)
            if project_facts_for(candidate):
                cwd = candidate
                break
        if cwd is None:
            cwd = _authoritative_workspace_root(task_id)
        mark_workspace_edited(session_id=session_id or task_id, cwd=cwd, paths=paths)
    except Exception:
        pass


def write_file_tool(path, content, task_id="default", cross_profile=False,
                    session_id=None):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")
    _mark_verification_stale(task_id, [path], session_id=session_id)
    return "ok"
"""

# One attempt: argv[1] names the sequence, the rest are the paths it writes.
_MAIN = """
import json
import os
import sys


def main():
    from agent.verification_evidence import record_terminal_result, verification_status
    from agent.verification_stop import build_verify_on_stop_nudge
    from tools.file_tools import write_file_tool

    root = os.environ["TERMINAL_CWD"]
    verifications = 0

    def verify():
        nonlocal verifications
        verifications += 1
        record_terminal_result(command="make lint", cwd=root, session_id="s", exit_code=0)

    scenario, *paths = sys.argv[1:]
    turn = []
    if scenario == "attempt":
        source = os.path.join(root, "src", "module.py")
        write_file_tool(source, "VALUE = 1\\n", task_id="s")
        turn.append(source)
    verify()
    for path in paths:
        write_file_tool(path, "written\\n", task_id="s")
        turn.append(path)
    if scenario == "attempt":
        attempts = 0
        while build_verify_on_stop_nudge(session_id="s", changed_paths=turn,
                                         attempts=attempts):
            attempts += 1
            verify()
    status = verification_status(session_id="s", cwd=root)
    print(json.dumps({"verifications": verifications, "status": status["status"],
                      "changed_paths": status["changed_paths"]}))
    return 0
"""


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stand_in_hermes(base: Path, version: str = "0.19.0") -> Path:
    hermes = base / "hermes"
    files = {
        "agent/__init__.py": "",
        "agent/coding_context.py": _CODING_CONTEXT,
        "agent/verification_evidence.py": _VERIFICATION_EVIDENCE,
        "agent/verification_stop.py": _VERIFICATION_STOP,
        "tools/__init__.py": "",
        "tools/file_tools.py": _FILE_TOOLS,
        "tools/file_operations.py": f"class ShellFileOperations:\n{_FALLBACK_0_19_0}",
        "hermes_cli/__init__.py": f'__version__ = "{version}"\n',
        "hermes_cli/main.py": _MAIN,
        f"hermes_agent-{version}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: hermes-agent\nVersion: {version}\n"
        ),
    }
    for name, text in files.items():
        (hermes / name).parent.mkdir(parents=True, exist_ok=True)
        (hermes / name).write_text(text, encoding="utf-8")
    return hermes


def _checkout(base: Path) -> Path:
    root = base / "crucible" / "repo"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return root


def _report(base: Path) -> Path:
    return base / "crucible" / "report" / "report.yaml"


def _bootstrap(base: Path, *argv: str, version: str = "0.19.0") -> subprocess.CompletedProcess[str]:
    hermes = _stand_in_hermes(base, version)
    root = base / "crucible" / "repo"
    return subprocess.run(
        [sys.executable, "-P", "-c", _wrapper().BOOTSTRAP, *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env={"PYTHONPATH": str(hermes), "TERMINAL_CWD": str(root)},
    )


def _seen(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    seen: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return seen


def test_a_report_only_write_keeps_the_checkout_passed(tmp_path: Path) -> None:
    _checkout(tmp_path)
    report = _report(tmp_path)
    seen = _seen(_bootstrap(tmp_path, "writes", str(report)))
    assert report.read_text(encoding="utf-8") == "written\n"
    assert seen == {"verifications": 1, "status": "passed", "changed_paths": []}


@pytest.mark.parametrize("name", ["src/module.py", "tests/test_module.py", "pyproject.toml"])
def test_a_write_under_the_root_still_turns_it_stale(tmp_path: Path, name: str) -> None:
    root = _checkout(tmp_path)
    path = root / name
    seen = _seen(_bootstrap(tmp_path, "writes", str(_report(tmp_path)), str(path)))
    assert seen == {"verifications": 1, "status": "stale", "changed_paths": [str(path)]}


def test_verify_write_report_finish_runs_verification_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(tmp_path)
    hermes = _stand_in_hermes(tmp_path)
    home = tmp_path / "home"
    wrapper = _wrapper()
    monkeypatch.setattr(wrapper, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(wrapper, "PROGRESS_SECONDS", 3600.0)
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["crucible-hermes", "attempt", str(_report(tmp_path))])
    monkeypatch.chdir(root)
    for name, value in {
        "PYTHONPATH": str(hermes),
        "TERMINAL_CWD": str(root),
        "HERMES_HOME": str(home),
        "CRUCIBLE_HERMES_USAGE": str(tmp_path / "hermes-usage.json"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CRUCIBLE_HERMES_MAX_TURNS", raising=False)
    monkeypatch.delenv("CRUCIBLE_HERMES_IDENTITY", raising=False)

    assert wrapper.main() == 0
    out, err = capfd.readouterr()
    seen = json.loads(out.strip().splitlines()[-1])
    assert seen == {
        "verifications": 1,
        "status": "passed",
        "changed_paths": [],
    }, err


def test_hermes_refuses_to_start_under_a_release_the_patch_was_not_written_for(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path)
    result = _bootstrap(tmp_path, "writes", version="0.20.0")
    assert result.returncode == 1
    assert "hermes-agent 0.19.0, found 0.20.0" in result.stderr
    assert result.stdout == ""


def test_the_guard_version_matches_the_image_pin() -> None:
    pins = (WRAPPER.parent / "Dockerfile").read_text(encoding="utf-8")
    assert f"ARG HARNESS_HERMES_VERSION={_wrapper().HERMES_VERSION}\n" in pins


def test_preflight_rejects_a_changed_guard_before_hermes_starts(tmp_path: Path) -> None:
    hermes = _stand_in_hermes(tmp_path)
    guard = hermes / "agent" / "verification_evidence.py"
    guard.write_text("def mark_workspace_edited(*, session_id, cwd): pass\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-P", "-c", _wrapper().PREFLIGHT + "\nprint('started')"],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(hermes)},
    )
    assert result.returncode == 70
    assert (
        "agent.verification_evidence.mark_workspace_edited is not the 0.19.0 shape" in result.stderr
    )
    assert "started" not in result.stdout
