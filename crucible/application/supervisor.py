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
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, ClassVar, TypeVar

from crucible.application.decisions import (
    DEFAULT_ESCALATION_STALE_HOURS,
    open_escalation,
    repeat_stale_escalation_wakes,
)
from crucible.application.errors import ApplicationError
from crucible.application.evidence import record_collection_evidence
from crucible.application.gates import evaluate_and_advance
from crucible.application.review import (
    author_attempt_ids,
    latest_work_attempt,
    record_review_report,
    review_evidence_payload,
)
from crucible.application.routing import reserve
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
from crucible.contracts.wake import WakeReason
from crucible.domain.entities import (
    Attempt,
    AttemptMetrics,
    CompletionClaimRecord,
    EvidenceRecord,
    Execution,
    ExecutionRole,
    Task,
)
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.exit_class import ExitClass, classify_exit
from crucible.domain.ids import new_id
from crucible.domain.lifecycle import (
    ATTEMPT_TERMINAL,
    AttemptState,
    ExecutionState,
    IllegalTransitionError,
    TaskState,
)
from crucible.domain.secrets import find_secrets
from crucible.logs import log_context
from crucible.ports.artifacts import ArtifactStore
from crucible.ports.clock import Clock
from crucible.ports.execution import (
    CollectedOutputs,
    ExecutionProvider,
    Handle,
    LaunchSpec,
    ObservationState,
    ProviderError,
    Workspace,
)
from crucible.ports.notification import WakeDeliverer
from crucible.ports.repository import FencedTokenRejectedError, UnitOfWork, UnitOfWorkFactory

log = logging.getLogger("crucible.supervisor")
T = TypeVar("T")

TERMINATION_TIMEOUT = "timeout"
TERMINATION_CANCEL = "cancel"


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
        lease_ttl_seconds: int = 30,
        attempt_lease_ttl_seconds: int = 60,
        grace_seconds: int = 60,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._clock = clock
        self._artifacts = artifact_store
        self._wakes = wake_deliverer
        self.holder = holder
        self.lease_ttl_seconds = lease_ttl_seconds
        self.attempt_lease_ttl_seconds = attempt_lease_ttl_seconds
        self.grace_seconds = grace_seconds
        self.fenced_token: int | None = None
        self._handles: dict[str, Handle] = {}
        self._workspaces: dict[str, Workspace] = {}

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
            await self._db(self._materialize_scheduled)
            result.launched = await self._launch_pending()
            observed, finished = await self._observe_attempts()
            result.observed, result.finished = observed, finished
            await self._sweep_cancellations()
            await self._db(self._materialize_evidence)
            await self._db(self._evaluate_pending_gates)
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
                role = (
                    ExecutionRole.CORRECT
                    if stored.document.get("correction")
                    else ExecutionRole.IMPLEMENT
                )
                if any(
                    e.contract_version == task.contract_version and e.role is role
                    for e in uow.executions.list_for_task(task.id)
                ):
                    continue
                policy = uow.policies.get(task.policy_name, task.policy_version)
                assert policy is not None
                req = stored.document["execution_request"]
                lifecycle = stored.document["lifecycle"]
                now = self._clock.now()
                execution = Execution(
                    id=new_id(),
                    task_id=task.id,
                    role=role,
                    contract_version=task.contract_version,
                    harness=str(req["harness"]),
                    model=str(req["model"]),
                    effort=req.get("effort"),
                    provider=str(req["provider"]),
                    image=str(req["image"]),
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
                            "path": artifact.path,
                            "name": artifact.type,
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
                out.append(_Pending(attempt, execution, task, stored.document))
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

    async def _launch_one(self, item: _Pending) -> bool:
        attempt, execution, task = item.attempt, item.execution, item.task
        env: dict[str, str] = {}
        if execution.role is ExecutionRole.REVIEW and task.head_sha:
            env["CRUCIBLE_REVIEW_HEAD_SHA"] = task.head_sha
        spec = LaunchSpec(
            attempt_id=attempt.id,
            task_id=task.id,
            external_id=task.external_id,
            role=execution.role.value,
            harness=execution.harness,
            model=execution.model,
            image=execution.image,
            timeout_seconds=execution.timeout_seconds,
            contract=item.contract,
            env=env,
            network=item.contract.get("constraints", {}).get("network", "policy"),
        )
        provider = self._provider(execution.provider)
        if not await self._db(partial(self._mark_preparing, attempt.id)):
            return False
        try:
            ws = await provider.prepare(spec)
        except ProviderError as exc:
            detail = str(exc)
            await self._db(partial(self._environment_failure, attempt.id, "prepare", detail))
            return False
        self._workspaces[attempt.id] = ws
        if not await self._db(partial(self._mark_launching, attempt.id, ws)):
            return False
        try:
            handle = await provider.launch(ws, spec)
        except ProviderError as exc:
            detail = str(exc)
            await self._db(partial(self._environment_failure, attempt.id, "launch", detail))
            return False
        self._handles[attempt.id] = handle
        await self._db(partial(self._mark_running, attempt.id, handle))
        log.info("attempt launched", extra={"handle": handle.ref, "provider": provider.name})
        return True

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
            move_attempt(
                uow,
                self._clock,
                attempt,
                AttemptState.LAUNCHING,
                EventKind.ATTEMPT_LAUNCHING,
                payload={"workspace": attempt.workspace_path},
            )
            uow.commit()
            return True

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
            await self._db(partial(self._renew_attempt_lease, attempt.id))
            return False
        if observation.state is ObservationState.LOST:
            await self._db(partial(self._finish_lost, attempt.id, observation.detail))
            return True
        outputs = await provider.collect(handle, self._workspace_for(attempt))
        await self._db(partial(self._finish_exited, attempt.id, observation.exit_code, outputs))
        self._handles.pop(attempt.id, None)
        self._workspaces.pop(attempt.id, None)
        return True

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
        self, attempt_id: str, exit_code: int | None, outputs: CollectedOutputs
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
            attempt.exit_class = classify_exit(
                exit_code=exit_code,
                report_present=outputs.report is not None,
                blocked_present=outputs.blocked_md is not None,
                timed_out=timed_out,
                killed=killed,
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
                },
            )
            execution = uow.executions.get(attempt.execution_id)
            assert execution is not None
            if execution.role is ExecutionRole.REVIEW:
                self._finish_review_attempt(uow, attempt, outputs)
                uow.leases.release_attempt_lease(attempt.id)
                uow.commit()
                return
            claim_ok = False
            cancelled = attempt.termination_reason == TERMINATION_CANCEL
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
                    "report_present": outputs.report is not None,
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
            self._classify_and_finish(uow, attempt, blocked_text, claim_ok=claim_ok)
            uow.commit()

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
        work = latest_work_attempt(uow, task)
        if work is not None:
            work_attempt, work_execution = work
            evaluate_and_advance(
                uow, self._clock, task=task, attempt=work_attempt, execution=work_execution
            )

    # ----- classification, retry, task transition --------------------------

    def _classify_and_finish(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        blocked_text: str | None,
        *,
        claim_ok: bool = False,
    ) -> None:
        execution = uow.executions.get(attempt.execution_id, for_update=True)
        task = uow.tasks.get(attempt.task_id, for_update=True)
        assert execution is not None and task is not None
        exit_class = attempt.exit_class or ExitClass.UNKNOWN
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
        retryable = exit_class.value in execution.retry_on and exit_class in (
            ExitClass.ENVIRONMENT,
            ExitClass.LOST,
            ExitClass.AUTH_FAILURE,
        )
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
        if any(a.state not in ATTEMPT_TERMINAL for a in attempts):
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
                    live = [a for a in attempts if a.state not in ATTEMPT_TERMINAL]
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
