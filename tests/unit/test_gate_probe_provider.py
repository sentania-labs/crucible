"""The isolated probe Job and its real shell command exit facts."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.k8sapi import LogFrame
from crucible.ports.execution import ProviderError
from tests.unit.kubernetes_fixtures import build, created, pod_of, spec


async def test_probe_job_is_uncredentialed_bounded_and_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, provider = build()
    launch = spec()
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
    assert job["spec"]["activeDeadlineSeconds"] == provider.config.launch_timeout_seconds + 2
    await_job.assert_awaited_once()
    assert await_job.call_args.kwargs["timeout"] == 2
    assert created(api, "networkpolicies", "np-gate-probe")
    assert not any(kind == "jobs" for kind, _ in api.objects)
    assert not any(kind == "networkpolicies" for kind, _ in api.objects)


async def test_probe_job_failure_names_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, provider = build()
    monkeypatch.setattr(provider, "_require_ready", AsyncMock())
    monkeypatch.setattr(provider, "_await_job", AsyncMock(return_value=1))
    with pytest.raises(ProviderError, match="gate probe Job exit 1"):
        await provider.probe_checks(spec(), [{"id": "V4", "command": "true"}])


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
