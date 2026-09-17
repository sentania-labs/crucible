"""The AGY adapter (07, S1, S3, S5, S6).

Launch: `agy -p "<pointer>" --model <model> --effort <effort>
--dangerously-skip-permissions --add-dir <identity> --output-format stream-json
--print-timeout <timeout>`. The prompt is a pointer under 1 KB (the per-argument ceiling
is the kernel's 128 KiB, S3) and the bundle travels by `--add-dir`. `--print-timeout`
defaults to five minutes in the CLI, so it is set to the attempt's own timeout.

Credential: the token file under `.gemini/antigravity-cli/`, mounted at the CLI's
config directory. The adapter's minimum is `ro` (12): AGY refreshes in memory once the
access token is past its one-hour expiry, and S1 saw the read-only mount refuse the
save while the run still completed. The probe and the live tier record whether a run
rotated the token; when one does, the configured mount mode becomes rw-narrow and the
file syncs back by the newer `token.expiry`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import (
    AdapterLaunch,
    AuthFile,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    MountMode,
    ParsedReport,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "agy"
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
    "429",
)


class AgyAdapter:
    name = NAME
    supported_versions = VersionRange("1.2.0", "1.3.0")

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=True,
            transcript_format=TranscriptFormat.STREAM_JSON,
            endpoints=("daily-cloudcode-pa.googleapis.com", "oauth2.googleapis.com"),
            shim="AGENTS.md",
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
            minimum_mode=MountMode.RO,
            config_dir_env=None,
            templates={},
            login_hint=(
                "HOME=<dir> agy, then paste the code within 60 seconds; have the browser "
                "signed in first"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        argv: list[str] = ["agy", "-p", base.POINTER_PROMPT, "--model", ctx.model]
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

    def classify_exit(self, exit: ExitInfo, stdout_tail: str, stderr_tail: str) -> ExitClass:
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
    """The final `result` line carries the token usage (S3); `init` names the model."""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        if isinstance(event.get("model"), str):
            model = str(event["model"])
        if event.get("type") == "result":
            tokens_in, tokens_out = _usage(event)
    source = "harness_transcript" if count else "none"
    return ReportMetrics(model, tokens_in, tokens_out, None, source), count
