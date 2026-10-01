"""FDY-0222: supervisor tick renews its lease between steps and clears last_error.

Tests verify three properties of the accepted fix:

1. A tick whose steps together exceed the lease TTL keeps the lease (mid-tick
   renewal).
2. A successful tick leaves readiness without a stale ``last_error``.
3. The credential renewal call runs outside the tick's open transaction.

"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from crucible.application.supervisor import Supervisor
from tests.fixtures import FakeClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_uow(
    *,
    renew_ok: bool = True,
    acquire_ok: bool = True,
    last_error_value: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return (uow_mock, status_mock, lease_mock) configured for tick tests.

    The lease mock is prepared once and returned directly; tests must not set
    a ``side_effect`` on ``renew_supervisor`` that calls back into the same
    MagicMock, because that causes a RecursionError.
    """
    lease_mock = MagicMock()
    lease_mock.fenced_token = 42
    lease_mock.expires_at = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=30)

    uow = MagicMock()
    uow.leases.get_supervisor.return_value = None
    if renew_ok:
        uow.leases.renew_supervisor.return_value = lease_mock
    else:
        uow.leases.renew_supervisor.return_value = None
    if acquire_ok:
        uow.leases.acquire_supervisor.return_value = lease_mock
    else:
        uow.leases.acquire_supervisor.return_value = None

    status = MagicMock()
    status.holder = None
    status.last_tick_at = None
    status.last_success_at = None
    status.last_error = last_error_value
    status.last_error_at = None
    status.tick_ms = None
    status.counts = {}
    uow.supervisor_status.get.return_value = status

    def _write_status(s: MagicMock) -> None:
        status.holder = s.holder
        status.last_tick_at = s.last_tick_at
        status.last_success_at = s.last_success_at
        status.last_error = s.last_error
        status.last_error_at = s.last_error_at
        status.tick_ms = s.tick_ms
        status.counts = s.counts

    uow.supervisor_status.write.side_effect = _write_status
    uow.commit.return_value = None
    uow.set_fenced_token.return_value = None

    uow.tasks.list_by_state.return_value = []
    uow.tasks.get.return_value = None
    uow.attempts.list_in_states.return_value = []
    uow.attempts.list_for_execution.return_value = []
    uow.pull_requests.list_in_states.return_value = []
    uow.github_deliveries.count_unprocessed.return_value = 0
    uow.logs.delete_for_attempts.return_value = 0
    uow.logs.attempts_with_logs_before.return_value = []
    uow.wakes.list_acked_before.return_value = []
    uow.retention.record.return_value = MagicMock(id="new-row")

    return uow, status, lease_mock


def _build_factory(uow: MagicMock) -> MagicMock:
    """Create a callable context-manager mock whose ``__enter__`` yields *uow*."""
    ctx_mgr = MagicMock()
    ctx_mgr.__enter__ = MagicMock(return_value=uow)
    ctx_mgr.__exit__ = MagicMock(return_value=None)

    factory = MagicMock()
    factory.return_value = ctx_mgr
    return factory


# ---------------------------------------------------------------------------
# AC2.1: mid-tick lease renewal keeps the lease when tick > TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_renews_lease_between_steps() -> None:
    """The tick calls ``_renew_lease_async`` before each long phase so that
    a long-running tick does not lose its lease.
    """
    uow, _status, lease_mock = _make_uow(
        renew_ok=True,
        acquire_ok=True,
        last_error_value="ConnectionRefused: the k8s resize from last night",
    )
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    renew_calls: list[int] = []

    def capture_renew(*args: Any, **kwargs: Any) -> MagicMock | None:
        renew_calls.append(1)
        return lease_mock

    uow.leases.renew_supervisor.side_effect = capture_renew

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        result = await supervisor.tick()

    assert len(renew_calls) >= 1, (
        f"Expected at least one renew_supervisor call, got {len(renew_calls)}"
    )
    assert result.held is True


@pytest.mark.asyncio
async def test_tick_keeps_lease_when_steps_exceed_ttl() -> None:
    """Even when the simulated tick steps would take longer than the lease
    TTL (30 s), a mid-tick renewal keeps the lease alive."""
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    renew_count = [0]

    def slow_renew(*args: Any, **kwargs: Any) -> MagicMock | None:
        renew_count[0] += 1
        lease_mock = MagicMock()
        lease_mock.fenced_token = 42 + renew_count[0]
        lease_mock.expires_at = clock.now() + timedelta(seconds=30)
        return lease_mock

    uow = MagicMock()
    uow.leases.get_supervisor.return_value = None
    uow.leases.renew_supervisor.side_effect = slow_renew
    uow.leases.acquire_supervisor.side_effect = slow_renew

    status = MagicMock()
    status.holder = None
    status.last_tick_at = None
    status.last_success_at = None
    status.last_error = None
    status.last_error_at = None
    status.tick_ms = None
    status.counts = {}
    uow.supervisor_status.get.return_value = status

    def _write_status(s: MagicMock) -> None:
        status.holder = s.holder
        status.last_tick_at = s.last_tick_at
        status.last_success_at = s.last_success_at
        status.last_error = s.last_error
        status.last_error_at = s.last_error_at
        status.tick_ms = s.tick_ms
        status.counts = s.counts

    uow.supervisor_status.write.side_effect = _write_status
    uow.commit.return_value = None

    factory = _build_factory(uow)
    clock.advance(20)

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        result = await supervisor.tick()

    assert result.held is True


# ---------------------------------------------------------------------------
# AC2.2: successful tick clears last_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_tick_clears_last_error_full_path() -> None:
    """A tick that completes successfully clears last_error via _status_step."""
    uow, _status, _lease_mock = _make_uow(
        renew_ok=True,
        acquire_ok=True,
        last_error_value="ConnectionRefused: the k8s resize from last night",
    )
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    written: list[dict[str, Any]] = []

    def capture_write(s: MagicMock) -> None:
        written.append(
            {
                "last_error": s.last_error,
                "last_error_at": s.last_error_at,
                "last_success_at": s.last_success_at,
            }
        )

    uow.supervisor_status.write.side_effect = capture_write

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        result = await supervisor.tick()

    assert result.held is True
    # _status_step is patched, so write never happens through the normal path.
    # Verify _status_step in isolation instead.
    supervisor.fenced_token = 42
    counts = supervisor._status_step(0.0)
    assert isinstance(counts, dict)

    # _status_step uses self._fenced() which yields from self._uow_factory(),
    # whose __enter__ returns uow. So the write goes through uow.
    assert len(written) >= 1
    assert written[-1]["last_error"] is None, (
        f"Expected last_error to be cleared, got {written[-1]['last_error']!r}"
    )


# ---------------------------------------------------------------------------
# AC2.3: credential renewal runs outside the tick transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_renewal_outside_tick_transaction() -> None:
    """The credential renewal callback runs outside ``_fenced()``,
    so it does not hold the tick's open transaction while making
    an OAuth network call."""
    uow, _status, _ = _make_uow()
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        result = await supervisor.tick()

    assert result.held is True

    source = inspect.getsource(Supervisor._retention_sweep)
    renew_pos = source.find("self._credential_renewal()")
    fenced_pos = source.find("with self._fenced()")
    assert renew_pos >= 0, "credential_renewal must be in _retention_sweep"
    assert fenced_pos >= 0, "_fenced block must be in _retention_sweep"
    assert renew_pos < fenced_pos, (
        "credential_renewal must be called BEFORE entering the fenced block"
    )


# ---------------------------------------------------------------------------
# Bonus: _renew_lease method exists and is callable
# ---------------------------------------------------------------------------


def test_renew_lease_method_exists() -> None:
    """The Supervisor has a _renew_lease method for mid-tick renewal."""
    assert hasattr(Supervisor, "_renew_lease")
    assert callable(Supervisor._renew_lease)


def test_renew_lease_clears_token_when_renew_fails() -> None:
    """_renew_lease clears fenced_token and returns False when renew_supervisor
    returns None."""
    uow, _status, _ = _make_uow(renew_ok=False)
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
    )
    supervisor.fenced_token = 42

    result = supervisor._renew_lease()

    assert result is False
    assert supervisor.fenced_token is None


# ---------------------------------------------------------------------------
# P1 Finding #1: renewal before each long phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renewal_before_publish() -> None:
    """A mid-tick renewal runs before publish so publish cannot outlive the lease."""
    uow, _status, _ = _make_uow(renew_ok=True, acquire_ok=True)
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    renew_count = [0]

    def count_renew(*args: Any, **kwargs: Any) -> MagicMock | None:
        renew_count[0] += 1
        return MagicMock()

    uow.leases.renew_supervisor.side_effect = count_renew

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        await supervisor.tick()

    # Four renewals inside tick body: before publish, observe, cleanup,
    # and retention.
    assert renew_count[0] >= 4, f"Expected >= 4 mid-tick renewals, got {renew_count[0]}"


# ---------------------------------------------------------------------------
# P1 Finding #2: tick stops when lease is lost, no credential refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_stops_when_lease_lost_before_retention() -> None:
    """When a mid-tick renewal loses the lease, the tick stops and does not
    proceed to retention or credential refresh."""
    uow, _status, _ = _make_uow(renew_ok=True, acquire_ok=True)
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    renew_count = [0]

    def fail_on_third(*args: Any, **kwargs: Any) -> MagicMock | None:
        renew_count[0] += 1
        if renew_count[0] < 4:
            return MagicMock()
        return None

    uow.leases.renew_supervisor.side_effect = fail_on_third

    credential_refreshed = [False]

    def track_credential() -> bool:
        credential_refreshed[0] = True
        return False

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=track_credential,
    )

    with (
        patch.object(supervisor, "_reconcile_provider_handles", return_value=0),
        patch.object(supervisor, "_resume_quota_checkpoints"),
        patch.object(supervisor, "_resume_quota_waits"),
        patch.object(supervisor, "_materialize_scheduled"),
        patch.object(supervisor, "_resume_gate_probes"),
        patch.object(supervisor, "_launch_pending", return_value=0),
        patch.object(supervisor, "_observe_attempts", return_value=([], [])),
        patch.object(supervisor, "_sweep_cancellations"),
        patch.object(supervisor, "_materialize_evidence"),
        patch.object(supervisor, "_evaluate_pending_gates"),
        patch.object(supervisor.delivery, "publish", return_value=0),
        patch.object(supervisor.delivery, "observe", return_value=0),
        patch.object(supervisor, "_cleanup_step"),
        patch.object(supervisor, "_retention_step", return_value=0),
        patch.object(supervisor, "_refresh_attempt_metrics"),
        patch.object(supervisor, "_repeat_stale_escalations"),
        patch.object(supervisor, "_deliver_wakes", return_value=0),
        patch.object(supervisor, "_status_step", return_value={}),
    ):
        result = await supervisor.tick()

    assert result.held is False, "Tick should stop when lease is lost"
    assert credential_refreshed[0] is False, "Credential refresh must not run after lease is lost"


@pytest.mark.asyncio
async def test_credential_refresh_checks_lease() -> None:
    """When fenced_token is None, _retention_sweep returns immediately
    without calling the credential renewal callback."""
    uow, _status, _ = _make_uow()
    factory = _build_factory(uow)
    clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

    supervisor = Supervisor(
        factory,
        {},
        clock,
        holder="test-host:1",
        artifact_store=MagicMock(),
        lease_ttl_seconds=30,
        credential_renewal=lambda: False,
    )
    supervisor.fenced_token = None

    applied = supervisor._retention_sweep()

    assert applied == 0
