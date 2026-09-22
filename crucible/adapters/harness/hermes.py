"""Hermes 0.19 worker adapter for local OpenAI-compatible endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass, classify_exit
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

NAME = "hermes"
HERMES_HOME = "/home/worker/.hermes"
AUTH_DIR = "/home/worker/.hermes-auth"
USAGE_NAME = "hermes-usage.json"

PROVIDER_PATTERNS = base.patterns(
    "connection refused",
    "connection error",
    "failed to connect",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "HTTP 500",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
)
QUOTA_PATTERNS = base.patterns(
    "quota exceeded",
    "insufficient_quota",
    "rate limit exceeded",
    "429 Too Many Requests",
)


def _usage(report_dir: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if report_dir is None:
        return None, "Hermes usage record was not available to the classifier"
    path = report_dir / USAGE_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"missing {USAGE_NAME}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"unparsable {USAGE_NAME}: {type(exc).__name__}"
    if not isinstance(document, dict):
        return None, f"unparsable {USAGE_NAME}: root is not an object"
    completed = document.get("completed")
    failed = document.get("failed")
    if not isinstance(completed, bool) or not isinstance(failed, bool):
        return None, f"unparsable {USAGE_NAME}: completed and failed must be booleans"
    return document, None


def _metrics(document: Mapping[str, Any] | None) -> ReportMetrics:
    if document is None:
        return ReportMetrics()

    def integer(name: str) -> int | None:
        value = document.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    cost = document.get("estimated_cost_usd")
    return ReportMetrics(
        model=str(document["model"]) if isinstance(document.get("model"), str) else None,
        tokens_in=integer("input_tokens"),
        tokens_out=integer("output_tokens"),
        cost_usd=float(cost)
        if isinstance(cost, int | float) and not isinstance(cost, bool)
        else None,
        source="hermes_usage",
        duration_ms=integer("duration_ms"),
        tool_calls=integer("tool_calls"),
    )


class HermesAdapter:
    name = NAME
    supported_versions = VersionRange("0.19.0", "0.20.0")

    def quota_reset_at(self, stdout_tail: str, stderr_tail: str) -> datetime | None:
        return None

    def provider_quota_event(self, stdout_tail: str, stderr_tail: str) -> ProviderQuotaEvent | None:
        # Hermes 0.19 has no structured provider-refusal event. Text may classify this
        # attempt, but it can never write shared pool exhaustion state.
        return None

    def provider_quota_exhausted(self, stdout_tail: str, stderr_tail: str) -> bool:
        return False

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=True,
            effort_flag=False,
            transcript_format=TranscriptFormat.NONE,
            endpoints=(),
            shim=None,
        )

    def credential_spec(self) -> CredentialSpec | None:
        return CredentialSpec(
            harness=NAME,
            mount_target=AUTH_DIR,
            auth_files=(AuthFile("api-key", env_var="OPENAI_API_KEY", sync_back=False),),
            minimum_mode=MountMode.RO,
            required_for_launch=False,
            login_hint="Paste the LiteLLM virtual key in the Crucible admin credential panel",
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        if ctx.endpoint != "local" or ctx.endpoint_url is None:
            raise ValueError("Hermes is supported only with a configured local endpoint")
        usage_path = f"{ctx.report_mount}/{USAGE_NAME}"
        spec = self.credential_spec()
        assert spec is not None
        return AdapterLaunch(
            argv=(
                "crucible-hermes",
                "--ignore-user-config",
                # This disables AGENTS.md, skills, and memory injection. The identity
                # bundle remains the only instruction source for the attempt.
                "--ignore-rules",
                # Safe mode also disables plugins and MCP servers. The explicit flags
                # stay present so this launch shape documents each boundary directly.
                "--safe-mode",
                # The container is the permission boundary, as for the other adapters.
                "--yolo",
                "--provider",
                "openai-api",
                "--model",
                ctx.model,
                "--toolsets",
                "terminal,file",
                "--usage-file",
                usage_path,
                "-z",
                base.POINTER_PROMPT,
            ),
            env={
                "HERMES_HOME": HERMES_HOME,
                "OPENAI_BASE_URL": ctx.endpoint_url,
                "OPENAI_API_KEY": "local-no-auth",
                "CRUCIBLE_HERMES_USAGE": usage_path,
            },
            env_from_files=spec.env_from_files() if ctx.credential_mounted else {},
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        usage, error = _usage(report_dir)
        transcript = report_dir / base.TRANSCRIPT_NAME
        text = base.read_text(transcript) if transcript.is_file() else None
        parsed = base.parse_report_dir(
            report_dir,
            exit,
            metrics=_metrics(usage),
            transcript_lines=len(text.splitlines()) if text is not None else 0,
        )
        return ParsedReport(
            claim=parsed.claim,
            raw=parsed.raw,
            errors=parsed.errors,
            blocked_md=parsed.blocked_md,
            report_present=parsed.report_present,
            progress=parsed.progress,
            metrics=parsed.metrics,
            transcript_lines=parsed.transcript_lines,
            transcript_name=parsed.transcript_name,
            run_evidence_error=error,
        )

    def classify_exit(
        self,
        exit: ExitInfo,
        stdout_tail: str,
        stderr_tail: str,
        report_dir: Path | None = None,
    ) -> ExitClass:
        if exit.lost:
            return ExitClass.LOST
        if exit.timed_out:
            return ExitClass.TIMEOUT
        if exit.killed:
            return ExitClass.KILLED
        if exit.oom_killed:
            return ExitClass.ENVIRONMENT

        usage, _ = _usage(report_dir)
        tails = (stdout_tail[-base.TAIL_LIMIT :], stderr_tail[-base.TAIL_LIMIT :])
        quota = base.first_match(tails, QUOTA_PATTERNS) is not None
        provider_error = base.first_match(tails, PROVIDER_PATTERNS) is not None
        if usage is not None and usage.get("failed") is True:
            if quota:
                return ExitClass.QUOTA_EXHAUSTED
            return ExitClass.PROVIDER_ERROR if provider_error else ExitClass.CRASHED
        if exit.exit_code == 75:
            return ExitClass.QUOTA_EXHAUSTED if quota else ExitClass.PROVIDER_ERROR
        if provider_error:
            return ExitClass.PROVIDER_ERROR
        if usage is not None and usage.get("completed") is True:
            return (
                ExitClass.COMPLETED if exit.report_present else ExitClass.COMPLETED_WITHOUT_REPORT
            )
        if quota:
            return ExitClass.QUOTA_EXHAUSTED
        # A missing usage record is handled as an evidence anomaly. The report still
        # decides whether a clean process reached the ordinary completion path.
        return classify_exit(
            exit_code=exit.exit_code,
            report_present=exit.report_present,
            blocked_present=False,
        )
