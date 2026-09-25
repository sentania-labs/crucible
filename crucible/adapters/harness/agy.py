"""The AGY adapter (07, S1, S3, S5, S6).

Launch: `agy -p "<pointer>" --model <model> --effort <effort>
--dangerously-skip-permissions --add-dir <identity> --output-format stream-json
--print-timeout <timeout>`. The prompt is a pointer under 1 KB (the per-argument ceiling
is the kernel's 128 KiB, S3) and the bundle travels by `--add-dir`. `--print-timeout`
defaults to five minutes in the CLI, so it is set to the attempt's own timeout.

Credential: the token file under `.gemini/antigravity-cli/`, mounted at the CLI's
config directory. 12 started it at `ro` pending evidence: AGY refreshes in memory once
the access token is past its one-hour expiry, and S1 saw the read-only mount refuse the
save. The C5 live run supplied the evidence: the first Crucible-side run after the
expiry rotated the token and the copy carried a newer `token.expiry`, so the minimum is
rw-narrow and the file syncs back by that field.

Commands (issue 128): the pinned 1.2.8 CLI has no launch-level command timeout and no
switch for backgrounding. Its `run_command` tool takes `Blocking` and
`WaitMsBeforeAsync` from the model, per call, and a command still running after that
wait continues in the background ("Background command is still running after %ds",
from the binary's strings, 2026-09-25); neither `agy --help` nor the public CLI docs
name a setting that changes it. `--print-timeout` bounds the whole turn and is the
attempt's own timeout. The one mitigation is in the prompt: run commands blocking, for
up to the launch's command timeout, and never end the turn with one running. What print mode does with a background command at exit was not
observed: AGY cannot run without a Google login, so it was not reproduced. Its
transcript format for background commands is unknown, so this adapter has no in-flight
evidence to read and its attempts are classified by exit code and report alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import (
    AGY_BINARY,
    AdapterLaunch,
    AuthFile,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    MountMode,
    ParsedReport,
    ProviderQuotaEvent,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "agy"
# Issue 128: AGY takes blocking and backgrounding from the model per command, and the
# launch has no setting for either, so the prompt says it. A mitigation, not a guard:
# nothing enforces it and AGY's exit with a command running is not detected.
BLOCKING_NOTE = (
    "Run every shell command blocking and wait for it to finish, up to {minutes} minutes; "
    "never end your turn while a command you started is still running."
)
CONFIG_DIR = "/home/worker/.gemini"
TOKEN_FILE = "antigravity-cli/antigravity-oauth-token"

AUTH_PATTERNS = base.patterns(
    "authentication failed or timed out",
    "authentication required",
    "Please sign in",
    "not logged in",
    "invalid_grant",
    "UNAUTHENTICATED",
    "Run 'agy' to log in",
)
QUOTA_PATTERNS = base.patterns(
    "RESOURCE_EXHAUSTED",
    "quota exceeded",
    "Quota exceeded",
    "rate limit exceeded",
    "Too Many Requests",
)


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    return (
        document.get("type") == "result"
        and document.get("status") == "ERROR"
        and "RESOURCE_EXHAUSTED" in str(document.get("error", ""))
    )


class AgyAdapter:
    name = NAME
    supported_versions = VersionRange("1.2.0", "1.3.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return base.quota_reset_at(stdout_tail, stderr_tail, quota=QUOTA_PATTERNS)

    def provider_quota_event(self, stdout_tail: str, stderr_tail: str) -> ProviderQuotaEvent | None:
        return base.provider_quota_event(
            stdout_tail, stderr_tail, predicate=_provider_quota_refusal
        )

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return self.provider_quota_event(stdout_tail, stderr_tail) is not None

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=True,
            transcript_format=TranscriptFormat.STREAM_JSON,
            # S6 named the model API and the OAuth refresh grant. The C5 live runs added
            # two more: the CLI's eligibility check calls the userinfo endpoint on
            # www.googleapis.com and then fetches the account's profile picture from
            # lh3.googleusercontent.com before any turn, and fails closed when either
            # is refused.
            endpoints=(
                "daily-cloudcode-pa.googleapis.com",
                "lh3.googleusercontent.com",
                "oauth2.googleapis.com",
                "www.googleapis.com",
            ),
            shim="AGENTS.md",
            # The Google OAuth code exchange (oauth2.googleapis.com/token) and the
            # userinfo call (www.googleapis.com/oauth2/v2/userinfo), from the pinned
            # 1.2.8 binary's strings on 2026-09-24. daily-cloudcode-pa is the model API,
            # so the prompt AGY's login command ends with cannot reach it from a login
            # Job; the token is judged by its shape and then by the probe.
            login_endpoints=("oauth2.googleapis.com", "www.googleapis.com"),
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            source_subdir=".gemini",
            auth_files=(
                AuthFile(
                    TOKEN_FILE, json=True, json_keys=("token",), issued_at=("token", "expiry")
                ),
            ),
            # rw-narrow since the C5 live run: the first Crucible-side run after the
            # one-hour expiry refreshed the token and the copy carried a newer expiry,
            # which the sync-back wrote to the source (12: "would make it rw-narrow").
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env=None,
            templates={},
            login_hint=(
                "HOME=<dir> agy, then paste the code within 60 seconds; have the browser "
                "signed in first"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        note = BLOCKING_NOTE.format(minutes=max(1, -(-ctx.command_timeout // 60_000)))
        argv: list[str] = [
            AGY_BINARY,
            "-p",
            f"{base.POINTER_PROMPT} {note}",
            "--model",
            ctx.model,
        ]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        argv += [
            "--dangerously-skip-permissions",
            "--add-dir",
            ctx.identity_mount,
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{max(int(ctx.timeout_seconds), 1)}s",
        ]
        return AdapterLaunch(
            argv=tuple(argv),
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        metrics, lines = _metrics(report_dir / base.TRANSCRIPT_NAME)
        return base.parse_report_dir(report_dir, exit, metrics=metrics, transcript_lines=lines)

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        # S5: AGY exits 1 for an auth failure and still emits a well-formed `result`
        # line; the class comes from its text, not from the line's presence.
        return base.classify_with_patterns(
            exit, stdout_tail, stderr_tail, auth=AUTH_PATTERNS, quota=QUOTA_PATTERNS
        )


def _usage(event: dict[str, Any]) -> tuple[int | None, int | None]:
    for key in ("usage", "token_usage", "tokens", "usageMetadata"):
        tokens_in, tokens_out = base.usage_totals(event.get(key))
        if tokens_in is not None or tokens_out is not None:
            return tokens_in, tokens_out
    tokens_in = event.get("input_tokens")
    tokens_out = event.get("output_tokens")
    if isinstance(tokens_in, int) or isinstance(tokens_out, int):
        return (
            tokens_in if isinstance(tokens_in, int) else None,
            tokens_out if isinstance(tokens_out, int) else None,
        )
    return None, None


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """The final `result` line carries the token usage (S3); `init` names the model.

    The 1.2.4 stream keys each line as `{"event": "result", "result": {...}}` (found
    live); S3's description read it as `type`. Both shapes are accepted."""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        kind = str(event.get("event") or event.get("type") or "")
        nested = event.get(kind)
        body: dict[str, Any] = nested if isinstance(nested, dict) else event
        if isinstance(body.get("model"), str):
            model = str(body["model"])
        if kind == "result":
            tokens_in, tokens_out = _usage(body)
    source = "harness_transcript" if count else "none"
    return ReportMetrics(model, tokens_in, tokens_out, None, source), count
