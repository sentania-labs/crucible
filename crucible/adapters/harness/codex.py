"""The Codex adapter (07, S1, S2, S5, S6).

Launch: `codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
--disable plugins -c check_for_update_on_startup=false --json -o <last message>
--model <model> -C <checkout>` with IDENTITY.md followed by the pointer prompt on stdin.
The bypass flag stays because Codex's own sandbox cannot run inside the worker (S2); the
container is the boundary. `--skip-git-repo-check` because the checkout belongs to the
container's uid and not to a host user Codex would trust (S1). `--disable plugins` keeps
it off github.com and chatgpt.com (S6). The update opt-out is a launch flag, not image
state (S7, S11).

Credential: `auth.json`, rw-narrow from the start because the CLI writes session and
log state beside it and fails on a read-only directory before auth is tested (S1). Only
`auth.json` syncs back, chosen by the newer `last_refresh` (12). `config.toml` is a
Crucible-owned template mounted read-only on top: the operator's per-project trust,
MCP servers and hook trust hashes never reach a worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import (
    CODEX_BINARY,
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

NAME = "codex"
CONFIG_DIR = "/home/worker/.codex"
LAST_MESSAGE = "codex-last-message.md"
CONFIG_TEMPLATE = (
    "# Crucible-owned Codex configuration template (12). The credential directory a\n"
    "# worker sees holds auth.json and this file; per-project trust, MCP servers and\n"
    "# hooks are never inherited from a worker or from the operator.\n"
)

AUTH_PATTERNS = base.patterns(
    "401 Unauthorized",
    "Missing bearer or basic authentication",
    "unauthorized",
    "Not logged in",
    "not authenticated",
    "codex login",
    "invalid_api_key",
    "Your ChatGPT session has expired",
)
QUOTA_PATTERNS = base.patterns(
    "usage_limit_reached",
    "usage limit",
    "insufficient_quota",
    "rate_limit_exceeded",
    "Rate limit reached",
    "429 Too Many Requests",
)


def _provider_quota_refusal(document: Mapping[str, Any]) -> bool:
    error = document.get("error")
    return (
        document.get("type") == "turn.failed"
        and isinstance(error, dict)
        and error.get("code") == "usage_limit_reached"
    )


class CodexAdapter:
    name = NAME
    supported_versions = VersionRange("0.153.0", "0.157.0")

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
            effort_flag=True,
            transcript_format=TranscriptFormat.JSON_EVENTS,
            # S6 named api.openai.com and auth.openai.com, with chatgpt.com "only if the
            # authenticated re-run shows the ChatGPT-plan backend needs it". The C5 live
            # run showed exactly that: with `auth_mode = chatgpt` the CLI reconnects to
            # chatgpt.com until it is permitted. ab.chatgpt.com stays denied.
            endpoints=("api.openai.com", "auth.openai.com", "chatgpt.com"),
            shim="AGENTS.md",
            # `login --device-auth` asks auth.openai.com for the user code, polls its
            # deviceauth token endpoint and exchanges at /oauth/token there (the pinned
            # 0.156.0 binary's strings, 2026-09-24). api.openai.com is the model API.
            login_endpoints=("auth.openai.com",),
        )

    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=NAME,
            mount_target=CONFIG_DIR,
            auth_files=(
                AuthFile(
                    "auth.json", json=True, json_keys=("tokens",), issued_at=("last_refresh",)
                ),
            ),
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env="CODEX_HOME",
            templates={"config.toml": CONFIG_TEMPLATE},
            login_hint=(
                "CODEX_HOME=<dir> codex login --device-auth; the device code expires in 15 minutes"
            ),
        )

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        argv: list[str] = [
            CODEX_BINARY,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--disable",
            "plugins",
            "-c",
            "check_for_update_on_startup=false",
        ]
        if ctx.effort:
            argv += ["-c", f"model_reasoning_effort={_toml_string(ctx.effort)}"]
        argv += [
            "--json",
            "-o",
            f"{ctx.report_mount}/{LAST_MESSAGE}",
            "--model",
            ctx.model,
            "-C",
            ctx.repo_mount,
        ]
        spec = self.credential_spec()
        return AdapterLaunch(
            argv=tuple(argv),
            env=spec.env() if ctx.credential_mounted else {},
            stdin_files=(f"{ctx.identity_mount}/IDENTITY.md",),
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


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _metrics(transcript: Path) -> tuple[ReportMetrics, int]:
    """`turn.completed` events carry `usage`; any event naming a `model` names it."""
    model: str | None = None
    tokens_in = 0
    tokens_out = 0
    seen_usage = False
    count = 0
    for event in base.json_lines(transcript):
        count += 1
        if isinstance(event.get("model"), str):
            model = str(event["model"])
        if event.get("type") == "turn.completed":
            i, o = base.usage_totals(event.get("usage"))
            if i is not None or o is not None:
                seen_usage = True
                tokens_in += i or 0
                tokens_out += o or 0
    source = "harness_transcript" if count else "none"
    return (
        ReportMetrics(
            model,
            tokens_in if seen_usage else None,
            tokens_out if seen_usage else None,
            None,
            source,
        ),
        count,
    )
