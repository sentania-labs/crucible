"""What the publisher container is, and what it can never be (12, 23, S10).

Two properties that would be a serious defect to lose and that nothing else asserts: the
token is not in anything the daemon records about the container, and the push cannot be
a force push. Both are checked against the real objects the provider sends and the real
script text it runs, not against a description of them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import scripts
from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.execution.publisher import (
    COMMIT_POLICY_REFUSED,
    DockerPublisher,
    PublisherConfig,
)
from crucible.ports.publish import PublishRequest
from tests.integration.fake_github import installation_token_value

HEAD = "a" * 40


def _request(**kw: Any) -> PublishRequest:
    base: dict[str, Any] = {
        "attempt_id": "01ATTEMPT",
        "task_id": "01TASK",
        "owner": "FDY-0042",
        "repository_url": "https://github.com/owner/repo.git",
        "work_branch": "crucible/FDY-0042",
        "base_ref": "main",
        "expected_head": HEAD,
        "bundle_path": "/var/lib/crucible/artifacts/workspaces/01ATTEMPT/output/work_branch.bundle",
        "bundle_sha256": "b" * 64,
        "image": "crucible-worker:script-harness-1.0.0",
        "policy": {"resources": {"cpus": 1, "memory": "512m", "pids": 128}},
    }
    base.update(kw)
    return PublishRequest(**base)


@pytest.fixture
def publisher() -> DockerPublisher:
    provider = DockerProvider(
        DockerConfig(endpoint="tcp://127.0.0.1:1", artifact_root="/var/lib/crucible/artifacts")
    )
    return DockerPublisher(provider, PublisherConfig())


def test_the_create_body_carries_no_token_anywhere_the_daemon_records(
    publisher: DockerPublisher,
) -> None:
    """S10's absence proof, at the one place Crucible controls: the create request.

    `docker inspect` shows `Config.Env`, `Cmd`, and every mount. If the token were in any
    of them it would be on disk in the daemon's own state for the life of the container.
    """
    value = installation_token_value()
    request = _request()
    body = publisher._body(
        request,
        script=scripts.publisher_script(
            clone_url=request.repository_url,
            work_branch=request.work_branch,
            base_ref=request.base_ref,
            expected_head=request.expected_head,
            author_name="crucible-worker",
            author_email="crucible-worker@users.noreply.github.com",
            commit_trailer="Crucible-Attempt",
        ),
        env={"HOME": "/home/worker"},
    )
    assert value not in json.dumps(body)
    assert "ghs_" not in json.dumps(body)
    for entry in body["Env"]:
        assert "token" not in str(entry).lower() or str(entry).lower().startswith(
            "crucible_token_file="
        )
    for mount in body["HostConfig"]["Mounts"]:
        assert "token" not in json.dumps(mount).lower()
    # The one way in is stdin, which is why these three have to be set.
    assert body["OpenStdin"] and body["StdinOnce"] and body["AttachStdin"]
    # And the tmpfs it lands on is not exported, not executable, and owned by the uid
    # the container runs as.
    tmpfs = body["HostConfig"]["Tmpfs"][scripts.TOKEN_MOUNT]
    assert "noexec" in tmpfs and "mode=0700" in tmpfs and "uid=1000" in tmpfs


def test_the_publisher_refuses_a_bundle_changed_after_collection(
    publisher: DockerPublisher, tmp_path: Path
) -> None:
    bundle = tmp_path / "work_branch.bundle"
    bundle.write_bytes(b"sealed branch bundle")
    sealed_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.write_bytes(b"changed branch bundle")

    with pytest.raises(ValueError, match="sealed sha256"):
        publisher._stage(tmp_path / "publish", str(bundle), sealed_sha256)


def test_the_publisher_script_can_never_force_push(publisher: DockerPublisher) -> None:
    """23: Crucible never force-pushes. A remote head that is not an ancestor fails the
    push and is recorded; it is never overwritten."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head=HEAD,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        commit_trailer="Crucible-Attempt",
    )
    # No force flag anywhere in the script, in any spelling.
    for forbidden in ("--force", "--force-with-lease", "--mirror", "+refs/", ":+refs/"):
        assert forbidden not in script, forbidden
    pushes = [line for line in script.splitlines() if "git push" in line]
    assert len(pushes) == 1, pushes
    push_line = pushes[0]
    # One push, one refspec, neither forced nor a delete.
    assert "refs/heads/crucible-publish:refs/heads/$WORK_BRANCH" in push_line
    for forbidden in ("-f", "--delete", "--prune", "--tags"):
        assert f" {forbidden}" not in push_line, forbidden


def test_the_script_refuses_the_push_when_commit_policy_fails() -> None:
    """23 step 4: verify every commit's author and trailer, then push. The check has to
    stop the push, not merely be written down beside it."""
    script = scripts.publisher_script(
        clone_url="https://github.com/owner/repo.git",
        work_branch="crucible/FDY-0042",
        base_ref="main",
        expected_head=HEAD,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        commit_trailer="Crucible-Attempt",
    )
    refusal = script.index("commit policy refused")
    push = script.index("git push --quiet origin")
    assert refusal < push, "the refusal must come before the push"
    assert f"exit {COMMIT_POLICY_REFUSED}" in script
    # And the token goes before the script does, on that path as on every other.
    tail = script[refusal:push]
    assert 'rm -f "$TOKDIR/token"' in tail


def test_a_commit_policy_refusal_maps_to_an_outcome_that_did_not_push(
    publisher: DockerPublisher, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "step.txt").write_text("commit-policy", encoding="utf-8")
    (out / "author-problems.txt").write_text(f"{HEAD}\tsomeone@example.invalid\n", encoding="utf-8")
    (out / "trailer-problems.txt").write_text(f"{HEAD}\n", encoding="utf-8")
    outcome = publisher._read_outcome(out, COMMIT_POLICY_REFUSED)
    assert not outcome.pushed
    assert outcome.step == "commit-policy"
    assert outcome.author_problems and outcome.trailer_problems
    assert "nothing was pushed" in outcome.detail


def test_the_publisher_outcome_is_redacted_before_it_is_recorded(
    publisher: DockerPublisher, tmp_path: Path
) -> None:
    """12: the publisher's own log and the remote's refusal are text that could carry a
    credential, and both are stored in an event."""
    value = installation_token_value()
    out = tmp_path / "out"
    out.mkdir()
    (out / "step.txt").write_text("push", encoding="utf-8")
    (out / "error.txt").write_text(f"remote refused: {value}", encoding="utf-8")
    (out / "publisher.log").write_text(f"Authorization: Bearer {value}", encoding="utf-8")
    outcome = publisher._read_outcome(out, 5)
    assert value not in outcome.detail
    assert value not in outcome.log_tail
    assert "[redacted:" in outcome.detail and "[redacted:" in outcome.log_tail


def test_the_publisher_runs_on_its_own_egress_network_by_default() -> None:
    """23 step 3: github.com and api.github.com only. The workers' proxy permits every
    model endpoint a harness needs, which is not what a container holding a GitHub
    credential should be able to reach."""
    assert PublisherConfig().network == "crucible-publish"
    assert PublisherConfig().network != "crucible-workers"
