"""Publication: the accepted head becomes a pushed branch and a pull request (23, 09).

Runs only after the pre-PR gates passed for the collected head, the internal review is
recorded, and Foundry's AcceptanceResult for that head is `accepted`. The order is the
one 23 fixes, and each step is an event before and after:

1. `publish_started` with the head SHA and the bundle it will push;
2. mint a repository-scoped installation token;
3. run the publisher container, which verifies the bundle, asserts the head, checks
   commit authorship and trailers, and pushes without force;
4. from Crucible, confirm the remote head (`branch_pushed_at_head`);
5. open the PR, or leave the existing one whose head just moved, with a body rendered
   from the contract and verified evidence only;
6. `publish_completed`, then `pr_exists_head_matches`, then the task moves on.

Anything that fails between 2 and 6 is `publish_failed` with the step and the response
class. The token is never in the record, the event, or the log; only its expiry is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from crucible.application.transitions import move_task, record_event
from crucible.application.wakes import create_wake
from crucible.contracts.evidence import EvidenceKind
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    Execution,
    ExternalReviewCycle,
    PullRequest,
    PullRequestHead,
    PullRequestState,
    PushedBy,
    Repository,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.external_review import CycleState, configured_components, required_rounds
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import TaskState
from crucible.domain.publication import (
    BodyInput,
    CorrectionEntry,
    CriterionMapping,
    TitleRefusedError,
    VerifiedCheck,
    body_sha256,
    render_body,
    validate_title,
)
from crucible.ports.clock import Clock
from crucible.ports.github import PullRequestRef
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.publish")

DEFAULT_TITLE = "Crucible delivery"


@dataclass(frozen=True, slots=True)
class PublishPlan:
    """Everything one publication needs, read once inside a fenced transaction.

    Nothing here is a live ORM object: the publisher and the GitHub calls run outside a
    transaction, and a plan that held rows would hold a connection with them."""

    task_id: str
    external_id: str
    principal_id: str
    attempt_id: str
    head_sha: str
    repository_id: str
    repository_name: str
    # Where the publisher pushes. Derived from the registered repository's API name, not
    # from its clone url: `repositories.url` is where Crucible's own containers *fetch*
    # from, which in a developer arrangement is a local mirror of a repository they hold
    # no credential for (docs/implementation-notes/c4.md).
    push_url: str
    installation_id: int | None
    base_ref: str
    work_branch: str
    deliverable_kind: str
    draft: bool
    image: str
    bundle_path: str
    policy: dict[str, Any] = field(default_factory=dict)
    title: str = DEFAULT_TITLE
    body: str = ""
    existing_pr_number: int | None = None
    timeout_seconds: int = 600
    problem: str = ""
    resume_step: str = ""
    retry_number: int = 0
    publish_retry_max: int = 3


def repository_slug(repository: Repository) -> str:
    """`owner/name`, which is what every API path needs.

    Taken from the clone url when that is a GitHub url, and from the registered name
    otherwise, so a repository whose url is a local mirror still resolves to the
    repository the App is installed on."""
    url = repository.url.rstrip("/")
    if "github.com" in url:
        if url.endswith(".git"):
            url = url[: -len(".git")]
        parts = [p for p in url.replace(":", "/").split("/") if p]
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    return repository.name.strip("/")


def push_url_for(repository: Repository, *, host: str = "github.com") -> str:
    """The https remote the publisher pushes to."""
    url = repository.url.rstrip("/")
    if "github.com" in url:
        return url if url.endswith(".git") else f"{url}.git"
    return f"https://{host}/{repository_slug(repository)}.git"


def verified_checks(uow: UnitOfWork, attempt_id: str) -> tuple[VerifiedCheck, ...]:
    """Crucible's own verifier runs, never the worker's report of them (11, 23)."""
    out: list[VerifiedCheck] = []
    for row in uow.evidence.list_for_attempt(attempt_id):
        if row.kind != EvidenceKind.VERIFICATION_RUN.value or not row.verified:
            continue
        out.append(
            VerifiedCheck(
                id=str(row.payload.get("id", "")),
                command=str(row.payload.get("command", "")),
                exit_code=int(row.payload.get("exit_code", -1)),
                expect_exit=int(row.payload.get("expect_exit", 0)),
                # The verifier's own log, stored as an artifact. The worker's log is never
                # what the body cites (23).
                artifact_id=row.artifact_id,
                ran=bool(row.payload.get("ran", True)),
            )
        )
    return tuple(out)


def criteria_mappings(
    contract: dict[str, Any], claim: dict[str, Any] | None
) -> tuple[CriterionMapping, ...]:
    """Each acceptance criterion with the mapping the claim proposed for it.

    The mapping is labelled by its own status word and sits beside the verification
    table; the body never presents it as a verified fact of its own (23)."""
    proposed = {
        str(m.get("id")): m
        for m in (claim or {}).get("acceptance_mapping", [])
        if isinstance(m, dict)
    }
    out: list[CriterionMapping] = []
    for criterion in contract.get("acceptance_criteria", []):
        cid = str(criterion.get("id", ""))
        mapping = proposed.get(cid, {})
        out.append(
            CriterionMapping(
                id=cid,
                text=str(criterion.get("text", "")),
                status=str(mapping.get("status", "not_reported")),
                evidence=str(mapping.get("evidence", "")),
            )
        )
    return tuple(out)


def correction_history(uow: UnitOfWork, task: Task) -> tuple[CorrectionEntry, ...]:
    out: list[CorrectionEntry] = []
    for version in uow.contracts.list_for_task(task.id):
        correction = version.document.get("correction")
        if not isinstance(correction, dict):
            continue
        out.append(
            CorrectionEntry(
                version=version.version,
                reason=str(correction.get("reason", "")),
                addresses=tuple(
                    str(a.get("ref") or a.get("id") or a) for a in correction.get("addresses", [])
                ),
            )
        )
    return tuple(out)


def review_reference(uow: UnitOfWork, task: Task) -> dict[str, str] | None:
    reports = [
        r
        for r in uow.review_reports.list_for_task(task.id)
        if r.head_sha == (task.head_sha or "") and r.superseded_at is None
    ]
    if not reports:
        return None
    report = reports[-1]
    reference = {"reviewer_kind": report.reviewer_kind, "report_id": report.id}
    if report.artifact_id:
        reference["report_artifact"] = report.artifact_id
    return reference


def build_plan(uow: UnitOfWork, task: Task, work: tuple[Attempt, Execution]) -> PublishPlan:
    """Read everything the publication needs and render the body. Pure of I/O beyond
    the database: the GitHub calls and the container come later."""
    attempt, execution = work
    stored = uow.contracts.get(task.id, execution.contract_version)
    assert stored is not None
    contract = stored.document
    repository = uow.repositories.get(task.repository_id)
    assert repository is not None
    policy = execution.policy_snapshot or {}
    claim_record = uow.claims.get(attempt.id)
    claim = claim_record.document if claim_record and claim_record.parsed_ok else None
    repo_section = contract.get("repository", {})
    base_ref = str(repo_section.get("base_ref") or repository.default_branch or "main")
    work_branch = str(repo_section.get("work_branch") or f"crucible/{task.external_id}")
    deliverables = [
        d for d in contract.get("deliverables", []) if d.get("kind") in ("pull_request", "branch")
    ]
    deliverable = deliverables[0] if deliverables else {"kind": "pull_request"}
    closes = tuple(str(c) for c in deliverable.get("closes", []))
    proposed = str((claim or {}).get("proposed_pull_request", {}).get("title") or "")
    problem = ""
    title = DEFAULT_TITLE if not proposed else ""
    if proposed:
        try:
            title = validate_title(proposed)
        except TitleRefusedError as exc:
            problem = str(exc)
            title = ""
    body = render_body(
        BodyInput(
            external_id=task.external_id,
            objective=str(contract.get("objective", "")),
            head_sha=task.head_sha or "",
            attempt_id=attempt.id,
            harness=execution.harness,
            harness_version=str(policy.get("harness_version", "")) or execution.model,
            image_digest=attempt.image_digest or execution.image,
            criteria=criteria_mappings(contract, claim),
            checks=verified_checks(uow, attempt.id),
            review_reference=review_reference(uow, task),
            corrections=correction_history(uow, task),
            closes=closes,
            limitations=tuple(str(x) for x in (claim or {}).get("limitations", [])),
            risks=tuple(str(x) for x in (claim or {}).get("risks", [])),
            artifact_verifications=tuple(
                str(v.get("path", ""))
                for v in contract.get("required_verification", [])
                if v.get("kind") == "artifact"
            ),
        )
    )
    existing = uow.pull_requests.get_for_task(task.id)
    git_policy = policy.get("git", {})
    publishing = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    publishing_payload = publishing.payload if publishing else {}
    return PublishPlan(
        task_id=task.id,
        external_id=task.external_id,
        principal_id=task.principal_id,
        attempt_id=attempt.id,
        head_sha=task.head_sha or "",
        repository_id=repository.id,
        repository_name=repository_slug(repository),
        push_url=push_url_for(repository),
        installation_id=repository.installation_id,
        base_ref=base_ref,
        work_branch=work_branch,
        deliverable_kind=str(deliverable.get("kind", "pull_request")),
        draft=bool(deliverable.get("draft", False)),
        image=attempt.image_digest or execution.image,
        bundle_path=f"{attempt.workspace_path}/output/work_branch.bundle",
        policy=policy,
        title=title,
        body=body,
        existing_pr_number=existing.number if existing else None,
        timeout_seconds=int(git_policy.get("publish_timeout_seconds", 600)),
        problem=problem,
        resume_step=str(publishing_payload.get("resume_step") or ""),
        retry_number=int(publishing_payload.get("retry_number", 0)),
        publish_retry_max=int(
            publishing_payload.get("publish_retry_max")
            or policy.get("limits", {}).get("publish_retry_max", 3)
        ),
    )


def record_publish_started(uow: UnitOfWork, clock: Clock, plan: PublishPlan) -> None:
    record_event(
        uow,
        clock,
        EventKind.PUBLISH_STARTED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=plan.task_id,
        attempt_id=plan.attempt_id,
        payload={
            "head_sha": plan.head_sha,
            "bundle": plan.bundle_path,
            "repository": plan.repository_name,
            "work_branch": plan.work_branch,
            "base_ref": plan.base_ref,
            "deliverable": plan.deliverable_kind,
            "body_sha256": body_sha256(plan.body),
            "resume_step": plan.resume_step,
            "retry_number": plan.retry_number,
            "publish_retry_max": plan.publish_retry_max,
        },
    )


def record_token_minted(
    uow: UnitOfWork,
    clock: Clock,
    plan: PublishPlan,
    *,
    expires_at: datetime,
    permissions: dict[str, str],
) -> None:
    """The expiry and the job, never the value (12)."""
    record_event(
        uow,
        clock,
        EventKind.INSTALLATION_TOKEN_MINTED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=plan.task_id,
        attempt_id=plan.attempt_id,
        payload={
            "repository": plan.repository_name,
            "installation_id": plan.installation_id,
            "expires_at": expires_at.isoformat(),
            "permissions": sorted(permissions),
            "note": "the token value is in memory and the publisher's tmpfs only (12)",
        },
    )


def fail_publish(
    uow: UnitOfWork,
    clock: Clock,
    task: Task,
    *,
    step: str,
    detail: str,
    response_class: str = "",
    attempt_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """23 step 7: the step and the API response class, never the token."""
    payload: dict[str, Any] = {
        "step": step,
        "detail": detail,
        "head_sha": task.head_sha,
        **(extra or {}),
    }
    if response_class:
        payload["response_class"] = response_class
    if task.state is TaskState.PUBLISHING:
        move_task(
            uow,
            clock,
            task,
            TaskState.PUBLISH_FAILED,
            EventKind.TASK_PUBLISH_FAILED,
            attempt_id=attempt_id,
            payload=payload,
        )
    else:
        record_event(
            uow,
            clock,
            EventKind.TASK_PUBLISH_FAILED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            attempt_id=attempt_id,
            payload=payload,
        )
    publishing = uow.events.latest_for_task_kind(task.id, EventKind.TASK_PUBLISHING.value)
    retry_number = int((publishing.payload if publishing else {}).get("retry_number", 0))
    stored_policy = uow.policies.get(task.policy_name, task.policy_version)
    retry_max = int(
        (publishing.payload if publishing else {}).get("publish_retry_max")
        or (stored_policy.document if stored_policy else {})
        .get("limits", {})
        .get("publish_retry_max", 3)
    )
    retries_remaining = max(retry_max - retry_number, 0)
    links = {"events": f"/v1/tasks/{task.id}/events"}
    if retries_remaining:
        links["republish"] = f"/v1/tasks/{task.id}/republish"
    create_wake(
        uow,
        clock,
        principal_id=task.principal_id,
        reason=WakeReason.PUBLISH_FAILED,
        summary=(
            f"publication failed at {step} on {task.head_sha}: {detail}; "
            f"manual publication retries used {retry_number} of {retry_max}, "
            f"{retries_remaining} remaining"
        )[:500],
        task=task,
        attempt_id=attempt_id,
        extra_links=links,
    )


def upsert_pull_request(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    plan: PublishPlan,
    ref: PullRequestRef,
    body_hash: str,
) -> tuple[PullRequest, bool]:
    """Write or refresh the PR row and record the head as one Crucible pushed."""
    now = clock.now()
    existing = uow.pull_requests.get_for_task(task.id, for_update=True)
    opened = existing is None
    if existing is None:
        pull_request = PullRequest(
            id=new_id(),
            task_id=task.id,
            repository_id=plan.repository_id,
            number=ref.number,
            url=ref.url,
            base_ref=ref.base_ref or plan.base_ref,
            work_branch=plan.work_branch,
            state=PullRequestState.OPEN,
            head_sha=plan.head_sha,
            title=ref.title or plan.title,
            body_sha256=body_hash,
            opened_at=now,
        )
        uow.pull_requests.add(pull_request)
    else:
        pull_request = existing
        pull_request.number = ref.number
        pull_request.url = ref.url
        pull_request.head_sha = plan.head_sha
        pull_request.title = ref.title or plan.title
        pull_request.body_sha256 = body_hash
        pull_request.state = PullRequestState.OPEN
        uow.pull_requests.save(pull_request)
    uow.pull_request_heads.add(
        PullRequestHead(
            id=new_id(),
            pull_request_id=pull_request.id,
            sha=plan.head_sha,
            pushed_by=PushedBy.CRUCIBLE,
            observed_at=now,
        )
    )
    record_event(
        uow,
        clock,
        EventKind.PULL_REQUEST_OPENED if opened else EventKind.PULL_REQUEST_HEAD_UPDATED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        attempt_id=plan.attempt_id,
        payload={
            "number": pull_request.number,
            "url": pull_request.url,
            "head_sha": plan.head_sha,
            "base_ref": pull_request.base_ref,
            "body_sha256": body_hash,
        },
    )
    return pull_request, opened


def advance_after_publish(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task: Task,
    plan: PublishPlan,
    pull_request: PullRequest | None,
    completed_rounds: int,
) -> TaskState:
    """09: branch deliverables reach `accepted`; a PR goes to external review or straight
    to certification, depending on whether the required rounds are already satisfied."""
    attempt_id = plan.attempt_id
    if plan.deliverable_kind == "branch":
        move_task(
            uow,
            clock,
            task,
            TaskState.ACCEPTED,
            EventKind.TASK_ACCEPTED,
            attempt_id=attempt_id,
            payload={
                "head_sha": plan.head_sha,
                "deliverable": "branch",
                "note": "the branch is pushed and verified; nothing is accepted unpublished",
            },
        )
        return TaskState.ACCEPTED
    rounds = required_rounds(plan.policy)
    assert pull_request is not None
    if completed_rounds >= rounds:
        move_task(
            uow,
            clock,
            task,
            TaskState.AWAITING_CI_CERTIFICATION,
            EventKind.TASK_AWAITING_CI_CERTIFICATION,
            attempt_id=attempt_id,
            payload={
                "head_sha": plan.head_sha,
                "pull_request": pull_request.number,
                "completed_rounds": completed_rounds,
                "required_rounds": rounds,
            },
        )
        return TaskState.AWAITING_CI_CERTIFICATION
    move_task(
        uow,
        clock,
        task,
        TaskState.AWAITING_EXTERNAL_REVIEW,
        EventKind.TASK_AWAITING_EXTERNAL_REVIEW,
        attempt_id=attempt_id,
        payload={
            "head_sha": plan.head_sha,
            "pull_request": pull_request.number,
            "completed_rounds": completed_rounds,
            "required_rounds": rounds,
        },
    )
    return TaskState.AWAITING_EXTERNAL_REVIEW


def open_review_cycle(
    uow: UnitOfWork,
    clock: Clock,
    *,
    task_id: str,
    pull_request: PullRequest,
    head_sha: str,
    policy: dict[str, Any],
    trigger: str = "publication",
) -> ExternalReviewCycle | None:
    """One cycle per published head, carrying the components the policy expects (23).

    A second call for the same head is a no-op: publication is re-runnable and a repeat
    must not manufacture a round."""
    if required_rounds(policy) <= 0:
        return None
    existing = [
        cycle
        for cycle in uow.review_cycles.list_for_pull_request(pull_request.id)
        if cycle.head_sha == head_sha and cycle.trigger == trigger
    ]
    if existing:
        return existing[0]
    components = list(configured_components(policy))
    cycle = ExternalReviewCycle(
        id=new_id(),
        pull_request_id=pull_request.id,
        head_sha=head_sha,
        components=components,
        completed_components={},
        state=CycleState.OPEN.value,
        opened_at=clock.now(),
        trigger=trigger,
    )
    uow.review_cycles.add(cycle)
    record_event(
        uow,
        clock,
        EventKind.EXTERNAL_REVIEW_CYCLE_OPENED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task_id,
        payload={
            "cycle_id": cycle.id,
            "head_sha": head_sha,
            "components": components,
            "trigger": trigger,
            "pull_request": pull_request.number,
        },
    )
    return cycle
