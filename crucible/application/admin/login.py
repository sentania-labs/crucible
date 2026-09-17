"""Credential onboarding (25): each harness's own interactive login, driven headlessly
through a pseudo-terminal, pointed directly at the dedicated Crucible directory.

The driver shows the operator the URL and the code, feeds back the code the operator
pastes, and writes any token the CLI prints once to a file, mode 600, without
displaying it. The operator's daily-use directories are never read, copied or
referenced: the login's home or config variable is the dedicated directory and nothing
else (12). The timing constraints are stated up front: Codex's device code expires in
15 minutes; AGY waits 60 seconds for the pasted code (S1b).
"""

from __future__ import annotations

import errno
import os
import pty
import re
import select
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    require_live_supervisor,
    require_reason,
)
from crucible.application.admin.credentials import check_shape, source_for, spec_for
from crucible.application.errors import ConflictError
from crucible.domain.events import EventKind
from crucible.domain.secrets import redact
from crucible.ports.repository import UnitOfWork

URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# A device or one-time code the CLI shows for the operator to enter elsewhere.
CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b")
PASTE_RE = re.compile(r"(paste|enter).{0,40}(code|token)|code\s*:\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LoginFlow:
    """One harness's login as the pty driver runs it."""

    harness: str
    argv: tuple[str, ...]
    # The variable that points the CLI at the dedicated directory (25 step 2).
    directory_env: str
    # Which subdirectory of the configured path that variable names ("" for the root).
    directory_subdir: str
    # Whether the operator pastes a code back into the CLI, and whether the CLI prints a
    # token once that becomes the credential file.
    pastes_code: bool
    captures_token: bool
    token_pattern: str
    token_file: str
    window: str


FLOWS: dict[str, LoginFlow] = {
    "claude_code": LoginFlow(
        harness="claude_code",
        argv=("claude", "setup-token"),
        directory_env="CLAUDE_CONFIG_DIR",
        directory_subdir="",
        pastes_code=True,
        captures_token=True,
        token_pattern=r"(sk-ant-[A-Za-z0-9_-]{20,})",
        token_file="oauth-token",
        window=(
            "Claude Code: approve in the browser and paste the code; the long-lived token "
            "is shown by the CLI once and is captured to oauth-token, never displayed"
        ),
    ),
    "codex": LoginFlow(
        harness="codex",
        argv=("codex", "login", "--device-auth"),
        directory_env="CODEX_HOME",
        directory_subdir="",
        pastes_code=False,
        captures_token=False,
        token_pattern="",
        token_file="",
        window="Codex: the device code expires in 15 minutes; enter it at the URL shown",
    ),
    "agy": LoginFlow(
        harness="agy",
        argv=("agy", "-p", "Reply with exactly the word OK and nothing else."),
        directory_env="HOME",
        directory_subdir="",
        pastes_code=True,
        captures_token=False,
        token_pattern="",
        token_file="",
        window=(
            "AGY: the CLI waits 60 seconds for the pasted code; have the browser signed "
            "in before starting"
        ),
    ),
}


@dataclass(slots=True)
class LoginSession:
    """What an in-progress or finished login shows: never a token."""

    harness: str
    started_at: float
    state: str = "starting"  # starting, waiting_for_operator, waiting_for_code, finished, failed
    url: str | None = None
    code: str | None = None
    prompt: str | None = None
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    token_written: bool = False
    error: str | None = None
    _code_from_operator: str | None = None
    _wake: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "state": self.state,
            "url": self.url,
            "code": self.code,
            "prompt": self.prompt,
            "output_tail": self.lines[-20:],
            "exit_code": self.exit_code,
            "token_written": self.token_written,
            "error": self.error,
        }

    def submit_code(self, code: str) -> None:
        self._code_from_operator = code
        self._wake.set()

    def wait_for_code(self, timeout: float) -> str | None:
        if self._wake.wait(timeout):
            self._wake.clear()
            code, self._code_from_operator = self._code_from_operator, None
            return code
        return None


def run_login(
    flow: LoginFlow,
    directory: str,
    *,
    session: LoginSession,
    argv: tuple[str, ...] | None = None,
    timeout: float = 900.0,
    emit: Callable[[str], None] | None = None,
) -> LoginSession:
    """Drive one login through a pty. `session` receives the URL, the code, and the
    prompts as they appear and supplies the operator's pasted code; the token, when the
    CLI prints one, is written to the credential file and replaced in the record."""
    target = Path(directory)
    if flow.directory_subdir:
        target = target / flow.directory_subdir
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "TERM": "dumb",
        "HOME": str(target),
        flow.directory_env: str(target),
    }
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            list(argv or flow.argv),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        os.close(slave)
    token_re = (
        re.compile(flow.token_pattern) if flow.captures_token and flow.token_pattern else None
    )
    buffer = ""
    deadline = time.monotonic() + timeout
    session.state = "waiting_for_operator"
    try:
        while True:
            if time.monotonic() > deadline:
                session.error = "login timed out"
                process.kill()
                break
            ready, _, _ = select.select([master], [], [], 0.25)
            if master in ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                buffer = _consume(buffer, flow, token_re, target, session, emit)
            elif process.poll() is not None:
                buffer = _consume(buffer + "\n", flow, token_re, target, session, emit)
                break
            if session.state == "waiting_for_code":
                code = session.wait_for_code(0.25)
                if code is not None:
                    os.write(master, (code.strip() + "\n").encode("utf-8"))
                    session.state = "waiting_for_operator"
        process.wait(timeout=5)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
    session.exit_code = process.returncode
    session.state = "finished" if process.returncode == 0 and not session.error else "failed"
    if session.state == "failed" and session.error is None:
        session.error = f"the login command exited {process.returncode}"
    return session


def _consume(
    buffer: str,
    flow: LoginFlow,
    token_re: re.Pattern[str] | None,
    target: Path,
    session: LoginSession,
    emit: Callable[[str], None] | None,
) -> str:
    """Handle every complete line in the buffer; keep the partial tail (a prompt)."""
    lines = buffer.split("\n")
    tail = lines.pop()
    for raw in lines:
        _line(raw.rstrip("\r"), flow, token_re, target, session, emit)
    if tail and PASTE_RE.search(tail):
        _line(tail.rstrip("\r"), flow, token_re, target, session, emit)
        return ""
    return tail


def _line(
    line: str,
    flow: LoginFlow,
    token_re: re.Pattern[str] | None,
    target: Path,
    session: LoginSession,
    emit: Callable[[str], None] | None,
) -> None:
    shown = line
    if token_re is not None:
        match = token_re.search(line)
        if match:
            _write_token(target / flow.token_file, match.group(1))
            session.token_written = True
            shown = line.replace(match.group(1), "[captured to " + flow.token_file + "]")
    shown = redact(shown)
    session.lines.append(shown)
    if emit is not None:
        emit(shown)
    url = URL_RE.search(shown)
    if url and session.url is None:
        session.url = url.group(0)
    code = CODE_RE.search(shown)
    if code and session.code is None and not flow.pastes_code:
        session.code = code.group(1)
    if flow.pastes_code and PASTE_RE.search(shown):
        session.prompt = shown.strip()
        session.state = "waiting_for_code"


def _write_token(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)


# ----- the service ------------------------------------------------------------


class LoginRegistry:
    """The in-progress logins of this process, one per harness (25: the API form
    returns the URL and polls for completion)."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._threads: dict[str, threading.Thread] = {}

    def get(self, harness: str) -> LoginSession | None:
        return self._sessions.get(harness)

    def start(self, ctx: AdminContext, harness: str, directory: str) -> LoginSession:
        existing = self._sessions.get(harness)
        if existing is not None and existing.state not in ("finished", "failed"):
            raise ConflictError(f"a login for {harness} is already in progress")
        flow = FLOWS[harness]
        session = LoginSession(harness=harness, started_at=time.time())
        self._sessions[harness] = session
        argv = ctx.login_commands.get(harness)
        thread = threading.Thread(
            target=run_login,
            args=(flow, directory),
            kwargs={"session": session, "argv": argv, "timeout": ctx.login_timeout_seconds},
            daemon=True,
            name=f"login-{harness}",
        )
        self._threads[harness] = thread
        thread.start()
        return session


def start_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
    reason: str | None,
) -> dict[str, Any]:
    """25 steps 1 to 3: the dedicated directory, the CLI's own login pointed at it, the
    URL and code for the operator. The windows are stated before anything runs."""
    reason = require_reason(reason)
    require_live_supervisor(ctx, uow)
    if harness not in FLOWS:
        raise ConflictError(f"harness {harness!r} has no interactive login flow")
    spec_for(ctx, harness)
    source = source_for(ctx, harness)
    flow = FLOWS[harness]
    session = registry.start(ctx, harness, source.path)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_STARTED,
        principal=principal,
        reason=reason,
        before=None,
        after=None,
        harness=harness,
        window=flow.window,
        command=list(ctx.login_commands.get(harness) or flow.argv),
    )
    return {"harness": harness, "window": flow.window, **session.as_dict()}


def login_status(registry: LoginRegistry, harness: str) -> dict[str, Any]:
    session = registry.get(harness)
    if session is None:
        return {"harness": harness, "state": "none"}
    return session.as_dict()


def submit_code(registry: LoginRegistry, harness: str, code: str) -> dict[str, Any]:
    session = registry.get(harness)
    if session is None or session.state in ("finished", "failed"):
        raise ConflictError(f"no login for {harness} is waiting for a code")
    session.submit_code(code)
    return session.as_dict()


def finish_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
) -> dict[str, Any]:
    """25 step 4 after the CLI exits: validate the resulting structure and record only
    the result. The probe (steps 5 to 8) is `credentials validate`."""
    session = registry.get(harness)
    if session is None or session.state not in ("finished", "failed"):
        raise ConflictError(f"the login for {harness} has not finished")
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    shape = check_shape(spec, source.path)
    state = uow.harnesses.get(harness)
    if state is not None:
        state.session_compatibility = "unverified"
        state.last_validated_at = None
        state.updated_at = ctx.clock.now()
        uow.harnesses.put(state)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_FINISHED,
        principal=principal,
        reason="login finished",
        before=None,
        after={"shape_ok": shape.ok, "session_compatibility": "unverified"},
        harness=harness,
        exit_code=session.exit_code,
        token_written=session.token_written,
        shape=shape.as_dict(),
    )
    return {"harness": harness, "login": session.as_dict(), "shape": shape.as_dict()}
