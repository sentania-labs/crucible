"""API auth, roles, idempotency keys, problem details, validation, pagination (04, 18)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from tests.fixtures import contract_document

pytestmark = pytest.mark.integration


def _client(ctx: AppContext, token: str | None) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(create_app(ctx), headers=headers)


def test_health_needs_no_auth(ctx: AppContext) -> None:
    r = _client(ctx, None).get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["schema_version"] == "1.0"


def test_missing_and_bad_tokens(ctx: AppContext, tokens: dict[str, str]) -> None:
    r = _client(ctx, None).get("/v1/tasks")
    assert r.status_code == 401
    assert r.json()["type"] == "urn:crucible:problem:unauthorized"
    r = _client(ctx, "cru_nope.nope").get("/v1/tasks")
    assert r.status_code == 401
    good = tokens["observer"]
    tampered = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    assert _client(ctx, tampered).get("/v1/tasks").status_code == 401


def test_observer_may_only_read(ctx: AppContext, tokens: dict[str, str]) -> None:
    c = _client(ctx, tokens["observer"])
    assert c.get("/v1/tasks").status_code == 200
    r = c.post("/v1/tasks", json=contract_document())
    assert r.status_code == 403 and r.json()["type"] == "urn:crucible:problem:forbidden"


def test_admin_only_repository_registration(ctx: AppContext, tokens: dict[str, str]) -> None:
    body = {
        "url": "https://github.com/example-org/other",
        "default_branch": "main",
        "policy_name": "default-software",
    }
    assert (
        _client(ctx, tokens["orchestrator"]).put("/v1/repositories/other", json=body).status_code
        == 403
    )
    r = _client(ctx, tokens["admin"]).put("/v1/repositories/other", json=body)
    assert r.status_code == 200 and r.json()["registered_by"] == "admin-principal"
    assert (
        _client(ctx, tokens["observer"]).get("/v1/repositories/other").json()["url"] == body["url"]
    )
    assert _client(ctx, tokens["observer"]).get("/v1/repositories/missing").status_code == 404


def test_submit_validation_problem_details(client: TestClient) -> None:
    doc = contract_document(external_id="EX-BAD")
    doc["repository"]["name"] = "not-registered"
    doc["lifecycle"]["max_attempts"] = 9
    doc["execution_request"]["image"] = "evil/image:latest"
    doc["required_verification"] = [{"id": "V1", "command": "make lint"}]
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:crucible:problem:contract-invalid"
    paths = {e["path"] for e in body["errors"]}
    assert {
        "repository.name",
        "lifecycle.max_attempts",
        "execution_request.image",
        "required_verification",
    } <= paths
    rejected = client.get("/v1/events", params={"kind": "contract_rejected"}).json()["items"]
    assert len(rejected) == 1 and rejected[0]["principal"] == "orchestrator-principal"


def test_submit_shape_errors_name_the_path(client: TestClient) -> None:
    r = client.post("/v1/tasks", json=contract_document(surprise=True))
    assert r.status_code == 422
    assert any(e["path"] == "surprise" for e in r.json()["errors"])


def test_docker_provider_not_registered_in_c1(client: TestClient) -> None:
    doc = contract_document()
    doc["execution_request"]["provider"] = "docker"
    r = client.post("/v1/tasks", json=doc)
    assert r.status_code == 422
    assert any("not registered" in e["message"] for e in r.json()["errors"])


def test_protected_branch_and_pattern(client: TestClient) -> None:
    doc = contract_document()
    doc["repository"]["work_branch"] = "main"
    r = client.post("/v1/tasks", json=doc)
    assert any(e["path"] == "repository.work_branch" for e in r.json()["errors"])


def test_closes_must_be_same_repository(client: TestClient) -> None:
    doc = contract_document()
    doc["deliverables"][0]["closes"] = ["https://github.com/other/repo/issues/1"]
    r = client.post("/v1/tasks", json=doc)
    assert any(e["path"] == "deliverables[0].closes[0]" for e in r.json()["errors"])


def test_duplicate_external_id_is_409(client: TestClient) -> None:
    assert client.post("/v1/tasks", json=contract_document()).status_code == 201
    r = client.post("/v1/tasks", json=contract_document())
    assert r.status_code == 409 and r.json()["type"] == "urn:crucible:problem:external-id-exists"


def test_idempotency_key(client: TestClient) -> None:
    doc = contract_document()
    r1 = client.post("/v1/tasks", json=doc, headers={"Idempotency-Key": "abc"})
    assert r1.status_code == 201
    r2 = client.post("/v1/tasks", json=doc, headers={"Idempotency-Key": "abc"})
    assert r2.status_code == 201 and r2.json() == r1.json()
    assert r2.headers["idempotent-replayed"] == "true"
    other = contract_document(title="different")
    r3 = client.post("/v1/tasks", json=other, headers={"Idempotency-Key": "abc"})
    assert r3.status_code == 422
    assert r3.json()["type"] == "urn:crucible:problem:idempotency-key-reuse"
    assert client.get("/v1/tasks").json()["items"].__len__() == 1


def test_start_disagreeing_with_contract_is_422(client: TestClient) -> None:
    task_id = client.post("/v1/tasks", json=contract_document()).json()["id"]
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "harness": "claude_code",
            "model": "gpt-5-codex-mini",
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "policy_version": 1,
        },
    )
    assert r.status_code == 422 and any(e["path"] == "harness" for e in r.json()["errors"])
    r = client.post(
        f"/v1/tasks/{task_id}/start",
        json={
            "harness": "codex",
            "model": "gpt-5-codex-mini",
            "provider": "fake",
            "image": "crucible-worker:fake-succeed",
            "policy_version": 1,
            "overrides": {"model": "x"},
        },
    )
    assert r.status_code == 422 and any(e["path"] == "overrides" for e in r.json()["errors"])
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "submitted"


def test_start_twice_is_409(client: TestClient) -> None:
    task_id = client.post("/v1/tasks", json=contract_document()).json()["id"]
    body = {
        "harness": "codex",
        "model": "gpt-5-codex-mini",
        "provider": "fake",
        "image": "crucible-worker:fake-succeed",
        "policy_version": 1,
    }
    assert client.post(f"/v1/tasks/{task_id}/start", json=body).status_code == 200
    r = client.post(f"/v1/tasks/{task_id}/start", json=body)
    assert (
        r.status_code == 409 and r.json()["type"] == "urn:crucible:problem:transition-not-allowed"
    )


def test_not_found_problems(client: TestClient) -> None:
    for path in (
        "/v1/tasks/01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "/v1/attempts/x",
        "/v1/executions/x",
        "/v1/tasks/x/events",
    ):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["type"] == "urn:crucible:problem:not-found", path
    r = client.get("/v1/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")


def test_list_filters_and_pagination(client: TestClient) -> None:
    ids = []
    for i in range(5):
        doc = contract_document(
            external_id=f"EX-{i:04d}", project="p-even" if i % 2 == 0 else "p-odd"
        )
        doc["repository"]["work_branch"] = f"crucible/EX-{i:04d}"
        ids.append(client.post("/v1/tasks", json=doc).json()["id"])
    page1 = client.get("/v1/tasks", params={"limit": 2}).json()
    assert [t["id"] for t in page1["items"]] == ids[:2] and page1["next_cursor"]
    page2 = client.get("/v1/tasks", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [t["id"] for t in page2["items"]] == ids[2:4]
    page3 = client.get("/v1/tasks", params={"limit": 2, "cursor": page2["next_cursor"]}).json()
    assert [t["id"] for t in page3["items"]] == ids[4:] and page3["next_cursor"] is None
    assert len(client.get("/v1/tasks", params={"project": "p-even"}).json()["items"]) == 3
    assert len(client.get("/v1/tasks", params={"state": "submitted"}).json()["items"]) == 5
    assert len(client.get("/v1/tasks", params={"external_id": "EX-0003"}).json()["items"]) == 1
    assert client.get("/v1/tasks", params={"repository": "unknown"}).json()["items"] == []
    assert (
        client.get("/v1/tasks", params={"repository": "example-service"}).json()["items"].__len__()
        == 5
    )
    assert (
        client.get("/v1/tasks", params={"updated_since": "2030-01-01T00:00:00+00:00"}).json()[
            "items"
        ]
        == []
    )
    assert client.get("/v1/tasks", params={"limit": 0}).status_code == 422


def test_global_events_pagination(client: TestClient) -> None:
    for i in range(3):
        doc = contract_document(external_id=f"EX-{i}")
        doc["repository"]["work_branch"] = f"crucible/EX-{i}"
        client.post("/v1/tasks", json=doc)
    page = client.get("/v1/events", params={"limit": 2, "kind": "task_submitted"}).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = client.get(
        "/v1/events", params={"limit": 2, "kind": "task_submitted", "cursor": page["next_cursor"]}
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert rest["items"][0]["seq"] > page["items"][-1]["seq"]


def test_openapi_published(client: TestClient) -> None:
    spec = client.get("/v1/openapi.json").json()
    assert "/v1/tasks/{task_id}/start" in spec["paths"]
    assert "TaskView" in spec["components"]["schemas"]


def test_idempotency_key_is_scoped_by_route(client: TestClient) -> None:
    a = contract_document(external_id="EX-A")
    a["repository"]["work_branch"] = "crucible/EX-A"
    b = contract_document(external_id="EX-B")
    b["repository"]["work_branch"] = "crucible/EX-B"
    ida = client.post("/v1/tasks", json=a).json()["id"]
    idb = client.post("/v1/tasks", json=b).json()["id"]
    body = {"reason": "r", "verbatim": "stop", "decided_by": "scott"}
    r1 = client.post(f"/v1/tasks/{ida}/cancel", json=body, headers={"Idempotency-Key": "same"})
    assert r1.status_code == 200 and r1.json()["id"] == ida
    r2 = client.post(f"/v1/tasks/{idb}/cancel", json=body, headers={"Idempotency-Key": "same"})
    assert r2.status_code == 422, "the same key on another route must not replay task A"
    assert client.get(f"/v1/tasks/{idb}").json()["state"] == "submitted"
