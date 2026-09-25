"""The first-run setup through the admin API, the CLI and the UI (crucible#119, #120,
#121, #123), against the stand-in gateway and GitHub App of tools/smoke/first_run_stubs.py
over real loopback HTTP.

The gateway half runs with the Docker provider's credential directories; the GitHub half
runs on the Kubernetes provider's shape, with the App credential in a Secret the service
owns (ADR 0016) on the in-memory API server. The kind proof drives the same flow on a
real cluster (docs/implementation-notes/first-run.md)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.adapters.clock import SystemClock
from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.fake import FakeProvider
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.github.appauth import AppAuthenticator, AppConfig
from crucible.adapters.github.apps import RestGitHubApps
from crucible.adapters.github.client import RestGitHubClient
from crucible.adapters.github.credentials import SecretAppCredentials
from crucible.adapters.github.transport import RestTransport
from crucible.application.admin.context import AdminContext
from crucible.application.supervisor import Supervisor
from crucible.cli import admin as cli
from tests.admin_cli import admin_main
from tests.integration.test_admin import (
    fake_login_cli,
    run_cli,
    seed_credentials,
    ui_sign_in,
)

pytestmark = pytest.mark.integration

STUBS_PATH = Path(__file__).parents[2] / "tools" / "smoke" / "first_run_stubs.py"
GATEWAY_KEY = "vk_" + "g" * 40
APP_ID = 4242


def _stubs_module() -> Any:
    spec = importlib.util.spec_from_file_location("first_run_stubs", STUBS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["first_run_stubs"] = module
    spec.loader.exec_module(module)
    return module


STUBS = _stubs_module()


def _rsa_pem() -> tuple[str, str]:
    """A throwaway App key made for this run and never written anywhere."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture(scope="module")
def app_key() -> tuple[str, str]:
    return _rsa_pem()


@pytest.fixture
def stubs(app_key: tuple[str, str]) -> Iterator[Any]:
    config = {
        "gateway_key": GATEWAY_KEY,
        "models": ["fast", "coder-large"],
        "app_id": APP_ID,
        "app_slug": "crucible-test",
        "public_key_pem": app_key[1],
        "installations": [
            {
                "id": 7,
                "account": "octo-lab",
                "type": "Organization",
                "repositories": [
                    {"full_name": "octo-lab/widgets", "default_branch": "trunk"},
                    {"full_name": "octo-lab/gadgets", "default_branch": "main"},
                    {"full_name": "octo-lab/old", "archived": True},
                ],
            },
            {
                "id": 9,
                "account": "someone",
                "type": "User",
                "repositories": [{"full_name": "someone/notes", "default_branch": "main"}],
            },
        ],
    }
    with STUBS.StubServer(config) as server:
        yield server


@pytest.fixture
def k8s_api() -> FakeKubernetesApi:
    return FakeKubernetesApi()


@pytest.fixture
def admin_ctx(
    ctx: AppContext,
    provider: FakeProvider,
    tmp_path: Path,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
) -> AdminContext:
    """Credential directories for the harnesses (the Docker shape) and the GitHub App
    credential in a Secret on the in-memory API server (the Kubernetes shape), both
    pointed at the stubs."""
    assert ctx.harnesses is not None
    root = tmp_path / "credentials"
    root.mkdir()
    store = SecretAppCredentials(k8s_api, name="crucible-github-app")
    transport = RestTransport(stubs.url, timeout=10)
    authenticator = AppAuthenticator(
        AppConfig(app_id=0, private_key_path="", api_base=stubs.url),
        transport,
        credentials=store,
    )
    admin = AdminContext(
        uow_factory=ctx.uow_factory,
        clock=ctx.clock,
        providers={"fake": provider},
        harnesses=ctx.harnesses,
        credential_sources=seed_credentials(root),
        artifact_root=str(tmp_path / "artifacts"),
        lease_ttl_seconds=30,
        credential_retention_hours=0,
        login_commands=fake_login_cli(tmp_path),
        probe_timeout_seconds=10,
        github=RestGitHubClient(authenticator, transport),
        github_credentials=store,
        github_apps=RestGitHubApps(authenticator, transport),
    )
    admin.github_app = type(admin.github_app)(api_base=stubs.url)
    ctx.admin = admin
    return admin


@pytest.fixture
def live(ctx: AppContext, provider: FakeProvider) -> Supervisor:
    return Supervisor(
        ctx.uow_factory,
        {"fake": provider},
        SystemClock(),
        holder="first-run-tests",
        artifact_store=ctx.artifact_store,
        lease_ttl_seconds=300,
        harnesses=ctx.harnesses,
    )


@pytest.fixture
def admin(
    ctx: AppContext, tokens: dict[str, str], admin_ctx: AdminContext, live: Supervisor
) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers={"Authorization": f"Bearer {tokens['admin']}"}) as c:
        yield c


@pytest.fixture
def config_file(migrated: str, admin_ctx: AdminContext, tmp_path: Path) -> Path:
    """The CLI's local mode: the same database and the same Hermes key directory."""
    source = admin_ctx.credential_sources["hermes"]
    path = tmp_path / "crucible.toml"
    path.write_text(
        "\n".join(
            [
                "[database]",
                f'url = "{migrated}"',
                "[supervisor]",
                "lease_ttl_seconds = 300",
                "[credentials.hermes]",
                f'path = "{source.path}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _readiness(client: TestClient, harness: str) -> dict[str, Any]:
    document = client.get("/v1/admin/status").json()["readiness"]
    return next(h for h in document["harnesses"] if h["name"] == harness)


def test_the_gateway_is_set_tested_and_its_models_picked(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    ctx: AppContext,
    tokens: dict[str, str],
    config_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live.tick())
    endpoint = f"{stubs.url}/v1"
    before = admin.get("/v1/admin/gateway").json()
    assert before["endpoint_url"] is None and before["key_set"] is False
    codes = [s["code"] for s in _readiness(admin, "hermes")["steps"]]
    assert codes[:2] == ["credential_missing", "endpoint_not_configured"]

    # #119: the URL and the key in one step, tested at once, reported in plain words.
    saved = admin.post(
        "/v1/admin/gateway",
        json={"reason": "first run", "endpoint_url": endpoint, "api_key": GATEWAY_KEY},
    )
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["test"]["passed"] is True
    assert result["test"]["summary"] == f"Gateway {endpoint} reachable, key accepted, 2 models."
    assert GATEWAY_KEY not in saved.text
    assert result["gateway"]["endpoint_url"] == endpoint
    assert result["gateway"]["credential_state"] == "validated"
    assert "GET /health/readiness" in stubs.stubs.seen and "GET /v1/models" in stubs.stubs.seen

    # #121: what the key can see, beside the seeded `coder` entry the gateway lacks.
    listing = admin.get("/v1/admin/gateway/models").json()
    rows = {row["id"]: row for row in listing["models"]}
    assert listing["reachable"] is True and listing["offered_count"] == 2
    assert rows["fast"]["offered"] is True and rows["fast"]["in_policy"] is False
    assert rows["coder"]["offered"] is False and rows["coder"]["in_policy"] is True

    refused = admin.post(
        "/v1/admin/gateway/models",
        json={"reason": "typo", "models": [{"id": "nope", "enabled": True}]},
    )
    assert refused.status_code == 409 and "does not offer ['nope']" in refused.json()["detail"]

    picked = admin.post(
        "/v1/admin/gateway/models",
        json={
            "reason": "use the fast model",
            "models": [{"id": "fast", "enabled": True, "enable_thinking": True}],
            "max_concurrency": 2,
        },
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["added"] == ["fast"] and picked.json()["enabled"] == ["fast"]
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    fast = next(m for m in local["models"] if m["id"] == "fast")
    assert fast["harness"] == "hermes" and fast["endpoint_url"] == endpoint
    assert fast["chat_template_kwargs"] == {"enable_thinking": True}
    assert local["pool"]["max_concurrency"] == 2
    codes = [s["code"] for s in _readiness(admin, "hermes")["steps"]]
    assert codes == ["no_promoted_image"]

    # A model the gateway stops offering is disabled with that reason, not removed.
    stubs.stubs.config["models"] = ["coder-large"]
    moved = admin.post(
        "/v1/admin/gateway/models",
        json={"reason": "gateway changed", "models": [{"id": "coder-large", "enabled": True}]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["disabled_not_offered"] == ["fast"]
    local = admin.get("/v1/admin/routing/local-endpoint").json()
    fast = next(m for m in local["models"] if m["id"] == "fast")
    assert fast["enabled"] is False and "no longer offers" in fast["disabled_reason"]

    # The CLI reads and writes the same state.
    shown = run_cli(config_file, "gateway", "show", capsys=capsys)
    assert shown["endpoint_url"] == endpoint and shown["key_set"] is True
    cli_models = run_cli(config_file, "gateway", "models", capsys=capsys)
    assert [row["id"] for row in cli_models["models"]][:1] == ["coder-large"]
    cli_pick = run_cli(
        config_file,
        "--reason",
        "thinking on",
        "gateway",
        "pick",
        "--enable",
        "coder-large",
        "--thinking",
        "coder-large",
        capsys=capsys,
    )
    assert cli_pick["enabled"] == ["coder-large"]

    # A wrong key is saved (the operator may fix the gateway next) and said plainly.
    wrong = admin.post(
        "/v1/admin/gateway",
        json={"reason": "wrong key", "endpoint_url": endpoint, "api_key": "vk_" + "w" * 40},
    )
    assert wrong.status_code == 200
    assert wrong.json()["test"]["summary"] == (
        f"Gateway {endpoint} reachable, but it refused the key (HTTP 401)."
    )
    audit = admin.get("/v1/admin/audit", params={"limit": 200}).text
    assert "local_gateway_updated" in audit
    assert GATEWAY_KEY not in audit and "w" * 40 not in audit

    # The UI page shows the URL, the result and the pick form, never the key.
    with TestClient(create_app(ctx)) as browser:
        ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/gateway")
        assert page.status_code == 200
        assert endpoint in page.text and GATEWAY_KEY not in page.text
        assert "the last test was refused" in page.text
        # With the wrong key the gateway lists nothing, so the page says why and still
        # shows the entries in force.
        assert f"Gateway {endpoint} refused the key (HTTP 401)." in page.text
        assert re.search(r'name="model\.\d\.id" value="coder-large"', page.text)


def test_github_is_connected_installed_and_a_repository_picked(
    admin: TestClient,
    live: Supervisor,
    stubs: Any,
    k8s_api: FakeKubernetesApi,
    app_key: tuple[str, str],
    ctx: AppContext,
    tokens: dict[str, str],
) -> None:
    asyncio.run(live.tick())
    assert admin.get("/v1/admin/github").json()["configured"] is False
    picker = admin.get("/v1/admin/github/installations").json()
    assert picker["connected"] is False and "No GitHub App is connected" in picker["error"]

    # A key that is not the App's is refused by GitHub, and nothing is stored.
    other, _ = _rsa_pem()
    refused = admin.post(
        "/v1/admin/github/app",
        json={"reason": "connect", "app_id": APP_ID, "private_key": other},
    )
    assert refused.status_code == 409
    assert f"GitHub refused the key for App {APP_ID} (HTTP 401)" in refused.json()["detail"]
    assert ("secrets", "crucible-github-app") not in k8s_api.objects

    connected = admin.post(
        "/v1/admin/github/app",
        json={"reason": "connect", "app_id": APP_ID, "private_key": app_key[0]},
    )
    assert connected.status_code == 200, connected.text
    body = connected.json()
    assert body["configured"] is True and body["app_id"] == APP_ID
    assert body["install_url"] == "https://github.com/apps/crucible-test/installations/new"
    assert body["stored_in"]["kind"] == "secret" and body["stored_in"]["service_owned"] is True
    assert "PRIVATE KEY" not in connected.text
    secret = k8s_api.objects[("secrets", "crucible-github-app")].body
    assert secret["metadata"]["labels"][k8sspec.LABEL_MANAGED_BY] == "crucible"
    assert set(secret["data"]) == {"app-id", "app.pem"}
    audit = admin.get("/v1/admin/audit", params={"limit": 200}).text
    assert "github_app_connected" in audit and "PRIVATE KEY" not in audit

    # #120: the picker, grouped by account, and a pick registers with GitHub's facts.
    picker = admin.get("/v1/admin/github/installations").json()
    assert [i["account"] for i in picker["installations"]] == ["octo-lab", "someone"]
    widgets = picker["installations"][0]["repositories"][-1]
    assert widgets["full_name"] == "octo-lab/widgets" and widgets["registered_as"] is None
    added = admin.post(
        "/v1/admin/github/repositories",
        json={
            "reason": "first repository",
            "installation_id": 7,
            "repository": "octo-lab/widgets",
            "attested_all_prs": True,
        },
    )
    assert added.status_code == 200, added.text
    registered = added.json()
    assert registered["repository"] == "widgets"
    assert registered["url"] == "https://github.com/octo-lab/widgets"
    assert registered["default_branch"] == "trunk" and registered["installation_id"] == 7
    picker = admin.get("/v1/admin/github/installations").json()
    widgets = next(
        r for r in picker["installations"][0]["repositories"] if r["full_name"].endswith("widgets")
    )
    assert widgets["registered_as"] == "widgets"
    uncovered = admin.post(
        "/v1/admin/github/repositories",
        json={
            "reason": "wrong installation",
            "installation_id": 9,
            "repository": "octo-lab/gadgets",
        },
    )
    assert uncovered.status_code == 409 and "does not cover" in uncovered.json()["detail"]
    archived = admin.post(
        "/v1/admin/github/repositories",
        json={"reason": "archived", "installation_id": 7, "repository": "octo-lab/old"},
    )
    assert archived.status_code == 409 and "archived" in archived.json()["detail"]
    steps = admin.get("/v1/admin/status").json()["readiness"]["steps"]
    assert "no_repository" not in [s["code"] for s in steps]

    # The UI: the install link, the picker's select of unregistered repositories.
    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/github")
        assert page.status_code == 200
        assert 'href="https://github.com/apps/crucible-test/installations/new"' in page.text
        assert '<option value="octo-lab/gadgets"' in page.text
        assert '<option value="octo-lab/widgets"' not in page.text
        assert "PRIVATE KEY" not in page.text
        posted = browser.post(
            "/ui/actions/github-add-repository",
            data={
                "csrf": csrf,
                "installation_id": "7",
                "repository": "octo-lab/gadgets",
                "name": "",
                "policy_name": "default-software",
                "attested_all_prs": "true",
                "reason": "second repository",
                "return_to": "/ui/github",
            },
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert "Registered gadgets" in unquote(posted.headers["location"])
    names = {r["repository"] for r in admin.get("/v1/admin/repositories").json()["items"]}
    assert {"widgets", "gadgets"} <= names


def test_the_cli_remote_mode_builds_the_first_run_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crucible.client.config import ADMIN_TOKEN_ENV  # noqa: PLC0415
    from crucible.client.http import Api  # noqa: PLC0415

    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"ok": True}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setattr(cli, "_read_api_key", lambda: GATEWAY_KEY)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    pem = tmp_path / "app.pem"
    pem.write_text("PEM-TEXT\n", encoding="utf-8")
    base = ["--api-url", "http://127.0.0.1:1", "--reason", "r"]
    admin_main([*base, "gateway", "set", "--endpoint-url", "http://gw/v1", "--key"])
    admin_main([*base, "gateway", "test"])
    admin_main(["--api-url", "http://127.0.0.1:1", "gateway", "models"])
    admin_main([*base, "gateway", "pick", "--enable", "a", "--disable", "b", "--thinking", "a"])
    admin_main([*base, "github", "connect", "--app-id", "5", "--private-key-file", str(pem)])
    admin_main(["--api-url", "http://127.0.0.1:1", "github", "installations"])
    admin_main([*base, "github", "add-repository", "--installation-id", "7", "--repository", "o/r"])
    expected: list[tuple[str, str, Any]] = [
        (
            "POST",
            "/v1/admin/gateway",
            {"reason": "r", "endpoint_url": "http://gw/v1", "api_key": GATEWAY_KEY},
        ),
        ("POST", "/v1/admin/gateway/test", {"reason": "r"}),
        ("GET", "/v1/admin/gateway/models", None),
        (
            "POST",
            "/v1/admin/gateway/models",
            {
                "reason": "r",
                "models": [
                    {"id": "a", "enabled": True, "enable_thinking": True, "capability": None},
                    {"id": "b", "enabled": False, "enable_thinking": False, "capability": None},
                ],
                "max_concurrency": None,
            },
        ),
        (
            "POST",
            "/v1/admin/github/app",
            {"reason": "r", "app_id": 5, "private_key": "PEM-TEXT\n", "webhook_secret": None},
        ),
        ("GET", "/v1/admin/github/installations", None),
        (
            "POST",
            "/v1/admin/github/repositories",
            {
                "reason": "r",
                "installation_id": 7,
                "repository": "o/r",
                "name": None,
                "policy_name": "default-software",
                "attested_all_prs": False,
                "attested_by": None,
            },
        ),
    ]
    assert calls == expected
    assert json.dumps(calls).count("PEM-TEXT") == 1
    assert re.search(r"cru_", json.dumps(calls)) is None
