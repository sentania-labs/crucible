"""The Claude Code adapter (07, S1, S1b, S5).

Launch: `claude -p --permission-mode bypassPermissions --append-system-prompt-file
IDENTITY.md --output-format stream-json --verbose --model <model>` with the pointer
prompt on stdin. `stream-json` requires `--verbose` in print mode.

Credential: the dedicated Crucible session uses the CLI's long-lived token (S1b), kept
as the file `oauth-token` in the credential directory and delivered through the CLI's
documented variable at container start, the one exception 07 allows to file-only
delivery. The top-level state file `.claude.json` is seeded beside it so the CLI finds
the state it expects, and `CLAUDE_CONFIG_DIR` points at the mounted copy so both live in
one directory (S1). Neither file is written back: the token does not refresh, and the
state file is state, not a credential. `settings.json` is a Crucible-owned template
mounted read-only on top, so no hook, MCP server or plugin definition reaches a worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
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
    ProviderQuotaEvent,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "claude_code"
CONFIG_DIR = "/home/worker/.claude"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

AUTH_PATTERNS = base.patterns(
    "Not logged in",
    "Please run /login",
    "Invalid API key",
    "authentication_error",
    "Invalid authentication credentials",
    "OAuth token has expired",
    "OAuth token revoked",
    "invalid x-api-key",
)
QUOTA_PATTERNS = base.patterns(
    "hit your limit",
    "hit your usage limit",
    "usage limit reached",
    "out of extra usage",
    "rate_limit_error",
    "Credit balance is too low",
)
# What the CLI actually emits when the subscription window is used up (first live
# sample, C5b, 08:58 CDT on 2026-09-17): a `rate_limit_event` whose status is
# "rejected", with `out_of_credits` as the overage reason, then a synthetic result with
# `terminal_reason` `api_error` and exit 1. A `rate_limit_event` alone is not a signal
# (the CLI emits one with status "allowed" on ordinary runs) and neither is a rejected
# status on its own: any failing run whose tail happens to carry one, a GitHub payload
# or a fixture among them, would otherwise be read as an exhausted quota and retried
# under the wrong rule. The signal is the two together in the one event line.
QUOTA_PATTERNS += base.correlated(
    r"rate_limit_event.*\"status\"\s*:\s*\"rejected\"",
    r"rate_limit_event.*out_of_credits",
)


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    if document.get("type") != "rate_limit_event":
        return False
    info = document.get("rate_limit_info")
    if not isinstance(info, dict):
        return False
    return any(
        info.get(key) in {"rejected", "out_of_credits"}
        for key in ("status", "overageStatus", "overageDisabledReason")
    )


class ClaudeCodeAdapter:
    name = NAME
    supported_versions = VersionRange("2.1.0", "2.2.0")

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
            prompt_on_stdin=True,
            model_flag=True,
            effort_flag=False,
            transcript_format=TranscriptFormat.STREAM_JSON,
            endpoints=("api.anthropic.com",),
            shim="CLAUDE.md",
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            auth_files=(
                AuthFile("oauth-token", env_var=TOKEN_ENV, sync_back=False),
                AuthFile(".claude.json", json=True, required=False, sync_back=False),
            ),
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env="CLAUDE_CONFIG_DIR",
            templates={"settings.json": "{}\n"},
            login_hint=(
                "CLAUDE_CONFIG_DIR=<dir> claude setup-token; the token is shown once and is "
                "kept as <dir>/oauth-token, mode 600"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        argv = (
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt-file",
            f"{ctx.identity_mount}/IDENTITY.md",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            ctx.model,
        )
        spec = self.credential_spec()
        return AdapterLaunch(
            argv=argv,
            env=spec.env() if ctx.credential_mounted else {},
            env_from_files=spec.env_from_files() if ctx.credential_mounted else {},
            stdin_text=base.POINTER_PROMPT,
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        metrics, lines = _metrics(report_dir / base.TRANSCRIPT_NAME)
        return base.parse_report_dir(report_dir, exit, metrics=metrics, transcript_lines=lines)

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        return base.classify_with_patterns(
            exit, stdout_tail, stderr_tail, auth=AUTH_PATTERNS, quota=QUOTA_PATTERNS
        )


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """The final `result` line carries `usage`, `total_cost_usd` and `modelUsage`; the
    `system` init line names the model. Both are the CLI's own report (S1)."""
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        kind = event.get("type")
        if kind == "system" and isinstance(event.get("model"), str):
            model = str(event["model"])
        if kind == "result":
            tokens_in, tokens_out = base.usage_totals(event.get("usage"))
            cost = base.cost_usd(event.get("total_cost_usd"))
            used: Any = event.get("modelUsage")
            if isinstance(used, dict) and used:
                model = str(next(iter(used)))
    source = "harness_transcript" if count else "none"
    return ReportMetrics(model, tokens_in, tokens_out, cost, source), count
