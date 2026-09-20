"""What the four adapters share (07): the pointer prompt, report-directory parsing
against CompletionClaimV1, and exit classification from the code plus both tails (S5).

Nothing here reads a credential. The report directory an adapter parses is the
collector's copy (08), and everything in it is data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from crucible.contracts.completion_claim import parse_claim
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.ports.execution import IDENTITY_MOUNT, REPORT_MOUNT
from crucible.ports.harness import ExitInfo, ParsedReport, ProviderQuotaEvent, ReportMetrics

# Argv carries only a short pointer; the identity bundle and the contract are files
# (07, S3). The same sentence for every harness.
POINTER_PROMPT = f"Read {IDENTITY_MOUNT}/IDENTITY.md and execute the task."
TRANSCRIPT_NAME = "transcript.jsonl"
TRANSCRIPT_PATH = f"{REPORT_MOUNT}/{TRANSCRIPT_NAME}"
PROGRESS_NAME = "progress.jsonl"
TAIL_LIMIT = 64 * 1024

Pattern = tuple[str, re.Pattern[str]]


def patterns(*texts: str) -> tuple[Pattern, ...]:
    return tuple((t, re.compile(re.escape(t), re.IGNORECASE)) for t in texts)


def correlated(*sources: str) -> tuple[Pattern, ...]:
    """Patterns whose text is a regular expression rather than a literal, for a signal
    that is only a signal when two things appear together on one line. `.` never crosses
    a newline here, which is what keeps the correlation to a single transcript event."""
    return tuple((s, re.compile(s, re.IGNORECASE)) for s in sources)


def first_match(tails: Sequence[str], candidates: Sequence[Pattern]) -> str | None:
    for tail in tails:
        for name, pattern in candidates:
            if pattern.search(tail):
                return name
    return None


def classify_with_patterns(
    exit: ExitInfo,
    stdout_tail: str,
    stderr_tail: str,
    *,
    auth: Sequence[Pattern],
    quota: Sequence[Pattern],
) -> ExitClass:
    """The confirmed table of S5: the deterministic code first, then the auth and quota
    patterns from both tails on a non-zero exit only. A pattern never turns a clean exit
    into a failure, and a termination Crucible performed is never reclassified."""
    base = classify_exit(
        exit_code=exit.exit_code,
        report_present=exit.report_present,
        blocked_present=exit.blocked_present,
        lost=exit.lost,
        timed_out=exit.timed_out,
        killed=exit.killed,
    )
    if base in (ExitClass.LOST, ExitClass.TIMEOUT, ExitClass.KILLED, ExitClass.BLOCKED):
        return base
    if exit.exit_code == 0:
        return base
    if exit.oom_killed:
        return ExitClass.ENVIRONMENT
    tails = (stdout_tail[-TAIL_LIMIT:], stderr_tail[-TAIL_LIMIT:])
    if first_match(tails, auth) is not None:
        return ExitClass.AUTH_FAILURE
    if first_match(tails, quota) is not None:
        return ExitClass.QUOTA_EXHAUSTED
    return base


_RESET_KEYS = frozenset({"reset_at", "resetAt", "resets_at", "resetsAt", "reset_time"})


def _reset_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _RESET_KEYS:
                yield item
            yield from _reset_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reset_values(item)


def _reset_from_document(document: Mapping[str, Any]) -> datetime | None:
    for raw in _reset_values(document):
        if isinstance(raw, (int, float)):
            seconds = float(raw) / (1000 if raw > 10_000_000_000 else 1)
            try:
                return datetime.fromtimestamp(seconds, tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def quota_reset_at(*tails: str, quota: Sequence[Pattern]) -> datetime | None:
    """Parse a machine timestamp only from a line that also proves quota exhaustion."""

    for tail in tails:
        for line in reversed(tail[-TAIL_LIMIT:].splitlines()):
            if first_match((line,), quota) is None:
                continue
            try:
                document = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(document, dict):
                return _reset_from_document(document)
    return None


def provider_quota_event(
    *tails: str, predicate: Callable[[Mapping[str, Any]], bool]
) -> ProviderQuotaEvent | None:
    """Return the refusal and reset from the same structured harness event."""
    for tail in tails:
        for line in reversed(tail[-TAIL_LIMIT:].splitlines()):
            try:
                document = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(document, dict) and predicate(document):
                return ProviderQuotaEvent(reset_at=_reset_from_document(document))
    return None


def provider_quota_exhausted(*tails: str, signals: Sequence[Pattern]) -> bool:
    """Shared pool state requires a structured signal emitted by the harness itself."""
    return first_match(tuple(tail[-TAIL_LIMIT:] for tail in tails), signals) is not None


def read_text(path: Path, limit: int = 8 * 1024 * 1024) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def json_lines(path: Path, limit: int = 32 * 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Every parseable JSON object in a JSONL file. A line that is not one is skipped:
    the transcript is the harness's own stream and nothing here trusts its shape."""
    text = read_text(path, limit)
    if text is None:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def parse_report_dir(
    report_dir: Path,
    exit: ExitInfo,
    *,
    metrics: ReportMetrics,
    transcript_lines: int,
) -> ParsedReport:
    """`report.yaml` against CompletionClaimV1, `blocked.md`, and `progress.jsonl` (07).

    A missing report with exit 0 is `completed_without_report`; that is the caller's
    classification, and this only says whether the file was there and whether it parsed."""
    report_file = report_dir / "report.yaml"
    raw = read_text(report_file) if report_file.is_file() else None
    claim: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []
    if raw is not None:
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            loaded = None
            errors.append({"loc": [], "msg": f"report.yaml is not YAML: {exc}", "type": "yaml"})
        if isinstance(loaded, dict):
            claim = loaded
            _, errors = parse_claim(loaded)
        elif not errors:
            errors.append({"loc": [], "msg": "report is not a mapping", "type": "shape"})
    blocked = report_dir / "blocked.md"
    blocked_md = read_text(blocked) if blocked.is_file() else None
    progress = tuple(json_lines(report_dir / PROGRESS_NAME, 4 * 1024 * 1024))[:1000]
    transcript = report_dir / TRANSCRIPT_NAME
    return ParsedReport(
        claim=claim,
        raw=raw,
        errors=errors,
        blocked_md=blocked_md,
        report_present=raw is not None,
        progress=progress,
        metrics=metrics,
        transcript_lines=transcript_lines,
        transcript_name=TRANSCRIPT_NAME if transcript.is_file() else None,
    )


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def usage_totals(usage: Any) -> tuple[int | None, int | None]:
    """(tokens_in, tokens_out) from the usage object shapes the three CLIs emit."""
    if not isinstance(usage, dict):
        return None, None
    tokens_in = _int(usage.get("input_tokens"))
    if tokens_in is None:
        tokens_in = _int(usage.get("prompt_tokens"))
    if tokens_in is None:
        tokens_in = _int(usage.get("promptTokenCount"))
    tokens_out = _int(usage.get("output_tokens"))
    if tokens_out is None:
        tokens_out = _int(usage.get("completion_tokens"))
    if tokens_out is None:
        tokens_out = _int(usage.get("candidatesTokenCount"))
    return tokens_in, tokens_out


def cost_usd(value: Any) -> float | None:
    return _float(value)
