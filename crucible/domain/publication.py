"""What Crucible puts on a pull request, and what it refuses to put there (23).

Pure functions over the contract, the verified evidence, and the worker's claim. The
title comes from the claim after validation; the body is rendered here and nothing
worker-asserted appears in it as a verified fact. Closing references come from
`deliverables[].closes` alone, and every other closing keyword is defanged before the
text reaches GitHub, because a body is a mutation of the repository's issue tracker as
surely as a push is a mutation of its refs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from crucible.domain.secrets import redact, scan_text

MAX_TITLE_LENGTH = 72
MAX_BODY_BYTES = 60_000

# The keywords GitHub acts on when they precede an issue reference. Case-insensitive,
# and GitHub accepts them anywhere in the body, not only at the start of a line.
CLOSING_KEYWORDS: tuple[str, ...] = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)
_CLOSING_RE = re.compile(
    r"\b(" + "|".join(CLOSING_KEYWORDS) + r")\b(\s*:?\s*)(?=(?:[\w.-]+/[\w.-]+)?#\d+|https?://)",
    re.IGNORECASE,
)
# A reference a `closes` entry may name: `#12`, `owner/repo#12`, or an issue URL.
_REF_RE = re.compile(
    r"\A(?:[\w.-]+/[\w.-]+)?#\d+\Z|\Ahttps://github\.com/[\w.-]+/[\w.-]+/issues/\d+\Z"
)


class TitleRefusedError(ValueError):
    """The claim's proposed title cannot be used as a PR title (23)."""


@dataclass(frozen=True, slots=True)
class VerifiedCheck:
    """One required verification command as Crucible's own verifier ran it (11)."""

    id: str
    command: str
    exit_code: int
    expect_exit: int = 0
    artifact_id: str | None = None
    ran: bool = True

    @property
    def ok(self) -> bool:
        return self.ran and self.exit_code == self.expect_exit


@dataclass(frozen=True, slots=True)
class CriterionMapping:
    id: str
    text: str
    status: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CorrectionEntry:
    version: int
    reason: str
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BodyInput:
    """Everything the body may name. Anything absent here cannot reach the body."""

    external_id: str
    objective: str
    head_sha: str
    attempt_id: str
    harness: str
    harness_version: str
    image_digest: str
    criteria: tuple[CriterionMapping, ...] = ()
    checks: tuple[VerifiedCheck, ...] = ()
    review_reference: dict[str, str] | None = None
    corrections: tuple[CorrectionEntry, ...] = ()
    closes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    artifact_verifications: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


def defang_closing_keywords(text: str) -> str:
    """Neutralize every closing keyword that precedes a reference.

    A backslash before the word stops GitHub reading it as a keyword and keeps the
    sentence readable, which matters because the text this runs over is a worker's
    summary of its own limitations."""
    return _CLOSING_RE.sub(lambda m: "\\" + m.group(1) + m.group(2), text)


def sanitize(text: str) -> str:
    """Redact secrets, then defang closing keywords. Order matters: a redaction marker
    must not reintroduce a keyword, and it cannot, but a secret containing `fixes #1`
    would otherwise survive the defanging as part of the value."""
    return defang_closing_keywords(redact(text))


def validate_title(proposed: str) -> str:
    """23: length, no secret patterns, no closing keywords. Raises rather than edits,
    because a silently rewritten title is a claim Crucible did not make."""
    title = " ".join(proposed.split())
    if not title:
        raise TitleRefusedError("the claim proposes an empty pull request title")
    # The secret check runs before the length check on purpose: a long title containing
    # a token would otherwise be refused as "too long", which tells the reader nothing
    # about the thing that matters.
    hit = scan_text(title)
    if hit is not None:
        raise TitleRefusedError(f"the proposed title matches the {hit} secret pattern")
    if len(title) > MAX_TITLE_LENGTH:
        raise TitleRefusedError(
            f"the proposed title is {len(title)} characters; the limit is {MAX_TITLE_LENGTH}"
        )
    if _CLOSING_RE.search(title):
        raise TitleRefusedError(
            "the proposed title carries a closing keyword; only deliverables[].closes "
            "may close an issue (23)"
        )
    if "\n" in proposed or "\r" in proposed:
        raise TitleRefusedError("a pull request title is one line")
    return title


def authorized_closes(closes: Sequence[str]) -> list[str]:
    """The subset of `deliverables[].closes` that is a reference GitHub understands.

    An entry that is not a reference is dropped rather than rendered, because a
    `Closes` line naming something else is noise at best."""
    out: list[str] = []
    for ref in closes:
        candidate = ref.strip()
        if _REF_RE.match(candidate) and candidate not in out:
            out.append(candidate)
    return out


def _bullets(title: str, items: Sequence[str], *, note: str = "") -> list[str]:
    if not items:
        return []
    heading = f"## {title}" if not note else f"## {title}\n\n{note}"
    return [heading, "", *[f"- {sanitize(str(item))}" for item in items], ""]


def render_body(body: BodyInput) -> str:
    """The pull request body, from the contract and verified evidence only (23)."""
    lines: list[str] = [
        f"## Objective ({body.external_id})",
        "",
        sanitize(body.objective),
        "",
    ]
    if body.criteria:
        lines += ["## Acceptance criteria", ""]
        lines += ["| Criterion | Status | Verified mapping |", "|---|---|---|"]
        for criterion in body.criteria:
            lines.append(
                f"| {criterion.id}: {_cell(criterion.text)} | {_cell(criterion.status)} "
                f"| {_cell(criterion.evidence)} |"
            )
        lines.append("")
    lines += ["## Verification", ""]
    if body.checks:
        lines += ["| Check | Command | Exit | Log artifact |", "|---|---|---|---|"]
        for check in body.checks:
            artifact = check.artifact_id or "none"
            exit_cell = str(check.exit_code) if check.ran else "not run"
            lines.append(
                f"| {_cell(check.id)} | `{_cell(check.command)}` | {exit_cell} "
                f"| `{_cell(artifact)}` |"
            )
        lines.append("")
        lines.append(
            "Every exit code above is Crucible's own re-run of the command in a verifier "
            "container, not the worker's report of it."
        )
        lines.append("")
    else:
        lines += ["The contract required no verification commands.", ""]
    if body.artifact_verifications:
        lines += _bullets("Required artifacts", body.artifact_verifications)
    if body.review_reference:
        lines += ["## Internal review", ""]
        for key in sorted(body.review_reference):
            lines.append(f"- {_cell(key)}: `{_cell(body.review_reference[key])}`")
        lines.append("")
    if body.corrections:
        lines += ["## Corrections", ""]
        for correction in body.corrections:
            addressed = ", ".join(correction.addresses) if correction.addresses else "none named"
            lines.append(
                f"- contract version {correction.version}: {sanitize(correction.reason)} "
                f"(addresses: {sanitize(addressed)})"
            )
        lines.append("")
    lines += [
        "## Provenance",
        "",
        f"- attempt: `{_cell(body.attempt_id)}`",
        f"- head: `{_cell(body.head_sha)}`",
        f"- harness: `{_cell(body.harness)}` `{_cell(body.harness_version)}`",
        f"- image digest: `{_cell(body.image_digest)}`",
        "",
    ]
    if body.limitations:
        lines += _bullets(
            "Limitations",
            body.limitations,
            note="Worker-asserted. Crucible did not verify these.",
        )
    if body.risks:
        lines += _bullets(
            "Risks", body.risks, note="Worker-asserted. Crucible did not verify these."
        )
    closes = authorized_closes(body.closes)
    if closes:
        lines += ["## Closes", ""]
        lines += [f"Closes {ref}" for ref in closes]
        lines.append("")
    lines += [
        "---",
        "",
        "Opened by Crucible from a verified head. The facts above are Crucible's "
        "observations; the judgment is the orchestrator's.",
    ]
    text = "\n".join(lines).rstrip() + "\n"
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        text = text.encode("utf-8")[:MAX_BODY_BYTES].decode("utf-8", "ignore")
        text += "\n\n(body truncated by Crucible at the GitHub size limit)\n"
    return text


def _cell(text: str) -> str:
    """One table cell: sanitized, single line, pipes escaped."""
    return sanitize(" ".join(str(text).split())).replace("|", "\\|")


def body_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
