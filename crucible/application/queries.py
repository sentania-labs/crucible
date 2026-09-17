"""Read side: task, execution, attempt, event, and supervisor views."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any

from crucible.application.errors import NotFoundError
from crucible.contracts.api import (
    AcceptanceView,
    ArtifactList,
    ArtifactView,
    AttemptSummary,
    AttemptView,
    CICertificationView,
    CIDecisionView,
    CompletionClaimView,
    ContractVersionView,
    DecisionView,
    EscalationView,
    EventList,
    EventView,
    EvidenceList,
    EvidenceView,
    ExecutionSummary,
    ExecutionView,
    ExternalReviewView,
    GateList,
    GateResultView,
    PullRequestHeadView,
    PullRequestView,
    ReactionView,
    ReviewCommentView,
    ReviewCycleView,
    ReviewReportView,
    SupervisorView,
    TaskList,
    TaskListItem,
    TaskView,
    WakeList,
    WakeView,
)
from crucible.domain.entities import (
    AcceptanceResult,
    Artifact,
    Attempt,
    Decision,
    Escalation,
    EscalationState,
    Event,
    EvidenceRecord,
    Execution,
    GateResultRecord,
    PullRequestState,
    ReviewReportRecord,
    Wake,
)
from crucible.domain.external_review import required_rounds
from crucible.domain.lifecycle import TaskState
from crucible.ports.execution import ExecutionProvider
from crucible.ports.repository import UnitOfWork

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        raise NotFoundError("cursor is not valid") from None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _attempt_summary(a: Attempt) -> AttemptSummary:
    return AttemptSummary(
        id=a.id,
        execution_id=a.execution_id,
        number=a.number,
        state=a.state,
        exit_code=a.exit_code,
        exit_class=a.exit_class,
        started_at=a.started_at,
        ended_at=a.ended_at,
        handle=a.handle,
    )


def _execution_summary(e: Execution, attempts: list[Attempt]) -> ExecutionSummary:
    return ExecutionSummary(
        id=e.id,
        role=e.role.value,
        state=e.state,
        harness=e.harness,
        model=e.model,
        provider=e.provider,
        image=e.image,
        contract_version=e.contract_version,
        max_attempts=e.max_attempts,
        created_at=e.created_at,
        ended_at=e.ended_at,
        attempts=[_attempt_summary(a) for a in attempts],
    )


def task_view(uow: UnitOfWork, task_id: str) -> TaskView:
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    principal = uow.principals.get(task.principal_id)
    repository = uow.repositories.get(task.repository_id)
    versions = uow.contracts.list_for_task(task.id)
    current = next((v for v in versions if v.version == task.contract_version), None)
    executions = uow.executions.list_for_task(task.id)
    summaries = [
        _execution_summary(e, list(uow.attempts.list_for_execution(e.id))) for e in executions
    ]
    all_attempts = [a for s in summaries for a in s.attempts]
    latest = max(all_attempts, key=lambda a: a.id) if all_attempts else None
    return TaskView(
        id=task.id,
        external_id=task.external_id,
        title=task.title,
        project=task.project,
        state=task.state,
        principal=principal.name if principal else task.principal_id,
        repository=repository.name if repository else task.repository_id,
        policy={"name": task.policy_name, "version": task.policy_version},
        contract_version=task.contract_version,
        created_at=task.created_at,
        updated_at=task.updated_at,
        closed_at=task.closed_at,
        contract_versions=[
            ContractVersionView(version=v.version, sha256=v.sha256, submitted_at=v.submitted_at)
            for v in versions
        ],
        contract=current.document if current else {},
        executions=summaries,
        latest_attempt=latest,
        head_sha=task.head_sha,
        gate_summary=gate_summary(uow, task.id),
        pull_request=_pr_summary(uow, task.id),
        open_escalations=[
            _escalation_view(e).model_dump(mode="json")
            for e in uow.escalations.list_for_task(task.id)
            if e.state is not EscalationState.CLOSED
        ],
        review_reports=[
            _review_view(uow, r).model_dump(mode="json")
            for r in uow.review_reports.list_for_task(task.id)
        ],
        acceptance_results=[
            _acceptance_view(uow, a).model_dump(mode="json")
            for a in uow.acceptance.list_for_task(task.id)
        ],
        decisions=[
            _decision_view(uow, d).model_dump(mode="json")
            for d in uow.decisions.list_for_task(task.id)
        ],
        unacked_wakes=len(
            uow.wakes.list_for_principal(
                task.principal_id, since=None, include_acked=False, limit=MAX_LIMIT
            )
        ),
    )


def task_list(
    uow: UnitOfWork,
    *,
    state: TaskState | None,
    project: str | None,
    repository: str | None,
    external_id: str | None,
    updated_since: datetime | None,
    cursor: str | None,
    limit: int | None,
) -> TaskList:
    size = clamp_limit(limit)
    repository_id: str | None = None
    if repository is not None:
        repo = uow.repositories.get_by_name(repository)
        if repo is None:
            return TaskList(items=[], next_cursor=None)
        repository_id = repo.id
    rows = uow.tasks.search(
        state=state,
        project=project,
        repository_id=repository_id,
        external_id=external_id,
        updated_since=updated_since,
        after_id=decode_cursor(cursor),
        limit=size + 1,
    )
    page = list(rows[:size])
    repo_names: dict[str, str] = {}
    items = []
    for t in page:
        if t.repository_id not in repo_names:
            repo = uow.repositories.get(t.repository_id)
            repo_names[t.repository_id] = repo.name if repo else t.repository_id
        items.append(
            TaskListItem(
                id=t.id,
                external_id=t.external_id,
                title=t.title,
                project=t.project,
                state=t.state,
                repository=repo_names[t.repository_id],
                contract_version=t.contract_version,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
        )
    next_cursor = encode_cursor(page[-1].id) if len(rows) > size and page else None
    return TaskList(items=items, next_cursor=next_cursor)


def _event_view(e: Event) -> EventView:
    assert e.seq is not None
    return EventView(
        seq=e.seq,
        ts=e.ts,
        kind=e.kind,
        task_id=e.task_id,
        execution_id=e.execution_id,
        attempt_id=e.attempt_id,
        principal=e.principal,
        verified=e.verified,
        payload=e.payload,
    )


def _seq_cursor(cursor: str | None) -> int:
    raw = decode_cursor(cursor)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        raise NotFoundError("cursor is not valid") from None


def _page_events(events: list[Event], size: int) -> EventList:
    page = events[:size]
    next_cursor = None
    if len(events) > size and page and page[-1].seq is not None:
        next_cursor = encode_cursor(str(page[-1].seq))
    return EventList(items=[_event_view(e) for e in page], next_cursor=next_cursor)


def task_events(
    uow: UnitOfWork, task_id: str, *, cursor: str | None, limit: int | None
) -> EventList:
    if uow.tasks.get(task_id) is None:
        raise NotFoundError(f"task {task_id} not found")
    size = clamp_limit(limit)
    events = list(uow.events.list_for_task(task_id, after_seq=_seq_cursor(cursor), limit=size + 1))
    return _page_events(events, size)


def global_events(
    uow: UnitOfWork,
    *,
    cursor: str | None,
    kind: str | None,
    since: datetime | None,
    limit: int | None,
) -> EventList:
    size = clamp_limit(limit)
    events = list(
        uow.events.list_global(
            after_seq=_seq_cursor(cursor), kind=kind, since=since, limit=size + 1
        )
    )
    return _page_events(events, size)


def execution_view(uow: UnitOfWork, execution_id: str) -> ExecutionView:
    e = uow.executions.get(execution_id)
    if e is None:
        raise NotFoundError(f"execution {execution_id} not found")
    attempts = list(uow.attempts.list_for_execution(e.id))
    return ExecutionView(
        id=e.id,
        task_id=e.task_id,
        role=e.role.value,
        state=e.state,
        contract_version=e.contract_version,
        harness=e.harness,
        model=e.model,
        effort=e.effort,
        provider=e.provider,
        image=e.image,
        max_attempts=e.max_attempts,
        retry_on=list(e.retry_on),
        timeout_seconds=e.timeout_seconds,
        policy_snapshot=e.policy_snapshot,
        created_at=e.created_at,
        ended_at=e.ended_at,
        attempts=[_attempt_summary(a) for a in attempts],
    )


def attempt_view(uow: UnitOfWork, attempt_id: str) -> AttemptView:
    a = uow.attempts.get(attempt_id)
    if a is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    lease = uow.leases.get_attempt_lease(a.id)
    claim = uow.claims.get(a.id)
    report: dict[str, Any] | None = None
    if claim is not None:
        report = {
            "parsed_ok": claim.parsed_ok,
            "parse_errors": claim.parse_errors,
            "document": claim.document,
        }
    return AttemptView(
        id=a.id,
        execution_id=a.execution_id,
        task_id=a.task_id,
        number=a.number,
        state=a.state,
        handle=a.handle,
        workspace_path=a.workspace_path,
        identity_sha256=a.identity_sha256,
        image_digest=a.image_digest,
        started_at=a.started_at,
        ended_at=a.ended_at,
        exit_code=a.exit_code,
        exit_class=a.exit_class,
        timeout_at=a.timeout_at,
        termination_reason=a.termination_reason,
        lease=(
            {
                "holder": lease.holder,
                "fenced_token": lease.fenced_token,
                "expires_at": lease.expires_at.isoformat(),
            }
            if lease
            else None
        ),
        heartbeat_summary={"signals": 0, "note": "heartbeats are C3"},
        report=report,
    )


def supervisor_health(uow: UnitOfWork, now: datetime, lease_ttl_seconds: int) -> tuple[bool, str]:
    """Supervisor is healthy only when the lease is held and the last tick inside the lease
    window succeeded. A held lease next to failing ticks is not healthy (10, 19)."""
    lease = uow.leases.get_supervisor()
    status = uow.supervisor_status.get()
    if lease is None:
        return False, "no supervisor lease"
    if lease.expires_at <= now:
        return False, f"lease held by {lease.holder} expired {lease.expires_at.isoformat()}"
    window_start = now - timedelta(seconds=lease_ttl_seconds)
    last_ok = status.last_success_at
    if last_ok is None or last_ok < window_start:
        detail = "no successful tick within the lease window"
        if status.last_error is not None:
            detail += f"; last error: {status.last_error}"
        return False, detail
    if status.last_error_at is not None and status.last_error_at > last_ok:
        return False, f"last tick failed: {status.last_error}"
    return True, f"held by {lease.holder}, last successful tick {last_ok.isoformat()}"


def supervisor_view(
    uow: UnitOfWork, providers: list[ExecutionProvider], now: datetime, lease_ttl_seconds: int
) -> SupervisorView:
    lease = uow.leases.get_supervisor()
    status = uow.supervisor_status.get()
    healthy, _ = supervisor_health(uow, now, lease_ttl_seconds)
    return SupervisorView(
        lease=(
            {
                "holder": lease.holder,
                "fenced_token": lease.fenced_token,
                "expires_at": lease.expires_at.isoformat(),
            }
            if lease
            else None
        ),
        last_tick_at=status.last_tick_at,
        last_success_at=status.last_success_at,
        last_error_at=status.last_error_at,
        last_error=status.last_error,
        healthy=healthy,
        tick_ms=status.tick_ms,
        counts={**status.counts, "wakes_unacked": uow.wakes.count_unacked()},
        providers=[{"name": p.name, **p.capabilities().as_dict()} for p in providers],
        github=_github_status(uow),
    )


def _github_status(uow: UnitOfWork) -> dict[str, Any]:
    """23: the observation status `GET /supervisor` reports, with the repositories whose
    reactions the App cannot read named rather than merely counted."""
    observed = uow.pull_requests.list_in_states([PullRequestState.OPENING, PullRequestState.OPEN])
    last_poll = max(
        (pr.last_polled_at for pr in observed if pr.last_polled_at is not None), default=None
    )
    unobservable = sorted({pr.id for pr in observed if not pr.reactions_observable})
    return {
        "observed_pull_requests": len(observed),
        "last_poll_at": last_poll.isoformat() if last_poll else None,
        "deliveries_pending": uow.github_deliveries.count_unprocessed(),
        "reactions_unobservable": unobservable,
    }


# ----- C2 views ----------------------------------------------------------------


def _escalation_view(e: Escalation) -> EscalationView:
    return EscalationView(
        id=e.id,
        state=e.state.value,
        question=e.question,
        attempt_id=e.attempt_id,
        opened_at=e.opened_at,
        closed_at=e.closed_at,
        decision_id=e.decision_id,
    )


def _principal_name(uow: UnitOfWork, principal_id: str | None) -> str | None:
    if principal_id is None:
        return None
    principal = uow.principals.get(principal_id)
    return principal.name if principal else principal_id


def _review_view(uow: UnitOfWork, r: ReviewReportRecord) -> ReviewReportView:
    return ReviewReportView(
        id=r.id,
        task_id=r.task_id,
        head_sha=r.head_sha,
        reviewer_kind=r.reviewer_kind,
        reviewer_attempt_id=r.reviewer_attempt_id,
        reviewer_principal=_principal_name(uow, r.reviewer_principal_id),
        verdict=str(r.document.get("verdict", "")),
        findings=len(r.document.get("findings", [])),
        document=r.document,
        created_at=r.created_at,
    )


def _acceptance_view(uow: UnitOfWork, a: AcceptanceResult) -> AcceptanceView:
    return AcceptanceView(
        id=a.id,
        head_sha=a.head_sha,
        principal=_principal_name(uow, a.principal_id) or a.principal_id,
        verdict=a.verdict.value,
        reasoning=a.reasoning,
        superseded_at=a.superseded_at,
        created_at=a.created_at,
    )


def _decision_view(uow: UnitOfWork, d: Decision) -> DecisionView:
    return DecisionView(
        id=d.id,
        kind=d.kind,
        principal=_principal_name(uow, d.principal_id) or d.principal_id,
        verbatim=d.verbatim,
        resolves=d.resolves,
        escalation_id=d.escalation_id,
        created_at=d.created_at,
    )


def _gate_view(g: GateResultRecord) -> GateResultView:
    return GateResultView(
        gate=g.gate,
        phase=g.phase,
        result=g.result,
        detail=g.detail,
        head_sha=g.head_sha,
        evidence_ids=list(g.evidence_ids),
        evaluated_at=g.evaluated_at,
    )


def gate_summary(uow: UnitOfWork, task_id: str) -> dict[str, Any]:
    """What the gates say for the head the task is currently bound to."""
    task = uow.tasks.get(task_id)
    rows = [
        g
        for g in uow.gate_results.list_for_task(task_id)
        if task is None or not task.head_sha or g.head_sha == task.head_sha
    ]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.result] = counts.get(row.result, 0) + 1
    return {
        "head_sha": task.head_sha if task else None,
        "counts": counts,
        "results": {row.gate: row.result for row in rows},
        "failing": sorted(row.gate for row in rows if row.result in ("fail", "error")),
    }


def attempt_gates(uow: UnitOfWork, attempt_id: str) -> GateList:
    attempt = uow.attempts.get(attempt_id)
    if attempt is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    rows = list(uow.gate_results.list_for_attempt(attempt_id))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.result] = counts.get(row.result, 0) + 1
    task = uow.tasks.get(attempt.task_id)
    return GateList(
        attempt_id=attempt_id,
        head_sha=task.head_sha if task else None,
        items=[_gate_view(r) for r in rows],
        counts=counts,
    )


def _evidence_view(e: EvidenceRecord) -> EvidenceView:
    return EvidenceView(
        id=int(e.id or 0),
        attempt_id=e.attempt_id,
        kind=e.kind,
        source=e.source,
        verified=e.verified,
        observed_at=e.observed_at,
        payload=e.payload,
        artifact_id=e.artifact_id,
    )


def attempt_evidence(uow: UnitOfWork, attempt_id: str) -> EvidenceList:
    if uow.attempts.get(attempt_id) is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    return EvidenceList(
        items=[_evidence_view(e) for e in uow.evidence.list_for_attempt(attempt_id)]
    )


def _artifact_view(a: Artifact) -> ArtifactView:
    return ArtifactView(
        id=a.id,
        attempt_id=a.attempt_id,
        task_id=a.task_id,
        type=a.type,
        filename=a.filename,
        size=a.size,
        sha256=a.sha256,
        content_type=a.content_type,
        created_by=a.created_by,
        created_at=a.created_at,
    )


def attempt_artifacts(uow: UnitOfWork, attempt_id: str) -> ArtifactList:
    if uow.attempts.get(attempt_id) is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    return ArtifactList(
        items=[_artifact_view(a) for a in uow.artifacts.list_for_attempt(attempt_id)]
    )


def artifact_view(uow: UnitOfWork, artifact_id: str) -> ArtifactView:
    artifact = uow.artifacts.get(artifact_id)
    if artifact is None:
        raise NotFoundError(f"artifact {artifact_id} not found")
    return _artifact_view(artifact)


def attempt_report(uow: UnitOfWork, attempt_id: str) -> CompletionClaimView:
    if uow.attempts.get(attempt_id) is None:
        raise NotFoundError(f"attempt {attempt_id} not found")
    claim = uow.claims.get(attempt_id)
    if claim is None:
        raise NotFoundError(f"attempt {attempt_id} has no parsed report")
    return CompletionClaimView(
        attempt_id=attempt_id,
        parsed_ok=claim.parsed_ok,
        parse_errors=claim.parse_errors,
        document=claim.document,
    )


def wake_view(uow: UnitOfWork, w: Wake) -> WakeView:
    return WakeView(
        id=w.id,
        principal=_principal_name(uow, w.principal_id) or w.principal_id,
        reason=w.reason,
        task_id=w.task_id,
        summary=str(w.payload.get("summary", "")),
        payload=w.payload,
        created_at=w.created_at,
        attempts=w.attempts,
        delivered_at=w.delivered_at,
        acked_at=w.acked_at,
        ack_note=w.ack_note,
        next_attempt_at=w.next_attempt_at,
        last_error=w.last_error,
        gave_up_at=w.gave_up_at,
    )


def wake_list(
    uow: UnitOfWork,
    *,
    principal_id: str,
    since: datetime | None,
    include_acked: bool,
    limit: int | None,
) -> WakeList:
    size = clamp_limit(limit)
    rows = list(
        uow.wakes.list_for_principal(
            principal_id, since=since, include_acked=include_acked, limit=size + 1
        )
    )
    page = rows[:size]
    next_cursor = encode_cursor(page[-1].id) if len(rows) > size and page else None
    return WakeList(items=[wake_view(uow, w) for w in page], next_cursor=next_cursor)


# ----- C4 views: the pull request and what GitHub said about it (04, 23) --------


def _pr_summary(uow: UnitOfWork, task_id: str) -> dict[str, Any] | None:
    """The short form the task view carries; the full record is /pull-request."""
    pull_request = uow.pull_requests.get_for_task(task_id)
    if pull_request is None:
        return None
    cycles = uow.review_cycles.list_for_pull_request(pull_request.id)
    comments = uow.review_comments.list_for_pull_request(pull_request.id)
    dispositions = uow.dispositions.list_for_comments([c.id for c in comments])
    certifications = uow.ci_certifications.list_for_task(task_id)
    return {
        "number": pull_request.number,
        "url": pull_request.url,
        "state": pull_request.state.value,
        "head_sha": pull_request.head_sha,
        "base_ref": pull_request.base_ref,
        "merged_at": pull_request.merged_at.isoformat() if pull_request.merged_at else None,
        "merge_sha": pull_request.merge_sha,
        "merged_by": pull_request.merged_by,
        "completed_rounds": sum(1 for c in cycles if c.state == "completed"),
        "comments": len(comments),
        "dispositions": len(dispositions),
        "reactions_observable": pull_request.reactions_observable,
        "ci_certification": certifications[-1].state if certifications else None,
    }


def pull_request_view(uow: UnitOfWork, task_id: str) -> PullRequestView:
    task = uow.tasks.get(task_id)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    pull_request = uow.pull_requests.get_for_task(task_id)
    if pull_request is None:
        raise NotFoundError(f"task {task_id} has no pull request")
    policy = uow.policies.get(task.policy_name, task.policy_version)
    required = required_rounds(policy.document) if policy else 1
    cycles = list(uow.review_cycles.list_for_pull_request(pull_request.id))
    comments = list(uow.review_comments.list_for_pull_request(pull_request.id))
    dispositions = {
        d.review_comment_id: d for d in uow.dispositions.list_for_comments([c.id for c in comments])
    }
    certifications = list(uow.ci_certifications.list_for_task(task_id))
    decisions = list(uow.ci_decisions.list_for_task(task_id))
    principals = {}
    for decision in decisions:
        principal = uow.principals.get(decision.principal_id)
        principals[decision.principal_id] = principal.name if principal else decision.principal_id
    gate_rows = [
        row
        for row in uow.gate_results.list_for_task(task_id)
        if row.phase in ("publication", "post_pr")
    ]
    return PullRequestView(
        id=pull_request.id,
        task_id=task_id,
        number=pull_request.number,
        url=pull_request.url,
        state=pull_request.state.value,
        base_ref=pull_request.base_ref,
        work_branch=pull_request.work_branch,
        head_sha=pull_request.head_sha,
        title=pull_request.title,
        body_sha256=pull_request.body_sha256,
        opened_at=pull_request.opened_at,
        merged_at=pull_request.merged_at,
        merge_sha=pull_request.merge_sha,
        merged_by=pull_request.merged_by,
        closed_at=pull_request.closed_at,
        closed_by=pull_request.closed_by,
        last_polled_at=pull_request.last_polled_at,
        reactions_observable=pull_request.reactions_observable,
        completed_rounds=sum(1 for c in cycles if c.state == "completed"),
        required_rounds=required,
        heads=[
            PullRequestHeadView(
                sha=head.sha, pushed_by=head.pushed_by.value, observed_at=head.observed_at
            )
            for head in uow.pull_request_heads.list_for_pull_request(pull_request.id)
        ],
        cycles=[
            ReviewCycleView(
                id=cycle.id,
                head_sha=cycle.head_sha,
                components=list(cycle.components),
                completed_components=dict(cycle.completed_components),
                state=cycle.state,
                trigger=cycle.trigger,
                opened_at=cycle.opened_at,
                completed_at=cycle.completed_at,
            )
            for cycle in cycles
        ],
        external_reviews=[
            ExternalReviewView(
                id=review.id,
                reviewer_login=review.reviewer_login,
                signal=review.signal,
                github_id=review.github_id,
                reviewed_sha=review.reviewed_sha,
                sha_inferred=review.sha_inferred,
                state=review.state,
                accepted=review.accepted,
                received_at=review.received_at,
            )
            for review in uow.external_reviews.list_for_pull_request(pull_request.id)
        ],
        comments=[
            ReviewCommentView(
                id=comment.id,
                github_id=comment.github_id,
                kind=comment.kind,
                login=comment.login,
                path=comment.path,
                line=comment.line,
                body=comment.body,
                reviewed_sha=comment.reviewed_sha,
                created_at=comment.created_at,
                updated_at=comment.updated_at,
                disposition=(
                    {
                        "disposition": dispositions[comment.id].disposition.value,
                        "reasoning": dispositions[comment.id].reasoning,
                        "created_at": dispositions[comment.id].created_at.isoformat(),
                    }
                    if comment.id in dispositions
                    else None
                ),
            )
            for comment in comments
        ],
        reactions=[
            ReactionView(
                subject_kind=reaction.subject_kind,
                subject_github_id=reaction.subject_github_id,
                github_id=reaction.github_id,
                login=reaction.login,
                content=reaction.content,
                observed_at=reaction.observed_at,
                removed_at=reaction.removed_at,
            )
            for reaction in uow.reactions.list_for_pull_request(pull_request.id)
        ],
        ci_certifications=[
            CICertificationView(
                id=c.id,
                head_sha=c.head_sha,
                state=c.state,
                detail=c.detail,
                required_checks=list(c.required_checks),
                check_runs=list(c.check_runs),
                failure=dict(c.failure),
                evaluated_at=c.evaluated_at,
            )
            for c in certifications
        ],
        ci_decisions=[
            CIDecisionView(
                id=d.id,
                cause=d.cause,
                action=d.action,
                reasoning=d.reasoning,
                principal=principals.get(d.principal_id, d.principal_id),
                created_at=d.created_at,
            )
            for d in decisions
        ],
        gates=[
            GateResultView(
                gate=row.gate,
                phase=row.phase,
                result=row.result,
                detail=row.detail,
                head_sha=row.head_sha or "",
                evidence_ids=list(row.evidence_ids),
                evaluated_at=row.evaluated_at,
            )
            for row in gate_rows
        ],
    )
