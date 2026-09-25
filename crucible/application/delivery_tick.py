"""The delivery half of the supervision tick (23, 09).

Three steps, in this order: publish every task that acceptance moved to `publishing`,
process any webhook deliveries that arrived, then poll every pull request in an observed
state and re-evaluate the post-PR gates.

All the GitHub and container I/O lives here; everything it produces is applied inside the
supervisor's fenced transactions by `crucible.application.observation` and
`crucible.application.publish`, which are pure of I/O. That split is what lets the
poll-only path and the webhook path be the same code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from crucible.application.observation import (
    OBSERVED_STATES,
    ObservationResult,
    advance_delivery,
    apply_observation,
    evaluate_delivery_gates,
    policy_for,
    poll_due,
    to_cycle,
)
from crucible.application.publish import (
    PublishPlan,
    advance_after_publish,
    build_plan,
    fail_publish,
    open_review_cycle,
    record_publish_started,
    record_token_minted,
    repository_slug,
    upsert_pull_request,
)
from crucible.application.review import latest_work_attempt
from crucible.application.transitions import record_event
from crucible.domain.entities import PullRequestState, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass
from crucible.domain.external_review import completed_rounds
from crucible.domain.lifecycle import TaskState
from crucible.domain.publication import body_sha256
from crucible.domain.secrets import redact
from crucible.ports.clock import Clock
from crucible.ports.github import GitHubClient, GitHubError, InstallationToken
from crucible.ports.publish import Publisher, PublishRequest
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.delivery")

T = TypeVar("T")


class FencedHost(Protocol):
    """What the coordinator needs of the supervisor: a fenced transaction and a thread."""

    def _fenced(self) -> AbstractContextManager[UnitOfWork]: ...

    async def _db(self, fn: Callable[[], T]) -> T: ...


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    poll_interval_seconds: int = 120
    reactions_poll_interval_seconds: int = 60
    ci_log_excerpt_bytes: int = 64 * 1024
    publisher_timeout_seconds: int = 600
    publisher_image: str | None = None


@dataclass(frozen=True, slots=True)
class PollPlan:
    task_id: str
    pull_request_id: str
    number: int
    repository_name: str
    installation_id: int | None
    base_ref: str
    attempt_id: str
    with_reactions: bool
    failed_run_id: str = ""


@dataclass(slots=True)
class DeliveryCounts:
    published: int = 0
    polled: int = 0
    deliveries: int = 0
    gate_passes: int = 0


class DeliveryCoordinator:
    """Publication and observation, driven from the supervisor tick."""

    def __init__(
        self,
        host: FencedHost,
        clock: Clock,
        *,
        github: GitHubClient | None = None,
        publisher: Publisher | None = None,
        config: DeliveryConfig | None = None,
    ) -> None:
        self._host = host
        self._clock = clock
        self._github = github
        self._publisher = publisher
        self.config = config or DeliveryConfig()

    @property
    def enabled(self) -> bool:
        return self._github_ready()

    def _github_ready(self) -> bool:
        """A client exists and, when it can say so, an App credential is in place. The
        credential may arrive at runtime from the Connect GitHub flow (ADR 0016)."""
        if self._github is None:
            return False
        configured = getattr(self._github, "configured", None)
        return not callable(configured) or bool(configured())

    def _fenced(self) -> Iterator[UnitOfWork]:  # pragma: no cover - thin delegate
        raise NotImplementedError

    # ----- publication --------------------------------------------------

    async def publish(self) -> int:
        if not self._github_ready() or self._publisher is None:
            return 0
        plans = await self._host._db(self._take_publishing)
        done = 0
        for plan in plans:
            if await self._publish_one(plan):
                done += 1
        return done

    async def push_quota_checkpoint(self, attempt_id: str, *, required: bool) -> tuple[bool, str]:
        """Push a collected quota checkpoint without opening or updating a pull request."""
        if not self._github_ready() or self._github is None or self._publisher is None:
            if required:
                return False, "the GitHub publisher is not configured"
            return True, "a publisher is not required for this repository"
        plan = await self._host._db(lambda: self._checkpoint_plan(attempt_id))
        if plan is None:
            return True, "the attempt has no checkpoint to push"
        if plan.installation_id is None:
            return False, f"repository {plan.repository_name} has no installation id"
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id,
                repository=plan.repository_name,
            )
            await self._host._db(lambda: self._record_minted(plan, token))
            outcome = await self._publisher.push(self._publish_request(plan), token)
            await self._host._db(lambda: self._record_publisher(plan, outcome))
            if not outcome.pushed:
                return False, outcome.detail or f"publisher exited {outcome.exit_code}"
            remote = await asyncio.to_thread(
                self._github.remote_head,
                token,
                repository=plan.repository_name,
                ref=plan.work_branch,
            )
            if remote != plan.head_sha:
                return False, f"remote branch is at {remote}, expected {plan.head_sha}"
            await self._host._db(lambda: self._record_pushed(plan, remote))
            return True, "checkpoint pushed"
        except GitHubError as exc:
            return False, redact(exc.message)
        except Exception as exc:
            return False, redact(f"{type(exc).__name__}: {exc}")
        finally:
            if token is not None:
                token.discard()

    def _checkpoint_plan(self, attempt_id: str) -> PublishPlan | None:
        with self._host._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED:
                return None
            task = uow.tasks.get(attempt.task_id)
            execution = uow.executions.get(attempt.execution_id)
            if task is None or execution is None or not task.head_sha:
                return None
            return build_plan(uow, task, (attempt, execution))

    def _publish_request(self, plan: PublishPlan) -> PublishRequest:
        return PublishRequest(
            attempt_id=plan.attempt_id,
            task_id=plan.task_id,
            owner=plan.external_id,
            repository_url=plan.push_url,
            work_branch=plan.work_branch,
            base_ref=plan.base_ref,
            expected_head=plan.head_sha,
            bundle_path=plan.bundle_path,
            bundle_sha256=plan.bundle_sha256,
            image=self.config.publisher_image or plan.image,
            policy=plan.policy,
            author_name=str(plan.policy.get("git", {}).get("author_name", "crucible-worker")),
            author_email=str(
                plan.policy.get("git", {}).get(
                    "author_email", "crucible-worker@users.noreply.github.com"
                )
            ),
            commit_trailer=str(
                plan.policy.get("git", {}).get("commit_trailer", "Crucible-Attempt")
            ),
            timeout_seconds=self.config.publisher_timeout_seconds,
        )

    def _take_publishing(self) -> list[PublishPlan]:
        plans: list[PublishPlan] = []
        with self._host._fenced() as uow:
            for task in uow.tasks.list_by_state(TaskState.PUBLISHING, for_update=True):
                work = latest_work_attempt(uow, task)
                if work is None:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="plan",
                        detail="no implementing or correcting attempt carries this head",
                    )
                    continue
                plan = build_plan(uow, task, work)
                record_publish_started(uow, self._clock, plan)
                if plan.problem:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="title",
                        detail=plan.problem,
                        attempt_id=plan.attempt_id,
                    )
                    continue
                if plan.installation_id is None:
                    fail_publish(
                        uow,
                        self._clock,
                        task,
                        step="mint",
                        detail=(
                            f"repository {plan.repository_name} is registered without an "
                            "installation id; registration records it (23)"
                        ),
                        attempt_id=plan.attempt_id,
                    )
                    continue
                plans.append(plan)
            uow.commit()
        return plans

    async def _publish_one(self, plan: PublishPlan) -> bool:
        assert self._github is not None and self._publisher is not None
        token: InstallationToken | None = None
        github_step = "installation_token"
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            await self._host._db(lambda: self._record_minted(plan, token))
            github_step = "branch_pushed_at_head"
            if plan.resume_step not in ("branch_pushed_at_head", "github"):
                outcome = await self._publisher.push(self._publish_request(plan), token)
                await self._host._db(lambda: self._record_publisher(plan, outcome))
                if not outcome.pushed:
                    await self._host._db(
                        lambda: self._fail(
                            plan,
                            step=outcome.step,
                            detail=outcome.detail or f"the publisher exited {outcome.exit_code}",
                            extra={
                                "remote_head_before": outcome.remote_head_before,
                                "author_problems": list(outcome.author_problems),
                                "trailer_problems": list(outcome.trailer_problems),
                            },
                        )
                    )
                    return False
            remote = await asyncio.to_thread(
                self._github.remote_head,
                token,
                repository=plan.repository_name,
                ref=plan.work_branch,
            )
            if remote != plan.head_sha:
                await self._host._db(
                    lambda: self._fail(
                        plan,
                        step="branch_pushed_at_head",
                        detail=(
                            f"the remote branch is at {remote}, not the accepted head "
                            f"{plan.head_sha}"
                        ),
                    )
                )
                return False
            await self._host._db(lambda: self._record_pushed(plan, remote))
            if plan.deliverable_kind == "branch":
                await self._host._db(lambda: self._finish(plan, ref=None))
                return True
            github_step = "github"
            ref = await asyncio.to_thread(
                self._github.find_pull_request,
                token,
                repository=plan.repository_name,
                head_branch=plan.work_branch,
            )
            if ref is None or ref.state != "open":
                ref = await asyncio.to_thread(
                    self._github.create_pull_request,
                    token,
                    repository=plan.repository_name,
                    title=plan.title,
                    head_branch=plan.work_branch,
                    base_ref=plan.base_ref,
                    body=plan.body,
                    draft=plan.draft,
                )
            else:
                ref = await asyncio.to_thread(
                    self._github.update_pull_request,
                    token,
                    repository=plan.repository_name,
                    number=ref.number,
                    title=plan.title,
                    body=plan.body,
                    base_ref=plan.base_ref,
                )
            resolved = ref
            # A reused pull request has to match the contract, not merely carry the new
            # head: a PR retargeted to another base, or left as a draft the contract did
            # not ask for, delivers something else. Neither is force-corrected; the
            # publication fails and Foundry decides (23).
            mismatch: list[str] = []
            if resolved.base_ref != plan.base_ref:
                mismatch.append(
                    f"base_ref is {resolved.base_ref!r}, the contract says {plan.base_ref!r}"
                )
            if resolved.draft != plan.draft:
                mismatch.append(f"draft is {resolved.draft}, the contract says {plan.draft}")
            if mismatch:
                await self._host._db(
                    lambda: self._fail(
                        plan,
                        step="github",
                        detail=(
                            f"pull request #{resolved.number} does not match the "
                            f"contract: {'; '.join(mismatch)}"
                        ),
                        extra={"pull_request": resolved.number},
                    )
                )
                return False
            await self._host._db(lambda: self._finish(plan, ref=resolved))
            return True
        except GitHubError as exc:
            failure = exc
            await self._host._db(
                lambda: self._fail(
                    plan,
                    step=github_step,
                    detail=failure.message,
                    response_class=failure.response_class,
                )
            )
            return False
        except Exception as exc:  # the publisher or the transport, not a state change
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("publication failed", extra={"task_id": plan.task_id, "error": detail})
            await self._host._db(lambda: self._fail(plan, step="publish", detail=detail))
            return False
        finally:
            if token is not None:
                token.discard()

    def _record_minted(self, plan: PublishPlan, token: InstallationToken | None) -> None:
        assert token is not None
        with self._host._fenced() as uow:
            record_token_minted(
                uow,
                self._clock,
                plan,
                expires_at=token.expires_at,
                permissions=token.permissions,
            )
            uow.commit()

    def _record_publisher(self, plan: PublishPlan, outcome: Any) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.PUBLISHER_FINISHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                attempt_id=plan.attempt_id,
                payload={
                    "pushed": outcome.pushed,
                    "step": outcome.step,
                    "exit_code": outcome.exit_code,
                    "bundle_head": outcome.head_sha,
                    "remote_head_before": outcome.remote_head_before,
                    "author_problems": list(outcome.author_problems),
                    "trailer_problems": list(outcome.trailer_problems),
                    # Both already redacted by the publisher adapter (12); truncated
                    # here so one failing container cannot fill the event log.
                    "detail": outcome.detail[:500],
                    "log_tail": outcome.log_tail[-2000:],
                },
            )
            uow.commit()

    def _record_pushed(self, plan: PublishPlan, remote: str | None) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.BRANCH_PUSHED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                attempt_id=plan.attempt_id,
                payload={
                    "work_branch": plan.work_branch,
                    "head_sha": remote,
                    "repository": plan.repository_name,
                },
            )
            uow.commit()

    def _fail(
        self,
        plan: PublishPlan,
        *,
        step: str,
        detail: str,
        response_class: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None:
                return
            fail_publish(
                uow,
                self._clock,
                task,
                step=step,
                detail=detail,
                response_class=response_class,
                attempt_id=plan.attempt_id,
                extra=extra,
            )
            uow.commit()

    def _finish(self, plan: PublishPlan, *, ref: Any) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            if task is None or task.state is not TaskState.PUBLISHING:
                return
            body_hash = body_sha256(plan.body)
            pull_request = None
            if ref is not None:
                pull_request, _opened = upsert_pull_request(
                    uow, self._clock, task=task, plan=plan, ref=ref, body_hash=body_hash
                )
            record_event(
                uow,
                self._clock,
                EventKind.PUBLISH_COMPLETED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                attempt_id=plan.attempt_id,
                payload={
                    "head_sha": plan.head_sha,
                    "pull_request": pull_request.number if pull_request else None,
                    "url": pull_request.url if pull_request else None,
                    "body_sha256": body_hash,
                },
            )
            rounds = 0
            if pull_request is not None:
                cycles = uow.review_cycles.list_for_pull_request(pull_request.id)
                # Rounds are counted per pull request across heads (23), so every
                # completed cycle on this PR counts, not only the ones on this head.
                rounds = completed_rounds([to_cycle(row) for row in cycles])
                evaluate_delivery_gates(
                    uow,
                    self._clock,
                    task=task,
                    attempt_id=plan.attempt_id,
                    pull_request=pull_request,
                    policy=plan.policy,
                    certification=None,
                    branch_pushed_sha=plan.head_sha,
                    phases=("publication",),
                )
            state = advance_after_publish(
                uow,
                self._clock,
                task=task,
                plan=plan,
                pull_request=pull_request,
                completed_rounds=rounds,
            )
            if pull_request is not None and state is TaskState.AWAITING_EXTERNAL_REVIEW:
                open_review_cycle(
                    uow,
                    self._clock,
                    task_id=task.id,
                    pull_request=pull_request,
                    head_sha=plan.head_sha,
                    policy=plan.policy,
                )
            uow.commit()

    # ----- observation --------------------------------------------------

    async def observe(self) -> int:
        if not self._github_ready():
            return 0
        plans = await self._host._db(self._due_polls)
        polled = 0
        for plan in plans:
            if await self._observe_one(plan):
                polled += 1
        await self._host._db(self._evaluate_gates)
        return polled

    def _due_polls(self) -> list[PollPlan]:
        now = self._clock.now()
        out: list[PollPlan] = []
        with self._host._fenced() as uow:
            forced = self._forced_pull_requests(uow)
            for state in sorted(OBSERVED_STATES, key=lambda s: s.value):
                for task in uow.tasks.list_by_state(state):
                    pull_request = uow.pull_requests.get_for_task(task.id)
                    if pull_request is None or pull_request.state in (
                        PullRequestState.MERGED,
                        PullRequestState.CLOSED,
                    ):
                        continue
                    due, with_reactions = poll_due(
                        pull_request,
                        now=now,
                        poll_interval_seconds=self.config.poll_interval_seconds,
                        reactions_interval_seconds=self.config.reactions_poll_interval_seconds,
                        task_state=task.state,
                    )
                    if pull_request.id in forced:
                        due = True
                        # 23: a review or comment delivery triggers an immediate
                        # reaction poll for its subject, and while the PR awaits
                        # external review the reaction *is* the verdict, so any
                        # delivery about it brings the reaction poll forward.
                        with_reactions = (
                            with_reactions
                            or forced[pull_request.id]
                            or task.state is TaskState.AWAITING_EXTERNAL_REVIEW
                        )
                    if not due:
                        continue
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    repository = uow.repositories.get(task.repository_id)
                    if repository is None:
                        continue
                    out.append(
                        PollPlan(
                            task_id=task.id,
                            pull_request_id=pull_request.id,
                            number=pull_request.number,
                            repository_name=repository_slug(repository),
                            installation_id=repository.installation_id,
                            base_ref=pull_request.base_ref,
                            attempt_id=work[0].id,
                            with_reactions=with_reactions,
                            failed_run_id=self._failed_run_id(uow, pull_request.id, task),
                        )
                    )
            uow.commit()
        return out

    def _forced_pull_requests(self, uow: UnitOfWork) -> dict[str, bool]:
        """Pull requests a webhook delivery says to look at now, and whether its subject
        needs an immediate reaction poll (23)."""
        forced: dict[str, bool] = {}
        now = self._clock.now()
        for delivery in uow.github_deliveries.list_unprocessed():
            number = 0
            pr_node = delivery.normalized.get("pull_request")
            if isinstance(pr_node, dict):
                number = int(pr_node.get("number", 0))
            elif delivery.normalized.get("is_pull_request"):
                number = int(delivery.normalized.get("issue_number", 0))
            uow.github_deliveries.mark_processed(delivery.delivery_id, now)
            if not number:
                continue
            for state in OBSERVED_STATES:
                for task in uow.tasks.list_by_state(state):
                    pull_request = uow.pull_requests.get_for_task(task.id)
                    if pull_request is not None and pull_request.number == number:
                        forced[pull_request.id] = forced.get(pull_request.id, False) or bool(
                            delivery.normalized.get("review") or delivery.normalized.get("comment")
                        )
        return forced

    def _failed_run_id(self, uow: UnitOfWork, pull_request_id: str, task: Task) -> str:
        certification = uow.ci_certifications.get_for_head(pull_request_id, task.head_sha or "")
        if certification is None or certification.state != "failed":
            return ""
        return str(certification.failure.get("run_id", ""))

    async def _observe_one(self, plan: PollPlan) -> bool:
        assert self._github is not None
        token: InstallationToken | None = None
        try:
            token = await asyncio.to_thread(
                self._github.installation_token,
                installation_id=plan.installation_id or 0,
                repository=plan.repository_name,
            )
            observation = await asyncio.to_thread(
                self._github.observe,
                token,
                repository=plan.repository_name,
                number=plan.number,
                base_ref=plan.base_ref,
                with_reactions=plan.with_reactions,
            )
            excerpt = ""
            if plan.failed_run_id:
                raw = await asyncio.to_thread(
                    self._github.workflow_run_logs,
                    token,
                    repository=plan.repository_name,
                    run_id=plan.failed_run_id,
                    limit_bytes=self.config.ci_log_excerpt_bytes,
                )
                # 12: a workflow log is text the repository controls, and it lands in
                # `ci_certifications.failure`. It is scanned and redacted at the fetch
                # site, before anything can store it.
                excerpt = redact(raw.decode("utf-8", "replace")[-4000:]) if raw else ""
        except GitHubError as exc:
            failure = exc
            await self._host._db(lambda: self._record_poll_error(plan, failure))
            return False
        finally:
            if token is not None:
                token.discard()
        await self._host._db(lambda: self._apply(plan, observation, excerpt))
        return True

    def _record_poll_error(self, plan: PollPlan, exc: GitHubError) -> None:
        with self._host._fenced() as uow:
            record_event(
                uow,
                self._clock,
                EventKind.GITHUB_RATE_LIMITED
                if exc.response_class == "rate_limited"
                else EventKind.PULL_REQUEST_POLLED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=plan.task_id,
                payload={
                    "pull_request": plan.number,
                    "ok": False,
                    "response_class": exc.response_class,
                    "status": exc.status,
                },
            )
            uow.commit()

    def _apply(self, plan: PollPlan, observation: Any, excerpt: str) -> None:
        with self._host._fenced() as uow:
            task = uow.tasks.get(plan.task_id, for_update=True)
            pull_request = uow.pull_requests.get(plan.pull_request_id, for_update=True)
            if task is None or pull_request is None:
                return
            apply_observation(
                uow,
                self._clock,
                task=task,
                pull_request=pull_request,
                observation=observation,
                policy=policy_for(uow, task),
                attempt_id=plan.attempt_id,
                with_reactions=plan.with_reactions,
                log_excerpt=excerpt,
            )
            uow.commit()

    def _evaluate_gates(self) -> None:
        """09: post-PR gates re-evaluate each reconcile tick until they resolve.

        This is also what turns a disposition recorded through the API into progress
        without waiting for the next poll."""
        with self._host._fenced() as uow:
            for state in (
                TaskState.EXTERNAL_FEEDBACK_RECEIVED,
                TaskState.AWAITING_CI_CERTIFICATION,
                TaskState.AWAITING_EXTERNAL_REVIEW,
            ):
                for task in uow.tasks.list_by_state(state, for_update=True):
                    pull_request = uow.pull_requests.get_for_task(task.id, for_update=True)
                    if pull_request is None:
                        continue
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    policy = policy_for(uow, task)
                    certification = uow.ci_certifications.get_for_head(
                        pull_request.id, task.head_sha or ""
                    )
                    gates = evaluate_delivery_gates(
                        uow,
                        self._clock,
                        task=task,
                        attempt_id=work[0].id,
                        pull_request=pull_request,
                        policy=policy,
                        certification=certification,
                        branch_pushed_sha=task.head_sha,
                    )
                    advance_delivery(
                        uow,
                        self._clock,
                        task=task,
                        pull_request=pull_request,
                        policy=policy,
                        gates=gates,
                        certification=certification,
                        result=ObservationResult(),
                    )
            uow.commit()
