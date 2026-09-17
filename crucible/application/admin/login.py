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
import shutil
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
    guard_mutation,
)
from crucible.application.admin.credentials import (
    RETIRED_MARK,
    check_shape,
    source_for,
    spec_for,
)
from crucible.application.errors import ConflictError
from crucible.domain.events import EventKind
from crucible.domain.secrets import redact
from crucible.ports.repository import UnitOfWork

URL_RE = re.compile(r"https?://[^\s'\"<>]+")
# A device or one-time code the CLI shows for the operator to enter elsewhere.
CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b")
# The CLI is ready for the code when it has printed a prompt: a line that ends in a
# prompt character with no newline after it. Matching the words alone flipped the session
# to `waiting_for_code` on informational text ("visit the URL and enter the code"), before
# the CLI was reading, and the pasted code went nowhere.
PASTE_RE = re.compile(
    r"(?:(?:paste|enter)[^\n]{0,40}(?:code|token)[^\n]{0,20}|code|token)\s*[:>?]\s*$",
    re.IGNORECASE,
)


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
        except OSError as exc:
            # The CLI is not on this host. Without this the session stayed `starting` for
            # ever and every later login for the harness was refused as in progress.
            os.close(master)
            session.state = "failed"
            session.exit_code = None
            session.error = f"could not start {(argv or flow.argv)[0]}: {exc.strerror or exc}"
            return session
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
        argv = ctx.login_commands.get(harness) or flow.argv
        if shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            # The login drives the harness's own CLI, and only the worker images carry
            # the three; the Crucible service image carries none (13). Refusing here is
            # the difference between a clear message and a session that never finishes.
            raise ConflictError(
                f"{argv[0]} is not installed on this host, so the {harness} login cannot "
                "run here; run `crucible-admin credentials login` in local mode on a host "
                f"that has {argv[0]}"
            )
        session = LoginSession(harness=harness, started_at=time.time())
        self._sessions[harness] = session
        thread = threading.Thread(
            target=self._run,
            args=(flow, directory, session, ctx.login_commands.get(harness)),
            kwargs={"timeout": ctx.login_timeout_seconds},
            daemon=True,
            name=f"login-{harness}",
        )
        self._threads[harness] = thread
        thread.start()
        return session

    @staticmethod
    def _run(
        flow: LoginFlow,
        directory: str,
        session: LoginSession,
        argv: tuple[str, ...] | None,
        *,
        timeout: float,
    ) -> None:
        """Whatever happens in the thread, the session ends in a terminal state: a
        session stuck in `starting` blocks every later login for that harness."""
        try:
            run_login(flow, directory, session=session, argv=argv, timeout=timeout)
        except Exception as exc:  # the thread has nowhere to raise
            session.state = "failed"
            if session.error is None:
                session.error = f"the login driver failed: {type(exc).__name__}: {exc}"


def start_login(
    ctx: AdminContext,
    uow: UnitOfWork,
    registry: LoginRegistry,
    *,
    principal: str,
    harness: str,
    reason: str | None,
    replace: bool = False,
) -> dict[str, Any]:
    """25 steps 1 to 3: the dedicated directory, the CLI's own login pointed at it, the
    URL and code for the operator. The windows are stated before anything runs.

    A login writes into the configured directory, so an existing credential that still
    passes the shape check is not overwritten silently: `replace` retires it the way
    rotate does (renamed aside, shredded by the retention sweep) before the CLI runs."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login {harness}"
    )
    if harness not in FLOWS:
        raise ConflictError(f"harness {harness!r} has no interactive login flow")
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    flow = FLOWS[harness]
    retired = _retire_existing(
        ctx, uow, spec, source, principal=principal, harness=harness, reason=reason, replace=replace
    )
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
        retained_as=retired,
    )
    return {
        "harness": harness,
        "window": flow.window,
        "retained_as": retired,
        **session.as_dict(),
    }


def _retire_existing(
    ctx: AdminContext,
    uow: UnitOfWork,
    spec: Any,
    source: Any,
    *,
    principal: str,
    harness: str,
    reason: str,
    replace: bool,
) -> str | None:
    """Refuse to overwrite a credential that still passes the shape check; with
    `replace`, move it aside under rotate's retained name so the retention sweep shreds
    it on its own schedule. Nothing is shredded here and nothing is read."""
    current = Path(source.path)
    if not current.is_dir() or not check_shape(spec, source.path).ok:
        return None
    if not replace:
        raise ConflictError(
            f"the {harness} credential already passes the shape check; a login would "
            "overwrite it. Pass replace to retain and replace it, or use rotate to swap "
            "in a prepared directory"
        )
    stamp = ctx.clock.now().strftime("%Y%m%dT%H%M%SZ")
    retired = current.with_name(current.name + RETIRED_MARK + stamp)
    os.rename(current, retired)
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_ROTATED,
        principal=principal,
        reason=f"login --replace: {reason}",
        before={"state": "present"},
        after={"state": "retained"},
        harness=harness,
        retained_as=retired.name,
        retention_hours=ctx.credential_retention_hours,
    )
    return retired.name


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
    reason: str | None = None,
) -> dict[str, Any]:
    """25 step 4 after the CLI exits: validate the resulting structure and record only
    the result. The probe (steps 5 to 8) is `credentials validate`.

    It writes `session_compatibility` and clears `last_validated_at`, so it is a mutation
    and takes the same two guards as every other one."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login finish {harness}"
    )
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
        reason=reason,
        before=None,
        after={"shape_ok": shape.ok, "session_compatibility": "unverified"},
        harness=harness,
        exit_code=session.exit_code,
        token_written=session.token_written,
        shape=shape.as_dict(),
    )
    return {"harness": harness, "login": session.as_dict(), "shape": shape.as_dict()}
