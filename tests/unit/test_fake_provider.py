from __future__ import annotations

import pytest

from crucible.adapters.execution.fake import FakeProvider
from crucible.ports.execution import LaunchSpec, ObservationState, ProviderError
from tests.fixtures import contract_document


def _spec(image: str, external_id: str = "EX-0001") -> LaunchSpec:
    return LaunchSpec(
        attempt_id=f"attempt-{external_id}",
        task_id="t",
        external_id=external_id,
        harness="codex",
        model="m",
        image=image,
        timeout_seconds=60,
        contract=contract_document(),
    )


async def _run(provider: FakeProvider, spec: LaunchSpec) -> tuple[int | None, ObservationState]:
    ws = await provider.prepare(spec)
    h = await provider.launch(ws, spec)
    obs = await provider.observe(h)
    while obs.state is ObservationState.RUNNING:
        obs = await provider.observe(h)
    return obs.exit_code, obs.state


async def test_succeed_writes_report() -> None:
    p = FakeProvider()
    spec = _spec("crucible-worker:fake-succeed")
    ws = await p.prepare(spec)
    h = await p.launch(ws, spec)
    obs = await p.observe(h)
    assert (obs.exit_code, obs.state) == (0, ObservationState.EXITED)
    out = await p.collect(h, ws)
    assert out.report is not None and out.report["task_external_id"] == "EX-0001"
    assert out.blocked_md is None


async def test_blocked_exit_75_with_blocked_md() -> None:
    p = FakeProvider()
    spec = _spec("crucible-worker:fake-blocked")
    ws = await p.prepare(spec)
    h = await p.launch(ws, spec)
    obs = await p.observe(h)
    assert obs.exit_code == 75
    out = await p.collect(h, ws)
    assert out.blocked_md and out.report is None


async def test_crash_and_environment_codes() -> None:
    p = FakeProvider()
    assert (await _run(p, _spec("crucible-worker:fake-crash", "A")))[0] == 1
    assert (await _run(p, _spec("crucible-worker:fake-environment", "B")))[0] == 70


async def test_hang_ignores_drain_dies_on_kill() -> None:
    p = FakeProvider()
    spec = _spec("crucible-worker:fake-hang")
    h = await p.launch(await p.prepare(spec), spec)
    for _ in range(5):
        assert (await p.observe(h)).state is ObservationState.RUNNING
    await p.terminate(h, "drain")
    assert (await p.observe(h)).state is ObservationState.RUNNING
    await p.terminate(h, "kill")
    obs = await p.observe(h)
    assert obs.state is ObservationState.EXITED and obs.exit_code == 137


async def test_vanish_is_lost_and_absent_from_reconcile() -> None:
    p = FakeProvider()
    spec = _spec("crucible-worker:fake-vanish-2")
    h = await p.launch(await p.prepare(spec), spec)
    assert (await p.observe(h)).state is ObservationState.RUNNING
    assert [x.attempt_id for x in await p.reconcile()] == [spec.attempt_id]
    assert (await p.observe(h)).state is ObservationState.LOST
    assert await p.reconcile() == []


async def test_observation_count_suffix() -> None:
    p = FakeProvider()
    spec = _spec("crucible-worker:fake-succeed-3")
    h = await p.launch(await p.prepare(spec), spec)
    states = [(await p.observe(h)).state for _ in range(3)]
    assert states == [ObservationState.RUNNING, ObservationState.RUNNING, ObservationState.EXITED]


async def test_script_overrides_image() -> None:
    p = FakeProvider()
    p.script("EX-0001", "crash")
    assert (await _run(p, _spec("crucible-worker:fake-succeed")))[0] == 1
    with pytest.raises(ValueError, match="unknown fake behavior"):
        p.script("x", "explode")


async def test_unknown_image_is_a_provider_error() -> None:
    p = FakeProvider()
    with pytest.raises(ProviderError):
        await p.prepare(_spec("crucible-worker:codex-1.0"))
    with pytest.raises(ProviderError):
        await p.prepare(_spec("crucible-worker:fake-explode"))
