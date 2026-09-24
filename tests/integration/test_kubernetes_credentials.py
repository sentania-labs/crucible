"""Credential administration on the Kubernetes provider (25, 26, ADR 0015, #92).

The admin API, the UI and the CLI's remote calls land on the same services; here they
run against a real database with the Kubernetes provider on the in-memory API. The
login is a Job and the credential is the harness Secret the service owns: an empty
namespace is logged into, finished, validated and probed through `/v1/admin`, the
Hermes key is set from the Routing page, and a login and an attempt of one harness are
kept apart (12). The kind tier drives the same flow on a real cluster."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeLogin, FakeRegistry
from crucible.adapters.execution.kubernetes import KubernetesConfig, KubernetesProvider
from crucible.adapters.harness.registry import default_registry
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.domain.entities import ImagePromotion
from crucible.domain.lifecycle import AttemptState
from crucible.domain.secrets import scan_text
from tests.integration.test_admin import ui_sign_in
from tests.integration.test_harness_registry import _submit_pinned

pytestmark = pytest.mark.integration

WORKER = "ghcr.io/sentania-labs/crucible-worker:20260916-k8s-login"
LABELS = {
    "crucible.harnesses": "agy,claude_code,codex,hermes",
    "crucible.harness.agy.version": "1.2.8",
    "crucible.harness.claude_code.version": "2.1.280",
    "crucible.harness.codex.version": "0.156.0",
    "crucible.harness.hermes.version": "0.19.0",
}
HOSTS = {
    "auth.openai.com": ["162.159.140.246/32"],
    "api.openai.com": ["162.159.140.245/32"],
    "chatgpt.com": ["162.159.140.247/32"],
    "platform.claude.com": ["160.79.104.20/32"],
    "api.anthropic.com": ["160.79.104.10/32"],
}
CODEX_AUTH = json.dumps(
    {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": "2026-09-24T00:00:00Z"}
).encode()


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def k8s_provider(k8s_api: FakeKubernetesApi) -> KubernetesProvider:
    registry = FakeRegistry(k8s_api)
    registry.register(WORKER, labels=LABELS)
    return KubernetesProvider(
        KubernetesConfig(
            poll_interval_seconds=0,
            launch_timeout_seconds=5,
            extra_image_allowlist=("ghcr.io/sentania-labs/crucible-worker:*",),
        ),
        k8s_api,  # type: ignore[arg-type]
        registry,
        harnesses=default_registry(),
        resolver=lambda host: list(HOSTS.get(host, ["203.0.113.1/32"])),
    )


@pytest.fixture
def admin_ctx(
    ctx: AppContext, provider: FakeProvider, k8s_provider: KubernetesProvider
) -> AdminContext:
    """A Kubernetes deployment: the fake provider is always wired, Docker is not, so
    the harness credentials are the Secrets the service owns (ADR 0015). No credential
    directory is configured anywhere."""
    assert ctx.harnesses is not None
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider, "kubernetes": k8s_provider},
        harnesses=ctx.harnesses,
        lease_ttl_seconds=30,
        login_timeout_seconds=10,
        probe_timeout_seconds=10,
    )
    ctx.admin = admin
    with ctx.uow_factory() as uow:
        uow.image_promotions.put(
            ImagePromotion(
                digest="sha256:" + "d" * 64,
                reference=WORKER,
                harnesses={k.rsplit(".", 2)[-2]: v for k, v in LABELS.items() if "version" in k},
                state="default",
                updated_at=ctx.clock.now(),
                updated_by="tests",
                reason="the worker image the login and the probe run",
            )
        )
        uow.commit()
    return admin


@pytest.fixture
def live(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    supervisor = Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="k8s-credentials",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )
    asyncio.run(supervisor.tick())
    return supervisor


@pytest.fixture
def admin(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext, live: Supervisor
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


def poll(client: TestClient, harness: str, *states: str) -> dict[str, Any]:
    for _ in range(400):
        state: dict[str, Any] = client.get(f"/v1/admin/credentials/{harness}/login").json()
        if state["state"] in states:
            return state
        time.sleep(0.02)
    raise AssertionError(f"the {harness} login never reached {states}: {state}")


def test_a_codex_login_fills_an_empty_namespace_and_the_probe_validates_it(
    admin: TestClient, k8s_api: FakeKubernetesApi
) -> None:
    before = admin.get("/v1/admin/credentials/codex").json()
    assert before["state"] == "absent"
    assert before["source"] == {
        "kind": "secret",
        "name": "crucible-harness-codex",
        "exists": False,
        "service_owned": False,
    }
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=3)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    assert started.json()["secret"] == "crucible-harness-codex"
    assert started.json()["retained_as"] is None
    state = poll(admin, "codex", "finished", "failed")
    assert state["state"] == "finished", state
    assert state["url"] == "https://example.invalid/device" and state["code"] == "C7AA-TEST"
    assert state["credential_written"] is True
    finished = admin.post("/v1/admin/credentials/codex/login/finish", json={"reason": "done"})
    assert finished.status_code == 200, finished.text
    assert finished.json()["shape"]["ok"] is True
    after = admin.get("/v1/admin/credentials/codex").json()
    assert after["state"] == "configured"
    assert after["source"]["exists"] and after["source"]["service_owned"]

    validated = admin.post("/v1/admin/credentials/codex/validate", json={"reason": "probe"})
    assert validated.status_code == 200, validated.text
    document = validated.json()
    assert document["probe"]["exit_class"] == "completed", document
    assert document["probe"]["image"] == WORKER
    assert document["probe"]["harness_version"] == "0.156.0"
    assert document["credential"]["state"] == "validated"
    assert scan_text(json.dumps(document)) is None
    # The probe left nothing but the harness Secret behind.
    for kind in ("jobs", "pods", "networkpolicies", "configmaps", "persistentvolumeclaims"):
        assert k8s_api.object_names(kind) == [], kind
    assert k8s_api.object_names("secrets") == ["crucible-harness-codex"]
    kinds = [
        row["kind"] for row in admin.get("/v1/admin/audit", params={"limit": 50}).json()["items"]
    ]
    for kind in (
        "credential_login_started",
        "credential_login_finished",
        "credential_validated",
        "credential_probed",
    ):
        assert kind in kinds, kind


def test_a_claude_login_takes_the_pasted_code_and_never_shows_the_token(
    admin: TestClient, k8s_api: FakeKubernetesApi
) -> None:
    k8s_api.login = FakeLogin(
        lines=["https://platform.claude.com/oauth/authorize?code=true"],
        prompt="Paste code here if prompted > ",
        after_code=["[pasted code]", "[captured to oauth-token]"],
        files={"/home/worker/.claude/oauth-token": b"not-a-real-value\n"},
    )
    admin.post("/v1/admin/credentials/claude_code/login", json={"reason": "onboarding"})
    waiting = poll(admin, "claude_code", "waiting_for_code", "failed")
    assert waiting["state"] == "waiting_for_code", waiting
    code = admin.post(
        "/v1/admin/credentials/claude_code/login/code",
        json={"code": "the-code#state", "reason": "complete onboarding"},
    )
    assert code.status_code == 200, code.text
    done = poll(admin, "claude_code", "finished", "failed")
    assert done["state"] == "finished", done
    assert done["token_written"] is True and done["credential_written"] is True
    assert "[captured to oauth-token]" in done["output_tail"]
    assert k8s_api.harness_secret("crucible-harness-claude_code") == {
        "oauth-token": b"not-a-real-value\n"
    }
    # A second login does not replace a credential that passes the shape check unless
    # it is told to, and nothing is touched when it refuses.
    again = admin.post("/v1/admin/credentials/claude_code/login", json={"reason": "again"})
    assert again.status_code == 409 and "Pass replace" in again.json()["detail"]
    assert len([r for r in k8s_api.created if r["kind"] == "jobs"]) == 1


def test_rotate_and_remove_say_the_credential_is_a_secret(admin: TestClient) -> None:
    for verb, body in (("rotate", {"new_path": "/tmp/x"}), ("remove", {})):
        response = admin.post(f"/v1/admin/credentials/codex/{verb}", json={"reason": "r", **body})
        assert response.status_code == 409, response.text
        assert "crucible-harness-codex" in response.json()["detail"]


def test_the_hermes_key_is_set_from_the_routing_page_and_never_shown(
    admin: TestClient, ctx: AppContext, tokens: dict[str, str], k8s_api: FakeKubernetesApi
) -> None:
    api_key = "vk_" + "q" * 40
    assert admin.get("/v1/admin/credentials/hermes").json()["key_set"] is False
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert "Hermes API key" in page.text and "Set Hermes API key" in page.text
        saved = browser.post(
            "/ui/actions/credential-set",
            data={
                "csrf": csrf,
                "api_key": api_key,
                "reason": "hermes key from the routing page",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert saved.status_code in (302, 303), saved.text
        after = browser.get("/ui/routing").text
        assert api_key not in after
    assert k8s_api.harness_secret("crucible-harness-hermes") == {
        "api-key": api_key.encode() + b"\n"
    }
    labels = k8s_api.objects[("secrets", "crucible-harness-hermes")].body["metadata"]["labels"]
    assert labels[k8sspec.LABEL_MANAGED_BY] == "crucible"
    view = admin.get("/v1/admin/credentials/hermes")
    assert view.json()["key_set"] is True and api_key not in view.text
    audit = admin.get("/v1/admin/audit", params={"limit": 50}).text
    assert "credential_set" in audit and api_key not in audit
    # The API form writes the same Secret and returns no value either.
    replaced = admin.post(
        "/v1/admin/credentials/hermes/set",
        json={"reason": "rotate the key", "api_key": api_key[:-1] + "r"},
    )
    assert replaced.status_code == 200 and api_key[:-1] not in replaced.text


async def test_a_login_refuses_while_an_attempt_holds_the_credential(
    admin: TestClient, live: Supervisor, tokens: dict[str, str], k8s_api: FakeKubernetesApi
) -> None:
    """12: a login replaces the credential, so it waits for an attempt that holds a
    copy of it; once that attempt has been collected the login may start."""
    task_id = _submit_pinned(admin, tokens, "crucible-worker:fake-hang", "EX-HOLDS")
    await live.tick()
    await live.tick()
    view = admin.get(f"/v1/tasks/{task_id}").json()
    assert view["latest_attempt"]["state"] == "running", view
    refused = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert refused.status_code == 409, refused.text
    assert "holds its credential" in refused.json()["detail"]
    assert view["latest_attempt"]["id"] in refused.json()["detail"]
    with live._fenced() as uow:
        attempt = uow.attempts.get(view["latest_attempt"]["id"], for_update=True)
        assert attempt is not None
        attempt.state = AttemptState.COLLECTED
        uow.attempts.save(attempt)
        uow.commit()
    k8s_api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=5)
    started = admin.post("/v1/admin/credentials/codex/login", json={"reason": "onboarding"})
    assert started.status_code == 200, started.text
    assert poll(admin, "codex", "finished", "failed")["state"] == "finished"


async def test_a_launch_waits_while_a_login_for_its_harness_runs(
    ctx: AppContext,
    client: TestClient,
    provider: FakeProvider,
    k8s_provider: KubernetesProvider,
    k8s_api: FakeKubernetesApi,
    tokens: dict[str, str],
) -> None:
    """12: the login Job is the lock the supervisor sees. A codex launch is deferred,
    not failed, while one exists, and goes ahead once it is gone."""
    supervisor = Supervisor(
        ctx.uow_factory,
        {"fake": provider, "kubernetes": k8s_provider},
        ctx.clock,
        holder="k8s-login-defer",
        artifact_store=ctx.artifact_store,
        harnesses=ctx.harnesses,
        lease_ttl_seconds=30,
    )
    k8s_api.login = FakeLogin(never_exits=True)
    k8s_api.create(
        "jobs",
        {
            "metadata": {
                "name": "login-codex-login0000000000000000000",
                "labels": {
                    k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN,
                    k8sspec.LABEL_HARNESS: "codex",
                    k8sspec.LABEL_LOGIN: "login0000000000000000000",
                },
            },
            "spec": {"template": {"metadata": {"labels": {}}, "spec": {}}},
        },
    )
    task_id = _submit_pinned(client, tokens, "crucible-worker:fake-succeed", "EX-WAITS")
    await supervisor.tick()
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] == "scheduled"
    deferred = [
        e
        for e in client.get(f"/v1/tasks/{task_id}/events").json()["items"]
        if e["kind"] == "harness_launch_deferred"
    ]
    assert deferred and "a login for codex is running" in deferred[0]["payload"]["detail"]
    k8s_api.delete("jobs", "login-codex-login0000000000000000000")
    await supervisor.tick()
    await supervisor.tick()
    assert client.get(f"/v1/tasks/{task_id}").json()["state"] != "scheduled"
