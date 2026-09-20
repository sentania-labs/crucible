"""The supervision tick (10). One active supervisor, enforced by the supervisor lease
and its fenced token; every write the supervisor makes carries that token via
SET LOCAL and the database rejects a stale one.

Tick order: lease, provider reconcile (orphans and adoption), materialize scheduled
tasks, launch pending attempts, observe running attempts (timeouts, exits, loss),
sweep cancellations, write the liveness row. Running the tick twice with nothing
happening in between changes nothing the second time except lease expiry times and
the liveness row; that property is tested.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from crucible.application.decisions import (
    DEFAULT_ESCALATION_STALE_HOURS,
    open_escalation,
    repeat_stale_escalation_wakes,
)
from crucible.application.delivery_tick import DeliveryConfig, DeliveryCoordinator
from crucible.application.errors import ApplicationError
from crucible.application.evidence import record_collection_evidence
from crucible.application.gates import evaluate_and_advance
from crucible.application.harnesses import (
    HarnessRegistry,
    effective_mount_mode,
    ingest_progress,
    record_credential_observation,
    record_launch_outcome,
)
from crucible.application.review import (
    author_attempt_ids,
    latest_work_attempt,
    record_review_report,
    review_evidence_payload,
)
from crucible.application.routing import load_routing, reserve, select_model
from crucible.application.transitions import (
    move_attempt,
    move_execution,
    move_task,
    record_event,
    record_rejected_transition,
)
from crucible.application.wakes import (
    create_wake,
    record_delivery,
    retry_hours_from_policy,
    wake_body,
)
from crucible.contracts.completion_claim import parse_claim
from crucible.contracts.evidence import ROLE_RUN_EVIDENCE, EvidenceKind, EvidenceSource
from crucible.contracts.task_contract import TaskContractV1
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    AttemptMetrics,
    CompletionClaimRecord,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    LogChunkRecord,
    PoolExhaustion,
    PullRequestState,
    RetentionAction,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import (
    ATTEMPT_TERMINAL,
    EXECUTION_TERMINAL,
    AttemptState,
    ExecutionState,
    IllegalTransitionError,
    TaskState,
)
from crucible.domain.secrets import find_secrets, redact
from crucible.logs import log_context
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.clock import Clock
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    CleanupPolicy,
    CollectedOutputs,
    ExecutionProvider,
    Handle,
    LaunchRefusedError,
    LaunchSpec,
    LogChunk,
    LogOffset,
    ObservationState,
    ProviderError,
    Workspace,
)
from crucible.ports.github import GitHubClient
from crucible.ports.harness import (
    CredentialSource,
    ExitInfo,
    HarnessGate,
    HarnessUnavailableError,
    LaunchContext,
    MountMode,
    ParsedReport,
)
from crucible.ports.notification import WakeDeliverer
from crucible.ports.publish import Publisher
from crucible.ports.repository import FencedTokenRejectedError, UnitOfWork, UnitOfWorkFactory

log = logging.getLogger("crucible.supervisor")
T = TypeVar("T")

TERMINATION_TIMEOUT = "timeout"
TERMINATION_CANCEL = "cancel"
# A launch the registry or the provider refused (07): recorded so the retry rule knows
# not to try the same refusal again.
TERMINATION_REFUSED = "harness_refused"

# 16 defaults, used when the policy names none.
DEFAULT_LOG_RETENTION_DAYS = 90
DEFAULT_WORKSPACE_RETENTION_DAYS = 14
DEFAULT_WAKE_RETENTION_DAYS = 30
RETENTION_BATCH = 200


class LeaseLostError(Exception):
    """This supervisor no longer holds the lease; it must stop acting."""


@dataclass(slots=True)
class TickResult:
    held: bool
    launched: int = 0
    observed: int = 0
    finished: int = 0
    orphans: int = 0
    wakes_delivered: int = 0
    published: int = 0
    pull_requests_polled: int = 0
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _CancelWork:
    task_id: str
    attempt: Attempt | None


@dataclass(slots=True)
class _Pending:
    attempt: Attempt
    execution: Execution
    task: Task
    contract: dict[str, Any]
    repository_url: str = ""


class Supervisor:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        providers: dict[str, ExecutionProvider],
        clock: Clock,
        *,
        holder: str,
        artifact_store: ArtifactStore,
        wake_deliverer: WakeDeliverer | None = None,
        github: GitHubClient | None = None,
        publisher: Publisher | None = None,
        delivery_config: DeliveryConfig | None = None,
        lease_ttl_seconds: int = 30,
        attempt_lease_ttl_seconds: int = 60,
        checkout_lease_ttl_seconds: int = 21600,
        grace_seconds: int = 60,
        harnesses: HarnessRegistry | None = None,
        harness_gates: Mapping[str, HarnessGate] | None = None,
        credential_sources: Mapping[str, CredentialSource] | None = None,
        credential_sweep: Callable[[UnitOfWork], int] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._clock = clock
        # 07: the adapters. Without a registry the launch spec carries the execution's
        # names and nothing harness-specific, which is the C3 shape the fake provider
        # runs; every real deployment injects one (cli/wiring.py).
        self._harnesses = harnesses
        self._harness_gates: dict[str, HarnessGate] = dict(harness_gates or {})
        self._credential_sources: dict[str, CredentialSource] = dict(credential_sources or {})
        # 25: rotated-out credential directories are shredded once their retention
        # window has elapsed; the retention step calls this with the fenced unit of work.
        self._credential_sweep = credential_sweep
        self._artifacts = artifact_store
        self._wakes = wake_deliverer
        self.holder = holder
        self.lease_ttl_seconds = lease_ttl_seconds
        self.attempt_lease_ttl_seconds = attempt_lease_ttl_seconds
        # Held for the life of the attempt (10); the TTL only bounds a lease whose
        # attempt died without a supervisor to release it.
        self.checkout_lease_ttl_seconds = checkout_lease_ttl_seconds
        self.grace_seconds = grace_seconds
        self.fenced_token: int | None = None
        self._handles: dict[str, Handle] = {}
        self._workspaces: dict[str, Workspace] = {}
        # The delivery half (23). With no GitHub client configured it is inert, which is
        # what every tier below the live one runs with.
        self.delivery = DeliveryCoordinator(
            self, clock, github=github, publisher=publisher, config=delivery_config
        )

    # ----- infrastructure -------------------------------------------------

    @property
    def holds_lease(self) -> bool:
        return self.fenced_token is not None

    @contextmanager
    def _fenced(self) -> Iterator[UnitOfWork]:
        """A transaction carrying this supervisor's fenced token."""
        if self.fenced_token is None:
            raise LeaseLostError("no lease held")
        try:
            with self._uow_factory() as uow:
                uow.set_fenced_token(self.fenced_token)
                yield uow
        except IllegalTransitionError as exc:
            # The attempting transaction rolled back; the rejection is recorded on its own (09).
            self._record_rejection(exc)
            raise
        except FencedTokenRejectedError as exc:
            log.warning("fenced write rejected; standing down", extra={"holder": self.holder})
            self.fenced_token = None
            raise LeaseLostError(str(exc)) from exc

    def _record_rejection(self, exc: IllegalTransitionError) -> None:
        if self.fenced_token is None:
            log.error("illegal transition with no lease; not recorded", extra={"error": str(exc)})
            return
        try:
            with self._uow_factory() as fresh:
                fresh.set_fenced_token(self.fenced_token)
                record_rejected_transition(fresh, self._clock, exc, principal=PRINCIPAL_CRUCIBLE)
                fresh.commit()
        except FencedTokenRejectedError:
            log.error("illegal transition; lease lost before it could be recorded")

    async def _db(self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    def _provider(self, name: str) -> ExecutionProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ProviderError(f"provider {name!r} is not registered") from None

    def _handle_for(self, attempt: Attempt) -> Handle:
        handle = self._handles.get(attempt.id)
        if handle is None:
            assert attempt.handle is not None
            provider = self._execution_provider_name(attempt)
            handle = Handle(provider=provider, ref=attempt.handle, attempt_id=attempt.id)
            self._handles[attempt.id] = handle
        return handle

    def _execution_provider_name(self, attempt: Attempt) -> str:
        with self._uow_factory() as uow:
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            return execution.provider

    def _workspace_for(self, attempt: Attempt) -> Workspace:
        ws = self._workspaces.get(attempt.id)
        if ws is None:
            root = attempt.workspace_path or f"unknown:///{attempt.id}"
            ws = Workspace(
                attempt_id=attempt.id,
                checkout_path=f"{root}/repo",
                identity_path=f"{root}/identity",
                report_path=f"{root}/report",
                output_path=f"{root}/output",
                identity_sha256=attempt.identity_sha256,
            )
            self._workspaces[attempt.id] = ws
        return ws

    # ----- lease ------------------------------------------------------------

    def _lease_step(self) -> bool:
        now = self._clock.now()
        with self._uow_factory() as uow:
            previous = uow.leases.get_supervisor()
            if self.fenced_token is not None:
                lease = uow.leases.renew_supervisor(
                    self.holder, self.fenced_token, now, self.lease_ttl_seconds
                )
                if lease is not None:
                    uow.commit()
                    return True
                self.fenced_token = None
                log.warning("supervisor lease lost", extra={"holder": self.holder})
            lease = uow.leases.acquire_supervisor(self.holder, now, self.lease_ttl_seconds)
            if lease is None:
                uow.commit()
                return False
            self.fenced_token = lease.fenced_token
            uow.set_fenced_token(lease.fenced_token)
            takeover = previous is not None and previous.holder != self.holder
            if (
                previous is None
                or previous.holder != self.holder
                or (previous.fenced_token != lease.fenced_token)
            ):
                record_event(
                    uow,
                    self._clock,
                    EventKind.SUPERVISOR_LEASE_ACQUIRED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={
                        "holder": self.holder,
                        "fenced_token": lease.fenced_token,
                        "previous_holder": previous.holder if previous else None,
                        "takeover": takeover,
                    },
                )
            uow.commit()
            log.info(
                "supervisor lease acquired",
                extra={"holder": self.holder, "fenced_token": lease.fenced_token},
            )
            return True

    def _release_step(self) -> None:
        if self.fenced_token is None:
            return
        with self._uow_factory() as uow:
            uow.set_fenced_token(self.fenced_token)
            if uow.leases.release_supervisor(self.holder, self.fenced_token):
                record_event(
                    uow,
                    self._clock,
                    EventKind.SUPERVISOR_LEASE_RELEASED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={"holder": self.holder, "fenced_token": self.fenced_token},
                )
            uow.commit()
        self.fenced_token = None

    async def stop(self) -> None:
        """Release the lease cleanly (a crash simply lets it expire)."""
        await self._db(self._release_step)

    # ----- tick -----------------------------------------------------------

    async def tick(self) -> TickResult:
        started = time.monotonic()
        held = await self._db(self._lease_step)
        result = TickResult(held=held)
        if not held:
            return result
        try:
            result.orphans = await self._reconcile_provider_handles()
            await self._db(self._resume_quota_waits)
            await self._db(self._materialize_scheduled)
            result.launched = await self._launch_pending()
            observed, finished = await self._observe_attempts()
            result.observed, result.finished = observed, finished
            await self._sweep_cancellations()
            await self._db(self._materialize_evidence)
            await self._db(self._evaluate_pending_gates)
            # After the gates, never before: 16 says nothing a gate consumed is deleted
            # while the task still needs it, and cleanup only ever runs for an attempt
            # that recorded logs_drained (08).
            # The delivery half (23): publish what acceptance released, then observe
            # every pull request in an observed state. Both are no-ops without a
            # configured GitHub client.
            result.published = await self.delivery.publish()
            result.pull_requests_polled = await self.delivery.observe()
            await self._cleanup_step()
            await self._retention_step()
            await self._db(self._refresh_attempt_metrics)
            await self._db(self._repeat_stale_escalations)
            result.wakes_delivered = await self._deliver_wakes()
            result.counts = await self._db(partial(self._status_step, started))
        except LeaseLostError:
            result.held = False
        except Exception as exc:
            # The liveness row must say the tick failed, or readiness lies (19).
            summary = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            await self._db(partial(self._record_failure, summary))
            raise
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _record_failure(self, summary: str) -> None:
        try:
            with self._fenced() as uow:
                status = uow.supervisor_status.get()
                status.holder = self.holder
                status.last_tick_at = self._clock.now()
                status.last_error_at = status.last_tick_at
                status.last_error = summary[:1000]
                uow.supervisor_status.write(status)
                uow.commit()
        except Exception:
            # Nothing more to do here: a stale last_success_at already fails readiness.
            log.exception("could not record the tick failure on the liveness row")

    async def reconcile(self) -> TickResult:
        """Reconciliation is the tick; running it twice changes nothing the second time."""
        return await self.tick()

    # ----- step: provider reconcile ---------------------------------------

    async def _reconcile_provider_handles(self) -> int:
        orphans = 0
        for name, provider in self._providers.items():
            handles = await provider.reconcile()
            for handle in handles:
                action = await self._db(partial(self._classify_handle, handle))
                if action == "orphan":
                    await provider.terminate(handle, "kill")
                    await self._db(partial(self._record_orphan, handle, name))
                    orphans += 1
        return orphans

    def _classify_handle(self, handle: Handle) -> str:
        """A provider handle with no live attempt behind it is an orphan (10 step 3)."""
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(handle.attempt_id)
            if (
                attempt is None
                or attempt.state in ATTEMPT_TERMINAL
                or attempt.state
                in (
                    AttemptState.EXITED,
                    AttemptState.COLLECTED,
                )
            ):
                return "orphan"
            self._handles.setdefault(handle.attempt_id, handle)
            return "keep"

    def _record_orphan(self, handle: Handle, provider: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(handle.attempt_id)
            record_event(
                uow,
                self._clock,
                EventKind.ORPHAN_REMOVED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id if attempt else None,
                execution_id=attempt.execution_id if attempt else None,
                attempt_id=attempt.id if attempt else None,
                payload={"provider": provider, "handle": handle.ref},
            )
            uow.commit()

    # ----- step: materialize scheduled tasks ------------------------------

    def _materialize_scheduled(self) -> None:
        with self._fenced() as uow:
            for task in uow.tasks.list_by_state(TaskState.SCHEDULED, for_update=True):
                attempts = uow.attempts.list_for_task(task.id)
                if any(a.state not in ATTEMPT_TERMINAL for a in attempts):
                    continue
                stored = uow.contracts.get(task.id, task.contract_version)
                assert stored is not None
                # A contract version carrying a correction section runs as a `correct`
                # execution against the existing branch (09). One execution per version.
                # So does anything scheduled after a pull request exists, including a
                # `recollect` decision: the work continues against the remote work
                # branch, never from base_ref again (09, 23).
                role = (
                    ExecutionRole.CORRECT
                    if stored.document.get("correction")
                    or uow.pull_requests.get_for_task(task.id) is not None
                    else ExecutionRole.IMPLEMENT
                )
                matching = [
                    e
                    for e in uow.executions.list_for_task(task.id)
                    if e.contract_version == task.contract_version and e.role is role
                ]
                # A task is scheduled again by a decision on a blocked task (09) as well
                # as by a correction. Being scheduled always means new work: a further
                # attempt on the execution that is still open, or a fresh execution when
                # every one for this version has ended.
                open_execution = next(
                    (e for e in matching if e.state not in EXECUTION_TERMINAL), None
                )
                if open_execution is not None:
                    attempts_so_far = uow.attempts.list_for_execution(open_execution.id)
                    number = max((a.number for a in attempts_so_far), default=0) + 1
                    resumed_attempt = self._create_attempt(uow, open_execution, number=number)
                    if task.quota_wait_started_at is not None:
                        resumed_attempt.resume_from_remote = True
                        uow.attempts.save(resumed_attempt)
                    record_event(
                        uow,
                        self._clock,
                        EventKind.EXECUTION_RESUMED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=task.id,
                        execution_id=open_execution.id,
                        payload={
                            "role": open_execution.role.value,
                            "contract_version": open_execution.contract_version,
                            "attempt_number": number,
                        },
                    )
                    continue
                policy = uow.policies.get(task.policy_name, task.policy_version)
                assert policy is not None
                req = stored.document["execution_request"]
                lifecycle = stored.document["lifecycle"]
                now = self._clock.now()
                pin = req.get("pin") or {}
                execution = Execution(
                    id=new_id(),
                    task_id=task.id,
                    role=role,
                    contract_version=task.contract_version,
                    harness=str(req.get("harness") or pin.get("harness") or "unselected"),
                    model=str(req.get("model") or pin.get("model") or "unselected"),
                    effort=req.get("effort"),
                    provider=str(req["provider"]),
                    image="unselected",
                    policy_snapshot=policy.document,
                    state=ExecutionState.CREATED,
                    max_attempts=int(lifecycle["max_attempts"]),
                    retry_on=[str(x) for x in lifecycle["retry_on"]],
                    timeout_seconds=int(req["timeout_seconds"]),
                    created_at=now,
                )
                uow.executions.add(execution)
                record_event(
                    uow,
                    self._clock,
                    EventKind.EXECUTION_CREATED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    payload={
                        "role": execution.role.value,
                        "harness": execution.harness,
                        "model": execution.model,
                        "provider": execution.provider,
                        "image": execution.image,
                        "max_attempts": execution.max_attempts,
                        "timeout_seconds": execution.timeout_seconds,
                    },
                )
                self._create_attempt(uow, execution, number=1)
            self._materialize_review_executions(uow)
            uow.commit()

    def _create_attempt(self, uow: UnitOfWork, execution: Execution, *, number: int) -> Attempt:
        attempt = Attempt(
            id=new_id(),
            execution_id=execution.id,
            task_id=execution.task_id,
            number=number,
            state=AttemptState.PENDING,
            created_at=self._clock.now(),
        )
        uow.attempts.add(attempt)
        record_event(
            uow,
            self._clock,
            EventKind.ATTEMPT_CREATED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=execution.id,
            attempt_id=attempt.id,
            payload={"number": number},
        )
        return attempt

    def _materialize_review_executions(self, uow: UnitOfWork) -> None:
        """A `review` execution the API asked for (04). Execution rows are fenced to the
        supervisor, so the request is an event and this materializes it."""
        for task in uow.tasks.list_by_state(TaskState.AWAITING_INTERNAL_REVIEW, for_update=True):
            requested = uow.events.latest_for_task_kind(
                task.id, EventKind.REVIEW_EXECUTION_REQUESTED.value
            )
            if requested is None:
                continue
            request = requested.payload
            contract_version = int(request.get("contract_version", task.contract_version))
            existing = uow.executions.list_for_task_by_role(task.id, ExecutionRole.REVIEW)
            # A failed review execution may be asked for again; a live or successful one
            # is the answer to this request.
            if any(
                e.contract_version == contract_version and e.state is not ExecutionState.FAILED
                for e in existing
            ):
                continue
            if any(
                e.contract_version == contract_version
                and e.state is ExecutionState.FAILED
                and e.created_at > requested.ts
                for e in existing
            ):
                continue
            policy = uow.policies.get(task.policy_name, task.policy_version)
            assert policy is not None
            now = self._clock.now()
            execution = Execution(
                id=new_id(),
                task_id=task.id,
                role=ExecutionRole.REVIEW,
                contract_version=contract_version,
                harness=str(request["harness"]),
                model=str(request["model"]),
                effort=request.get("effort"),
                provider=str(request["provider"]),
                image=str(request["image"]),
                policy_snapshot=policy.document,
                state=ExecutionState.CREATED,
                max_attempts=1,
                retry_on=[],
                timeout_seconds=int(request["timeout_seconds"]),
                created_at=now,
            )
            uow.executions.add(execution)
            record_event(
                uow,
                self._clock,
                EventKind.EXECUTION_CREATED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                payload={
                    "role": execution.role.value,
                    "harness": execution.harness,
                    "model": execution.model,
                    "provider": execution.provider,
                    "image": execution.image,
                    "head_sha": task.head_sha,
                    "note": "reviewer_must_not_be_author: a review never shares an attempt",
                },
            )
            self._create_attempt(uow, execution, number=1)

    # ----- step: gates, metrics, escalations, wakes -------------------------

    def _materialize_evidence(self) -> None:
        """`evidence` is fenced to the supervisor (14), so the rows a gate consumes are
        derived here from what the API recorded: uploaded artifacts and review reports.

        Idempotent: a row is written only when no evidence already points at the source."""
        with self._fenced() as uow:
            for state in (
                TaskState.REPORTED,
                TaskState.AWAITING_INTERNAL_REVIEW,
                TaskState.PRE_PR_GATES_FAILED,
                TaskState.AWAITING_ACCEPTANCE,
            ):
                for task in uow.tasks.list_by_state(state):
                    self._evidence_for_task(uow, task)
            uow.commit()

    def _evidence_for_task(self, uow: UnitOfWork, task: Task) -> None:
        existing = list(uow.evidence.list_for_task(task.id))
        seen_artifacts = {e.artifact_id for e in existing if e.artifact_id is not None}
        seen_reports = {
            str(e.payload.get("review_report_id"))
            for e in existing
            if e.kind == EvidenceKind.REVIEW_RECEIVED.value
        }
        work = latest_work_attempt(uow, task)
        if work is not None:
            attempt = work[0]
            for artifact in uow.artifacts.list_for_attempt(attempt.id):
                if artifact.type != "run_evidence" or artifact.id in seen_artifacts:
                    continue
                uow.evidence.add(
                    EvidenceRecord(
                        id=None,
                        attempt_id=attempt.id,
                        task_id=task.id,
                        kind=EvidenceKind.ARTIFACT_PRESENT.value,
                        observed_at=self._clock.now(),
                        source=EvidenceSource.CRUCIBLE.value,
                        verified=True,
                        payload={
                            "role": ROLE_RUN_EVIDENCE,
                            # The gate compares the name the contract asked for, not the
                            # content-addressed path the bytes landed at.
                            "path": artifact.filename,
                            "stored_at": artifact.path,
                            "size": artifact.size,
                            "uploaded_by": artifact.created_by,
                        },
                        artifact_id=artifact.id,
                    )
                )
        authors = author_attempt_ids(uow, task)
        for report in uow.review_reports.list_for_task(task.id):
            if report.id in seen_reports:
                continue
            uow.evidence.add(
                EvidenceRecord(
                    id=None,
                    attempt_id=report.reviewer_attempt_id,
                    task_id=task.id,
                    kind=EvidenceKind.REVIEW_RECEIVED.value,
                    observed_at=self._clock.now(),
                    source=EvidenceSource.CRUCIBLE.value,
                    verified=True,
                    payload=review_evidence_payload(
                        report,
                        reviewer_is_author=report.reviewer_attempt_id in authors,
                    ),
                    artifact_id=report.artifact_id,
                )
            )

    def _evaluate_pending_gates(self) -> None:
        """10 step 5: every task in `reported` with pending gates is evaluated.

        A task waiting in `awaiting_internal_review` is re-evaluated too: gate_results is
        fenced to the supervisor (14), so the API records the review report and the next
        tick is what resolves the gate."""
        for state in (TaskState.REPORTED, TaskState.AWAITING_INTERNAL_REVIEW):
            with self._fenced() as uow:
                for task in uow.tasks.list_by_state(state, for_update=True):
                    work = latest_work_attempt(uow, task)
                    if work is None:
                        continue
                    attempt, execution = work
                    if attempt.state not in ATTEMPT_TERMINAL:
                        continue
                    evaluate_and_advance(
                        uow, self._clock, task=task, attempt=attempt, execution=execution
                    )
                uow.commit()

    def _refresh_attempt_metrics(self) -> None:
        """Fold gate counts, corrections, and the acceptance verdict into AttemptMetrics.

        The API writes acceptance and contract versions but cannot write this fenced table
        (14), so the supervisor backfills it. Writing the same values twice changes
        nothing, which keeps reconciliation idempotent."""
        with self._fenced() as uow:
            # States whose metrics can still change. A closed or cancelled task is done
            # with, and rescanning it every tick would grow the tick without end.
            settled = (
                TaskState.AWAITING_ACCEPTANCE,
                TaskState.ACCEPTED,
                TaskState.PRE_PR_GATES_FAILED,
                TaskState.REJECTED,
            )
            for state in settled:
                for task in uow.tasks.list_by_state(state):
                    self._metrics_for_task(uow, task)
            uow.commit()

    def _metrics_for_task(self, uow: UnitOfWork, task: Task) -> None:
        acceptances = [a for a in uow.acceptance.list_for_task(task.id) if a.superseded_at is None]
        verdict = acceptances[-1].verdict.value if acceptances else None
        corrections = sum(
            1 for c in uow.contracts.list_for_task(task.id) if (c.document or {}).get("correction")
        )
        for execution in uow.executions.list_for_task(task.id):
            if execution.role is ExecutionRole.REVIEW:
                continue
            for attempt in uow.attempts.list_for_execution(execution.id):
                metrics = uow.attempt_metrics.get(attempt.id)
                if metrics is None:
                    continue
                gates = uow.gate_results.list_for_attempt(attempt.id)
                passed = sum(1 for g in gates if g.result == "pass")
                failed = sum(1 for g in gates if g.result in ("fail", "error"))
                after = sum(
                    1
                    for c in uow.contracts.list_for_task(task.id)
                    if (c.document or {}).get("correction")
                    and c.version > execution.contract_version
                )
                unchanged = (
                    metrics.gates_passed == passed
                    and metrics.gates_failed == failed
                    and metrics.corrections_after == after
                    and metrics.acceptance_verdict == verdict
                )
                if unchanged:
                    continue
                metrics.gates_passed = passed
                metrics.gates_failed = failed
                metrics.corrections_after = after
                metrics.acceptance_verdict = verdict
                uow.attempt_metrics.put(metrics)
                record_event(
                    uow,
                    self._clock,
                    EventKind.ATTEMPT_METRICS_RECORDED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={
                        "gates_passed": passed,
                        "gates_failed": failed,
                        "corrections_after": after,
                        "acceptance_verdict": verdict,
                        "corrections_total": corrections,
                    },
                )

    def _repeat_stale_escalations(self) -> None:
        with self._fenced() as uow:
            repeat_stale_escalation_wakes(
                uow, self._clock, stale_hours=self._escalation_stale_hours(uow)
            )
            uow.commit()

    def _escalation_stale_hours(self, uow: UnitOfWork) -> int:
        for escalation in uow.escalations.list_open():
            task = uow.tasks.get(escalation.task_id)
            if task is None:
                continue
            policy = uow.policies.get(task.policy_name, task.policy_version)
            if policy is not None:
                return int(
                    policy.document.get("limits", {}).get(
                        "escalation_stale_hours", DEFAULT_ESCALATION_STALE_HOURS
                    )
                )
        return DEFAULT_ESCALATION_STALE_HOURS

    async def _deliver_wakes(self) -> int:
        """10 step 8: redeliver every wake past its retry schedule. Poll is the fallback,
        so a missing or failing webhook only delays (17)."""
        if self._wakes is None or not self._wakes.configured:
            return 0
        delivered = 0
        for wake_id, body, retry_hours in await self._db(self._list_due_wakes):
            result = await self._wakes.deliver(body)
            await self._db(
                partial(self._record_wake_delivery, wake_id, result.ok, result.detail, retry_hours)
            )
            delivered += int(result.ok)
        return delivered

    def _list_due_wakes(self) -> list[tuple[str, bytes, int]]:
        out: list[tuple[str, bytes, int]] = []
        with self._uow_factory() as uow:
            for wake in uow.wakes.list_undelivered(self._clock.now()):
                policy_document = None
                if wake.task_id is not None:
                    task = uow.tasks.get(wake.task_id)
                    if task is not None:
                        policy = uow.policies.get(task.policy_name, task.policy_version)
                        policy_document = policy.document if policy else None
                out.append(
                    (wake.id, wake_body(uow, wake), retry_hours_from_policy(policy_document))
                )
        return out

    def _record_wake_delivery(self, wake_id: str, ok: bool, detail: str, retry_hours: int) -> None:
        with self._fenced() as uow:
            record_delivery(
                uow, self._clock, wake_id, ok=ok, detail=detail, retry_hours=retry_hours
            )
            uow.commit()

    # ----- step: launch pending attempts -------------------------------

    def _list_pending(self) -> list[_Pending]:
        out: list[_Pending] = []
        with self._uow_factory() as uow:
            for attempt in uow.attempts.list_in_states([AttemptState.PENDING]):
                execution = uow.executions.get(attempt.execution_id)
                task = uow.tasks.get(attempt.task_id)
                if execution is None or task is None:
                    continue
                if execution.role is ExecutionRole.REVIEW:
                    if task.state is not TaskState.AWAITING_INTERNAL_REVIEW:
                        continue
                elif task.state not in (TaskState.SCHEDULED, TaskState.RUNNING):
                    continue
                stored = uow.contracts.get(task.id, execution.contract_version)
                assert stored is not None
                repository = uow.repositories.get(task.repository_id)
                out.append(
                    _Pending(
                        attempt,
                        execution,
                        task,
                        stored.document,
                        repository.url if repository else "",
                    )
                )
        return out

    async def _launch_pending(self) -> int:
        launched = 0
        for item in await self._db(self._list_pending):
            with log_context(
                task_id=item.task.id, execution_id=item.execution.id, attempt_id=item.attempt.id
            ):
                try:
                    if await self._launch_one(item):
                        launched += 1
                except LeaseLostError:
                    raise
                except Exception:
                    log.exception("launch step failed; continuing with the next attempt")
        return launched

    def _build_spec(
        self,
        attempt: Attempt,
        execution: Execution,
        task: Task,
        contract: dict[str, Any],
        repository_url: str = "",
    ) -> LaunchSpec:
        env: dict[str, str] = {}
        if execution.role is ExecutionRole.REVIEW and task.head_sha:
            env["CRUCIBLE_REVIEW_HEAD_SHA"] = task.head_sha
        selected_harness = attempt.selected_harness or execution.harness
        selected_model = attempt.selected_model or execution.model
        selected_image = attempt.selected_image or execution.image
        effective_contract = copy.deepcopy(contract)
        if attempt.resume_from_remote:
            effective_contract.setdefault("repository", {})["resume_from_work_branch"] = True
        spec = LaunchSpec(
            attempt_id=attempt.id,
            task_id=task.id,
            external_id=task.external_id,
            role=execution.role.value,
            harness=selected_harness,
            model=selected_model,
            image=selected_image,
            timeout_seconds=execution.timeout_seconds,
            contract=effective_contract,
            env=env,
            network=contract.get("constraints", {}).get("network", "policy"),
            policy=execution.policy_snapshot or {},
            owner=task.principal_id,
            repository_url=repository_url,
            effort=execution.effort,
        )
        adapter = self._harnesses.get(selected_harness) if self._harnesses else None
        if adapter is None:
            return spec
        # 07: the adapter's launch shape. Argv carries the pointer; the identity and
        # the contract are files; a credential value is never in any of it.
        launch = adapter.build_launch(
            LaunchContext(
                attempt_id=attempt.id,
                model=selected_model,
                effort=execution.effort,
                timeout_seconds=execution.timeout_seconds,
                identity_mount=IDENTITY_MOUNT,
                report_mount=REPORT_MOUNT,
                repo_mount=REPO_MOUNT,
                credential_mounted=adapter.credential_spec() is not None,
            )
        )
        return replace(
            spec,
            command=tuple(launch.argv),
            env={**env, **launch.env},
            env_from_files=dict(launch.env_from_files),
            stdin_files=tuple(launch.stdin_files),
            stdin_text=launch.stdin_text,
            transcript_path=launch.transcript_path,
        )

    def _spec_for(self, attempt: Attempt) -> LaunchSpec | None:
        """Rebuild the launch spec from the database, for a collect after a restart."""
        with self._uow_factory() as uow:
            execution = uow.executions.get(attempt.execution_id)
            task = uow.tasks.get(attempt.task_id)
            if execution is None or task is None:
                return None
            stored = uow.contracts.get(task.id, execution.contract_version)
            if stored is None:
                return None
            repository = uow.repositories.get(task.repository_id)
            return self._build_spec(
                attempt, execution, task, stored.document, repository.url if repository else ""
            )

    @staticmethod
    def checkout_key(contract: dict[str, Any], external_id: str, repository_url: str = "") -> str:
        """One checkout lease per repository url and work branch (10)."""
        repository = contract.get("repository", {})
        url = repository_url or str(repository.get("url", "")) or str(repository.get("name", ""))
        branch = str(repository.get("work_branch") or f"crucible/{external_id}")
        return f"{url}#{branch}"

    def _harness_gate(self, execution: Execution) -> str | None:
        """07 and 25: the registry's answer for this execution's harness, as a refusal
        reason or None. Unknown and disabled names are refused with a wake."""
        if self._harnesses is None:
            return None
        with self._uow_factory() as uow:
            state = uow.harnesses.get(execution.harness)
        try:
            self._harnesses.resolve(execution.harness, gates=self._harness_gates, state=state)
        except HarnessUnavailableError as exc:
            return exc.reason
        return None

    def _harness_busy(self, execution: Execution) -> str | None:
        """05b: per-harness concurrency, which is 1 whenever the credential mounts
        rw-narrow (12). A launch over the limit waits; it is not a failure."""
        policy = execution.policy_snapshot or {}
        limit = int(
            (policy.get("concurrency", {}).get("per_harness") or {}).get(execution.harness, 1)
        )
        adapter = self._harnesses.get(execution.harness) if self._harnesses else None
        credential = adapter.credential_spec() if adapter is not None else None
        if credential is not None:
            source = self._credential_sources.get(execution.harness)
            if effective_mount_mode(credential, source) is MountMode.RW_NARROW:
                limit = 1
        with self._uow_factory() as uow:
            # An attempt holds its credential copy until collect has synced it back and
            # removed it, which is after `exited`: a second seeding before that is the
            # refresh race 12 gives as the reason for the cap.
            live = uow.attempts.list_in_states(
                [
                    AttemptState.PREPARING,
                    AttemptState.LAUNCHING,
                    AttemptState.RUNNING,
                    AttemptState.TERMINATING,
                    AttemptState.EXITED,
                ]
            )
            running = 0
            for other in live:
                other_execution = uow.executions.get(other.execution_id)
                if other_execution is not None and other_execution.harness == execution.harness:
                    running += 1
        if running >= limit:
            return f"{running} of {limit} {execution.harness} worker(s) already running"
        return None

    def _checkout_lease_free(self, attempt_id: str, key: str) -> bool:
        with self._uow_factory() as uow:
            held = uow.leases.get_checkout_lease(key)
        return held is None or held.holder == attempt_id or held.expires_at <= self._clock.now()

    def _eligible_harnesses(self, *, needs_credential: bool = True) -> set[str] | None:
        if self._harnesses is None:
            return None
        eligible: set[str] = set()
        with self._uow_factory() as uow:
            for name in self._harnesses.names():
                adapter = self._harnesses.get(name)
                if adapter is None:
                    continue
                source = self._credential_sources.get(name)
                if (
                    needs_credential
                    and adapter.credential_spec() is not None
                    and (source is None or not Path(source.path).is_dir())
                ):
                    continue
                try:
                    self._harnesses.resolve(
                        name, gates=self._harness_gates, state=uow.harnesses.get(name)
                    )
                except HarnessUnavailableError:
                    continue
                eligible.add(name)
        return eligible

    def _selection_for(self, uow: UnitOfWork, item: _Pending) -> Any:
        routing = load_routing(uow, item.execution.policy_snapshot or {})
        if routing is None:
            return None
        contract = TaskContractV1.model_validate(item.contract)
        request = contract.execution_request
        eligible = (
            None
            if request.pinned_model is not None
            else self._eligible_harnesses(needs_credential=request.provider.value != "fake")
        )
        selection = select_model(
            uow,
            routing,
            tier=request.tier.value,
            project=item.task.project,
            provider=request.provider.value,
            now=self._clock.now(),
            eligible_harnesses=eligible,
            pinned_model=request.pinned_model,
            pinned_harness=request.pinned_harness.value if request.pinned_harness else None,
        )
        if request.provider.value == "fake" and request.image and selection.selected is not None:
            selection = replace(selection, image=request.image)
        return selection

    def _preview_route(self, item: _Pending) -> Any:
        with self._uow_factory() as uow:
            return self._selection_for(uow, item)

    def _mark_routed_launching(self, item: _Pending) -> bool:
        with self._fenced() as uow:
            attempt = uow.attempts.get(item.attempt.id, for_update=True)
            task = uow.tasks.get(item.task.id, for_update=True)
            execution = uow.executions.get(item.execution.id, for_update=True)
            assert attempt is not None and task is not None and execution is not None
            if attempt.state is not AttemptState.PENDING or task.state not in (
                TaskState.SCHEDULED,
                TaskState.RUNNING,
            ):
                return False
            current = replace(item, attempt=attempt, execution=execution, task=task)
            selection = self._selection_for(uow, current)
            if selection is None or selection.selected is None or selection.image is None:
                self._enter_quota_wait(uow, task, attempt, execution, selection)
                uow.commit()
                return False
            chosen = selection.selected
            attempt.selected_model = chosen.id
            attempt.selected_harness = chosen.harness
            attempt.selected_image = selection.image
            attempt.selected_pool = chosen.pool
            attempt.ordered_candidates = list(selection.candidates)
            execution.model = chosen.id
            execution.harness = chosen.harness
            execution.image = selection.image
            uow.attempts.save(attempt)
            uow.executions.save(execution)
            move_attempt(
                uow, self._clock, attempt, AttemptState.PREPARING, EventKind.ATTEMPT_PREPARING
            )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_ROUTED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "tier": item.contract["execution_request"]["tier"],
                    "model": chosen.id,
                    "harness": chosen.harness,
                    "image": selection.image,
                    "pool": chosen.pool,
                    "ordered_candidates": list(selection.candidates),
                },
            )
            uow.commit()
            return True

    async def _launch_one(self, item: _Pending) -> bool:
        attempt, execution, task = item.attempt, item.execution, item.task
        review = execution.role is ExecutionRole.REVIEW
        if not review:
            selection = await self._db(partial(self._preview_route, item))
            if selection is None or selection.selected is None or selection.image is None:
                await self._db(partial(self._mark_routed_launching, item))
                return False
            attempt = replace(
                attempt,
                selected_model=selection.selected.id,
                selected_harness=selection.selected.harness,
                selected_image=selection.image,
                selected_pool=selection.selected.pool,
                ordered_candidates=list(selection.candidates),
            )
            execution = replace(
                execution,
                model=selection.selected.id,
                harness=selection.selected.harness,
                image=selection.image,
            )
            item = replace(item, attempt=attempt, execution=execution)
        refusal = await self._db(partial(self._harness_gate, execution))
        if refusal is not None:
            # The same path an environment failure at prepare takes: the attempt and the
            # execution become active first, so the refusal can end them.
            if await self._db(partial(self._mark_preparing, attempt.id)):
                await self._db(partial(self._refuse_launch, attempt.id, "registry", refusal))
            return False
        provider = self._provider(execution.provider)
        key = self.checkout_key(item.contract, task.external_id, item.repository_url)
        # 10 first, then 05b: an attempt whose checkout another attempt holds waits on
        # the lease and says so; only a launch that could take the checkout is held back
        # by the per-harness cap.
        if review or await self._db(partial(self._checkout_lease_free, attempt.id, key)):
            busy = await self._db(partial(self._harness_busy, execution))
            if busy is not None:
                await self._db(partial(self._defer_launch, attempt.id, busy))
                return False
        if not review and not await self._db(partial(self._take_checkout_lease, attempt.id, key)):
            # A second attempt on the same repository and branch waits; it is not a
            # failure, and nothing of the holder's checkout is disturbed (10).
            return False
        if review:
            if not await self._db(partial(self._mark_preparing, attempt.id)):
                return False
        elif not await self._db(partial(self._mark_routed_launching, item)):
            return False
        spec = self._build_spec(attempt, execution, task, item.contract, item.repository_url)
        try:
            ws = await provider.prepare(spec)
        except LaunchRefusedError as exc:
            await self._db(partial(self._refuse_launch, attempt.id, "prepare", str(exc)))
            return False
        except ProviderError as exc:
            detail = str(exc)
            await self._db(partial(self._environment_failure, attempt.id, "prepare", detail))
            return False
        self._workspaces[attempt.id] = ws
        await self._db(partial(self._record_prepared, attempt.id, ws))
        if not await self._db(partial(self._mark_launching, attempt.id, ws)):
            return False
        try:
            handle = await provider.launch(ws, spec)
        except LaunchRefusedError as exc:
            await self._discard(provider, ws, spec)
            await self._db(partial(self._refuse_launch, attempt.id, "launch", str(exc)))
            return False
        except ProviderError as exc:
            detail = str(exc)
            await self._discard(provider, ws, spec)
            await self._db(partial(self._environment_failure, attempt.id, "launch", detail))
            return False
        self._handles[attempt.id] = handle
        await self._db(partial(self._mark_running, attempt.id, handle))
        log.info("attempt launched", extra={"handle": handle.ref, "provider": provider.name})
        return True

    async def _discard(
        self, provider: ExecutionProvider, ws: Workspace | None, spec: LaunchSpec | None
    ) -> None:
        """12: an attempt that will never be collected still had a credential copy seeded
        for it; the provider removes it now, because cleanup only visits attempts whose
        logs were drained and retention never looks at a workspace it did not."""
        if ws is None:
            return
        try:
            await provider.discard(ws, spec)
        except Exception:  # the attempt still ends; the leak is logged, not hidden
            log.exception("credential copy discard failed", extra={"attempt_id": ws.attempt_id})

    def _mark_preparing(self, attempt_id: str) -> bool:
        """Begin the launch, unless the task was cancelled after the attempt was listed."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            execution = uow.executions.get(attempt.execution_id, for_update=True)
            assert task is not None and execution is not None
            review = execution.role is ExecutionRole.REVIEW
            allowed = (
                (TaskState.AWAITING_INTERNAL_REVIEW,)
                if review
                else (TaskState.SCHEDULED, TaskState.RUNNING)
            )
            if attempt.state is not AttemptState.PENDING or task.state not in allowed:
                return False
            move_attempt(
                uow, self._clock, attempt, AttemptState.PREPARING, EventKind.ATTEMPT_PREPARING
            )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            if not review and task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            uow.commit()
            return True

    def _mark_launching(self, attempt_id: str, ws: Workspace) -> bool:
        """Reserve the quota pool and move to launching in one fenced transaction (05b).

        A pool that crossed its soft limit since submit refuses the attempt with class
        quota_exhausted and wakes Foundry."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            reservation = reserve(
                uow,
                execution.policy_snapshot or {},
                harness=execution.harness,
                model_id=execution.model,
                now=self._clock.now(),
            )
            if not reservation.ok:
                task = uow.tasks.get(attempt.task_id, for_update=True)
                assert task is not None
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = self._clock.now()
                record_event(
                    uow,
                    self._clock,
                    EventKind.QUOTA_EXHAUSTED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"pool": reservation.pool, "detail": reservation.detail},
                )
                create_wake(
                    uow,
                    self._clock,
                    principal_id=task.principal_id,
                    reason=WakeReason.QUOTA_EXHAUSTED,
                    summary=reservation.detail,
                    task=task,
                    attempt_id=attempt.id,
                )
                move_attempt(
                    uow,
                    self._clock,
                    attempt,
                    AttemptState.COLLECTED,
                    EventKind.ATTEMPT_COLLECTED,
                    payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
                )
                self._classify_and_finish(uow, attempt, None)
                uow.commit()
                return False
            self._ensure_metrics(uow, attempt, execution, reservation)
            record_event(
                uow,
                self._clock,
                EventKind.QUOTA_RESERVED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={"pool": reservation.pool, "detail": reservation.detail},
            )
            attempt.workspace_path = ws.checkout_path.removesuffix("/repo")
            attempt.identity_sha256 = ws.identity_sha256 or attempt.identity_sha256
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.LAUNCHING,
                EventKind.ATTEMPT_LAUNCHING,
                payload={
                    "workspace": attempt.workspace_path,
                    "model": attempt.selected_model,
                    "harness": attempt.selected_harness,
                    "image": attempt.selected_image,
                    "pool": attempt.selected_pool,
                    "ordered_candidates": list(attempt.ordered_candidates),
                },
            )
            uow.commit()
            return True

    def _record_launch_workspace(self, attempt_id: str, ws: Workspace) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.workspace_path = ws.checkout_path.removesuffix("/repo")
            attempt.identity_sha256 = ws.identity_sha256 or attempt.identity_sha256
            uow.attempts.save(attempt)
            uow.commit()

    def _ensure_metrics(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, reservation: Any
    ) -> None:
        if uow.attempt_metrics.get(attempt.id) is not None:
            return
        uow.attempt_metrics.put(
            AttemptMetrics(
                attempt_id=attempt.id,
                task_id=attempt.task_id,
                model=execution.model,
                harness=execution.harness,
                endpoint_kind=reservation.endpoint_kind,
                pool=reservation.pool,
                cost_source="none",
                created_at=self._clock.now(),
            )
        )

    def _mark_running(self, attempt_id: str, handle: Handle) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            now = self._clock.now()
            if handle.image_digest and attempt.image_digest != handle.image_digest:
                # 13: every attempt records the image digest it ran, resolved at launch.
                attempt.image_digest = handle.image_digest
                record_event(
                    uow,
                    self._clock,
                    EventKind.IMAGE_RESOLVED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"image": execution.image, "digest": handle.image_digest},
                )
            attempt.handle = handle.ref
            attempt.started_at = now
            attempt.timeout_at = now + timedelta(seconds=execution.timeout_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.RUNNING,
                EventKind.ATTEMPT_RUNNING,
                payload={"handle": handle.ref, "timeout_at": attempt.timeout_at.isoformat()},
            )
            assert self.fenced_token is not None
            uow.leases.upsert_attempt_lease(
                attempt.id, self.holder, self.fenced_token, now, self.attempt_lease_ttl_seconds
            )
            uow.commit()

    def _environment_failure(self, attempt_id: str, stage: str, detail: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.exit_class = ExitClass.ENVIRONMENT
            attempt.ended_at = self._clock.now()
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"stage": stage, "detail": detail, "exit_class": ExitClass.ENVIRONMENT},
            )
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _defer_launch(self, attempt_id: str, detail: str) -> None:
        """The attempt stays pending; one event says why it did not launch this tick."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None
            latest = uow.events.latest_for_task_kind(
                attempt.task_id, EventKind.HARNESS_LAUNCH_DEFERRED.value
            )
            if latest is not None and latest.payload.get("attempt_id") == attempt.id:
                return
            record_event(
                uow,
                self._clock,
                EventKind.HARNESS_LAUNCH_DEFERRED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"attempt_id": attempt.id, "detail": detail},
            )
            uow.commit()

    def _refuse_launch(self, attempt_id: str, stage: str, detail: str) -> None:
        """07, 13, 25: an unknown or disabled harness, a version outside the tested
        range, or a missing credential. A refusal, never a warning: the attempt ends as
        an environment failure that does not retry, and Foundry is woken."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            execution = uow.executions.get(attempt.execution_id)
            assert task is not None and execution is not None
            attempt.termination_reason = TERMINATION_REFUSED
            record_event(
                uow,
                self._clock,
                EventKind.HARNESS_REFUSED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"harness": execution.harness, "stage": stage, "detail": detail[:1000]},
            )
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.HARNESS_UNAVAILABLE,
                summary=f"launch refused for harness {execution.harness}: {detail[:500]}",
                task=task,
                attempt_id=attempt.id,
                extra_links={"harnesses": "/v1/harnesses"},
            )
            record_launch_outcome(
                uow,
                self._clock,
                name=execution.harness,
                outcome="refused",
                at=self._clock.now(),
            )
            attempt.exit_class = ExitClass.ENVIRONMENT
            attempt.ended_at = self._clock.now()
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"stage": stage, "detail": detail, "exit_class": ExitClass.ENVIRONMENT},
            )
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _record_bare_evidence(self, uow: UnitOfWork, attempt: Attempt) -> None:
        """An attempt that produced nothing still records its exit, so the gates that
        read it fail rather than wait (09, 11)."""
        task = uow.tasks.get(attempt.task_id)
        if task is None:
            return
        execution = uow.executions.get(attempt.execution_id)
        if execution is not None and execution.role is ExecutionRole.REVIEW:
            return
        record_collection_evidence(
            uow,
            self._clock,
            self._artifacts,
            attempt=attempt,
            task=task,
            outputs=CollectedOutputs(report=None, report_raw=None, blocked_md=None),
            claim=None,
            claim_parsed_ok=False,
            parse_errors=[],
        )
        self._record_wall_time(uow, attempt)

    # ----- checkout lease, workspace, logs, cleanup, retention -------------

    def _take_checkout_lease(self, attempt_id: str, key: str) -> bool:
        """One attempt at a time per repository and work branch (10). The holder is the
        attempt, so a takeover by another supervisor does not hand the checkout over."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None and self.fenced_token is not None
            lease = uow.leases.acquire_checkout_lease(
                key,
                attempt_id,
                self.fenced_token,
                self._clock.now(),
                self.checkout_lease_ttl_seconds,
            )
            if lease is None:
                held = uow.leases.get_checkout_lease(key)
                existing = held.holder if held else "unknown"
                last = uow.events.latest_for_task_kind(
                    attempt.task_id, EventKind.CHECKOUT_LEASE_DENIED
                )
                if last is None or last.payload.get("attempt_id") != attempt_id:
                    record_event(
                        uow,
                        self._clock,
                        EventKind.CHECKOUT_LEASE_DENIED,
                        principal=PRINCIPAL_CRUCIBLE,
                        task_id=attempt.task_id,
                        execution_id=attempt.execution_id,
                        attempt_id=attempt_id,
                        payload={"key": key, "held_by": existing, "attempt_id": attempt_id},
                    )
                uow.commit()
                return False
            record_event(
                uow,
                self._clock,
                EventKind.CHECKOUT_LEASE_TAKEN,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"key": key},
            )
            uow.commit()
            return True

    def _release_checkout_leases(self, uow: UnitOfWork, attempt: Attempt) -> None:
        for lease in uow.leases.list_checkout_leases():
            if lease.holder != attempt.id:
                continue
            if uow.leases.release_checkout_lease(lease.key, attempt.id):
                record_event(
                    uow,
                    self._clock,
                    EventKind.CHECKOUT_LEASE_RELEASED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"key": lease.key},
                )

    def _record_prepared(self, attempt_id: str, ws: Workspace) -> None:
        """08 wants it recorded as an event which branch the checkout started from."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id)
            assert attempt is not None
            record_event(
                uow,
                self._clock,
                EventKind.WORKSPACE_PREPARED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={
                    "work_branch": ws.work_branch,
                    "started_from": ws.started_from,
                    "identity_sha256": ws.identity_sha256,
                },
            )
            uow.commit()

    async def _pull_logs(
        self, attempt: Attempt, provider: ExecutionProvider, handle: Handle
    ) -> int:
        """One log pull, appended and the resume position advanced (10)."""
        offset = LogOffset(
            timestamp=attempt.log_resume_ts.isoformat() if attempt.log_resume_ts else None,
            line_sha256=attempt.log_resume_sha256,
            occurrence=attempt.log_resume_occurrence,
        )
        try:
            chunks = await provider.logs(handle, offset)
        except ProviderError as exc:
            log.warning("log pull failed (%s); the next tick tries again", exc)
            return 0
        if not chunks:
            return 0
        return int(await self._db(partial(self._store_logs, attempt.id, tuple(chunks))))

    def _store_logs(self, attempt_id: str, chunks: tuple[LogChunk, ...]) -> int:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None:
                return 0
            offset = uow.logs.last_offset(attempt_id)
            stored = 0
            for chunk in chunks:
                if not chunk.content:
                    continue
                # 12: provider log capture passes through the redaction filter before
                # storage. The resume position keeps the hash of the raw line, which is
                # what the daemon's stream is compared against on the next pull.
                text = chunk.content.decode("utf-8", "replace")
                cleaned = redact(text)
                content = chunk.content if cleaned == text else cleaned.encode("utf-8")
                end = offset + len(content)
                uow.logs.append(
                    LogChunkRecord(
                        id=None,
                        attempt_id=attempt_id,
                        stream=chunk.stream,
                        offset_start=offset,
                        offset_end=end,
                        ts=chunk.ts or self._clock.now(),
                        line_sha256=chunk.line_sha256 or "",
                        occurrence=chunk.occurrence,
                        content=content,
                    )
                )
                offset = end
                stored += 1
                if chunk.ts is not None and chunk.line_sha256:
                    attempt.log_resume_ts = chunk.ts
                    attempt.log_resume_sha256 = chunk.line_sha256
                    attempt.log_resume_occurrence = chunk.occurrence
            if stored:
                uow.attempts.save(attempt)
            uow.commit()
            return stored

    def _mark_logs_drained(self, attempt_id: str) -> None:
        """The final pull after exit. Cleanup never runs before this (08, 10)."""
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.logs_drained_at is not None:
                return
            attempt.logs_drained_at = self._clock.now()
            uow.attempts.save(attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_LOGS_DRAINED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"chunks": len(uow.logs.list_for_attempt(attempt_id, limit=10_000))},
            )
            uow.commit()

    def _list_cleanup_due(self) -> list[tuple[Attempt, str, str]]:
        """Attempts whose logs are drained, that are done, and that are not cleaned."""
        out: list[tuple[Attempt, str, str]] = []
        with self._uow_factory() as uow:
            states = [
                AttemptState.COLLECTED,
                AttemptState.SUCCEEDED,
                AttemptState.BLOCKED,
                AttemptState.FAILED,
            ]
            for attempt in uow.attempts.list_in_states(states):
                if attempt.logs_drained_at is None or attempt.cleaned_up_at is not None:
                    continue
                execution = uow.executions.get(attempt.execution_id)
                if execution is None:
                    continue
                cleanup = (execution.policy_snapshot or {}).get("cleanup", {})
                succeeded = attempt.state is AttemptState.SUCCEEDED
                choice = str(
                    cleanup.get("workspace_on_success" if succeeded else "workspace_on_failure")
                    or ("keep_diff_only" if succeeded else "keep")
                )
                out.append((attempt, execution.provider, choice))
        return out

    async def _cleanup_step(self) -> int:
        """08: remove the container, keep or delete the workspace per policy, release
        the checkout lease, and record it. Only ever after `logs_drained`."""
        cleaned = 0
        for attempt, provider_name, choice in await self._db(self._list_cleanup_due):
            try:
                provider = self._provider(provider_name)
            except ProviderError:
                continue
            policy = {
                "delete": CleanupPolicy.DELETE,
                "keep": CleanupPolicy.KEEP,
                "keep_diff_only": CleanupPolicy.KEEP_DIFF_ONLY,
            }.get(choice, CleanupPolicy.KEEP)
            try:
                spec = await self._db(partial(self._spec_for, attempt))
                await provider.cleanup(self._workspace_for(attempt), policy, spec)
            except ProviderError:
                log.exception("cleanup failed; the next tick tries again")
                continue
            await self._db(partial(self._mark_cleaned, attempt.id, choice))
            self._workspaces.pop(attempt.id, None)
            self._handles.pop(attempt.id, None)
            cleaned += 1
        return cleaned

    def _mark_cleaned(self, attempt_id: str, choice: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            if attempt is None or attempt.cleaned_up_at is not None:
                return
            attempt.cleaned_up_at = self._clock.now()
            uow.attempts.save(attempt)
            self._release_checkout_leases(uow, attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_CLEANED_UP,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt_id,
                payload={"workspace": choice},
            )
            uow.commit()

    async def _retention_step(self) -> int:
        """16: deterministic, idempotent, and every deletion an event and a row."""
        applied = await self._db(self._retention_sweep)
        keep = await self._db(self._live_attempt_ids)
        for provider in self._providers.values():
            try:
                applied += await provider.retention(keep)
            except ProviderError:
                log.warning("provider retention failed; the next tick tries again")
        return applied

    def _live_attempt_ids(self) -> list[str]:
        with self._uow_factory() as uow:
            return [
                attempt.id
                for attempt in uow.attempts.list_in_states(
                    [
                        AttemptState.PENDING,
                        AttemptState.PREPARING,
                        AttemptState.LAUNCHING,
                        AttemptState.RUNNING,
                        AttemptState.TERMINATING,
                        AttemptState.EXITED,
                        AttemptState.COLLECTED,
                    ]
                )
            ]

    def _retention_for(self, uow: UnitOfWork, task: Task | None) -> tuple[dict[str, Any], str, int]:
        """The retention section of the policy that governs this task, and its version.

        Every deletion names the policy version that authorized it (16), so the window
        comes from the task's own policy, never from a global default."""
        if task is None:
            return {}, "unknown", 0
        policy = uow.policies.get(task.policy_name, task.policy_version)
        section = dict((policy.document if policy else {}).get("retention", {}))
        return section, task.policy_name, task.policy_version

    def _retention_sweep(self) -> int:
        with self._fenced() as uow:
            now = self._clock.now()
            applied = 0
            if self._credential_sweep is not None:
                applied += self._credential_sweep(uow)

            def act(
                kind: str, subject: str, name: str, version: int, detail: dict[str, Any]
            ) -> bool:
                row = uow.retention.record(
                    RetentionAction(
                        id=new_id(),
                        kind=kind,
                        subject=subject,
                        policy_name=name,
                        policy_version=version,
                        acted_at=now,
                        detail=detail,
                    )
                )
                if row is None:
                    return False
                record_event(
                    uow,
                    self._clock,
                    EventKind.RETENTION_APPLIED,
                    principal=PRINCIPAL_CRUCIBLE,
                    payload={"kind": kind, "subject": subject, **detail},
                )
                return True

            for attempt_id in uow.logs.attempts_with_logs_before(now, RETENTION_BATCH):
                attempt = uow.attempts.get(attempt_id)
                if attempt is None:
                    continue
                task = uow.tasks.get(attempt.task_id)
                section, name, version = self._retention_for(uow, task)
                days = int(section.get("logs_and_transcripts_days") or DEFAULT_LOG_RETENTION_DAYS)
                newest = uow.logs.attempts_with_logs_before(
                    now - timedelta(days=days), RETENTION_BATCH
                )
                if attempt_id not in newest:
                    continue
                removed = uow.logs.delete_for_attempts([attempt_id])
                if act("logs", attempt_id, name, version, {"chunks": removed, "days": days}):
                    applied += 1

            floor = now - timedelta(days=1)
            for wake in uow.wakes.list_acked_before(floor, RETENTION_BATCH):
                task = uow.tasks.get(wake.task_id) if wake.task_id else None
                section, name, version = self._retention_for(uow, task)
                days = int(section.get("wakes_after_ack_days") or DEFAULT_WAKE_RETENTION_DAYS)
                if wake.acked_at is None or wake.acked_at > now - timedelta(days=days):
                    continue
                uow.wakes.delete(wake.id)
                if act("wake", wake.id, name, version, {"days": days}):
                    applied += 1
            uow.commit()
            return applied

    # ----- step: observe ---------------------------------------------------

    def _list_live(self) -> list[Attempt]:
        with self._uow_factory() as uow:
            return list(
                uow.attempts.list_in_states(
                    [
                        AttemptState.PREPARING,
                        AttemptState.LAUNCHING,
                        AttemptState.RUNNING,
                        AttemptState.TERMINATING,
                    ]
                )
            )

    async def _observe_attempts(self) -> tuple[int, int]:
        observed = finished = 0
        for attempt in await self._db(self._list_live):
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                observed += 1
                try:
                    if await self._observe_one(attempt):
                        finished += 1
                except LeaseLostError:
                    raise
                except Exception:
                    log.exception("observe step failed; continuing with the next attempt")
        return observed, finished

    async def _observe_one(self, attempt: Attempt) -> bool:
        if attempt.state in (AttemptState.PREPARING, AttemptState.LAUNCHING):
            return await self._reconcile_stranded(attempt)
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        handle = self._handles.get(attempt.id) or Handle(
            provider=provider_name, ref=attempt.handle or "", attempt_id=attempt.id
        )
        self._handles[attempt.id] = handle
        observation = await provider.observe(handle)
        now = self._clock.now()
        if observation.state is ObservationState.RUNNING:
            if attempt.drain_deadline is not None and now >= attempt.drain_deadline:
                # Past the grace window (timeout or cancel): kill, and keep killing every
                # tick until the provider stops seeing it. The event is written once.
                await provider.terminate(handle, "kill")
                if attempt.killed_at is None:
                    await self._db(partial(self._record_kill, attempt.id))
                else:
                    log.warning("worker survived kill; retrying", extra={"handle": handle.ref})
                return False
            if (
                attempt.state is AttemptState.RUNNING
                and attempt.timeout_at is not None
                and attempt.drain_deadline is None
                and now >= attempt.timeout_at
            ):
                await provider.terminate(handle, "drain")
                await self._db(partial(self._record_drain, attempt.id, TERMINATION_TIMEOUT))
                return False
            # Log bytes advancing is a heartbeat signal (10); the pull is also what
            # keeps the stored stream current for a live tail.
            await self._pull_logs(attempt, provider, handle)
            await self._db(partial(self._renew_attempt_lease, attempt.id))
            return False
        if observation.state is ObservationState.LOST:
            # Nothing more can arrive from a worker the provider cannot see, and its
            # credential copy will never be synced: remove it now (12).
            await self._discard(
                provider,
                self._workspace_for(attempt),
                await self._db(partial(self._spec_for, attempt)),
            )
            await self._db(partial(self._mark_logs_drained, attempt.id))
            await self._db(partial(self._finish_lost, attempt.id, observation.detail))
            return True
        # The final drain before anything is collected or cleaned up (08, 10).
        await self._pull_logs(attempt, provider, handle)
        await self._db(partial(self._mark_logs_drained, attempt.id))
        spec = await self._db(partial(self._spec_for, attempt))
        collection_error: str | None = None
        try:
            outputs = await provider.collect(handle, self._workspace_for(attempt), spec)
        except ProviderError as exc:
            # 16: a provider that failed while producing the outputs is an environment
            # failure. The attempt still finishes, with nothing collected, so the next
            # tick does not try the same collection again forever.
            collection_error = str(exc)
            outputs = CollectedOutputs(report=None, report_raw=None, blocked_md=None)
            log.warning("collection failed (%s); the attempt fails as environment", exc)
        await self._db(
            partial(
                self._finish_exited,
                attempt.id,
                observation.exit_code,
                outputs,
                collection_error,
                observation.oom_killed,
                defer_quota=True,
            )
        )
        if await self._db(partial(self._quota_checkpoint_pending, attempt.id)):
            repository_url = spec.repository_url if spec is not None else ""
            required = provider_name == "docker" and not (
                repository_url.startswith("/") or repository_url.startswith("file://")
            )
            pushed, detail = await self.delivery.push_quota_checkpoint(
                attempt.id, required=required
            )
            await self._db(partial(self._finish_deferred_quota, attempt.id, pushed, detail))
        self._handles.pop(attempt.id, None)
        self._workspaces.pop(attempt.id, None)
        return True

    def _quota_checkpoint_pending(self, attempt_id: str) -> bool:
        with self._uow_factory() as uow:
            attempt = uow.attempts.get(attempt_id)
            if attempt is None or attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED:
                return False
            execution = uow.executions.get(attempt.execution_id)
            task = uow.tasks.get(attempt.task_id)
            return bool(
                execution is not None
                and execution.state is ExecutionState.ACTIVE
                and task is not None
                and task.state is TaskState.RUNNING
            )

    def _finish_deferred_quota(self, attempt_id: str, pushed: bool, detail: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            execution = uow.executions.get(attempt.execution_id, for_update=True)
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert execution is not None and task is not None
            if (
                attempt.exit_class is not ExitClass.QUOTA_EXHAUSTED
                or execution.state is not ExecutionState.ACTIVE
                or task.state is not TaskState.RUNNING
            ):
                return
            if pushed:
                self._handle_quota_exit(uow, task, execution, attempt)
            else:
                record_event(
                    uow,
                    self._clock,
                    EventKind.TASK_PUBLISH_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={
                        "step": "quota_checkpoint",
                        "detail": detail[:1000],
                        "head_sha": task.head_sha,
                    },
                )
                move_execution(
                    uow,
                    self._clock,
                    execution,
                    ExecutionState.FAILED,
                    EventKind.EXECUTION_FAILED,
                    payload={"exit_class": "quota_exhausted", "checkpoint_push": "failed"},
                )
                self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
            uow.commit()

    async def _reconcile_stranded(self, attempt: Attempt) -> bool:
        """An attempt still in preparing or launching after the launch step ran was left
        there by a supervisor that died mid-launch. If the provider can see a worker for
        it, adopt it as running; otherwise collect it as environment so the retry rule
        applies (10, 16)."""
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        handle = self._handles.get(attempt.id)
        if handle is None:
            discovered = {h.attempt_id: h for h in await provider.reconcile()}
            handle = discovered.get(attempt.id)
        if handle is None and attempt.handle is not None:
            handle = Handle(provider=provider_name, ref=attempt.handle, attempt_id=attempt.id)
        if handle is not None:
            observation = await provider.observe(handle)
            if observation.state is not ObservationState.LOST:
                self._handles[attempt.id] = handle
                await self._db(partial(self._adopt, attempt.id, handle))
                return False
        await self._db(
            partial(
                self._environment_failure,
                attempt.id,
                "reconcile",
                "attempt stranded in launch with no worker the provider can see",
            )
        )
        return True

    def _adopt(self, attempt_id: str, handle: Handle) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state not in (AttemptState.PREPARING, AttemptState.LAUNCHING):
                return
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            now = self._clock.now()
            if attempt.state is AttemptState.PREPARING:
                move_attempt(
                    uow, self._clock, attempt, AttemptState.LAUNCHING, EventKind.ATTEMPT_LAUNCHING
                )
            attempt.handle = handle.ref
            attempt.started_at = attempt.started_at or now
            attempt.timeout_at = attempt.started_at + timedelta(seconds=execution.timeout_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.RUNNING,
                EventKind.ATTEMPT_ADOPTED,
                payload={"handle": handle.ref},
            )
            assert self.fenced_token is not None
            uow.leases.upsert_attempt_lease(
                attempt.id, self.holder, self.fenced_token, now, self.attempt_lease_ttl_seconds
            )
            uow.commit()

    def _renew_attempt_lease(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            assert self.fenced_token is not None
            # Attempt leases are not a fenced table; check the supervisor lease explicitly.
            if not uow.leases.verify_supervisor(self.holder, self.fenced_token):
                self.fenced_token = None
                raise LeaseLostError("supervisor lease changed hands")
            uow.leases.upsert_attempt_lease(
                attempt_id,
                self.holder,
                self.fenced_token,
                self._clock.now(),
                self.attempt_lease_ttl_seconds,
            )
            uow.commit()

    def _record_drain(self, attempt_id: str, reason: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            now = self._clock.now()
            attempt.drain_deadline = now + timedelta(seconds=self.grace_seconds)
            attempt.termination_reason = reason
            uow.attempts.save(attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_TIMEOUT_DRAIN,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={
                    "reason": reason,
                    "drain_deadline": attempt.drain_deadline.isoformat(),
                    "grace_seconds": self.grace_seconds,
                },
            )
            uow.commit()

    def _record_kill(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            attempt.killed_at = self._clock.now()
            uow.attempts.save(attempt)
            kind = (
                EventKind.ATTEMPT_CANCEL_KILL
                if attempt.termination_reason == TERMINATION_CANCEL
                else EventKind.ATTEMPT_TIMEOUT_KILL
            )
            record_event(
                uow,
                self._clock,
                kind,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=attempt.task_id,
                execution_id=attempt.execution_id,
                attempt_id=attempt.id,
                payload={"reason": attempt.termination_reason},
            )
            uow.commit()

    def _finish_lost(self, attempt_id: str, detail: str | None) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            now = self._clock.now()
            attempt.ended_at = now
            attempt.exit_class = ExitClass.LOST
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.EXITED,
                EventKind.ATTEMPT_LOST,
                payload={"detail": detail, "last_handle": attempt.handle},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"report_present": False, "blocked_present": False},
            )
            uow.leases.release_attempt_lease(attempt.id)
            self._record_bare_evidence(uow, attempt)
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _finish_exited(
        self,
        attempt_id: str,
        exit_code: int | None,
        outputs: CollectedOutputs,
        collection_error: str | None = None,
        oom_killed: bool = False,
        *,
        defer_quota: bool = False,
    ) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            attempt.exit_code = exit_code
            attempt.ended_at = self._clock.now()
            timed_out = attempt.termination_reason == TERMINATION_TIMEOUT
            killed = attempt.termination_reason == TERMINATION_CANCEL
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            # 07: a report file that is present but does not parse is a parse failure,
            # recorded as one; only a missing file is "without report".
            report_present = outputs.report_raw is not None or outputs.report is not None
            exit_info = ExitInfo(
                exit_code=exit_code,
                report_present=report_present,
                blocked_present=outputs.blocked_md is not None,
                oom_killed=oom_killed,
                timed_out=timed_out,
                killed=killed,
            )
            adapter = self._harnesses.get(execution.harness) if self._harnesses else None
            if adapter is not None:
                # 07 and S5: the adapter classifies from the code and both tails.
                attempt.exit_class = adapter.classify_exit(
                    exit_info, outputs.stdout_tail, outputs.stderr_tail
                )
            else:
                attempt.exit_class = classify_exit(
                    exit_code=exit_code,
                    report_present=report_present,
                    blocked_present=outputs.blocked_md is not None,
                    timed_out=timed_out,
                    killed=killed,
                )
                if oom_killed and not (timed_out or killed):
                    attempt.exit_class = ExitClass.ENVIRONMENT
            if attempt.exit_class is ExitClass.QUOTA_EXHAUSTED:
                reset_at = (
                    adapter.quota_reset_at(outputs.stdout_tail, outputs.stderr_tail)
                    if adapter is not None
                    else None
                )
                self._mark_pool_exhausted(uow, attempt, execution, reset_at)
            parsed: ParsedReport | None = None
            if adapter is not None and attempt.workspace_path:
                report_dir = Path(attempt.workspace_path) / "output" / "report"
                if report_dir.is_dir():
                    parsed = adapter.parse_report(report_dir, exit_info)
            self._record_credential_sync(uow, attempt, execution, outputs)
            if collection_error is not None:
                # Whatever the worker's own exit said, Crucible has no outputs from it.
                attempt.exit_class = ExitClass.ENVIRONMENT
                record_event(
                    uow,
                    self._clock,
                    EventKind.COLLECTION_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"detail": collection_error[:1000], "exit_code": exit_code},
                )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.EXITED,
                EventKind.ATTEMPT_EXITED,
                payload={
                    "exit_code": exit_code,
                    "exit_class": attempt.exit_class.value,
                    "termination_reason": attempt.termination_reason,
                    "oom_killed": oom_killed,
                },
            )
            if execution.role is ExecutionRole.REVIEW:
                self._finish_review_attempt(uow, attempt, outputs)
                uow.leases.release_attempt_lease(attempt.id)
                uow.commit()
                return
            claim_ok = False
            cancelled = attempt.termination_reason == TERMINATION_CANCEL
            if outputs.report is None and outputs.report_raw is not None and not cancelled:
                # The file exists and is not a YAML mapping (a bare colon in a value is
                # the usual cause). The adapter's errors say so; nothing of it is stored.
                record_event(
                    uow,
                    self._clock,
                    EventKind.REPORT_PARSE_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={
                        "errors": (
                            parsed.errors
                            if parsed is not None and parsed.errors
                            else [
                                {"loc": [], "msg": "report.yaml is not a mapping", "type": "yaml"}
                            ]
                        )
                    },
                )
            if outputs.report is not None and not cancelled:
                claim, errors = parse_claim(outputs.report)
                claim_ok = claim is not None
                secret_hits = find_secrets(outputs.report)
                if secret_hits:
                    errors = errors + [
                        {"loc": [m.path], "msg": f"secret pattern {m.pattern}", "type": "secret"}
                        for m in secret_hits
                    ]
                    claim_ok = False
                    document: dict[str, Any] = {"redacted": True}
                else:
                    document = outputs.report
                uow.claims.put(
                    CompletionClaimRecord(
                        attempt_id=attempt.id,
                        document=document,
                        parsed_ok=claim_ok,
                        parse_errors=errors,
                    )
                )
                record_event(
                    uow,
                    self._clock,
                    EventKind.REPORT_PARSED if claim_ok else EventKind.REPORT_PARSE_FAILED,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=attempt.task_id,
                    execution_id=attempt.execution_id,
                    attempt_id=attempt.id,
                    payload={"errors": errors} if errors else {"schema": "CompletionClaimV1"},
                )
            blocked_text: str | None = None
            if outputs.blocked_md is not None:
                blocked_text = (
                    "[redacted: secret pattern]"
                    if find_secrets(outputs.blocked_md)
                    else outputs.blocked_md
                )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={
                    "report_present": report_present,
                    "report_parsed": claim_ok,
                    "partial_report_kept_unparsed": cancelled and outputs.report is not None,
                    "blocked_present": outputs.blocked_md is not None,
                },
            )
            uow.leases.release_attempt_lease(attempt.id)
            claim_document = outputs.report if (outputs.report and not cancelled) else None
            stored_claim = uow.claims.get(attempt.id) if claim_document else None
            errors = list(stored_claim.parse_errors) if stored_claim else []
            head = record_collection_evidence(
                uow,
                self._clock,
                self._artifacts,
                attempt=attempt,
                task=task,
                outputs=outputs,
                claim=claim_document,
                claim_parsed_ok=claim_ok,
                parse_errors=errors,
            )
            if head:
                task.head_sha = head
                task.updated_at = self._clock.now()
                uow.tasks.save(task)
            self._record_wall_time(uow, attempt)
            if parsed is not None:
                self._record_harness_metrics(uow, attempt, parsed)
                if parsed.progress:
                    ingest_progress(
                        uow,
                        self._clock,
                        attempt_id=attempt.id,
                        task_id=attempt.task_id,
                        execution_id=attempt.execution_id,
                        progress=parsed.progress,
                    )
            self._classify_and_finish(
                uow, attempt, blocked_text, claim_ok=claim_ok, defer_quota=defer_quota
            )
            uow.commit()

    def _record_credential_sync(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, outputs: CollectedOutputs
    ) -> None:
        """12 and 25: what the sync-back did, as an event and on the harness row. Names,
        booleans and reasons; never a value."""
        now = self._clock.now()
        auth_failure = attempt.exit_class is ExitClass.AUTH_FAILURE
        record_launch_outcome(
            uow,
            self._clock,
            name=execution.harness,
            outcome=attempt.exit_class.value if attempt.exit_class else "unknown",
            at=now,
            auth_failure=auth_failure,
        )
        sync = outputs.credential_sync
        if sync is None:
            return
        record_event(
            uow,
            self._clock,
            EventKind.CREDENTIAL_SYNCED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload=sync.as_dict(),
        )
        record_credential_observation(
            uow,
            self._clock,
            name=execution.harness,
            mount_mode=MountMode(sync.mount_mode),
            changed=sync.changed,
            at=now,
        )

    def _record_harness_metrics(
        self, uow: UnitOfWork, attempt: Attempt, parsed: ParsedReport
    ) -> None:
        """05b: what the transcript said about tokens and cost, on the metrics row the
        pools count. A harness that reports nothing leaves null, and the pool counts
        attempts (routing.py)."""
        metrics = uow.attempt_metrics.get(attempt.id)
        if metrics is None:
            return
        reported = parsed.metrics
        if reported.model is not None:
            # The model that answered, beside the one the contract named: a harness that
            # silently substituted one is visible to pool accounting and history (05b).
            metrics.model_reported = reported.model
        if reported.tokens_in is not None:
            metrics.tokens_in = reported.tokens_in
        if reported.tokens_out is not None:
            metrics.tokens_out = reported.tokens_out
        if reported.cost_usd is not None:
            metrics.cost_units = reported.cost_usd
        if reported.source != "none":
            metrics.cost_source = reported.source
        uow.attempt_metrics.put(metrics)
        record_event(
            uow,
            self._clock,
            EventKind.ATTEMPT_METRICS_RECORDED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                **reported.as_dict(),
                "model_requested": metrics.model,
                "transcript_lines": parsed.transcript_lines,
                "transcript": parsed.transcript_name,
                "progress_lines": len(parsed.progress),
            },
        )

    def _record_wall_time(self, uow: UnitOfWork, attempt: Attempt) -> None:
        metrics = uow.attempt_metrics.get(attempt.id)
        if metrics is None:
            return
        if attempt.started_at is not None and attempt.ended_at is not None:
            metrics.wall_ms = int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
        metrics.exit_class = attempt.exit_class.value if attempt.exit_class else None
        uow.attempt_metrics.put(metrics)

    def _finish_review_attempt(
        self, uow: UnitOfWork, attempt: Attempt, outputs: CollectedOutputs
    ) -> None:
        """A `review` execution succeeds when a ReviewReportV1 parses (09). Its verdict
        does not move the task by itself; Foundry's acceptance does (11)."""
        task = uow.tasks.get(attempt.task_id, for_update=True)
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        assert task is not None and execution is not None
        recorded = False
        detail = "no review report was produced"
        if outputs.report is not None:
            try:
                record_review_report(
                    uow,
                    self._clock,
                    task=task,
                    document=outputs.report,
                    reviewer_kind="crucible_review_execution",
                    reviewer_attempt_id=attempt.id,
                    reviewer_principal_id=None,
                    principal_name=PRINCIPAL_CRUCIBLE,
                )
                recorded = True
                detail = "ReviewReportV1 recorded"
            except ApplicationError as exc:
                detail = exc.detail
                if exc.event is not None:
                    # The supervisor's transaction does not roll back here, so the
                    # rejection is recorded in place rather than by the API handler.
                    uow.events.append(exc.event)
        move_attempt(
            uow,
            self._clock,
            attempt,
            AttemptState.COLLECTED,
            EventKind.ATTEMPT_COLLECTED,
            payload={"role": "review", "review_recorded": recorded, "detail": detail},
        )
        self._record_wall_time(uow, attempt)
        if recorded and attempt.exit_code == 0:
            move_attempt(
                uow, self._clock, attempt, AttemptState.SUCCEEDED, EventKind.ATTEMPT_SUCCEEDED
            )
            move_execution(
                uow, self._clock, execution, ExecutionState.SUCCEEDED, EventKind.EXECUTION_SUCCEEDED
            )
        else:
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"role": "review", "detail": detail},
            )
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"role": "review", "detail": detail},
            )
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.ATTEMPT_FAILED,
                summary=(
                    f"the review execution produced no usable ReviewReportV1 ({detail}); "
                    f"{task.head_sha} still has no non-author review"
                ),
                task=task,
                attempt_id=attempt.id,
                extra_links={"review": f"/v1/tasks/{task.id}/review"},
            )
        work = latest_work_attempt(uow, task)
        if work is not None:
            work_attempt, work_execution = work
            evaluate_and_advance(
                uow, self._clock, task=task, attempt=work_attempt, execution=work_execution
            )

    # ----- reactive quota routing -----------------------------------------

    def _routing_context(
        self, uow: UnitOfWork, task: Task, execution: Execution
    ) -> tuple[Any, TaskContractV1] | None:
        stored = uow.contracts.get(task.id, execution.contract_version)
        routing = load_routing(uow, execution.policy_snapshot or {})
        if stored is None or routing is None:
            return None
        return routing, TaskContractV1.model_validate(stored.document)

    def _class_pool_resets(
        self, uow: UnitOfWork, routing: Any, contract: TaskContractV1
    ) -> list[Any]:
        tier = routing.tiers[contract.execution_request.tier.value]
        pinned = contract.execution_request.pinned_model
        pools = {
            model.pool
            for model in routing.models
            if model.enabled
            and model.capability in tier.allowed_capability
            and (pinned is None or model.id == pinned)
        }
        now = self._clock.now()
        return sorted(
            mark.reset_at
            for mark in uow.pool_exhaustions.list_all()
            if mark.pool in pools and mark.cleared_at is None and mark.reset_at > now
        )

    def _enter_quota_wait(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        execution: Execution,
        selection: Any,
    ) -> None:
        context = self._routing_context(uow, task, execution)
        now = self._clock.now()
        if context is None:
            if attempt.state not in ATTEMPT_TERMINAL:
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = now
                move_attempt(
                    uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED
                )
                move_attempt(
                    uow, self._clock, attempt, AttemptState.FAILED, EventKind.ATTEMPT_FAILED
                )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
            return
        routing, contract = context
        resets = self._class_pool_resets(uow, routing, contract)
        if not resets:
            if attempt.state not in ATTEMPT_TERMINAL:
                attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
                attempt.ended_at = now
                move_attempt(
                    uow, self._clock, attempt, AttemptState.COLLECTED, EventKind.ATTEMPT_COLLECTED
                )
                move_attempt(
                    uow, self._clock, attempt, AttemptState.FAILED, EventKind.ATTEMPT_FAILED
                )
            if execution.state is ExecutionState.CREATED:
                move_execution(
                    uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
                )
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            if task.state is TaskState.SCHEDULED:
                move_task(
                    uow,
                    self._clock,
                    task,
                    TaskState.RUNNING,
                    EventKind.TASK_RUNNING,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"attempt_number": attempt.number},
                )
            self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
            return
        if attempt.state not in ATTEMPT_TERMINAL:
            attempt.exit_class = ExitClass.QUOTA_EXHAUSTED
            attempt.ended_at = now
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": ExitClass.QUOTA_EXHAUSTED.value},
            )
        if execution.state is ExecutionState.CREATED:
            move_execution(
                uow, self._clock, execution, ExecutionState.ACTIVE, EventKind.EXECUTION_ACTIVE
            )
        first_wait = task.quota_wait_started_at is None
        task.quota_wait_started_at = task.quota_wait_started_at or now
        deadline = task.quota_wait_started_at + timedelta(
            seconds=routing.reroute.resume_max_wait_seconds
        )
        task.resume_at = min(resets[0], deadline)
        uow.tasks.save(task)
        if task.state is not TaskState.AWAITING_QUOTA:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.AWAITING_QUOTA,
                EventKind.TASK_AWAITING_QUOTA,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "tier": contract.execution_request.tier.value,
                    "resume_at": task.resume_at.isoformat(),
                    "ordered_candidates": list(selection.candidates) if selection else [],
                },
            )
        if first_wait:
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=WakeReason.AWAITING_QUOTA,
                summary=(
                    f"all pools for class {contract.execution_request.tier.value} are exhausted; "
                    f"Crucible will resume at {task.resume_at.isoformat()}"
                ),
                task=task,
                attempt_id=attempt.id,
            )

    def _mark_pool_exhausted(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        execution: Execution,
        reset_at: Any,
    ) -> None:
        task = uow.tasks.get(attempt.task_id)
        assert task is not None
        context = self._routing_context(uow, task, execution)
        if context is None or attempt.selected_pool is None:
            return
        routing, _ = context
        now = self._clock.now()
        reset = reset_at or now + timedelta(
            seconds=routing.pools[attempt.selected_pool].default_cooldown_seconds
        )
        mark = uow.pool_exhaustions.put(
            PoolExhaustion(
                pool=attempt.selected_pool,
                exhausted_at=now,
                reset_at=reset,
                task_id=attempt.task_id,
                attempt_id=attempt.id,
                reason="harness reported quota_exhausted",
            )
        )
        record_event(
            uow,
            self._clock,
            EventKind.POOL_EXHAUSTED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=attempt.task_id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                "pool": mark.pool,
                "reset_at": mark.reset_at.isoformat(),
                "source": "harness" if reset_at else "policy_default_cooldown",
            },
        )

    def _handle_quota_exit(
        self, uow: UnitOfWork, task: Task, execution: Execution, attempt: Attempt
    ) -> None:
        context = self._routing_context(uow, task, execution)
        if context is None:
            move_execution(
                uow, self._clock, execution, ExecutionState.FAILED, EventKind.EXECUTION_FAILED
            )
            self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
            return
        routing, _contract = context
        reroutes = sum(
            event.kind == EventKind.TASK_REROUTED.value
            and int(event.payload.get("contract_version", 0)) == execution.contract_version
            for event in uow.events.list_for_task(task.id, after_seq=0, limit=1000)
        )
        if task.head_sha:
            record_event(
                uow,
                self._clock,
                EventKind.QUOTA_WIP_COMMITTED,
                principal=PRINCIPAL_CRUCIBLE,
                task_id=task.id,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "commit_sha": task.head_sha,
                    "message": f"wip(crucible): attempt {attempt.id}",
                },
            )
        if reroutes >= routing.reroute.reroute_max:
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"exit_class": "quota_exhausted", "reroute_cap": reroutes},
            )
            self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
            return
        stored = uow.contracts.get(task.id, execution.contract_version)
        assert stored is not None
        item = _Pending(attempt, execution, task, stored.document)
        selection = self._selection_for(uow, item)
        if selection is not None and selection.selected is not None and selection.image is not None:
            nxt = self._create_attempt(uow, execution, number=attempt.number + 1)
            nxt.resume_from_remote = True
            nxt.selected_model = selection.selected.id
            nxt.selected_harness = selection.selected.harness
            nxt.selected_image = selection.image
            nxt.selected_pool = selection.selected.pool
            nxt.ordered_candidates = list(selection.candidates)
            uow.attempts.save(nxt)
            move_task(
                uow,
                self._clock,
                task,
                TaskState.SCHEDULED,
                EventKind.TASK_REROUTED,
                execution_id=execution.id,
                attempt_id=attempt.id,
                payload={
                    "contract_version": execution.contract_version,
                    "from_attempt_id": attempt.id,
                    "from_pool": attempt.selected_pool,
                    "to_attempt_id": nxt.id,
                    "model": selection.selected.id,
                    "harness": selection.selected.harness,
                    "why": "previous pool reported quota exhaustion",
                    "wip_commit_sha": task.head_sha,
                    "ordered_candidates": list(selection.candidates),
                },
            )
            return
        self._enter_quota_wait(uow, task, attempt, execution, selection)

    def _resume_quota_waits(self) -> None:
        with self._fenced() as uow:
            now = self._clock.now()
            for task in uow.tasks.list_by_state(TaskState.AWAITING_QUOTA, for_update=True):
                if task.resume_at is not None and task.resume_at > now:
                    continue
                executions = [
                    execution
                    for execution in uow.executions.list_for_task(task.id)
                    if execution.state is ExecutionState.ACTIVE
                ]
                attempts = uow.attempts.list_for_task(task.id)
                if not executions or not attempts:
                    continue
                execution = executions[-1]
                attempt = attempts[-1]
                stored = uow.contracts.get(task.id, execution.contract_version)
                assert stored is not None
                selection = self._selection_for(
                    uow, _Pending(attempt, execution, task, stored.document)
                )
                if selection is not None and selection.selected is not None:
                    task.resume_at = None
                    uow.tasks.save(task)
                    move_task(
                        uow,
                        self._clock,
                        task,
                        TaskState.SCHEDULED,
                        EventKind.TASK_QUOTA_RESUMED,
                        execution_id=execution.id,
                        attempt_id=attempt.id,
                        payload={"model": selection.selected.id, "pool": selection.selected.pool},
                    )
                    continue
                context = self._routing_context(uow, task, execution)
                if context is None:
                    continue
                routing, contract = context
                started = task.quota_wait_started_at or now
                deadline = started + timedelta(seconds=routing.reroute.resume_max_wait_seconds)
                if now >= deadline:
                    move_execution(
                        uow,
                        self._clock,
                        execution,
                        ExecutionState.FAILED,
                        EventKind.EXECUTION_FAILED,
                        payload={"exit_class": "quota_exhausted", "wait_cap_exceeded": True},
                    )
                    self._task_reported(uow, task, attempt, ExitClass.QUOTA_EXHAUSTED, {})
                    continue
                resets = self._class_pool_resets(uow, routing, contract)
                task.resume_at = min(resets[0], deadline) if resets else deadline
                uow.tasks.save(task)
                record_event(
                    uow,
                    self._clock,
                    EventKind.TASK_AWAITING_QUOTA,
                    principal=PRINCIPAL_CRUCIBLE,
                    task_id=task.id,
                    execution_id=execution.id,
                    attempt_id=attempt.id,
                    payload={"resume_at": task.resume_at.isoformat(), "rechecked": True},
                )
            uow.commit()

    # ----- classification, retry, task transition --------------------------

    def _classify_and_finish(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        blocked_text: str | None,
        *,
        claim_ok: bool = False,
        defer_quota: bool = False,
    ) -> None:
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        task = uow.tasks.get(attempt.task_id, for_update=True)
        assert execution is not None and task is not None
        if execution.role is ExecutionRole.REVIEW:
            # A review execution that never produced a report (prepare or launch failed,
            # the worker was lost, the quota refused it) has no path to `reported`: the
            # task is waiting in awaiting_internal_review and 09 gives it no such edge.
            self._finish_failed_review(uow, attempt, execution, task)
            return
        exit_class = attempt.exit_class or ExitClass.UNKNOWN
        # 10: the checkout lease is released on a terminal attempt state.
        self._release_checkout_leases(uow, attempt)
        if exit_class is ExitClass.COMPLETED and claim_ok:
            move_attempt(
                uow, self._clock, attempt, AttemptState.SUCCEEDED, EventKind.ATTEMPT_SUCCEEDED
            )
        elif exit_class is ExitClass.BLOCKED:
            move_attempt(uow, self._clock, attempt, AttemptState.BLOCKED, EventKind.ATTEMPT_BLOCKED)
        else:
            if exit_class is ExitClass.COMPLETED and not claim_ok:
                attempt.exit_class = ExitClass.COMPLETED_WITHOUT_REPORT
                exit_class = attempt.exit_class
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": exit_class.value},
            )
        common = {"execution_id": execution.id, "attempt_id": attempt.id}
        if task.state in (TaskState.CANCELLING, TaskState.CANCELLED):
            self._finish_cancelling(uow, task)
            return
        if attempt.state is AttemptState.SUCCEEDED:
            move_execution(
                uow, self._clock, execution, ExecutionState.SUCCEEDED, EventKind.EXECUTION_SUCCEEDED
            )
            self._task_reported(uow, task, attempt, exit_class, common)
            return
        if attempt.state is AttemptState.BLOCKED:
            move_task(
                uow,
                self._clock,
                task,
                TaskState.BLOCKED,
                EventKind.TASK_BLOCKED,
                payload={**common, "exit_class": exit_class.value, "blocked_md": blocked_text},
                **common,
            )
            # 09: entering `blocked` opens an escalation and creates a wake.
            open_escalation(
                uow,
                self._clock,
                task=task,
                attempt_id=attempt.id,
                question=blocked_text or "the worker exited 75 without a question",
            )
            return
        if exit_class is ExitClass.QUOTA_EXHAUSTED:
            # Reactive rerouting is only for a worker that actually ran. A reserve-time
            # refusal has no worktree to checkpoint and follows the established
            # quota-exhausted report path.
            if attempt.started_at is None:
                move_execution(
                    uow,
                    self._clock,
                    execution,
                    ExecutionState.FAILED,
                    EventKind.EXECUTION_FAILED,
                    payload={"exit_class": exit_class.value, "phase": "reserve"},
                )
                self._task_reported(uow, task, attempt, exit_class, common)
                return
            if defer_quota:
                return
            self._handle_quota_exit(uow, task, execution, attempt)
            return
        retryable = exit_class.value in execution.retry_on and exit_class in (
            ExitClass.ENVIRONMENT,
            ExitClass.LOST,
            ExitClass.AUTH_FAILURE,
        )
        if attempt.termination_reason == TERMINATION_REFUSED:
            # 07: a refused launch would be refused again; Foundry has the wake.
            retryable = False
        if retryable and attempt.number < execution.max_attempts:
            nxt = self._create_attempt(uow, execution, number=attempt.number + 1)
            move_task(
                uow,
                self._clock,
                task,
                TaskState.SCHEDULED,
                EventKind.TASK_RETRY_SCHEDULED,
                payload={
                    **common,
                    "exit_class": exit_class.value,
                    "next_attempt_id": nxt.id,
                    "next_attempt_number": nxt.number,
                    "max_attempts": execution.max_attempts,
                },
                **common,
            )
            return
        move_execution(
            uow,
            self._clock,
            execution,
            ExecutionState.FAILED,
            EventKind.EXECUTION_FAILED,
            payload={
                "exit_class": exit_class.value,
                "attempts_used": attempt.number,
                "max_attempts": execution.max_attempts,
                "retry_eligible": retryable,
            },
        )
        self._task_reported(uow, task, attempt, exit_class, common)

    def _finish_failed_review(
        self, uow: UnitOfWork, attempt: Attempt, execution: Execution, task: Task
    ) -> None:
        """A review execution that produced no ReviewReportV1 ends without moving the task.

        09: a review execution's outcome does not change task state by itself. The task
        stays in `awaiting_internal_review` and Foundry is woken, so the review can be
        asked for again rather than the tick failing on an illegal transition forever."""
        exit_class = attempt.exit_class or ExitClass.UNKNOWN
        if attempt.state not in ATTEMPT_TERMINAL:
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"role": "review", "exit_class": exit_class.value},
            )
        if execution.state not in EXECUTION_TERMINAL:
            move_execution(
                uow,
                self._clock,
                execution,
                ExecutionState.FAILED,
                EventKind.EXECUTION_FAILED,
                payload={"role": "review", "exit_class": exit_class.value},
            )
        if task.state in (TaskState.CANCELLING, TaskState.CANCELLED):
            self._finish_cancelling(uow, task)
            return
        create_wake(
            uow,
            self._clock,
            principal_id=task.principal_id,
            reason=WakeReason.ATTEMPT_FAILED,
            summary=(
                f"the review execution ended {exit_class.value} without a ReviewReportV1; "
                f"{task.head_sha} still has no non-author review"
            ),
            task=task,
            attempt_id=attempt.id,
            extra_links={"review": f"/v1/tasks/{task.id}/review"},
        )

    # Exit classes that wake Foundry once no retry remains (17).
    _FAILURE_WAKE_REASONS: ClassVar[dict[ExitClass, WakeReason]] = {
        ExitClass.TIMEOUT: WakeReason.TIMED_OUT,
        ExitClass.LOST: WakeReason.LOST,
        ExitClass.AUTH_FAILURE: WakeReason.AUTH_FAILURE,
        ExitClass.QUOTA_EXHAUSTED: WakeReason.QUOTA_EXHAUSTED,
    }

    def _task_reported(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        exit_class: ExitClass,
        common: dict[str, str],
    ) -> None:
        stored = uow.contracts.get(task.id, task.contract_version)
        tier = (
            stored.document.get("execution_request", {}).get("tier") if stored is not None else None
        )
        move_task(
            uow,
            self._clock,
            task,
            TaskState.REPORTED,
            EventKind.TASK_REPORTED,
            payload={
                **common,
                "exit_class": exit_class.value,
                "attempt_state": attempt.state.value,
                "head_sha": task.head_sha,
                "tier": tier,
            },
            **common,
        )
        if attempt.state is AttemptState.FAILED:
            reason = self._FAILURE_WAKE_REASONS.get(exit_class, WakeReason.ATTEMPT_FAILED)
            create_wake(
                uow,
                self._clock,
                principal_id=task.principal_id,
                reason=reason,
                summary=(
                    f"attempt {attempt.number} ended {exit_class.value} with no retry remaining; "
                    "the pre-PR gates will say so"
                ),
                task=task,
                attempt_id=attempt.id,
            )

    def _finish_cancelling(self, uow: UnitOfWork, task: Task) -> None:
        """Once no attempt is live, close open executions; a cancelling task becomes cancelled."""
        attempts = uow.attempts.list_for_task(task.id)
        # An unsupervised attempt (15) has no worker to wait for; it stays as the record
        # of a run Crucible never observed, and the cancellation settles around it.
        if any(a.state not in ATTEMPT_TERMINAL and not a.unsupervised for a in attempts):
            return
        for e in uow.executions.list_for_task(task.id):
            if e.state in (ExecutionState.CREATED, ExecutionState.ACTIVE):
                move_execution(
                    uow, self._clock, e, ExecutionState.CANCELLED, EventKind.EXECUTION_CANCELLED
                )
        if task.state is TaskState.CANCELLING:
            move_task(uow, self._clock, task, TaskState.CANCELLED, EventKind.TASK_CANCELLED)

    # ----- step: cancellations -----------------------------------------

    def _list_cancel_work(self) -> list[_CancelWork]:
        """Live attempts of cancelling or cancelled tasks, plus tasks whose executions can close."""
        out: list[_CancelWork] = []
        with self._uow_factory() as uow:
            for state in (TaskState.CANCELLING, TaskState.CANCELLED):
                for task in uow.tasks.list_by_state(state):
                    attempts = uow.attempts.list_for_task(task.id)
                    live = [
                        a
                        for a in attempts
                        if a.state not in ATTEMPT_TERMINAL and not a.unsupervised
                    ]
                    out.extend(_CancelWork(task_id=task.id, attempt=a) for a in live)
                    open_exec = any(
                        e.state in (ExecutionState.CREATED, ExecutionState.ACTIVE)
                        for e in uow.executions.list_for_task(task.id)
                    )
                    if not live and (open_exec or state is TaskState.CANCELLING):
                        out.append(_CancelWork(task_id=task.id, attempt=None))
        return out

    async def _sweep_cancellations(self) -> None:
        for work in await self._db(self._list_cancel_work):
            attempt = work.attempt
            if attempt is None:
                await self._db(partial(self._settle_cancelled_task, work.task_id))
                continue
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                if attempt.state is AttemptState.RUNNING:
                    provider_name = await self._db(partial(self._execution_provider_name, attempt))
                    await self._provider(provider_name).terminate(
                        self._handle_for(attempt), "drain"
                    )
                    await self._db(partial(self._mark_terminating, attempt.id))
                elif attempt.state is AttemptState.PENDING:
                    await self._db(partial(self._cancel_pending, attempt.id))

    def _mark_terminating(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state is not AttemptState.RUNNING:
                return
            attempt.termination_reason = TERMINATION_CANCEL
            attempt.drain_deadline = self._clock.now() + timedelta(seconds=self.grace_seconds)
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.TERMINATING,
                EventKind.ATTEMPT_TERMINATING,
                payload={"mode": "drain", "drain_deadline": attempt.drain_deadline.isoformat()},
            )
            uow.commit()

    def _cancel_pending(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            if attempt.state is not AttemptState.PENDING:
                return
            attempt.exit_class = ExitClass.KILLED
            attempt.termination_reason = TERMINATION_CANCEL
            attempt.ended_at = self._clock.now()
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.COLLECTED,
                EventKind.ATTEMPT_COLLECTED,
                payload={"reason": "task cancelled before launch"},
            )
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.FAILED,
                EventKind.ATTEMPT_FAILED,
                payload={"exit_class": ExitClass.KILLED.value},
            )
            task = uow.tasks.get(attempt.task_id, for_update=True)
            assert task is not None
            self._finish_cancelling(uow, task)
            uow.commit()

    def _settle_cancelled_task(self, task_id: str) -> None:
        with self._fenced() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            if task is None or task.state not in (TaskState.CANCELLING, TaskState.CANCELLED):
                return
            self._finish_cancelling(uow, task)
            uow.commit()

    # ----- step: status -------------------------------------------------

    def _status_step(self, started: float) -> dict[str, int]:
        with self._fenced() as uow:
            counts = {
                "tasks_scheduled": len(uow.tasks.list_by_state(TaskState.SCHEDULED)),
                "tasks_running": len(uow.tasks.list_by_state(TaskState.RUNNING)),
                "tasks_cancelling": len(uow.tasks.list_by_state(TaskState.CANCELLING)),
                "attempts_pending": len(uow.attempts.list_in_states([AttemptState.PENDING])),
                "attempts_live": len(
                    uow.attempts.list_in_states([AttemptState.RUNNING, AttemptState.TERMINATING])
                ),
                # 23: the delivery half's own depths, which `GET /supervisor` reports as
                # the GitHub observation status.
                "tasks_publishing": len(uow.tasks.list_by_state(TaskState.PUBLISHING)),
                "pull_requests_observed": len(
                    uow.pull_requests.list_in_states(
                        [PullRequestState.OPENING, PullRequestState.OPEN]
                    )
                ),
                "github_deliveries_pending": uow.github_deliveries.count_unprocessed(),
            }
            status = uow.supervisor_status.get()
            status.holder = self.holder
            status.last_tick_at = self._clock.now()
            status.last_success_at = status.last_tick_at
            status.tick_ms = int((time.monotonic() - started) * 1000)
            status.counts = counts
            uow.supervisor_status.write(status)
            uow.commit()
            return counts
