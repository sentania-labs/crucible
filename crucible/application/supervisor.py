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
from typing import Any, TypeVar

from crucible.application.transitions import (
    move_attempt,
    move_execution,
    move_task,
    record_event,
    record_rejected_transition,
)
from crucible.contracts.completion_claim import parse_claim
from crucible.domain.entities import (
    Attempt,
    CompletionClaimRecord,
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
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)


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
        lease_ttl_seconds: int = 30,
        attempt_lease_ttl_seconds: int = 60,
        grace_seconds: int = 60,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._clock = clock
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
        except FencedTokenRejectedError as exc:
            log.warning("fenced write rejected; standing down", extra={"holder": self.holder})
            self.fenced_token = None
            raise LeaseLostError(str(exc)) from exc

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
            result.counts = await self._db(partial(self._status_step, started))
        except LeaseLostError:
            result.held = False
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

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
                elif action == "adopt":
                    self._handles[handle.attempt_id] = handle
        return orphans

    def _classify_handle(self, handle: Handle) -> str:
        with self._fenced() as uow:
            attempt = uow.attempts.get(handle.attempt_id, for_update=True)
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
            if attempt.state is AttemptState.LAUNCHING:
                attempt.handle = handle.ref
                attempt.started_at = attempt.started_at or self._clock.now()
                execution = uow.executions.get(attempt.execution_id)
                assert execution is not None
                attempt.timeout_at = attempt.started_at + timedelta(
                    seconds=execution.timeout_seconds
                )
                move_attempt(
                    uow,
                    self._clock,
                    attempt,
                    AttemptState.RUNNING,
                    EventKind.ATTEMPT_ADOPTED,
                    payload={"handle": handle.ref},
                )
                uow.commit()
                return "adopt"
            if attempt.handle is None:
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
                policy = uow.policies.get(task.policy_name, task.policy_version)
                assert policy is not None
                req = stored.document["execution_request"]
                lifecycle = stored.document["lifecycle"]
                now = self._clock.now()
                execution = Execution(
                    id=new_id(),
                    task_id=task.id,
                    role=ExecutionRole.IMPLEMENT,
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

    # ----- step: launch pending attempts -------------------------------

    def _list_pending(self) -> list[_Pending]:
        out: list[_Pending] = []
        with self._uow_factory() as uow:
            for attempt in uow.attempts.list_in_states([AttemptState.PENDING]):
                execution = uow.executions.get(attempt.execution_id)
                task = uow.tasks.get(attempt.task_id)
                if execution is None or task is None:
                    continue
                if task.state not in (TaskState.SCHEDULED, TaskState.RUNNING):
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
                if await self._launch_one(item):
                    launched += 1
        return launched

    async def _launch_one(self, item: _Pending) -> bool:
        attempt, execution, task = item.attempt, item.execution, item.task
        spec = LaunchSpec(
            attempt_id=attempt.id,
            task_id=task.id,
            external_id=task.external_id,
            harness=execution.harness,
            model=execution.model,
            image=execution.image,
            timeout_seconds=execution.timeout_seconds,
            contract=item.contract,
            network=item.contract.get("constraints", {}).get("network", "policy"),
        )
        provider = self._provider(execution.provider)
        await self._db(partial(self._mark_preparing, attempt.id))
        try:
            ws = await provider.prepare(spec)
        except ProviderError as exc:
            detail = str(exc)
            await self._db(partial(self._environment_failure, attempt.id, "prepare", detail))
            return False
        self._workspaces[attempt.id] = ws
        await self._db(partial(self._mark_launching, attempt.id, ws))
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

    def _mark_preparing(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
            task = uow.tasks.get(attempt.task_id, for_update=True)
            execution = uow.executions.get(attempt.execution_id, for_update=True)
            assert task is not None and execution is not None
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
            uow.commit()

    def _mark_launching(self, attempt_id: str, ws: Workspace) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
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
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    # ----- step: observe ---------------------------------------------------

    def _list_live(self) -> list[Attempt]:
        with self._uow_factory() as uow:
            return list(
                uow.attempts.list_in_states([AttemptState.RUNNING, AttemptState.TERMINATING])
            )

    async def _observe_attempts(self) -> tuple[int, int]:
        observed = finished = 0
        for attempt in await self._db(self._list_live):
            with log_context(
                task_id=attempt.task_id, execution_id=attempt.execution_id, attempt_id=attempt.id
            ):
                observed += 1
                if await self._observe_one(attempt):
                    finished += 1
        return observed, finished

    async def _observe_one(self, attempt: Attempt) -> bool:
        provider_name = await self._db(partial(self._execution_provider_name, attempt))
        provider = self._provider(provider_name)
        handle = self._handles.get(attempt.id) or Handle(
            provider=provider_name, ref=attempt.handle or "", attempt_id=attempt.id
        )
        self._handles[attempt.id] = handle
        observation = await provider.observe(handle)
        now = self._clock.now()
        if observation.state is ObservationState.RUNNING:
            if attempt.state is AttemptState.RUNNING and attempt.timeout_at is not None:
                if attempt.drain_deadline is None and now >= attempt.timeout_at:
                    await provider.terminate(handle, "drain")
                    await self._db(partial(self._record_drain, attempt.id, TERMINATION_TIMEOUT))
                    return False
                if attempt.drain_deadline is not None and now >= attempt.drain_deadline:
                    await provider.terminate(handle, "kill")
                    await self._db(partial(self._record_kill, attempt.id))
                    return False
            if attempt.state is AttemptState.TERMINATING and (
                attempt.drain_deadline is not None and now >= attempt.drain_deadline
            ):
                await provider.terminate(handle, "kill")
                await self._db(partial(self._record_kill, attempt.id))
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

    def _renew_attempt_lease(self, attempt_id: str) -> None:
        with self._fenced() as uow:
            assert self.fenced_token is not None
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
            attempt.drain_deadline = None
            uow.attempts.save(attempt)
            record_event(
                uow,
                self._clock,
                EventKind.ATTEMPT_TIMEOUT_KILL,
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
            self._classify_and_finish(uow, attempt, None)
            uow.commit()

    def _finish_exited(
        self, attempt_id: str, exit_code: int | None, outputs: CollectedOutputs
    ) -> None:
        with self._fenced() as uow:
            attempt = uow.attempts.get(attempt_id, for_update=True)
            assert attempt is not None
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
            claim_ok = False
            if outputs.report is not None:
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
                    "blocked_present": outputs.blocked_md is not None,
                },
            )
            uow.leases.release_attempt_lease(attempt.id)
            self._classify_and_finish(uow, attempt, blocked_text, claim_ok=claim_ok)
            uow.commit()

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
        if task.state is TaskState.CANCELLING:
            self._finish_cancelling(uow, task, execution)
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
                payload={
                    **common,
                    "exit_class": exit_class.value,
                    "blocked_md": blocked_text,
                    "note": "escalation and wake rows are C2",
                },
                **common,
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

    def _task_reported(
        self,
        uow: UnitOfWork,
        task: Task,
        attempt: Attempt,
        exit_class: ExitClass,
        common: dict[str, str],
    ) -> None:
        try:
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
                    "note": "pre-PR gate evaluation is C2",
                },
                **common,
            )
        except IllegalTransitionError as exc:
            uow.rollback()
            with self._uow_factory() as fresh:
                record_rejected_transition(fresh, self._clock, exc, principal=PRINCIPAL_CRUCIBLE)
                fresh.commit()
            raise

    def _finish_cancelling(self, uow: UnitOfWork, task: Task, execution: Execution) -> None:
        attempts = uow.attempts.list_for_task(task.id)
        if any(a.state not in ATTEMPT_TERMINAL for a in attempts):
            return
        for e in uow.executions.list_for_task(task.id):
            if e.state in (ExecutionState.CREATED, ExecutionState.ACTIVE):
                move_execution(
                    uow, self._clock, e, ExecutionState.CANCELLED, EventKind.EXECUTION_CANCELLED
                )
        move_task(uow, self._clock, task, TaskState.CANCELLED, EventKind.TASK_CANCELLED)

    # ----- step: cancellations -----------------------------------------

    def _list_cancel_work(self) -> list[Attempt]:
        out: list[Attempt] = []
        with self._uow_factory() as uow:
            for state in (TaskState.CANCELLING, TaskState.CANCELLED):
                for task in uow.tasks.list_by_state(state):
                    out.extend(
                        a
                        for a in uow.attempts.list_for_task(task.id)
                        if a.state not in ATTEMPT_TERMINAL
                    )
            for task in uow.tasks.list_by_state(TaskState.CANCELLING):
                if all(a.state in ATTEMPT_TERMINAL for a in uow.attempts.list_for_task(task.id)):
                    out.append(
                        Attempt(
                            id="",
                            execution_id="",
                            task_id=task.id,
                            number=0,
                            state=AttemptState.FAILED,
                            created_at=self._clock.now(),
                        )
                    )
        return out

    async def _sweep_cancellations(self) -> None:
        for attempt in await self._db(self._list_cancel_work):
            if attempt.id == "":
                await self._db(partial(self._settle_cancelled_task, attempt.task_id))
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
            execution = uow.executions.get(attempt.execution_id, for_update=True)
            assert task is not None and execution is not None
            if task.state is TaskState.CANCELLING:
                self._finish_cancelling(uow, task, execution)
            elif execution.state in (ExecutionState.CREATED, ExecutionState.ACTIVE):
                move_execution(
                    uow,
                    self._clock,
                    execution,
                    ExecutionState.CANCELLED,
                    EventKind.EXECUTION_CANCELLED,
                )
            uow.commit()

    def _settle_cancelled_task(self, task_id: str) -> None:
        with self._fenced() as uow:
            task = uow.tasks.get(task_id, for_update=True)
            if task is None or task.state is not TaskState.CANCELLING:
                return
            execution = next(iter(uow.executions.list_for_task(task.id)), None)
            if execution is None:
                move_task(uow, self._clock, task, TaskState.CANCELLED, EventKind.TASK_CANCELLED)
            else:
                self._finish_cancelling(uow, task, execution)
            uow.commit()

    # ----- step: status -------------------------------------------------

    def _status_step(self, started: float) -> dict[str, int]:
        with self._uow_factory() as uow:
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
            status.tick_ms = int((time.monotonic() - started) * 1000)
            status.counts = counts
            uow.supervisor_status.write(status)
            uow.commit()
            return counts
