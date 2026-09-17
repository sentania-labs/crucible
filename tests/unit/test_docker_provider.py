"""The Docker provider against a stub daemon (08, 13).

No containers here: a stub client answers the Engine API so the refusals and the
failure paths can be exercised deterministically. The real containers are the e2e
tier's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.docker import (
    THROWAWAY_API_ERROR,
    THROWAWAY_TIMED_OUT,
    CollectionFailedError,
    DockerConfig,
    DockerProvider,
)
from crucible.adapters.execution.dockerapi import DockerApiError, LogFrame
from crucible.ports.execution import Handle, LaunchSpec, ProviderError, Workspace
from tests.fixtures import contract_document

IMAGE = "crucible-worker:script-harness-1.0.0-abc"
LABELS = {"crucible.harness": "script-harness", "crucible.harness_version": "1.0.0"}
DIGEST = "crucible-worker@sha256:" + "b" * 64


class StubClient:
    """Enough of the Engine API for the provider, with the failures a test wants."""

    def __init__(
        self,
        *,
        labels: dict[str, str] | None = None,
        wait: Exception | int = 0,
    ) -> None:
        self.labels = LABELS if labels is None else labels
        self.wait = wait
        self.inspections = 0
        self.created: list[dict[str, Any]] = []
        self.killed: list[str] = []
        self.removed: list[str] = []

    def inspect_image(self, reference: str) -> dict[str, Any]:
        self.inspections += 1
        return {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [DIGEST],
            "Config": {"Labels": dict(self.labels)},
        }

    def inspect_network(self, name: str) -> dict[str, Any]:
        return {"Id": "net"}

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        self.created.append({"name": name, "body": body})
        return f"container-{len(self.created)}"

    def start_container(self, container_id: str) -> None:
        return None

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        if isinstance(self.wait, Exception):
            raise self.wait
        return self.wait

    def kill_container(self, container_id: str, *, signal: str = "SIGKILL") -> None:
        self.killed.append(container_id)

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.removed.append(container_id)

    def container_logs(self, container_id: str, **kw: Any) -> list[LogFrame]:
        return [LogFrame("stderr", b"2026-09-16T16:49:48.000000000Z it went wrong\n")]

    def list_containers(self, **kw: Any) -> list[dict[str, Any]]:
        return []


def config(tmp_path: Path) -> DockerConfig:
    return DockerConfig(
        endpoint="tcp://127.0.0.1:1",
        artifact_root=str(tmp_path),
        mount_kind="bind",
        artifact_host_root=str(tmp_path),
        artifact_volume="",
        collector_timeout_seconds=1,
        verifier_timeout_seconds=1,
    )


def spec(**policy: Any) -> LaunchSpec:
    document = contract_document()
    document["repository"]["work_branch"] = "crucible/EX-0001"
    return LaunchSpec(
        attempt_id="01ATTEMPT0000000000000000A",
        task_id="01TASK00000000000000000000",
        external_id="EX-0001",
        role="implement",
        harness="script-harness",
        model="none",
        image=IMAGE,
        timeout_seconds=600,
        contract=document,
        policy={"images": {"allowlist": ["crucible-worker:*"]}, **policy},
        repository_url="/repos/example.git",
    )


def provider(tmp_path: Path, client: StubClient) -> DockerProvider:
    return DockerProvider(config(tmp_path), client=client)  # type: ignore[arg-type]


# ----- the digest cache is a fact about the daemon, not a permission ---------


async def test_the_same_reference_under_a_different_policy_is_refused(tmp_path: Path) -> None:
    """13: a cache hit must not be a way past the allowlist."""
    client = StubClient()
    docker = provider(tmp_path, client)
    permitted = spec()
    assert await docker._resolve_image(permitted) == DIGEST
    assert client.inspections == 1

    refused = spec()
    refused.policy["images"]["allowlist"] = ["ghcr.io/someone-else/worker:*"]
    with pytest.raises(ProviderError, match="outside the policy allowlist"):
        await docker._resolve_image(refused)
    # The daemon was not asked again: only the reference-to-digest mapping is cached.
    assert client.inspections == 1


async def test_the_version_range_is_checked_on_every_launch(tmp_path: Path) -> None:
    """07: a refusal, never a warning, and never skipped because the digest is known."""
    client = StubClient()
    docker = provider(tmp_path, client)
    assert await docker._resolve_image(spec()) == DIGEST

    other_harness = spec()
    object.__setattr__(other_harness, "harness", "codex")
    with pytest.raises(ProviderError, match="declares harness"):
        await docker._resolve_image(other_harness)
    assert client.inspections == 1


async def test_an_image_outside_the_tested_range_is_refused(tmp_path: Path) -> None:
    client = StubClient(
        labels={"crucible.harness": "script-harness", "crucible.harness_version": "9.9.9"}
    )
    with pytest.raises(ProviderError, match="outside the tested range"):
        await provider(tmp_path, client)._resolve_image(spec())


# ----- a throwaway container that never finishes -----------------------------


def workspace_for(tmp_path: Path, attempt_id: str) -> Workspace:
    root = tmp_path / "workspaces" / attempt_id
    (root / "output").mkdir(parents=True)
    (root / "verify").mkdir(parents=True)
    return Workspace(
        attempt_id=attempt_id,
        checkout_path=str(root / "repo"),
        identity_path=str(root / "identity"),
        report_path=str(root / "report"),
        output_path=str(root / "output"),
        work_branch="crucible/EX-0001",
    )


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), OSError("connection reset")],
)
async def test_a_hung_collector_fails_the_collection_rather_than_escaping(
    tmp_path: Path, failure: Exception
) -> None:
    """16: the attempt becomes an environment failure; nothing raises past the caller
    and nothing reruns the same collection forever."""
    client = StubClient(wait=failure)
    docker = provider(tmp_path, client)
    launch = spec()
    handle = Handle(provider="docker", ref="worker", attempt_id=launch.attempt_id)
    with pytest.raises(CollectionFailedError) as raised:
        await docker.collect(handle, workspace_for(tmp_path, launch.attempt_id), launch)
    assert "did not finish within" in str(raised.value)
    # The container that would not finish was killed, then removed.
    assert client.killed and client.removed


async def test_a_collector_the_daemon_refused_fails_the_collection(tmp_path: Path) -> None:
    client = StubClient(wait=DockerApiError(500, "no such container"))
    docker = provider(tmp_path, client)
    launch = spec()
    handle = Handle(provider="docker", ref="worker", attempt_id=launch.attempt_id)
    with pytest.raises(CollectionFailedError):
        await docker.collect(handle, workspace_for(tmp_path, launch.attempt_id), launch)


async def test_a_hung_verifier_fails_verification_ran_with_the_reason(tmp_path: Path) -> None:
    """11: a command Crucible could not re-run has not been verified."""
    client = StubClient(wait=TimeoutError())
    docker = provider(tmp_path, client)
    launch = spec()
    root = tmp_path / "workspaces" / launch.attempt_id
    (root / "output" / "tree").mkdir(parents=True)
    (root / "verify").mkdir(parents=True)
    runs = await docker._run_verifier(launch)
    assert {run.id for run in runs} == {"V1", "V2", "V3"}
    assert all(not run.ran for run in runs)
    assert all("did not finish within" in run.detail for run in runs)
    assert all(not run.ok for run in runs)


async def test_the_sentinels_are_outside_any_real_exit_code() -> None:
    assert THROWAWAY_TIMED_OUT < 0 and THROWAWAY_API_ERROR < 0
    assert THROWAWAY_TIMED_OUT != THROWAWAY_API_ERROR
