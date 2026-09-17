"""The publisher container (23 "Publication", S10).

One hardened, throwaway container per publication. It gets the branch bundle read-only,
a tmpfs for the token, a tmpfs home to work in, an output directory, and the egress
network. It never gets the worker's checkout, the worker's `.git` directory, the
database, the socket, or any credential other than the one token, and that token arrives
on stdin and lives only on a tmpfs the container loses when it stops.

What it does inside: verify the bundle, fetch the work branch from it, assert the fetched
head is the collected head Crucible recorded, check every commit's author and trailer
against policy, and push without force. Every API call after the push is Crucible's own,
so the container needs no API token beyond git's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any

from crucible.adapters.execution import scripts
from crucible.adapters.execution.create_policy import (
    CreateRequestRefusedError,
)
from crucible.adapters.execution.create_policy import (
    check as check_create,
)
from crucible.adapters.execution.docker import (
    LABEL_ATTEMPT,
    LABEL_OWNER,
    LABEL_ROLE,
    LABEL_TASK,
    DockerProvider,
)
from crucible.adapters.execution.dockerapi import DockerApiError
from crucible.domain.secrets import redact
from crucible.ports.execution import LaunchSpec
from crucible.ports.github import InstallationToken
from crucible.ports.publish import PublishOutcome, PublishRequest

log = logging.getLogger("crucible.publisher")

ROLE_PUBLISHER = "publisher"
# The script's own exit for "every commit was checked and at least one failed policy".
# Distinct from a failed push, because nothing was attempted against the remote.
COMMIT_POLICY_REFUSED = 6
# S10: a token is valid for an hour whatever the container does, so a publisher that
# outlives ten minutes is treated as failed rather than left to hold one.
MAX_PUBLISHER_SECONDS = 600


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    """23 step 3: the egress network with an allowlist of `github.com` and
    `api.github.com` only. `network` defaults to the publisher's own, not the workers',
    because the workers' proxy permits every model endpoint a harness needs and a
    container holding a GitHub credential has no business reaching any of them."""

    network: str = "crucible-publish"
    egress_proxy: str | None = None
    no_proxy: str = "localhost,127.0.0.1"
    credential_host: str = "github.com"
    timeout_seconds: int = MAX_PUBLISHER_SECONDS
    token_tmpfs_bytes: int = 64 * 1024


class DockerPublisher:
    """The `Publisher` port on the same daemon, through the same socket proxy."""

    def __init__(self, provider: DockerProvider, config: PublisherConfig | None = None) -> None:
        self._provider = provider
        self.config = config or PublisherConfig()
        self._network_ready = False

    @property
    def _client(self) -> Any:
        return self._provider.client

    def _root(self, attempt_id: str) -> Path:
        return Path(self._provider.config.artifact_root) / "publish" / attempt_id

    async def _ensure_network(self) -> None:
        """The publisher's own internal network. `internal` means no default route: the
        only way out is the proxy this network is joined to, which is the point."""
        if self._network_ready or self.config.network in ("none", ""):
            return
        try:
            await asyncio.to_thread(self._client.inspect_network, self.config.network)
        except DockerApiError as exc:
            if exc.status != 404:
                raise
            await asyncio.to_thread(self._client.create_network, self.config.network, internal=True)
        self._network_ready = True

    async def push(self, request: PublishRequest, token: InstallationToken) -> PublishOutcome:
        """Run one publisher container to completion and report what it did."""
        await self._ensure_network()
        root = self._root(request.attempt_id)
        await asyncio.to_thread(shutil.rmtree, root, True)
        await asyncio.to_thread(self._stage, root, request.bundle_path)
        script = scripts.publisher_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name=request.author_name,
            author_email=request.author_email,
            commit_trailer=request.commit_trailer,
            credential_host=self.config.credential_host,
        )
        env = {
            "HOME": "/home/worker",
            "CRUCIBLE_ATTEMPT_ID": request.attempt_id,
        }
        if self.config.egress_proxy:
            env.update(
                {
                    "HTTPS_PROXY": self.config.egress_proxy,
                    "https_proxy": self.config.egress_proxy,
                    "NO_PROXY": self.config.no_proxy,
                    "no_proxy": self.config.no_proxy,
                }
            )
        body = self._body(request, script=script, env=env)
        name = f"crucible-publisher-{request.attempt_id}"
        container_id = ""
        exit_code = -1
        try:
            try:
                # The image is the attempt's own resolved digest, which the policy
                # allowlist admits by tag; the provider resolves it the same way for
                # every throwaway container it creates.
                check_create(
                    body,
                    self._provider._create_policy(request_spec(request), resolved=request.image),
                )
            except CreateRequestRefusedError as exc:
                return PublishOutcome(
                    pushed=False,
                    head_sha="",
                    step="create",
                    detail=f"the create-request policy refused the publisher: {exc}",
                )
            container_id = await asyncio.to_thread(self._client.create_container, name, body)
            await asyncio.to_thread(self._client.start_container, container_id)
            # The value leaves memory here and nowhere else: stdin to a tmpfs (S10).
            await asyncio.to_thread(
                self._client.write_stdin, container_id, token.reveal().encode("utf-8")
            )
            exit_code = int(
                await asyncio.to_thread(
                    self._client.wait_container,
                    container_id,
                    timeout=float(min(request.timeout_seconds, self.config.timeout_seconds)),
                )
            )
        except DockerApiError as exc:
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="container",
                detail=f"the publisher container could not run: {exc}",
                exit_code=-1,
            )
        except (TimeoutError, OSError, HTTPException) as exc:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.kill_container, container_id)
            return PublishOutcome(
                pushed=False,
                head_sha="",
                step="timeout",
                detail=(
                    f"the publisher did not finish within {self.config.timeout_seconds}s "
                    f"({type(exc).__name__})"
                ),
                exit_code=-2,
            )
        finally:
            if container_id:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._client.remove_container, container_id, force=True)
        return await asyncio.to_thread(self._read_outcome, root / "out", exit_code)

    def _stage(self, root: Path, bundle_path: str) -> None:
        """Copy the branch bundle into a directory of its own and make the output dir.

        The publisher gets the bundle and nothing else of the collector's output: the
        diff, the report copy, and the fresh tree are evidence the gates read, and a
        container holding a credential has no business seeing them (08, 23)."""
        source = Path(bundle_path)
        if not source.is_file():
            raise FileNotFoundError(f"no branch bundle at {bundle_path}")
        for leaf in ("bundle", "out"):
            directory = root / leaf
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(self._provider.config.workspace_dir_mode)
        target = root / "bundle" / "work_branch.bundle"
        shutil.copyfile(source, target)
        target.chmod(0o644)
        root.chmod(self._provider.config.workspace_dir_mode)

    def _body(self, request: PublishRequest, *, script: str, env: dict[str, str]) -> dict[str, Any]:
        spec = request_spec(request)
        host = self._provider._hardened(spec, network=self.config.network)
        host["Tmpfs"] = {
            **host["Tmpfs"],
            scripts.TOKEN_MOUNT: (
                f"rw,nosuid,nodev,noexec,size={self.config.token_tmpfs_bytes},"
                "mode=0700,uid=1000,gid=1000"
            ),
        }
        host["Mounts"] = [
            self._provider._volume_mount(
                f"publish/{request.attempt_id}/bundle", scripts.BUNDLE_MOUNT, read_only=True
            ),
            self._provider._volume_mount(
                f"publish/{request.attempt_id}/out", scripts.PUBLISH_MOUNT, read_only=False
            ),
        ]
        return {
            "Image": request.image,
            "Cmd": ["sh", "-c", script],
            "User": "1000:1000",
            "WorkingDir": "/home/worker",
            "Env": [f"{k}={v}" for k, v in sorted(env.items())],
            "Labels": {
                LABEL_ATTEMPT: request.attempt_id,
                LABEL_TASK: request.task_id,
                LABEL_OWNER: request.owner,
                LABEL_ROLE: ROLE_PUBLISHER,
            },
            "Tty": False,
            # `docker run -i`: stdin stays open until the attach closes it, which is how
            # the token arrives without ever being on argv or in the environment.
            "OpenStdin": True,
            "StdinOnce": True,
            "AttachStdin": True,
            "HostConfig": host,
        }

    def _read_outcome(self, root: Path, exit_code: int) -> PublishOutcome:
        step = _read(root / "step.txt") or "unknown"
        head = _read(root / "bundle-head.txt")
        detail = redact(_read(root / "error.txt"))
        pushed = _read(root / "push.txt") == "ok" and exit_code == 0
        authors = tuple(_lines(root / "author-problems.txt"))
        trailers = tuple(_lines(root / "trailer-problems.txt"))
        if exit_code == COMMIT_POLICY_REFUSED and not detail:
            detail = (
                f"commit policy refused the push: {len(authors)} author problem(s), "
                f"{len(trailers)} trailer problem(s); nothing was pushed"
            )
        return PublishOutcome(
            pushed=pushed,
            head_sha=head,
            step=step,
            detail=detail,
            exit_code=exit_code,
            remote_head_before=_read(root / "remote-head-before.txt"),
            # Crucible's own container output, redacted before it is recorded: git can
            # be made to print a header and a remote can answer with anything (12).
            log_tail=redact(_read(root / "publisher.log", limit=8000)[-8000:]),
            trailer_problems=trailers,
            author_problems=authors,
        )

    async def cleanup(self, attempt_ids: Sequence[str]) -> int:
        removed = 0
        for attempt_id in attempt_ids:
            root = self._root(attempt_id)
            if root.exists():
                await asyncio.to_thread(shutil.rmtree, root, True)
                removed += 1
        return removed


def request_spec(request: PublishRequest) -> LaunchSpec:
    """A LaunchSpec-shaped view of the publish request.

    The create-request policy and the hardened body are written against a launch spec,
    and the publisher is a launch with a different script. Building a full spec here
    would mean carrying a contract the publisher never reads, so this is the narrow
    stand-in, with the fields those two functions actually use."""
    return LaunchSpec(
        attempt_id=request.attempt_id,
        task_id=request.task_id,
        external_id="",
        owner=request.owner,
        harness="",
        model="",
        image=request.image,
        contract={},
        policy={str(k): v for k, v in request.policy.items()},
        env={},
        timeout_seconds=request.timeout_seconds,
        network="policy",
        role="publish",
        repository_url=request.repository_url,
    )


def _read(path: Path, *, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _lines(path: Path) -> list[str]:
    text = _read(path)
    return [line for line in text.splitlines() if line.strip()]
