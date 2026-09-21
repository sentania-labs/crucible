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
    record_refusal,
    refuse_secret_shaped,
)
from crucible.application.admin.credentials import (
    RETIRED_MARK,
    CredentialAdminError,
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
    cancel_requested: bool = False
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
            "cancel_requested": self.cancel_requested,
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
            if session.cancel_requested:
                session.error = "login cancelled"
                process.kill()
                break
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

    def resolve(self, ctx: AdminContext, harness: str) -> tuple[str, ...]:
        """Every refusal `start` can raise, raised before anything on disk has moved.

        The caller retires the credential that is at the configured path, so a refusal
        that only surfaced inside `start` left the credential renamed aside with no login
        running and the retention sweep free to shred it."""
        existing = self._sessions.get(harness)
        if existing is not None and existing.state not in ("finished", "failed"):
            raise ConflictError(f"a login for {harness} is already in progress")
        flow = FLOWS[harness]
        argv = tuple(ctx.login_commands.get(harness) or flow.argv)
        runner = self.container_runner(ctx)
        if runner is None and shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            # The login drives the harness's own CLI, and only the worker images carry
            # the three; the Crucible service image carries none (13). Refusing here is
            # the difference between a clear message and a session that never finishes.
            raise ConflictError(
                f"{argv[0]} is not installed on this host, so the {harness} login cannot "
                "run here; run `crucible-admin credentials login` in local mode on a host "
                f"that has {argv[0]}"
            )
        return argv

    @staticmethod
    def container_runner(ctx: AdminContext) -> Any | None:
        return next(
            (
                provider
                for provider in ctx.providers.values()
                if callable(getattr(provider, "run_login_container", None))
            ),
            None,
        )

    def start(
        self,
        ctx: AdminContext,
        harness: str,
        directory: str,
        *,
        image: str | None = None,
    ) -> LoginSession:
        argv = self.resolve(ctx, harness)
        flow = FLOWS[harness]
        runner = self.container_runner(ctx)
        if runner is not None and not image:
            raise ConflictError(f"no promoted worker image is available for {harness}")
        session = LoginSession(harness=harness, started_at=time.time())
        self._sessions[harness] = session
        if runner is not None:
            assert image is not None
            thread = threading.Thread(
                target=self._run_container,
                args=(runner, flow, image, directory, session, argv),
                kwargs={"timeout": ctx.login_timeout_seconds},
                daemon=True,
                name=f"login-{harness}",
            )
        else:
            thread = threading.Thread(
                target=self._run,
                args=(flow, directory, session, argv),
                kwargs={"timeout": ctx.login_timeout_seconds},
                daemon=True,
                name=f"login-{harness}",
            )
        self._threads[harness] = thread
        try:
            thread.start()
        except Exception as exc:
            # A registered session that never reaches a terminal state refuses every later
            # login for the harness as one already in progress (correction 17), and a
            # thread that could not be created is exactly that case.
            session.state = "failed"
            session.error = f"the login thread could not be started: {type(exc).__name__}"
            self._threads.pop(harness, None)
            raise
        deadline = time.monotonic() + 5.0
        while session.state == "starting" and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if session.state == "failed":
            raise ConflictError(session.error or f"the {harness} login failed to start")
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

    @staticmethod
    def _run_container(
        runner: Any,
        flow: LoginFlow,
        image: str,
        directory: str,
        session: LoginSession,
        argv: tuple[str, ...],
        *,
        timeout: int,
    ) -> None:
        try:
            import asyncio  # noqa: PLC0415

            asyncio.run(
                runner.run_login_container(
                    flow=flow,
                    image=image,
                    directory=directory,
                    session=session,
                    argv=argv,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            session.state = "failed"
            session.error = f"the login container failed: {type(exc).__name__}: {exc}"


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
    rotate does (renamed aside, shredded by the retention sweep) before the CLI runs.

    Nothing on disk moves until every precondition that can refuse has been checked, and
    a start that fails after the retire puts the credential back at its configured path.
    The harness CLI is absent from the service image (13), so the executable check alone
    used to rename a valid credential aside and then refuse, leaving the harness with no
    credential at its configured path and the retained copy eligible for the retention
    sweep."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login {harness}"
    )
    if harness not in FLOWS:
        raise ConflictError(f"harness {harness!r} has no interactive login flow")
    spec = spec_for(ctx, harness)
    source = source_for(ctx, harness)
    flow = FLOWS[harness]
    # Every refusal first: the harness is known, the credential spec and directory are
    # configured, the CLI exists, no login is already running, the directory can be
    # written, and `replace` is set when a credential is there to be replaced. The event
    # this call ends with refuses a secret-shaped payload, so its one configured field is
    # scanned here too rather than after the credential has moved.
    argv = registry.resolve(ctx, harness)
    refuse_secret_shaped(" ".join(argv), field="login command")
    # Whether the existing directory is being replaced decides what has to be writable,
    # so it is decided first: a replacement renames the directory away and the CLI creates
    # a fresh one, and only the parent is written. An operator who protects a credential
    # directory read-only on purpose is entitled to replace it.
    replaceable = _check_replaceable(spec, source, harness=harness, replace=replace)
    _check_writable(source, harness=harness, reuse=not replaceable)
    image = None
    runner = getattr(registry, "container_runner", lambda _ctx: None)(ctx)
    if runner is not None:
        promoted = next(
            (
                item
                for item in uow.image_promotions.list_all()
                if item.harness == harness and item.state == "default"
            ),
            None,
        )
        if promoted is None:
            raise ConflictError(
                f"no promoted worker image is available for {harness}; promote one first"
            )
        image = promoted.reference
    retired = (
        _retire_existing(ctx, uow, source, principal=principal, harness=harness, reason=reason)
        if replaceable
        else None
    )
    try:
        if runner is not None:
            Path(source.path).mkdir(parents=True, exist_ok=True, mode=0o700)
            session = registry.start(ctx, harness, source.path, image=image)
        else:
            session = registry.start(ctx, harness, source.path)
    except Exception as exc:
        if retired is None:
            raise
        restored = _restore_retired(
            ctx, source, retired, principal=principal, harness=harness, failure=type(exc).__name__
        )
        raise CredentialAdminError(
            f"the {harness} login could not start ({type(exc).__name__}) after the existing "
            + (
                "credential had been retired, so it was put back at its configured path"
                if restored
                else f"credential had been retired, and it could not be put back: it is at "
                f"{retired} and the configured path is not the credential. Move it back or "
                "rotate a prepared directory in before the retention sweep shreds it"
            )
            + f": {exc}"
        ) from exc
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
        command=list(argv),
        image=image,
        retained_as=retired,
    )
    return {
        "harness": harness,
        "window": flow.window,
        "retained_as": retired,
        **session.as_dict(),
    }


def _check_writable(source: Any, *, harness: str, reuse: bool) -> None:
    """What the login has to be able to write, and only that.

    The parent is written in both paths: the retire renames the directory inside it and
    the CLI creates the new directory there. The directory itself is only written when
    the login will reuse it, which is when there is nothing at the configured path to
    retire. A replacement renames it away and never writes into it, so a credential
    directory an operator deliberately holds read-only is still replaceable.

    `os.access` answers for the real uid, so this is the clear message rather than a
    guarantee: the rename itself is still the authority, which is why a failed start is
    restored."""
    current = Path(source.path)
    parent = current.parent
    if not parent.is_dir():
        raise CredentialAdminError(
            f"the credential root {parent} for harness {harness!r} does not exist, so the "
            f"login has nowhere to write (credentials.{harness}.path)"
        )
    if not os.access(parent, os.W_OK | os.X_OK):
        raise CredentialAdminError(
            f"the credential root {parent} for harness {harness!r} is not writable by the "
            "Crucible service user, so the login cannot create or retire the directory"
        )
    if reuse and current.is_dir() and not os.access(current, os.W_OK | os.X_OK):
        raise CredentialAdminError(
            f"the {harness} credential directory {current} is not writable by the "
            "Crucible service user, so the login cannot write into it; a login that "
            "replaces an existing credential renames the directory aside instead and "
            "does not need it writable"
        )


def _check_replaceable(spec: Any, source: Any, *, harness: str, replace: bool) -> bool:
    """Whether a credential that still passes the shape check is at the configured path,
    and therefore has to be retired before the login writes over it. Refuses when one is
    there and `replace` was not given. Moves nothing: the shape check parses the named
    auth files and no value leaves it (12)."""
    current = Path(source.path)
    if not current.is_dir() or not check_shape(spec, source.path).ok:
        return False
    if not replace:
        raise ConflictError(
            f"the {harness} credential already passes the shape check; a login would "
            "overwrite it. Pass replace to retain and replace it, or use rotate to swap "
            "in a prepared directory"
        )
    return True


def _retire_existing(
    ctx: AdminContext,
    uow: UnitOfWork,
    source: Any,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> str | None:
    """Move the existing credential aside under rotate's retained name so the retention
    sweep shreds it on its own schedule. Nothing is shredded here and nothing is read.
    Every refusal has already been raised by the time this runs."""
    current = Path(source.path)
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


def _restore_retired(
    ctx: AdminContext,
    source: Any,
    retired_name: str,
    *,
    principal: str,
    harness: str,
    failure: str,
) -> bool:
    """The start failed after the credential had been moved aside, so put it back at the
    configured path: a credential is never left off its path because a later step failed.
    Returns whether it is back, because the caller's message to the operator is only true
    if it is.

    The caller's transaction rolls back with the exception and takes the retire event with
    it, but a rename does not roll back, so the undo is here and the failure is recorded
    through a unit of work of its own, the way a failed rotate records its own.

    Nothing at the configured path is removed to make room. A concurrent login that has
    already created the directory owns it, and renaming the retained copy over it would
    mix two credentials; the operator is told instead, which is recoverable, while a
    destroyed directory is not."""
    current = Path(source.path)
    retired = current.with_name(retired_name)
    restored = False
    problem = ""
    try:
        if not retired.is_dir():
            problem = "the retired directory is not where it was left"
        elif current.exists():
            if current.is_dir() and not any(current.iterdir()):
                current.rmdir()
                os.rename(retired, current)
                restored = True
            else:
                problem = "something else is at the configured path already"
        else:
            os.rename(retired, current)
            restored = True
    except OSError as exc:
        problem = f"the rename back failed with {type(exc).__name__}"
    record_refusal(
        ctx,
        principal=principal,
        operation=f"credentials login {harness}",
        detail=(
            f"the login failed to start ({failure}) after the credential was retired; "
            + (
                f"{retired_name} was renamed back to the configured path"
                if restored
                else f"{retired_name} is still retired and the configured path is not the "
                f"credential, because {problem}"
            )
        ),
    )
    return restored


def login_status(registry: LoginRegistry, harness: str) -> dict[str, Any]:
    session = registry.get(harness)
    if session is None:
        return {"harness": harness, "state": "none"}
    return session.as_dict()


def submit_code(
    registry: LoginRegistry,
    harness: str,
    code: str,
    *,
    ctx: AdminContext | None = None,
    uow: UnitOfWork | None = None,
    principal: str = "",
    reason: str | None = None,
) -> dict[str, Any]:
    session = registry.get(harness)
    if session is None or session.state != "waiting_for_code":
        raise ConflictError(f"no login for {harness} is waiting for a code")
    audited_reason: str | None = None
    if ctx is not None and uow is not None:
        audited_reason = guard_mutation(
            ctx,
            uow,
            reason,
            principal=principal,
            operation=f"credentials login code {harness}",
        )
    session.submit_code(code)
    if ctx is not None and uow is not None:
        assert audited_reason is not None
        admin_event(
            uow,
            ctx,
            EventKind.CREDENTIAL_LOGIN_CODE_SUBMITTED,
            principal=principal,
            reason=audited_reason,
            before=None,
            after={"submitted": True},
            harness=harness,
        )
    return session.as_dict()


def cancel_login(
    registry: LoginRegistry,
    harness: str,
    *,
    ctx: AdminContext,
    uow: UnitOfWork,
    principal: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"credentials login cancel {harness}"
    )
    session = registry.get(harness)
    if session is None or session.state in ("finished", "failed"):
        raise ConflictError(f"no login for {harness} is in progress")
    session.cancel_requested = True
    session._wake.set()
    admin_event(
        uow,
        ctx,
        EventKind.CREDENTIAL_LOGIN_CANCELLED,
        principal=principal,
        reason=reason,
        before={"state": session.state},
        after={"cancel_requested": True},
        harness=harness,
    )
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
