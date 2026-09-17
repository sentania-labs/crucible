"""PR observation: what Crucible does with one poll (23, 09).

Polling is the complete observation path; webhooks only shorten latency, so a delivery is
turned into the same normalized records and then the same functions here run. That is why
this module takes an `Observation` snapshot rather than a client: the poll-only path and
the webhook-accelerated path are the same code, and an integration test asserts they end
in the same rows.

Everything here runs inside one fenced transaction. Nothing calls GitHub; the supervisor
does the I/O and hands the snapshot over.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from crucible.application.publish import open_review_cycle
from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.wake import WakeReason
from crucible.domain.certification import (
    CertificationState,
    CheckSource,
    ObservedCheck,
    certify,
    wait_timeout_hours,
)
from crucible.domain.entities import (
    CICertification,
    DispositionKind,
    ExternalReview,
    ExternalReviewCycle,
    GateResultRecord,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Reaction,
    ReviewComment,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.external_review import (
    CLEAN_REACTION,
    Cycle,
    CycleState,
    Signal,
    SignalKind,
    apply_signal,
    completed_rounds,
    final_sha_satisfied,
    head_at,
    is_accepted,
    required_rounds,
    reviewer_logins,
)
from crucible.domain.gates import (
    POST_PR_GATES,
    PUBLICATION_GATES,
    DeliveryInput,
    GateResult,
    configured,
    evaluate_delivery,
)
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.github import Observation
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.observation")

PHASE_PUBLICATION = "publication"
PHASE_POST_PR = "post_pr"

# The task states in which a pull request is observed (23).
OBSERVED_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.CI_CERTIFICATION_FAILED,
        TaskState.READY_FOR_MERGE,
        TaskState.HEAD_DIVERGED,
    }
)
# The states a head change moves to `head_diverged` from (09).
DIVERGENCE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_EXTERNAL_REVIEW,
        TaskState.EXTERNAL_FEEDBACK_RECEIVED,
        TaskState.AWAITING_CI_CERTIFICATION,
        TaskState.READY_FOR_MERGE,
    }
)
DEFAULT_EXTERNAL_TIMEOUT_HOURS = 24
DEFAULT_CI_TIMEOUT_HOURS = 6


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ObservationResult:
    changed: bool = False
    diverged: bool = False
    accepted_signals: int = 0
    new_comments: int = 0
    completed_cycles: int = 0
    certification: str = ""
    state: str = ""
    notes: list[str] = field(default_factory=list)


def policy_for(uow: UnitOfWork, task: Task) -> dict[str, Any]:
    stored = uow.policies.get(task.policy_name, task.policy_version)
    return stored.document if stored else {}


def accepted_head(uow: UnitOfWork, task: Task) -> str:
    """The head every state after `publishing` is bound to (09)."""
    return task.head_sha or ""


# ----- heads and divergence ---------------------------------------------


def observe_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observed_sha: str,
    result: ObservationResult,
) -> None:
    """A head Crucible did not push moves the task to `head_diverged` (09, 23)."""
    if not observed_sha or observed_sha == pull_request.head_sha:
        return
    known = {
        head.sha: head for head in uow.pull_request_heads.list_for_pull_request(pull_request.id)
    }
    now = clock.now()
    previous = pull_request.head_sha
    pull_request.head_sha = observed_sha
    uow.pull_requests.save(pull_request)
    ours = observed_sha in known and known[observed_sha].pushed_by is PushedBy.CRUCIBLE
    if not ours:
        uow.pull_request_heads.add(
            PullRequestHead(
                id=new_id(),
                pull_request_id=pull_request.id,
                sha=observed_sha,
                pushed_by=PushedBy.OTHER,
                observed_at=now,
            )
        )
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_HEAD_OBSERVED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "from": previous,
            "to": observed_sha,
            "pushed_by": PushedBy.CRUCIBLE.value if ours else PushedBy.OTHER.value,
        },
    )
    result.changed = True
    if ours or task.state not in DIVERGENCE_STATES:
        return
    supersede_for_head(uow, clock, task=task, reason="head_diverged", new_head=observed_sha)
    move_task(
        uow,
        clock,
        task,
        TaskState.HEAD_DIVERGED,
        EventKind.TASK_HEAD_DIVERGED,
        payload={
            "pull_request": pull_request.number,
            "accepted_head": previous,
            "observed_head": observed_sha,
            "note": (
                "nothing about the new SHA is trusted; CI on it is recorded and cannot "
                "move the task (23)"
            ),
        },
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.HEAD_DIVERGED,
        summary=(
            f"pull request #{pull_request.number} moved from {previous} to {observed_sha} "
            "out of band; the previous head's acceptance, review, and gates are superseded"
        ),
        task=task,
        extra_links={"head_decision": f"/v1/tasks/{task.id}/head-decision"},
    )
    result.diverged = True
    result.changed = True


def supersede_for_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    reason: str,
    new_head: str,
    principal: str = PRINCIPAL_CRUCIBLE,
) -> None:
    """09: the previous head's acceptance, review report, and gate results are marked
    superseded and kept as history. Nothing is deleted; a superseded row is the record
    that this head was once believed."""
    now = clock.now()
    uow.acceptance.supersede_for_task(task.id, now)
    superseded_reports = 0
    for report in uow.review_reports.list_for_task(task.id):
        if report.superseded_at is None and report.head_sha == (task.head_sha or ""):
            uow.review_reports.supersede(report.id, now)
            superseded_reports += 1
    record_event(
        uow,
        clock,
        EventKind.SUPERSEDED_FOR_HEAD,
        principal=principal,
        task_id=task.id,
        payload={
            "reason": reason,
            "superseded_head": task.head_sha,
            "new_head": new_head,
            "review_reports": superseded_reports,
        },
    )


# ----- reactions ---------------------------------------------------------


def record_reactions(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> list[Signal]:
    """Store what a reaction poll saw, and note what disappeared.

    Polling cannot tell a deleted reaction from one that never existed, and it cannot see
    one created and deleted between two cycles (S12). A reaction that was recorded and is
    now absent is marked removed, with no actor and no delete time, because neither is
    knowable."""
    now = clock.now()
    if not observation.reactions_observable:
        if pull_request.reactions_observable:
            pull_request.reactions_observable = False
            uow.pull_requests.save(pull_request)
            record_event(
                uow,
                clock,
                EventKind.REACTIONS_UNOBSERVABLE,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "pull_request": pull_request.number,
                    "detail": observation.reactions_detail,
                    "note": (
                        "the App lacks Issues read, which is the one permission the "
                        "PR-level reactions endpoint needs; a clean external review is "
                        "signalled only there (23, S12). Recorded, not fatal."
                    ),
                },
            )
            result.notes.append("reactions unobservable")
            result.changed = True
        return []
    if not pull_request.reactions_observable:
        pull_request.reactions_observable = True
        uow.pull_requests.save(pull_request)
        result.changed = True
    stored = {
        (r.subject_kind, r.subject_github_id, r.github_id): r
        for r in uow.reactions.list_for_pull_request(pull_request.id)
    }
    seen: set[tuple[str, str, str]] = set()
    signals: list[Signal] = []
    for observed in observation.reactions:
        key = (observed.subject_kind, observed.subject_github_id, observed.github_id)
        seen.add(key)
        existing = stored.get(key)
        if existing is not None:
            if existing.removed_at is not None:
                existing.removed_at = None
                uow.reactions.save(existing)
                result.changed = True
            continue
        if not uow.reactions.add(
            Reaction(
                id=new_id(),
                pull_request_id=pull_request.id,
                subject_kind=observed.subject_kind,
                subject_github_id=observed.subject_github_id,
                github_id=observed.github_id,
                login=observed.login,
                content=observed.content,
                created_at=observed.created_at,
                observed_at=now,
            )
        ):
            # Already recorded by an earlier tick this one did not see. Nothing new
            # happened, so nothing is recorded and nothing becomes a signal.
            continue
        record_event(
            uow,
            clock,
            EventKind.REACTION_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "subject_kind": observed.subject_kind,
                "subject_github_id": observed.subject_github_id,
                "login": observed.login,
                "content": observed.content,
                "created_at": observed.created_at.isoformat(),
            },
        )
        result.changed = True
        if observed.subject_kind == "pull_request":
            signals.append(
                Signal(
                    kind=SignalKind.REACTION,
                    login=observed.login,
                    github_id=observed.github_id,
                    created_at=observed.created_at,
                    content=observed.content,
                )
            )
    for key, existing in stored.items():
        if key in seen or existing.removed_at is not None:
            continue
        existing.removed_at = now
        uow.reactions.save(existing)
        record_event(
            uow,
            clock,
            EventKind.REACTION_REMOVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "subject_kind": existing.subject_kind,
                "content": existing.content,
                "login": existing.login,
                "note": (
                    "observed absent; polling cannot see the deletion itself, only that "
                    "the set shrank (S12)"
                ),
            },
        )
        result.changed = True
    return signals


# ----- reviews and comments ----------------------------------------------


def record_reviews(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> list[Signal]:
    signals: list[Signal] = []
    for review in observation.reviews:
        if uow.external_reviews.get_by_github(pull_request.id, "review", review.github_id):
            continue
        signals.append(
            Signal(
                kind=SignalKind.REVIEW,
                login=review.login,
                github_id=review.github_id,
                created_at=review.submitted_at,
                body=review.body,
                reviewed_sha=review.commit_id,
                has_findings=review.state.upper() in ("CHANGES_REQUESTED", "COMMENTED"),
            )
        )
    return signals


def record_comments(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    allowlist: frozenset[str],
    result: ObservationResult,
) -> list[Signal]:
    """Store review comments and issue comments, updating one edited in place.

    The reviewer's summary comment is edited rather than replaced (S12), so a changed
    body or `updated_at` is a change; the row keeps its identity and the edit never counts
    as a round."""
    signals: list[Signal] = []
    now = clock.now()
    for comment in (*observation.review_comments, *observation.issue_comments):
        existing = uow.review_comments.get_by_github(
            pull_request.id, comment.kind, comment.github_id
        )
        digest = _sha(comment.body)
        if existing is not None:
            if existing.body_sha256 != digest or existing.updated_at < comment.updated_at:
                existing.body = comment.body
                existing.body_sha256 = digest
                existing.updated_at = comment.updated_at
                uow.review_comments.save(existing)
                record_event(
                    uow,
                    clock,
                    EventKind.REVIEW_COMMENT_RECEIVED
                    if comment.kind == "review_comment"
                    else EventKind.ISSUE_COMMENT_RECEIVED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    payload={
                        "pull_request": pull_request.number,
                        "comment_id": existing.id,
                        "github_id": comment.github_id,
                        "login": comment.login,
                        "edited": True,
                        "body_sha256": digest,
                        "note": "edited in place; an edit never counts as a round (23)",
                    },
                )
                result.changed = True
            continue
        row = ReviewComment(
            id=new_id(),
            pull_request_id=pull_request.id,
            external_review_id=None,
            github_id=comment.github_id,
            kind=comment.kind,
            login=comment.login,
            path=comment.path,
            line=comment.line,
            body=comment.body,
            body_sha256=digest,
            reviewed_sha=comment.commit_id,
            created_at=comment.created_at,
            updated_at=comment.updated_at or now,
        )
        if not uow.review_comments.add(row):
            continue
        record_event(
            uow,
            clock,
            EventKind.REVIEW_COMMENT_RECEIVED
            if comment.kind == "review_comment"
            else EventKind.ISSUE_COMMENT_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "comment_id": row.id,
                "github_id": comment.github_id,
                "login": comment.login,
                "path": comment.path,
                "line": comment.line,
                "body_sha256": digest,
                "allowlisted": comment.login in allowlist,
            },
        )
        result.changed = True
        if comment.kind == "review_comment" and comment.login in allowlist:
            # Only what the dispositions gate counts: a comment from another login, or a
            # standalone issue comment, is recorded and asks nothing of Foundry.
            result.new_comments += 1
        # An inline review comment belongs to its review object, which carries the
        # round; only a standalone comment is a signal of its own.
        if comment.kind == "issue_comment":
            signals.append(
                Signal(
                    kind=SignalKind.COMMENT,
                    login=comment.login,
                    github_id=comment.github_id,
                    created_at=comment.created_at,
                    body=comment.body,
                    has_findings=True,
                )
            )
    return signals


def to_cycle(row: ExternalReviewCycle) -> Cycle:
    return Cycle(
        id=row.id,
        head_sha=row.head_sha,
        components=tuple(row.components),
        opened_at=row.opened_at,
        state=CycleState(row.state),
        completed_components=dict(row.completed_components),
        completed_at=row.completed_at,
    )


def attach_signals(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    signals: list[Signal],
    policy: dict[str, Any],
    result: ObservationResult,
) -> None:
    """Record each signal and attach the accepted ones to the open cycle (23).

    A signal from a login that is not allowlisted is recorded and satisfies nothing; that
    is the whole of ADR 0008's bound on who may move this task."""
    if not signals:
        return
    now = clock.now()
    heads = [
        (head.sha, head.observed_at)
        for head in uow.pull_request_heads.list_for_pull_request(pull_request.id)
    ]
    rows = list(uow.review_cycles.list_for_pull_request(pull_request.id))
    open_rows = [row for row in rows if row.state == CycleState.OPEN.value]
    for signal in sorted(signals, key=lambda s: s.created_at):
        accepted = is_accepted(signal, policy)
        reviewed_sha = signal.reviewed_sha
        inferred = False
        if reviewed_sha is None:
            reviewed_sha, inferred = head_at(
                heads, signal.created_at, fallback=pull_request.head_sha
            )
        review = ExternalReview(
            id=new_id(),
            pull_request_id=pull_request.id,
            cycle_id=None,
            reviewer_login=signal.login,
            signal=signal.kind.value,
            github_id=signal.github_id,
            reviewed_sha=reviewed_sha,
            body=signal.body,
            body_sha256=_sha(signal.body),
            received_at=now,
            state=signal.content or "",
            accepted=accepted,
            sha_inferred=inferred,
        )
        if not accepted:
            if not uow.external_reviews.add(review):
                continue
            record_event(
                uow,
                clock,
                EventKind.EXTERNAL_REVIEW_IGNORED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "pull_request": pull_request.number,
                    "login": signal.login,
                    "signal": signal.kind.value,
                    "github_id": signal.github_id,
                    "reason": (
                        "the login is not in external_review.reviewer_logins, or the "
                        "signal shape is not accepted; recorded, satisfies nothing (23)"
                    ),
                },
            )
            result.changed = True
            continue
        target = _cycle_for(open_rows, reviewed_sha)
        if target is not None:
            review.cycle_id = target.id
        if not uow.external_reviews.add(review):
            continue
        record_event(
            uow,
            clock,
            EventKind.EXTERNAL_REVIEW_RECEIVED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "login": signal.login,
                "signal": signal.kind.value,
                "github_id": signal.github_id,
                "reviewed_sha": reviewed_sha,
                "sha_inferred": inferred,
                "cycle_id": review.cycle_id,
                "content": signal.content,
            },
        )
        result.accepted_signals += 1
        result.changed = True
        if target is None:
            continue
        domain_cycle = to_cycle(target)
        domain_cycle, completed, just_done = apply_signal(domain_cycle, signal, at=now)
        target.completed_components = dict(domain_cycle.completed_components)
        target.state = domain_cycle.state.value
        target.completed_at = domain_cycle.completed_at
        uow.review_cycles.save(target)
        if just_done:
            result.completed_cycles += 1
            record_event(
                uow,
                clock,
                EventKind.EXTERNAL_REVIEW_CYCLE_COMPLETED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                payload={
                    "cycle_id": target.id,
                    "head_sha": target.head_sha,
                    "components": list(target.components),
                    "completed_by": list(completed),
                    "clean": signal.is_clean_reaction,
                },
            )
            open_rows = [row for row in open_rows if row.id != target.id]


def _cycle_for(rows: list[ExternalReviewCycle], head_sha: str) -> ExternalReviewCycle | None:
    """The open cycle a signal attaches to: the one on its head, else the oldest open."""
    for row in rows:
        if row.head_sha == head_sha:
            return row
    return rows[0] if rows else None


# ----- CI certification ---------------------------------------------------


def _source(name: str) -> CheckSource:
    try:
        return CheckSource(name)
    except ValueError:
        return CheckSource.CHECK_RUN


def observed_checks(observation: Observation) -> tuple[ObservedCheck, ...]:
    return tuple(
        ObservedCheck(
            name=check.name,
            status=check.status,
            conclusion=check.conclusion,
            head_sha=check.head_sha,
            source=_source(check.source),
            url=check.url,
            external_id=check.external_id,
            workflow=check.workflow,
            job=check.job,
        )
        for check in observation.checks
    )


def certify_head(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    policy: dict[str, Any],
    head_sha: str,
    log_excerpt: str = "",
) -> CICertification:
    """Compute and record the certification for the accepted head (23, ADR 0009)."""
    checks = observed_checks(observation)
    outcome = certify(
        policy,
        head_sha=head_sha,
        branch_protection=observation.required_checks,
        observed=checks,
    )
    failure: dict[str, Any] = {}
    if outcome.state is CertificationState.FAILED:
        first = outcome.failures[0]
        failure = {
            "check": first.name,
            "workflow": first.workflow,
            "job": first.job,
            "conclusion": first.conclusion,
            "head_sha": first.head_sha,
            "url": first.url,
            "run_id": first.external_id,
            "all": [
                {"check": c.name, "conclusion": c.conclusion, "url": c.url}
                for c in outcome.failures
            ],
        }
        if log_excerpt:
            failure["log_excerpt"] = log_excerpt
    certification = CICertification(
        id=new_id(),
        pull_request_id=pull_request.id,
        task_id=task.id,
        head_sha=head_sha,
        state=outcome.state.value,
        required_checks=list(outcome.required),
        check_runs=[
            {
                "name": c.name,
                "status": c.status,
                "conclusion": c.conclusion,
                "source": c.source.value,
                "url": c.url,
                "workflow": c.workflow,
            }
            for c in outcome.observed
        ],
        failure=failure,
        detail=outcome.detail,
        evaluated_at=clock.now(),
    )
    previous = uow.ci_certifications.get_for_head(pull_request.id, head_sha)
    stored = uow.ci_certifications.put(certification)
    if previous is None or previous.state != stored.state or previous.detail != stored.detail:
        record_event(
            uow,
            clock,
            EventKind.CI_CERTIFICATION_RECORDED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "certification_id": stored.id,
                "head_sha": head_sha,
                "state": stored.state,
                "detail": stored.detail,
                "required_checks": stored.required_checks,
                "required_from": outcome.source,
                "failure": {k: v for k, v in failure.items() if k != "log_excerpt"},
            },
        )
    return stored


# ----- merge, close, and advancement -------------------------------------


def observe_state(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    result: ObservationResult,
) -> None:
    """Merged, or closed unmerged. Crucible has no merge endpoint; it only observes (23)."""
    ref = observation.pull_request
    now = clock.now()
    if ref.merged and pull_request.state is not PullRequestState.MERGED:
        pull_request.state = PullRequestState.MERGED
        pull_request.merged_at = ref.merged_at or now
        pull_request.merge_sha = ref.merge_commit_sha
        pull_request.merged_by = ref.merged_by
        uow.pull_requests.save(pull_request)
        record_event(
            uow,
            clock,
            EventKind.PULL_REQUEST_STATE_CHANGED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "state": "merged",
                "merge_sha": pull_request.merge_sha,
                "merged_by": pull_request.merged_by,
            },
        )
        result.changed = True
        if task.state is TaskState.READY_FOR_MERGE:
            move_task(
                uow,
                clock,
                task,
                TaskState.MERGED,
                EventKind.TASK_MERGED,
                payload={
                    "pull_request": pull_request.number,
                    "merge_sha": pull_request.merge_sha,
                    "merged_by": pull_request.merged_by,
                    "merged_at": (pull_request.merged_at or now).isoformat(),
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.MERGED,
                summary=(
                    f"pull request #{pull_request.number} was merged by "
                    f"{pull_request.merged_by or 'someone'} as {pull_request.merge_sha}"
                ),
                task=task,
                extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
            )
        return
    if ref.state == "closed" and not ref.merged and pull_request.state is PullRequestState.OPEN:
        pull_request.state = PullRequestState.CLOSED
        pull_request.closed_at = ref.closed_at or now
        pull_request.closed_by = ref.closed_by
        uow.pull_requests.save(pull_request)
        record_event(
            uow,
            clock,
            EventKind.PULL_REQUEST_STATE_CHANGED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            payload={
                "pull_request": pull_request.number,
                "state": "closed",
                "closed_by": pull_request.closed_by,
            },
        )
        result.changed = True
        if task.state in OBSERVED_STATES and task.state is not TaskState.HEAD_DIVERGED:
            move_task(
                uow,
                clock,
                task,
                TaskState.REJECTED,
                EventKind.TASK_REJECTED,
                payload={
                    "pull_request": pull_request.number,
                    "reason": "the pull request was closed without being merged (23)",
                    "closed_by": pull_request.closed_by,
                },
            )


def evaluate_delivery_gates(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    attempt_id: str,
    pull_request: PullRequest | None,
    policy: dict[str, Any],
    certification: CICertification | None,
    branch_pushed_sha: str | None,
    phases: tuple[str, ...] = (PHASE_PUBLICATION, PHASE_POST_PR),
) -> dict[str, Any]:
    """Evaluate the publication and post-PR gates for the accepted head (09, 11, 23).

    Post-PR gates re-evaluate each reconcile tick until they resolve or the task is
    terminal, so this is written to be idempotent: the same input writes the same rows."""
    head = accepted_head(uow, task)
    comments = (
        list(uow.review_comments.list_for_pull_request(pull_request.id)) if pull_request else []
    )
    allowlist = reviewer_logins(policy)
    needing = [c for c in comments if c.login in allowlist and c.kind == "review_comment"]
    recorded = list(uow.dispositions.list_for_comments([c.id for c in needing]))
    dispositioned = {d.review_comment_id for d in recorded}
    # 09: advancement needs every comment dispositioned *and none of them fix*. A `fix`
    # is Foundry saying the work is not done; what follows it is a correction contract,
    # which clears it by replacing the head the comments belong to.
    fix_dispositions = tuple(
        d.review_comment_id for d in recorded if d.disposition is DispositionKind.FIX
    )
    cycles = (
        [to_cycle(row) for row in uow.review_cycles.list_for_pull_request(pull_request.id)]
        if pull_request
        else []
    )
    signals = (
        [
            Signal(
                kind=SignalKind(row.signal),
                login=row.reviewer_login,
                github_id=row.github_id,
                created_at=row.received_at,
                body=row.body,
                reviewed_sha=row.reviewed_sha,
                content=row.state,
            )
            for row in uow.external_reviews.list_for_pull_request(pull_request.id)
            if row.accepted
        ]
        if pull_request
        else []
    )
    final_sha = None
    section = policy.get("external_review", {})
    if isinstance(section, dict) and section.get("require_review_on_final_sha"):
        final_sha = final_sha_satisfied(cycles, signals, head)
    di = DeliveryInput(
        policy=policy,
        accepted_head=head,
        branch_pushed_sha=branch_pushed_sha,
        pr_number=pull_request.number if pull_request else None,
        pr_head_sha=pull_request.head_sha if pull_request else None,
        pr_state=pull_request.state.value if pull_request else "",
        completed_rounds=completed_rounds(cycles),
        required_rounds=required_rounds(policy),
        undispositioned=tuple(c.id for c in needing if c.id not in dispositioned),
        fix_dispositions=fix_dispositions,
        comment_count=len(needing),
        certification_state=certification.state if certification else "",
        certification_detail=certification.detail if certification else "",
        final_sha=final_sha,
    )
    names: list[str] = []
    if PHASE_PUBLICATION in phases:
        names.extend(configured(policy, "publication", PUBLICATION_GATES))
    if PHASE_POST_PR in phases:
        names.extend(configured(policy, "post_pr", POST_PR_GATES))
    outcomes = evaluate_delivery(names, di)
    stored = {
        row.gate: (row.result, row.detail)
        for row in uow.gate_results.list_for_attempt(attempt_id)
        if row.head_sha == head
    }
    changed = False
    now = clock.now()
    for gate, outcome in outcomes.items():
        if stored.get(gate) == (outcome.result.value, outcome.detail):
            continue
        changed = True
        uow.gate_results.put(
            GateResultRecord(
                id=new_id(),
                task_id=task.id,
                attempt_id=attempt_id,
                head_sha=head,
                gate=gate,
                phase=PHASE_PUBLICATION if gate in PUBLICATION_GATES else PHASE_POST_PR,
                result=outcome.result.value,
                detail=outcome.detail,
                evidence_ids=list(outcome.evidence_ids),
                evaluated_at=now,
            )
        )
    summary = {
        "results": {gate: outcome.result.value for gate, outcome in outcomes.items()},
        "details": {gate: outcome.detail for gate, outcome in outcomes.items()},
        "completed_rounds": di.completed_rounds,
        "required_rounds": di.required_rounds,
        "undispositioned": list(di.undispositioned),
        "fix_dispositions": list(di.fix_dispositions),
    }
    if changed:
        record_event(
            uow,
            clock,
            EventKind.GATES_EVALUATED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            attempt_id=attempt_id,
            payload={"head_sha": head, "phase": "delivery", **summary},
        )
    return summary


def advance_delivery(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
    gates: dict[str, Any],
    certification: CICertification | None,
    result: ObservationResult,
) -> None:
    """Move the task on what the gates now say (09)."""
    results = gates.get("results", {})
    dispositions_ok = results.get("feedback_dispositions_complete") in (
        GateResult.PASS.value,
        GateResult.SKIPPED.value,
    )
    if task.state is TaskState.AWAITING_EXTERNAL_REVIEW and result.accepted_signals:
        # 09: a signal moves the task to `external_feedback_received` whatever the round
        # count says. Whether it then advances, and to where, is the dispositions gate's
        # answer and `advance_from_feedback`'s branch, never this condition.
        move_task(
            uow,
            clock,
            task,
            TaskState.EXTERNAL_FEEDBACK_RECEIVED,
            EventKind.TASK_EXTERNAL_FEEDBACK_RECEIVED,
            payload={
                "pull_request": pull_request.number,
                "accepted_signals": result.accepted_signals,
                "new_comments": result.new_comments,
                "completed_rounds": gates.get("completed_rounds"),
            },
        )
        summary = (
            f"{result.accepted_signals} external review signal(s) on "
            f"#{pull_request.number} at {pull_request.head_sha}: "
            f"{result.new_comments} comment(s), 0 dispositions recorded"
        )
        if result.new_comments == 0 and dispositions_ok:
            # 23: a round with no findings has nothing to disposition. Crucible records
            # it and advances without a wake for judgment; with rounds still outstanding
            # `advance_from_feedback` sends it back to wait for the next one.
            advance_from_feedback(
                uow, clock, task=task, pull_request=pull_request, gates=gates, policy=policy
            )
            return
        create_wake(
            uow,
            clock,
            principal_id=task.principal_id,
            reason=WakeReason.EXTERNAL_FEEDBACK_RECEIVED,
            summary=summary,
            task=task,
            extra_links={
                "pull_request": f"/v1/tasks/{task.id}/pull-request",
                "dispositions": f"/v1/tasks/{task.id}/dispositions",
            },
        )
        return
    if task.state is TaskState.EXTERNAL_FEEDBACK_RECEIVED and dispositions_ok:
        advance_from_feedback(
            uow, clock, task=task, pull_request=pull_request, gates=gates, policy=policy
        )
        return
    if task.state is TaskState.AWAITING_CI_CERTIFICATION and certification is not None:
        if certification.state == CertificationState.FAILED.value:
            move_task(
                uow,
                clock,
                task,
                TaskState.CI_CERTIFICATION_FAILED,
                EventKind.TASK_CI_CERTIFICATION_FAILED,
                payload={
                    "pull_request": pull_request.number,
                    "certification_id": certification.id,
                    "head_sha": certification.head_sha,
                    "failure": {
                        k: v for k, v in certification.failure.items() if k != "log_excerpt"
                    },
                    "note": "no automatic retry, no automatic worker correction (ADR 0009)",
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.CI_CERTIFICATION_FAILED,
                summary=(f"required CI failed on {certification.head_sha}: {certification.detail}"),
                task=task,
                extra_links={"ci_decision": f"/v1/tasks/{task.id}/ci-decision"},
            )
            return
        if certification.state in (
            CertificationState.GREEN.value,
            CertificationState.SKIPPED.value,
        ):
            move_task(
                uow,
                clock,
                task,
                TaskState.READY_FOR_MERGE,
                EventKind.TASK_READY_FOR_MERGE,
                payload={
                    "pull_request": pull_request.number,
                    "head_sha": certification.head_sha,
                    "certification": certification.state,
                    "detail": certification.detail,
                },
            )
            create_wake(
                uow,
                clock,
                principal_id=task.principal_id,
                reason=WakeReason.READY_FOR_MERGE,
                summary=ready_summary(uow, task, pull_request, certification, gates),
                task=task,
                extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
            )


def advance_from_feedback(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    gates: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    """09: every comment dispositioned, none is `fix`, and the rounds decide where next."""
    completed = int(gates.get("completed_rounds", 0))
    needed = int(gates.get("required_rounds", 0))
    if completed >= needed:
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            payload={
                "pull_request": pull_request.number,
                "completed_rounds": completed,
                "required_rounds": needed,
            },
        )
        return
    move_task(
        uow,
        clock,
        task,
        TaskState.AWAITING_EXTERNAL_REVIEW,
        EventKind.TASK_AWAITING_EXTERNAL_REVIEW,
        payload={
            "pull_request": pull_request.number,
            "completed_rounds": completed,
            "required_rounds": needed,
            "note": "rounds outstanding; the task waits for another cycle (09)",
        },
    )
    maybe_request_trigger(uow, clock, task=task, pull_request=pull_request, policy=policy)


def maybe_request_trigger(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
) -> None:
    """23: with `retrigger_after_correction`, Crucible wakes the orchestrator to post the
    trigger under the operator's account. Crucible never posts it: an App-authored
    trigger comment is refused by the provider, and it is not Crucible's act (S12)."""
    section = policy.get("external_review", {})
    if not isinstance(section, dict) or not section.get("retrigger_after_correction"):
        return
    cycles = uow.review_cycles.list_for_pull_request(pull_request.id)
    if any(
        cycle.head_sha == pull_request.head_sha and cycle.state == CycleState.OPEN.value
        for cycle in cycles
    ):
        return
    open_review_cycle(
        uow,
        clock,
        task_id=task.id,
        pull_request=pull_request,
        head_sha=pull_request.head_sha,
        policy=policy,
        trigger="retrigger",
    )
    record_event(
        uow,
        clock,
        EventKind.EXTERNAL_REVIEW_TRIGGER_NEEDED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "head_sha": pull_request.head_sha,
            "note": (
                "the trigger comment is posted by the orchestrator under the operator's "
                "account; Crucible does not post under its App identity (23, S12)"
            ),
        },
    )
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.EXTERNAL_REVIEW_TRIGGER_NEEDED,
        summary=(
            f"the policy asks for a new review round on #{pull_request.number} at "
            f"{pull_request.head_sha}; post the provider's trigger under the operator's "
            "account"
        ),
        task=task,
        extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
    )


def ready_summary(
    uow: UnitOfWork,
    task: Task,
    pull_request: PullRequest,
    certification: CICertification,
    gates: dict[str, Any],
) -> str:
    """23's ready-for-merge report: Crucible produces the facts, Foundry the sentence."""
    comments = list(uow.review_comments.list_for_pull_request(pull_request.id))
    dispositions = uow.dispositions.list_for_comments([c.id for c in comments])
    checks = ", ".join(str(name) for name in certification.required_checks) or "none required"
    return (
        f"{pull_request.url} is ready for merge at {pull_request.head_sha}. "
        f"External review: {gates.get('completed_rounds', 0)} of "
        f"{gates.get('required_rounds', 0)} round(s), {len(comments)} comment(s), "
        f"{len(dispositions)} disposition(s). CI: {certification.state} "
        f"({checks}). Merging is the operator's act; Crucible has no merge endpoint."
    )[:1000]


# ----- timeouts -----------------------------------------------------------


def repeat_overdue_wakes(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    policy: dict[str, Any],
) -> bool:
    """Nothing received is overdue silently (23). A repeat wake, no state change.

    The clock starts when the task entered the state it is waiting in, not when the pull
    request was opened: a correction on a three-day-old pull request enters certification
    with nothing outstanding yet, and measuring from `opened_at` would call it overdue on
    its first poll."""
    now = clock.now()
    if task.state is TaskState.AWAITING_EXTERNAL_REVIEW:
        hours = wait_timeout_hours(policy, "external_review", DEFAULT_EXTERNAL_TIMEOUT_HOURS)
        reason = WakeReason.EXTERNAL_REVIEW_OVERDUE
        entered = EventKind.TASK_AWAITING_EXTERNAL_REVIEW
        what = "an external review signal"
    elif task.state is TaskState.AWAITING_CI_CERTIFICATION:
        hours = wait_timeout_hours(policy, "ci_certification", DEFAULT_CI_TIMEOUT_HOURS)
        reason = WakeReason.CI_CERTIFICATION_OVERDUE
        entered = EventKind.TASK_AWAITING_CI_CERTIFICATION
        what = "a required check conclusion"
    else:
        return False
    since = waiting_since(uow, task, entered, fallback=pull_request.opened_at)
    if now - since < timedelta(hours=hours):
        return False
    latest = _latest_wake_at(uow, task, reason.value)
    if latest is not None and now - latest < timedelta(hours=hours):
        return False
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=reason,
        summary=(
            f"#{pull_request.number} has waited more than {hours}h for {what} on "
            f"{pull_request.head_sha}"
        ),
        task=task,
        extra_links={"pull_request": f"/v1/tasks/{task.id}/pull-request"},
    )
    return True


def waiting_since(uow: UnitOfWork, task: Task, kind: EventKind, *, fallback: datetime) -> datetime:
    """When the task entered the state it is waiting in. Each transition writes its event
    in the same transaction as the state change (09), so the event is the record."""
    event = uow.events.latest_for_task_kind(task.id, kind.value)
    return event.ts if event is not None else fallback


def _latest_wake_at(uow: UnitOfWork, task: Task, reason: str) -> datetime | None:
    latest: datetime | None = None
    for wake in uow.wakes.list_for_principal(
        task.principal_id, since=None, include_acked=True, limit=200
    ):
        if (
            wake.task_id == task.id
            and wake.reason == reason
            and (latest is None or wake.created_at > latest)
        ):
            latest = wake.created_at
    return latest


def poll_due(
    pull_request: PullRequest,
    *,
    now: datetime,
    poll_interval_seconds: int,
    reactions_interval_seconds: int,
    task_state: TaskState,
) -> tuple[bool, bool]:
    """(poll now, include reactions).

    23: reactions are polled every `reactions_poll_interval_seconds` while a PR is
    `awaiting_external_review`, because the pickup reaction is transient and the clean
    verdict is a reaction; on other states they ride the ordinary poll."""
    last = pull_request.last_polled_at
    due = last is None or (now - last).total_seconds() >= poll_interval_seconds
    last_reactions = pull_request.last_reactions_polled_at
    reactions_due = (
        last_reactions is None
        or (now - last_reactions).total_seconds() >= reactions_interval_seconds
    )
    if task_state is TaskState.AWAITING_EXTERNAL_REVIEW and reactions_due:
        return True, True
    return due, due and reactions_due


def clean_reaction_present(observation: Observation, allowlist: frozenset[str]) -> bool:
    return any(
        reaction.subject_kind == "pull_request"
        and reaction.content == CLEAN_REACTION
        and reaction.login in allowlist
        for reaction in observation.reactions
    )


def apply_observation(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    pull_request: PullRequest,
    observation: Observation,
    policy: dict[str, Any],
    attempt_id: str,
    with_reactions: bool,
    log_excerpt: str = "",
) -> ObservationResult:
    """One poll, applied. The whole of what a tick does with a pull request."""
    result = ObservationResult()
    now = clock.now()
    pull_request.last_polled_at = now
    if with_reactions:
        pull_request.last_reactions_polled_at = now
    uow.pull_requests.save(pull_request)
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_POLLED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        payload={
            "pull_request": pull_request.number,
            "head_sha": observation.pull_request.head_sha,
            "state": observation.pull_request.state,
            "reviews": len(observation.reviews),
            "review_comments": len(observation.review_comments),
            "issue_comments": len(observation.issue_comments),
            "reactions": len(observation.reactions),
            "checks": len(observation.checks),
            "reactions_observable": observation.reactions_observable,
            "rate_limit_remaining": observation.rate_limit_remaining,
        },
    )
    allowlist = reviewer_logins(policy)
    signals: list[Signal] = []
    signals += record_reviews(
        uow, clock, task=task, pull_request=pull_request, observation=observation, result=result
    )
    signals += record_comments(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observation=observation,
        allowlist=allowlist,
        result=result,
    )
    if with_reactions:
        signals += record_reactions(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            observation=observation,
            result=result,
        )
    attach_signals(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        signals=signals,
        policy=policy,
        result=result,
    )
    observe_head(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observed_sha=observation.pull_request.head_sha,
        result=result,
    )
    certification: CICertification | None = None
    head = accepted_head(uow, task)
    if head:
        certification = certify_head(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            observation=observation,
            policy=policy,
            head_sha=head,
            log_excerpt=log_excerpt,
        )
        result.certification = certification.state
    if result.diverged:
        result.state = task.state.value
        return result
    observe_state(
        uow,
        clock,
        task=task,
        pull_request=pull_request,
        observation=observation,
        result=result,
    )
    if task.state in OBSERVED_STATES:
        gates = evaluate_delivery_gates(
            uow,
            clock,
            task=task,
            attempt_id=attempt_id,
            pull_request=pull_request,
            policy=policy,
            certification=certification,
            branch_pushed_sha=head,
        )
        advance_delivery(
            uow,
            clock,
            task=task,
            pull_request=pull_request,
            policy=policy,
            gates=gates,
            certification=certification,
            result=result,
        )
        repeat_overdue_wakes(uow, clock, task=task, pull_request=pull_request, policy=policy)
    result.state = task.state.value
    return result
