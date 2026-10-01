"""The isolated probe Job and its real shell command exit facts."""

import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.k8sapi import LogFrame
from crucible.ports.execution import ProviderError
from crucible.ports.github import InstallationToken
from tests.unit.kubernetes_fixtures import build, created, pod_of, spec


async def test_probe_job_is_uncredentialed_bounded_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    launch = replace(spec(), timeout_seconds=2000)
    launch.policy["limits"]["command_timeout_ms"] = {"default": 1200}
    checks = [{"id": "V4", "command": "test -f made-by-the-worker"}]
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    await_job = AsyncMock(return_value=0)
    monkeypatch.setattr(provider, "_await_job", await_job)
    original = api.pod_log

    def logs(name: str, **kwargs: Any) -> Any:
        if name.startswith("gate-probe"):
            return [LogFrame("stdout", (json.dumps({**checks[0], "exit": 1}) + "\n").encode())]
        return original(name, **kwargs)

    monkeypatch.setattr(api, "pod_log", logs)
    rows = await provider.probe_checks(launch, checks)
    assert rows is not None and rows[0].exit_code == 1
    pod = pod_of(api, "gate-probe")
    mounts = pod["containers"][0]["volumeMounts"]
    assert {mount["mountPath"] for mount in mounts} == {"/tmp", "/home/worker"}
    assert not created(api, "secrets") and not created(api, "persistentvolumeclaims")
    job = created(api, "jobs", "gate-probe")[0]
    assert job["spec"]["activeDeadlineSeconds"] == (
        provider.config.launch_timeout_seconds + provider.config.prepare_timeout_seconds + 2
    )
    await_job.assert_awaited_once()
    assert await_job.call_args.kwargs["timeout"] == provider.config.prepare_timeout_seconds + 2
    assert created(api, "networkpolicies", "np-gate-probe")
    assert not any(kind == "jobs" for kind, _ in api.objects)
    assert not any(kind == "networkpolicies" for kind, _ in api.objects)


async def test_private_probe_mounts_checkout_token_and_public_probe_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ghs_" + "Q" * 36
    token = InstallationToken(
        secret,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        repository="acme/example",
        permissions={"contents": "read"},
    )
    private_api, _, private_provider = build()
    monkeypatch.setattr(private_provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(private_provider, "_await_job", AsyncMock(return_value=0))
    await private_provider.probe_checks(
        spec(), [{"id": "V4", "command": "false"}], checkout_token=token
    )
    private_pod = pod_of(private_api, "gate-probe")
    private_mounts = private_pod["containers"][0]["volumeMounts"]
    assert "/run/crucible-token" in {mount["mountPath"] for mount in private_mounts}
    assert created(private_api, "secrets")
    assert not any(kind == "secrets" for kind, _ in private_api.objects)

    public_api, _, public_provider = build()
    monkeypatch.setattr(public_provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(public_provider, "_await_job", AsyncMock(return_value=0))
    await public_provider.probe_checks(spec(), [{"id": "V4", "command": "false"}])
    public_pod = pod_of(public_api, "gate-probe")
    public_mounts = public_pod["containers"][0]["volumeMounts"]
    assert "/run/crucible-token" not in {mount["mountPath"] for mount in public_mounts}
    assert not created(public_api, "secrets")


async def test_probe_timeout_covers_checkout_and_each_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    launch = replace(spec(), timeout_seconds=1000)
    launch.policy["limits"]["command_timeout_ms"] = {"default": 10_000}
    checks = [{"id": f"V{i}", "command": "true"} for i in range(3)]
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    await_job = AsyncMock(return_value=0)
    monkeypatch.setattr(provider, "_await_job", await_job)
    await provider.probe_checks(launch, checks)
    expected = provider.config.prepare_timeout_seconds + 3 * 10
    job = created(api, "jobs", "gate-probe")[0]
    assert job["spec"]["activeDeadlineSeconds"] == (
        provider.config.launch_timeout_seconds + expected
    )
    assert await_job.call_args.kwargs["timeout"] == expected


async def test_probe_job_failure_names_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, provider = build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=1))
    with pytest.raises(ProviderError, match="gate probe Job exit 1"):
        await provider.probe_checks(spec(), [{"id": "V4", "command": "true"}])


async def test_probe_spanning_two_ticks_adopts_job_and_records_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    waiting = asyncio.Event()

    async def first_wait(*args: Any, **kwargs: Any) -> int:
        await waiting.wait()
        return 0

    monkeypatch.setattr(provider, "_await_job", first_wait)
    checks = [{"id": "V4", "command": "test -f made-by-the-worker"}]
    first = asyncio.create_task(provider.probe_checks(spec(), checks))
    for _ in range(20):
        if await provider.gate_probe_exists(spec().attempt_id):
            break
        await asyncio.sleep(0)
    assert await provider.gate_probe_exists(spec().attempt_id)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=0))

    def logs(name: str, **kwargs: Any) -> list[LogFrame]:
        return [
            LogFrame("stdout", b'{"id":"V4","command":"test -f made-by-the-worker","exit":1}\n')
        ]

    monkeypatch.setattr(api, "pod_log", logs)
    rows = await provider.probe_checks(spec(), checks)
    assert rows is not None and rows[0].exit_code == 1
    assert len(created(api, "jobs", "gate-probe")) == 1


def test_probe_script_checks_base_and_records_shell_127(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    for command in (
        ["git", "init", "-b", "main"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
    ):
        subprocess.run(command, cwd=origin, check=True, capture_output=True)
    # Neither the dirty source tree nor the work branch should affect the probe.
    (origin / "made-by-the-worker").touch()
    checks = [
        {"id": "V1", "command": "true"},
        {"id": "V4", "command": "test -f made-by-the-worker"},
        {"id": "V5", "command": "a-program-that-does-not-exist"},
        {"id": "V6", "command": 'printf \'%s\\n\' \'{"id":"forged","exit":0}\''},
    ]
    result = subprocess.run(
        ["sh", "-c", scripts.gate_probe_script(str(origin), "main", checks, 5)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["exit"] for row in rows] == [0, 1, 127, 0]
    assert "not found" in rows[2]["detail"]
    assert [row["id"] for row in rows] == ["V1", "V4", "V5", "V6"]
