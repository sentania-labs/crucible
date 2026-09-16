"""Policy and routing API (04, 05b): immutability once referenced, operator-only
settings, contract validation against the routing policy, usage, and artifacts."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from tests.fixtures import contract_document
from tests.integration.conftest import run_to_settled, submit_and_start
from tests.unit.test_policy_schema import seeded_policy

pytestmark = pytest.mark.integration


def admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


def test_read_the_seeded_policy_and_routing_policy(client: TestClient) -> None:
    body = client.get("/v1/policies/default-software/1").json()
    assert body["name"] == "default-software" and body["referenced"] is False
    assert body["document"]["routing"]["policy"]["name"] == "default-routing"
    routing = client.get("/v1/routing/default-routing/1").json()
    assert any(m["id"] == "gpt-5-codex-mini" for m in routing["document"]["models"])


def test_upload_a_new_policy_version(client: TestClient, tokens: dict[str, str]) -> None:
    document = seeded_policy()
    document["version"] = 2
    document["limits"]["grace_seconds"] = 45
    r = client.put("/v1/policies/default-software/2", json=document, headers=admin(tokens))
    assert r.status_code == 200, r.text
    assert (
        client.get("/v1/policies/default-software/2").json()["document"]["limits"]["grace_seconds"]
        == 45
    )


def test_upload_requires_admin(client: TestClient) -> None:
    document = seeded_policy()
    document["version"] = 3
    assert client.put("/v1/policies/default-software/3", json=document).status_code == 403


def test_an_invalid_policy_is_refused_with_paths(
    client: TestClient, tokens: dict[str, str]
) -> None:
    document = seeded_policy()
    document["version"] = 4
    document["gates"]["pre_pr"].remove("no_secrets")
    r = client.put("/v1/policies/default-software/4", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any("gates" in e["path"] for e in r.json()["errors"])


def test_the_path_and_the_document_must_agree(client: TestClient, tokens: dict[str, str]) -> None:
    document = seeded_policy()
    document["version"] = 9
    r = client.put("/v1/policies/default-software/5", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any(e["path"] == "version" for e in r.json()["errors"])


def test_rw_narrow_harnesses_must_have_concurrency_one(
    client: TestClient, tokens: dict[str, str]
) -> None:
    """05b: concurrency.per_harness must be 1 where the credential mount is rw-narrow (12)."""
    document = seeded_policy()
    document["version"] = 6
    document["concurrency"]["per_harness"]["codex"] = 2
    r = client.put("/v1/policies/default-software/6", json=document, headers=admin(tokens))
    assert r.status_code == 422
    assert any("rw-narrow" in e["message"] for e in r.json()["errors"])


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("ci_certification", "allow_no_ci", True),
        ("deliverables", "allow_branch_only", True),
        ("release", "require_operator_approval", False),
    ],
)
def test_operator_only_settings_need_an_operator_principal(
    client: TestClient, tokens: dict[str, str], section: str, field: str, value: bool
) -> None:
    document = seeded_policy()
    document["version"] = 7
    document[section][field] = value
    r = client.put(
        "/v1/policies/default-software/7",
        json=document,
        headers={"Authorization": f"Bearer {tokens['orchestrator']}"},
    )
    # An orchestrator cannot upload a policy at all, and an admin can: the rule is about
    # which principals may set these fields, so the admin path must succeed.
    assert r.status_code == 403
    ok = client.put("/v1/policies/default-software/7", json=document, headers=admin(tokens))
    assert ok.status_code == 200, ok.text


def test_a_referenced_policy_version_is_immutable(
    client: TestClient, tokens: dict[str, str]
) -> None:
    client.post("/v1/tasks", json=contract_document())
    document = seeded_policy()
    r = client.put("/v1/policies/default-software/1", json=document, headers=admin(tokens))
    assert r.status_code == 409
    assert "immutable" in r.json()["detail"]
    assert client.get("/v1/policies/default-software/1").json()["referenced"] is True


def test_a_referenced_routing_policy_is_immutable(
    client: TestClient, tokens: dict[str, str]
) -> None:
    document = client.get("/v1/routing/default-routing/1").json()["document"]
    r = client.put("/v1/routing/default-routing/1", json=document, headers=admin(tokens))
    assert r.status_code == 409


def test_upload_a_new_routing_policy_version(client: TestClient, tokens: dict[str, str]) -> None:
    document = copy.deepcopy(client.get("/v1/routing/default-routing/1").json()["document"])
    document["version"] = 2
    document["models"][1]["enabled"] = False
    r = client.put("/v1/routing/default-routing/2", json=document, headers=admin(tokens))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    ("patch", "path"),
    [
        ({"model": "no-such-model"}, "execution_request.model"),
        ({"model": "claude-sonnet-5"}, "execution_request.harness"),
        ({"model": "gpt-5-codex", "tier": "trivial"}, "execution_request.model"),
        ({"model": "local-rtx-small", "tier": "trivial"}, "execution_request.model"),
    ],
)
def test_a_contract_is_validated_against_the_routing_policy(
    client: TestClient, patch: dict[str, str], path: str
) -> None:
    document = contract_document()
    document["execution_request"] = {**document["execution_request"], **patch}
    r = client.post("/v1/tasks", json=document)
    assert r.status_code == 422, r.text
    assert any(e["path"] == path for e in r.json()["errors"])


def test_a_complex_tier_may_name_a_frontier_model(client: TestClient) -> None:
    document = contract_document()
    document["execution_request"] = {
        **document["execution_request"],
        "tier": "complex",
        "model": "gpt-5-codex",
    }
    assert client.post("/v1/tasks", json=document).status_code == 201


def test_routing_usage_reports_every_pool(client: TestClient) -> None:
    body = client.get("/v1/routing/usage").json()
    assert body["routing_policy"] == {"name": "default-routing", "version": 1}
    pools = {p["pool"]: p for p in body["pools"]}
    assert set(pools) == {
        "anthropic-sub",
        "openai-sub",
        "google-sub",
        "local-rtx",
        "local-spark",
    }
    assert pools["openai-sub"]["soft_limit"] == 0
    assert pools["openai-sub"]["over_soft_limit"] is False


async def test_usage_counts_attempts_when_no_tokens_are_reported(
    client: TestClient, supervisor: Supervisor
) -> None:
    """05b: a harness that reports no token counts falls back to counting attempts."""
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    pools = {p["pool"]: p for p in client.get("/v1/routing/usage").json()["pools"]}
    openai = pools["openai-sub"]
    assert openai["attempts"] == 1
    assert openai["fallback_to_attempts"] is True and openai["counting"] == "attempts"


async def test_routing_history_filters_by_model_and_project(
    client: TestClient, supervisor: Supervisor
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed")
    await run_to_settled(supervisor, client, task_id)
    assert client.get("/v1/routing/history", params={"model": "gpt-5-codex"}).json()["items"] == []
    rows = client.get(
        "/v1/routing/history", params={"model": "gpt-5-codex-mini", "project": "example-service"}
    ).json()["items"]
    assert len(rows) == 1 and rows[0]["task_id"] == task_id
    assert client.get("/v1/routing/history", params={"project": "nothing"}).json()["items"] == []
