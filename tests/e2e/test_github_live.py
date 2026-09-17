"""The live GitHub tier (18, 23): a real App, a real repository, real containers.

Local only. It runs the whole delivery half against
`sentania-labs/crucible-spike-target`: mint an installation token from the mounted key,
push the collected head from the bundle in a publisher container, open the pull request
with the rendered body, observe it, and merge-observe. It deletes every branch and closes
every pull request it created, and it never touches the default branch.

Skipped with a reason unless the App key, the App record, and the target repository are
all configured, so a partial run is never mistaken for a pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.execution.docker import DockerProvider
from crucible.adapters.execution.publisher import DockerPublisher, PublisherConfig
from crucible.adapters.github.client import RestGitHubClient
from crucible.application.delivery_tick import DeliveryConfig
from crucible.application.repositories import register_repository
from crucible.application.supervisor import Supervisor
from crucible.contracts.api import ExternalReviewAttestation, RepositoryRegistration
from crucible.domain.secrets import scan_text
from tests.e2e import github_live
from tests.e2e.conftest import RUN_ID, e2e_contract, run_until, submit_and_start, upload_review
from tests.e2e.github_live import LiveConfig
from tests.e2e.policy import e2e_policy_document, e2e_routing_document

NOT_CONFIGURED = github_live.why_not_configured()
pytestmark = [
    pytest.mark.e2e_github,
    pytest.mark.skipif(bool(NOT_CONFIGURED), reason=NOT_CONFIGURED or "configured"),
]

POLL = DeliveryConfig(poll_interval_seconds=0, reactions_poll_interval_seconds=0)


@pytest.fixture(scope="session")
def live_config() -> LiveConfig:
    return github_live.load_config()


@pytest.fixture
def github(live_config: LiveConfig) -> RestGitHubClient:
    return github_live.client(live_config)


@pytest.fixture
def mirror(artifact_root: Path, live_config: LiveConfig) -> str:
    """A bare mirror of the target inside the artifact root (github_live docstring)."""
    return github_live.seed_mirror(artifact_root / "e2e-repos", live_config)


@pytest.fixture
def cleanup(github: RestGitHubClient, live_config: LiveConfig) -> Iterator[github_live.Cleanup]:
    record = github_live.Cleanup(config=live_config, github=github, branches=[], pull_requests=[])
    yield record
    result = record.run()
    print(f"live cleanup: {result}")
    assert not result["errors"], result["errors"]


@pytest.fixture
def publisher(provider: DockerProvider, stack: dict[str, Any]) -> DockerPublisher:
    return DockerPublisher(
        provider,
        PublisherConfig(
            network=provider.config.workers_network,
            egress_proxy=str(stack["egress_proxy"]),
            timeout_seconds=300,
        ),
    )


@pytest.fixture
def live_supervisor(
    ctx: AppContext,
    provider: DockerProvider,
    github: RestGitHubClient,
    publisher: DockerPublisher,
) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"docker": provider},
        ctx.clock,
        holder=f"e2e-github-{RUN_ID}",
        artifact_store=ctx.artifact_store,
        github=github,
        publisher=publisher,
        delivery_config=POLL,
        lease_ttl_seconds=300,
        grace_seconds=5,
    )


def register(ctx: AppContext, live_config: LiveConfig, mirror: str) -> None:
    with ctx.uow_factory() as uow:
        register_repository(
            uow,
            ctx.clock,
            principal_name="tests",
            name=live_config.repository,
            registration=RepositoryRegistration(
                # Where Crucible's own containers fetch from. The publisher's remote is
                # derived from the registered name, so the push goes to GitHub.
                url=mirror,
                default_branch="main",
                policy_name="e2e-script",
                installation_id=live_config.installation_id,
                external_review=ExternalReviewAttestation(
                    attested_all_prs=True, attested_by="operator"
                ),
            ),
        )
        uow.commit()


def live_contract(external_id: str, live_config: LiveConfig, image: str) -> dict[str, Any]:
    document = e2e_contract(external_id, live_config.repository, image)
    document["repository"]["work_branch"] = github_live.branch_for(external_id)
    # The target repository carries no check scripts and this tier never pushes to its
    # default branch, so the required commands are ones any tree satisfies.
    document["required_verification"] = [
        {"id": "V1", "command": "echo lint ok", "expect_exit": 0},
        {"id": "V2", "command": "echo test ok", "expect_exit": 0},
        {"id": "V3", "command": "echo scan ok", "expect_exit": 0},
        {"id": "V4", "kind": "artifact", "path": "report/run-evidence.md"},
    ]
    document["deliverables"] = [
        {
            "kind": "pull_request",
            "target": f"https://github.com/{live_config.repository}",
            "draft": False,
            "closes": [],
        }
    ]
    return document


def live_policy() -> dict[str, Any]:
    document = e2e_policy_document()
    document["repository"]["required_checks"] = ["echo lint ok", "echo test ok", "echo scan ok"]
    # This repository has no CI of its own; the task waits in certification and the
    # timeout wakes Foundry, which is the behaviour 23 asks for and what this asserts.
    document["network"]["egress_allowlist"] = ["github.com"]
    return document


@pytest.fixture
def live_client(ctx: AppContext, tokens: dict[str, str]) -> Iterator[TestClient]:
    app = create_app(ctx)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['admin']}"}) as admin:
        routing = e2e_routing_document()
        assert admin.put(
            f"/v1/routing/{routing['name']}/{routing['version']}", json=routing
        ).status_code in (200, 201)
        document = live_policy()
        assert admin.put(
            f"/v1/policies/{document['name']}/{document['version']}", json=document
        ).status_code in (200, 201)
    with TestClient(app, headers={"Authorization": f"Bearer {tokens['orchestrator']}"}) as c:
        yield c


async def test_a_task_reaches_a_real_pull_request_and_a_real_merge(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
) -> None:
    register(ctx, live_config, mirror)
    external_id = f"C4-{RUN_ID}"
    branch = github_live.branch_for(external_id)
    cleanup.add_branch(branch)
    document = live_contract(external_id, live_config, worker_image)
    task_id = submit_and_start(live_client, document)

    state = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_internal_review", "pre_pr_gates_failed"},
        max_ticks=60,
    )
    assert state == "awaiting_internal_review", live_client.get(f"/v1/tasks/{task_id}").json()[
        "gate_summary"
    ]
    upload_review(live_client, task_id)
    state = await run_until(
        live_supervisor, live_client, task_id, {"awaiting_acceptance"}, max_ticks=20
    )
    view = live_client.get(f"/v1/tasks/{task_id}").json()
    accepted = live_client.post(
        f"/v1/tasks/{task_id}/accept",
        json={
            "verdict": "accepted",
            "reasoning": "The live tier accepts its own head to exercise publication.",
            "head_sha": view["head_sha"],
        },
    )
    assert accepted.status_code == 200, accepted.text

    state = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_external_review", "awaiting_ci_certification", "publish_failed"},
        max_ticks=20,
        pause=2.0,
    )
    events = live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    assert state != "publish_failed", [
        e for e in events["items"] if e["kind"] == "task_publish_failed"
    ]
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    cleanup.add_pull_request(int(record["number"]))
    print(f"live pull request: {record['url']} at {record['head_sha']}")
    assert record["head_sha"] == view["head_sha"]
    assert record["state"] == "open"

    access = github_live.token(github, live_config)
    try:
        remote = github.remote_head(access, repository=live_config.repository, ref=branch)
        assert remote == view["head_sha"]
        live = github.get_pull_request(
            access, repository=live_config.repository, number=record["number"]
        )
    finally:
        access.discard()
    assert live.head_sha == view["head_sha"]
    assert live.base_ref == "main"

    # The body is on GitHub; what Crucible kept is its hash. Nothing secret-shaped is in
    # the title or the body, and neither carries an unauthorized closing keyword.
    assert scan_text(live.title) is None

    # 23: no external review is expected on this run within the tier's patience, so the
    # round is not what this asserts. The merge observation is.
    status, payload = github_live.merge_with_app_token(
        github, live_config, record["number"], sha=view["head_sha"]
    )
    assert status == 200, payload
    merged = await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"merged", "ready_for_merge", "rejected"},
        max_ticks=30,
        pause=2.0,
    )
    final = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    assert final["state"] == "merged", final
    assert final["merge_sha"]
    assert merged in ("merged", "ready_for_merge")
    kinds = [
        e["kind"]
        for e in live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()[
            "items"
        ]
    ]
    for kind in (
        "installation_token_minted",
        "publisher_finished",
        "branch_pushed",
        "pull_request_opened",
        "publish_completed",
        "pull_request_polled",
    ):
        assert kind in kinds, kind


async def test_no_token_reaches_the_daemon_the_logs_or_the_database(
    ctx: AppContext,
    live_client: TestClient,
    live_supervisor: Supervisor,
    live_config: LiveConfig,
    github: RestGitHubClient,
    mirror: str,
    cleanup: github_live.Cleanup,
    worker_image: str,
    provider: DockerProvider,
) -> None:
    """12 and S10's absence proof, at the level this tier can assert it.

    The publisher container is gone by the time the assertions run, so what is checked is
    every place the value could have been left behind: the container inspection the
    daemon still holds for the run, the daemon's log of it, the events, and the database.
    """
    register(ctx, live_config, mirror)
    external_id = f"C4T-{RUN_ID}"
    branch = github_live.branch_for(external_id)
    cleanup.add_branch(branch)
    document = live_contract(external_id, live_config, worker_image)
    task_id = submit_and_start(live_client, document)
    await run_until(
        live_supervisor, live_client, task_id, {"awaiting_internal_review"}, max_ticks=60
    )
    upload_review(live_client, task_id)
    await run_until(live_supervisor, live_client, task_id, {"awaiting_acceptance"}, max_ticks=20)
    view = live_client.get(f"/v1/tasks/{task_id}").json()
    live_client.post(
        f"/v1/tasks/{task_id}/accept",
        json={"verdict": "accepted", "reasoning": "publication only", "head_sha": view["head_sha"]},
    )
    await run_until(
        live_supervisor,
        live_client,
        task_id,
        {"awaiting_external_review", "awaiting_ci_certification", "publish_failed"},
        max_ticks=20,
        pause=2.0,
    )
    record = live_client.get(f"/v1/tasks/{task_id}/pull-request").json()
    cleanup.add_pull_request(int(record["number"]))

    haystack: list[str] = []
    events = live_client.get(f"/v1/tasks/{task_id}/events", params={"limit": 200}).json()
    haystack.append(str(events))
    with ctx.engine.begin() as connection:
        for table, column in connection.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type IN "
                "('text', 'character varying', 'jsonb')"
            )
        ).all():
            values = connection.execute(
                text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}"')
            ).scalars()
            haystack.extend(str(v) for v in values if v is not None)
    blob = "\n".join(haystack)
    # No installation token, in any of its shapes, anywhere Crucible wrote.
    assert scan_text(blob) is None, "a secret-shaped value reached a Crucible record"
    assert "ghs_" not in blob
    # And the publisher's output directory kept no copy of it either.
    publish_root = Path(provider.config.artifact_root) / "publish"
    for path in publish_root.rglob("*"):
        if path.is_file():
            content = path.read_bytes()[:1_000_000].decode("utf-8", "replace")
            assert "ghs_" not in content, path
            assert scan_text(content) is None, path
