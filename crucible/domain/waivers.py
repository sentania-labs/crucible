"""Operator waivers for the two post-PR waits (hades FDY-0139, ADR 0025).

A task waits in `awaiting_external_review` for a reviewer and in
`awaiting_ci_certification` for CI. Either can simply never arrive: a reviewer that does
not review, a repository with no CI. The operator's way out is a Decision on the task
with one of these kinds. It is recorded, audited like every other decision, applies to
this task only, and is never inferred: nothing but a recorded decision waives anything.
"""

from __future__ import annotations

from collections.abc import Iterable

from crucible.domain.entities import Decision
from crucible.domain.lifecycle import TaskState
from crucible.domain.secrets import redact

# The remaining external review rounds for this task are waived.
WAIVE_EXTERNAL_REVIEW = "waive_external_review"
# This repository has no CI for this task: an empty required-check set with nothing
# observed on the head is `skipped` rather than pending for ever.
ACCEPT_NO_CI = "accept_no_ci"
WAIVER_KINDS: frozenset[str] = frozenset({WAIVE_EXTERNAL_REVIEW, ACCEPT_NO_CI})
# Where a waiver may be recorded: a pull request is open and Crucible is observing it.
# Not `head_diverged`, where nothing about the observed head is trusted and the next
# step is a head decision, not a wait.
WAIVABLE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.CI_CERTIFICATION_FAILED,
        TaskState.READY_FOR_MERGE,
    }
)


def latest_waivers(decisions: Iterable[Decision]) -> dict[str, Decision]:
    """The newest waiver decision of each kind, by kind."""
    out: dict[str, Decision] = {}
    for decision in decisions:
        if decision.kind not in WAIVER_KINDS:
            continue
        current = out.get(decision.kind)
        if current is None or decision.created_at >= current.created_at:
            out[decision.kind] = decision
    return out


def waiver_words(decision: Decision) -> str:
    """How a gate detail or a certification names the decision that waived it."""
    # The words are copied into gate and certification details, so they are redacted
    # like any other text a person typed.
    reason = redact(" ".join(decision.verbatim.split()))
    if len(reason) > 200:
        reason = reason[:197] + "..."
    return f"decision {decision.id}: {reason}"
