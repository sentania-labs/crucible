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

Issue 388:
- The response allowance the gateway enforces is Hermes's own `model.max_tokens`, written
  beside `model.context_length`. Hermes then reserves it out of the window when it decides
  when to compress (131072 and 32000 trigger at 74304 input tokens, not 98304) and every
  request carries it as max_tokens. When Hermes retries one call with a lower cap after
  the gateway refuses the allowance, its one-call cap outranks `model.max_tokens`, so the
  lower figure is what is sent; nothing here sends max_tokens any other way.
- `-z` has no way to pass the routing entry's thinking setting. The bootstrap adds it to
  the request overrides an agent is built with, as `chat_template_kwargs.enable_thinking`,
  unless the caller already named one.

Hades #385: Hermes's content search falls back to `grep -r --exclude-dir='.*' ... ROOT`
when there is no ripgrep, and GNU grep applies that pattern to ROOT itself, so a search
of `.` (or of any root whose last component starts with a dot) finds nothing. The image
now carries ripgrep, and the bootstrap also runs that grep from inside the root with no
file operand, which grep never excludes, so only hidden directories below the root are
skipped. The `cd` runs in a subshell: Hermes records the shell's directory after every
command as the session's, and a search must not move the agent. Both patches are for
Hermes 0.19.0 alone: the bootstrap refuses to start any other version rather than patch
code it was not written against, and before Hermes starts, main() imports the patched
module once on its own and stops the attempt if the fallback is not the 0.19.0 one.
That check cannot be left to the import inside Hermes: Hermes's tool discovery catches
every exception and only logs it, and would start without its file tools.
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
# fallback of content search (hades #385), and the thinking setting its requests
# carry when the caller named none. Each applies only once its module is imported
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
THINKING = {"on": True, "off": False}.get(os.environ.get("CRUCIBLE_HERMES_THINKING") or "")
# `max_iterations` is the tenth parameter of AIAgent.__init__ after self (0.19).
POSITION = 9


def _check_version():
    try:
        found = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError:
        found = None
    if found != HERMES_VERSION:
        sys.exit(
            f"crucible-hermes: the bootstrap is written for hermes-agent {HERMES_VERSION} "
            f"and found {found}; review its patches against that release first"
        )


def _with_thinking(overrides):
    # Only extra_body.chat_template_kwargs.enable_thinking, and only when absent. Never
    # max_tokens: a request override outranks Hermes's own lower cap on a retry.
    merged = dict(overrides or {})
    body = dict(merged.get("extra_body") or {})
    template = dict(body.get("chat_template_kwargs") or {})
    template.setdefault("enable_thinking", THINKING)
    body["chat_template_kwargs"] = template
    merged["extra_body"] = body
    return merged


class _AgentDefaults(importlib.abc.MetaPathFinder):
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
            names = list(inspect.signature(original).parameters)[1:]
            if THINKING is not None and "request_overrides" not in names:
                sys.exit("crucible-hermes: AIAgent takes no request_overrides")
            overrides = names.index("request_overrides") if THINKING is not None else -1

            def __init__(self, *args, **kwargs):
                if LIMIT > 0 and len(args) <= POSITION and "max_iterations" not in kwargs:
                    kwargs["max_iterations"] = LIMIT
                if THINKING is not None:
                    if len(args) > overrides:
                        args = list(args)
                        args[overrides] = _with_thinking(args[overrides])
                    else:
                        kwargs["request_overrides"] = _with_thinking(
                            kwargs.get("request_overrides")
                        )
                original(self, *args, **kwargs)

            module.AIAgent.__init__ = __init__

        loader.exec_module = exec_module
        return spec


_check_version()
if LIMIT > 0 or THINKING is not None:
    sys.meta_path.insert(0, _AgentDefaults())
sys.meta_path.insert(0, _GrepRoot())
""".replace("@HERMES_VERSION@", HERMES_VERSION)

# Run by main() before Hermes starts: the version check above, then the patched module
# imported on its own, so a fallback of another shape exits non-zero here instead of
# being swallowed by Hermes's tool discovery (hades #385).
PREFLIGHT = (
    PATCHES
    + r"""
try:
    import tools.file_operations
except RuntimeError as error:
    raise SystemExit(str(error))
"""
)
PREFLIGHT_FAILED = (
    "crucible-hermes: the Hermes in this image is not the one its patches were written "
    "for (hades #385); not starting it"
)

BOOTSTRAP = (
    PATCHES
    + r"""
sys.argv = ["hermes", *sys.argv[1:]]
from hermes_cli.main import main

sys.exit(main())
"""
))


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


def write_settings(home: Path, context_length: int, max_output_tokens: int = 0) -> None:
    """Hermes's own `model.context_length` and `model.max_tokens`, in the per-run home it
    reads config from. Each is only written when given; the home starts empty on every
    launch."""
    lines = []
    if context_length > 0:
        lines.append(f"  context_length: {context_length}")
    if max_output_tokens > 0:
        lines.append(f"  max_tokens: {max_output_tokens}")
    if not lines:
        return
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    config.write_text("model:\n" + "\n".join(lines) + "\n", encoding="utf-8")


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
    write_settings(
        home,
        _limit("CRUCIBLE_HERMES_CONTEXT_LENGTH"),
        _limit("CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"),
    )
    argv = inline_identity(sys.argv[1:], os.environ.get("CRUCIBLE_HERMES_IDENTITY"))
    # Hades #385: the patches are checked before Hermes starts; the reason is on stderr.
    if subprocess.run([HERMES_PYTHON, "-P", "-c", PREFLIGHT], check=False).returncode != 0:
        print(PREFLIGHT_FAILED, file=sys.stderr, flush=True)
        return 2
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
