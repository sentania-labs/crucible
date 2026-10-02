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

Hades #385: Hermes's content search falls back to `grep -r --exclude-dir='.*' ... ROOT`
when there is no ripgrep, and GNU grep applies that pattern to ROOT itself, so a search
of `.` (or of any root whose last component starts with a dot) finds nothing. The image
now carries ripgrep, and the bootstrap also runs that grep from inside the root with no
file operand, which grep never excludes, so only hidden directories below the root are
skipped. Both patches are for Hermes 0.19.0 alone: the bootstrap refuses to start any
other version rather than patch code it was not written against.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

HERMES_PYTHON = "/opt/hermes/bin/python"
# How often the session store is looked at for progress.
PROGRESS_SECONDS = 15.0
PROGRESS_LINE = "crucible-hermes: working, session updated"

# The Hermes release the patches below were written against. Any other version stops the
# bootstrap before Hermes starts (hades #385).
HERMES_VERSION = "0.19.0"

# Run inside the Hermes virtual environment, ahead of Hermes itself. It changes the turn
# budget an agent is built with when the caller named none, and the root of the grep
# fallback of content search (hades #385). Each applies only once its module is imported
# the ordinary way, so Hermes's own import order (its approval mode is read at import)
# is untouched.
PATCHES = r"""
import importlib.abc
import importlib.metadata
import importlib.util
import inspect
import os
import sys

EXPECTED = "@HERMES_VERSION@"
try:
    FOUND = importlib.metadata.version("hermes-agent")
except importlib.metadata.PackageNotFoundError:
    FOUND = "none"
if FOUND != EXPECTED:
    raise SystemExit(
        f"crucible-hermes: its patches are for hermes-agent {EXPECTED}, found {FOUND}; "
        "refusing to start Hermes unpatched (hades #385)"
    )

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


# Hades #385. The 0.19.0 fallback, exactly: these lines are what the patch replaces the
# effect of, so a Hermes whose fallback reads differently fails here, at import.
GREP_SHAPE = (
    "cmd_parts = [\"grep\", \"-rnH\"]",
    "cmd_parts.append(\"--exclude-dir='.*'\")",
    "cmd_parts.append(self._escape_shell_arg(path))",
    "cmd_parts.extend([\"|\", \"head\", \"-n\", str(fetch_limit)])",
    "cmd = \"set -o pipefail; \" + \" \".join(cmd_parts)",
)
GREP_HEAD = "set -o pipefail; grep -rnH "


class _RootedShell:
    # The file operations object as the fallback sees it, except that its grep command
    # runs from inside the root with no file operand. GNU grep applies --exclude-dir to
    # every operand it is given, `.` and `./` included, but never to the `.` it searches
    # when it is given none; that `.` is also left out of the names it prints.
    def __init__(self, ops, root):
        self._ops = ops
        self._root = ops._escape_shell_arg(root)

    def __getattr__(self, name):
        return getattr(self._ops, name)

    def _exec(self, command, *args, **kwargs):
        operand = f" {self._root} | head -n "
        if command.startswith(GREP_HEAD) and operand in command:
            before, _, after = command.rpartition(operand)
            command = (
                f"set -o pipefail; cd -- {self._root} >/dev/null 2>&1 && grep -rnH "
                f"{before[len(GREP_HEAD):]} | head -n {after}"
            )
        return self._ops._exec(command, *args, **kwargs)


def _rooted(root, name):
    return (root if root.endswith("/") else root + "/") + name


def _patch_grep(module):
    shell = module.ShellFileOperations
    original = shell._search_with_grep
    source = inspect.getsource(original)
    missing = [line for line in GREP_SHAPE if line not in source]
    if missing:
        raise RuntimeError(
            "crucible-hermes: Hermes's grep fallback is not the one hades #385 patches; "
            f"missing {missing}"
        )

    def _search_with_grep(self, pattern, path, file_glob, limit, offset, output_mode, context):
        probe = self._exec(f"test -d {self._escape_shell_arg(path)} && echo directory")
        if probe.stdout.strip() != "directory":
            return original(self, pattern, path, file_glob, limit, offset, output_mode, context)
        result = original(
            _RootedShell(self, path), pattern, path, file_glob, limit, offset, output_mode,
            context,
        )
        # Grep printed names relative to the root; give them back the root, as grep
        # does for an operand, so they read as they would have with ripgrep.
        for match in result.matches:
            match.path = _rooted(path, match.path)
        result.files = [_rooted(path, name) for name in result.files]
        result.counts = {_rooted(path, name): count for name, count in result.counts.items()}
        return result

    shell._search_with_grep = _search_with_grep


class _GrepRoot(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "tools.file_operations":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module):
            execute(module)
            _patch_grep(module)

        loader.exec_module = exec_module
        return spec


if LIMIT > 0:
    sys.meta_path.insert(0, _TurnBudget())
sys.meta_path.insert(0, _GrepRoot())
""".replace("@HERMES_VERSION@", HERMES_VERSION)

BOOTSTRAP = (
    PATCHES
    + r"""
sys.argv = ["hermes", *sys.argv[1:]]
from hermes_cli.main import main

sys.exit(main())
"""
)


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


def _enrich_usage(usage_path: Path, home: Path, max_turns: int = 0) -> None:
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
        session_id = usage.get("session_id")
        row = None
        try:
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
        except sqlite3.Error:
            row = None
        if row is not None:
            usage["duration_ms"] = _milliseconds(row[0], row[1])
            usage["tool_calls"] = row[2] if isinstance(row[2], int) else None
        temporary = usage_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(usage, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(usage_path)
    except (OSError, ValueError, AttributeError):
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
