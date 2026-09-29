"""ADR 0028: the routing order and demotion settings from the admin API, the CLI and the
Routing page. Each save writes a new routing version and a policy version naming it;
the version before is left as it was."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from crucible.adapters.api.app import create_app
from crucible.adapters.api.deps import AppContext
from crucible.application.supervisor import Supervisor
from crucible.client.config import ADMIN_TOKEN_ENV
from crucible.client.http import Api
from tests.admin_cli import admin_main
from tests.integration.test_admin import (  # noqa: F401
    admin_client,
    admin_ctx,
    config_file,
    credential_root,
    live_supervisor,
    run_cli,
    ui_sign_in,
)

pytestmark = pytest.mark.integration


def _routing(client: TestClient, view: dict[str, Any]) -> dict[str, Any]:
    ref = view["routing_policy"]
    response = client.get(f"/v1/routing/{ref['name']}/{ref['version']}")
    assert response.status_code == 200, response.text
    return dict(response.json()["document"])


def test_routing_preference_through_api_cli_and_ui(
    ctx: AppContext,
    tokens: dict[str, str],
    admin_client: TestClient,  # noqa: F811
    live_supervisor: Supervisor,  # noqa: F811
    config_file: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    asyncio.run(live_supervisor.tick())
    first = admin_client.get("/v1/admin/routing/preference").json()
    # The migrated in-force version names no order: the default reads Hermes first.
    assert first["local_pools"] == ["lab-local"]
    assert first["tiers"]["trivial"] == {
        "prefer_pools": ["lab-local"],
        "default": True,
        "prefer": ["small"],
        "allowed_capability": ["small", "mid"],
    }
    assert first["tiers"]["standard"]["prefer_pools"] == ["lab-local"]
    assert first["tiers"]["complex"]["prefer_pools"] == []
    assert first["rotation"]["demote_min_sample"] == 5
    before = _routing(admin_client, first)

    saved = admin_client.post(
        "/v1/admin/routing/preference",
        json={
            "reason": "api: subscriptions before the gateway for standard",
            "tiers": {"standard": ["anthropic-sub", "lab-local"]},
            "rotation": {"demote_min_sample": 3, "probe_after_minutes": 30},
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["routing_policy"]["version"] == first["routing_policy"]["version"] + 1
    assert body["policy"]["version"] == first["policy"]["version"] + 1
    assert body["tiers"]["standard"] == {
        **first["tiers"]["standard"],
        "prefer_pools": ["anthropic-sub", "lab-local"],
        "default": False,
    }
    assert body["tiers"]["trivial"]["default"] is True
    assert body["rotation"]["demote_min_sample"] == 3
    assert body["rotation"]["probe_after_minutes"] == 30
    stored = _routing(admin_client, body)
    assert stored["tiers"]["standard"]["prefer_pools"] == ["anthropic-sub", "lab-local"]
    # The version before is untouched: a task that references it routes as it did.
    assert _routing(admin_client, first) == before

    refusals: list[dict[str, Any]] = [
        {"tiers": {"standard": ["nowhere"]}},
        {"tiers": {"standard": "lab-local"}},
        {"tiers": {"urgent": ["lab-local"]}},
        {"rotation": {"demote_min_sample": 1}},
        {"rotation": {"demote_min_sample": "3"}},
        {"rotation": {"quality_feedback": "false"}},
        {"rotation": {"strategy": "random"}},
        {"rotation": {"demote_min_sample": 21}},
        {"rotation": {"probe_after_minutes": 10**13}},
        {"tiers": []},
        {},
        # The order already in force: nothing to save, so no version is written.
        {"tiers": {"standard": ["anthropic-sub", "lab-local"]}},
    ]
    for refused in refusals:
        response = admin_client.post(
            "/v1/admin/routing/preference", json={"reason": "api: refused", **refused}
        )
        assert response.status_code == 422, (refused, response.text)
    assert (
        admin_client.get("/v1/admin/routing/preference").json()["routing_policy"]
        == (body["routing_policy"])
    )

    cli_view = run_cli(config_file, "routing", "preference", capsys=capsys)
    assert cli_view["tiers"]["standard"]["prefer_pools"] == ["anthropic-sub", "lab-local"]
    cli_saved = run_cli(
        config_file,
        "--reason",
        "cli: back to the default for standard",
        "routing",
        "set-preference",
        "--tier=standard=default",
        "--tier=complex=google-sub",
        "--no-quality-feedback",
        capsys=capsys,
    )
    assert cli_saved["tiers"]["standard"]["default"] is True
    assert cli_saved["tiers"]["standard"]["prefer_pools"] == ["lab-local"]
    assert cli_saved["tiers"]["complex"]["prefer_pools"] == ["google-sub"]
    assert cli_saved["rotation"]["quality_feedback"] is False
    assert cli_saved["rotation"]["demote_min_sample"] == 3

    with TestClient(create_app(ctx)) as browser:
        csrf = ui_sign_in(browser, tokens["admin"])
        page = browser.get("/ui/routing")
        assert page.status_code == 200
        assert "Routing order" in page.text
        assert "complex: google-sub first" in page.text
        assert "off: failures never move routing" in page.text
        assert 'action="/ui/actions/routing-preference"' in page.text
        assert "Routing order and demotion" in page.text
        ui_saved = browser.post(
            "/ui/actions/routing-preference",
            data={
                "csrf": csrf,
                "default_trivial": "true",
                "prefer_trivial": "lab-local",
                # The box stays ticked, but the order typed over the default wins.
                "default_standard": "true",
                "prefer_standard": "lab-local, anthropic-sub",
                "default_complex": "true",
                "prefer_complex": "google-sub",
                "quality_feedback": "true",
                "quality_window": "20",
                "demote_failure_percent": "40",
                "demote_min_sample": "4",
                "probe_after_minutes": "90",
                "reason": "ui: gateway first, stricter demotion",
                "return_to": "/ui/routing",
            },
            follow_redirects=False,
        )
        assert ui_saved.status_code == 303
        assert "Completed" in unquote(ui_saved.headers.get("location", ""))
        page = browser.get("/ui/routing")
        assert "standard: lab-local then anthropic-sub first" in page.text
        assert "at 40% blocking-gate failures" in page.text
    final = admin_client.get("/v1/admin/routing/preference").json()
    assert final["tiers"]["standard"]["prefer_pools"] == ["lab-local", "anthropic-sub"]
    assert final["tiers"]["complex"] == {**first["tiers"]["complex"], "default": True}
    assert final["rotation"] == {
        "strategy": "weighted-least-recent",
        "quality_feedback": True,
        "quality_window": 20,
        "demote_failure_percent": 40,
        "demote_min_sample": 4,
        "probe_after_minutes": 90,
    }
    assert final["routing_policy"]["version"] == first["routing_policy"]["version"] + 3


def test_the_cli_remote_mode_sends_the_routing_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_call(self: Any, method: str, path: str, body: Any = None, **_: Any) -> Any:
        calls.append((method, path, body))
        return {"tiers": {}, "rotation": {}, "pools": []}

    monkeypatch.setattr(Api, "call", fake_call)
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "cru_" + "0" * 26 + "." + "s" * 40)
    admin_main(["--api-url", "http://127.0.0.1:1", "routing", "preference"])
    admin_main(
        [
            "--api-url",
            "http://127.0.0.1:1",
            "--reason",
            "r",
            "routing",
            "set-preference",
            "--tier=trivial=lab-local,anthropic-sub",
            "--tier=complex=",
            "--probe-after-minutes=45",
        ]
    )
    assert calls == [
        ("GET", "/v1/admin/routing/preference", None),
        (
            "POST",
            "/v1/admin/routing/preference",
            {
                "reason": "r",
                "tiers": {"trivial": ["lab-local", "anthropic-sub"], "complex": []},
                "rotation": {"probe_after_minutes": 45},
            },
        ),
    ]
