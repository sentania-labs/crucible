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
MCP servers and hook trust hashes never reach a worker. Parallel copies are allowed,
but OpenAI rotates refresh tokens on use, so simultaneous renewals can break login.
Keep auth_failure and its retry-once safety net; operators can roll back with
concurrency.per_harness.codex: 1 if the supervisor auth-failure counts rise.

Commands (issue 128): the pinned models run commands through unified exec
(`shell_type: unified_exec` in the CLI's own model catalog), which returns after at most
30 seconds with a session the model must poll with `write_stdin`; a turn that ends
instead leaves the command to die with the process (reproduced on 0.156.0 against a
stub model, 2026-09-25). Turning the `unified_exec` feature off does not change the
tool, so nothing at the launch can make a command block. The launch sets
`background_terminal_max_timeout`, the longest single poll, to the launch's command
timeout so one poll can wait out a long build. A session the model leaves open when it
ends its turn is a background process that dies with the sandbox, not unfinished work
(issue 153): Codex exits only once the turn is over, so it never exits while a command
it was waiting on is still running, and its exit is classified on the exit code and
the report alone. While the run lasts, an open `command_execution` item still counts as
activity for the stall clock (issue 152).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from crucible.adapters.harness import base
from crucible.adapters.harness.hermes import HermesAdapter
from crucible.domain.exit_class import ExitClass
from crucible.domain.harness_concurrency import HARNESS_CONCURRENCY
from crucible.domain.harness_settings import DEFAULT_HERMES_CONTEXT_LENGTH, hermes_run_limits
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
    concurrency = HARNESS_CONCURRENCY[NAME]
    parallel_attempts_safe = concurrency.parallel_attempts_safe
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
            minimum_mode=MountMode(self.concurrency.minimum_mode),
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
            # Issue 128: the longest poll of a running command, from the launch.
            "-c",
            f"background_terminal_max_timeout={ctx.command_timeout}",
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
        env = spec.env() if ctx.credential_mounted else {}
        env_from_files: dict[str, str] = {}
        if ctx.endpoint == "local" and ctx.endpoint_url:
            gateway_credential = HermesAdapter().credential_spec()
            assert gateway_credential is not None
            limits = hermes_run_limits(ctx.harness_settings)
            context_window = limits.context_length or DEFAULT_HERMES_CONTEXT_LENGTH
            env = {
                "CODEX_HOME": CONFIG_DIR,
                "OPENAI_API_KEY": "local-no-auth",
                "CRUCIBLE_CODEX_CONFIG": (
                    'model_provider = "local_gateway"\n'
                    f"model_context_window = {context_window}\n"
                    "[model_providers.local_gateway]\n"
                    'name = "Local gateway"\n'
                    f"base_url = {_toml_string(ctx.endpoint_url)}\n"
                    'env_key = "OPENAI_API_KEY"\n'
                    'wire_api = "responses"\n'
                ),
            }
            if ctx.credential_mounted:
                env_from_files = gateway_credential.env_from_files()
        return AdapterLaunch(
            argv=tuple(argv),
            env=env,
            env_from_files=env_from_files,
            stdin_files=(f"{ctx.identity_mount}/IDENTITY.md",),
            stdin_text=base.POINTER_PROMPT,
            transcript_path=f"{ctx.report_mount}/{base.TRANSCRIPT_NAME}",
            workdir=ctx.repo_mount,
        )

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        transcript = report_dir / base.TRANSCRIPT_NAME
        metrics, lines = _metrics(transcript)
        return base.parse_report_dir(
            report_dir,
            exit,
            metrics=metrics,
            transcript_lines=lines,
        )

    def classify_exit(
        self, exit: ExitInfo, stdout_tail: str, stderr_tail: str, report_dir: Path | None = None
    ) -> ExitClass:
        return base.classify_with_patterns(
            exit, stdout_tail, stderr_tail, auth=AUTH_PATTERNS, quota=QUOTA_PATTERNS
        )

    def command_tracker(self) -> CommandTracker:
        return CommandTracker()


class CommandTracker(base.LineTracker):
    """Issue 152: `command_execution` items the live `--json` log started and has not
    completed. Only commands count: other items (a todo list) stay open for a turn."""

    def __init__(self) -> None:
        super().__init__()
        self._started: dict[str, str] = {}

    def line(self, text: str) -> None:
        event = base.json_object(text) or {}
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        if event.get("type") == "item.started":
            self._started[item_id] = base.in_flight_summary(
                f"command {item_id}", str(item.get("command") or item_id)
            )
        elif event.get("type") == "item.completed":
            self._started.pop(item_id, None)

    @property
    def running(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._started.items())


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


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
