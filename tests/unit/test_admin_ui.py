"""Small template checks for the script-free administrative surface."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.requests import Request

from crucible.adapters.ui.router import templates


def request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


def base_context(path: str) -> dict[str, object]:
    return {
        "request": request(path),
        "title": "Test",
        "active": "/ui",
        "nav": (("/ui", "Status"),),
        "principal": SimpleNamespace(name="reader", role=SimpleNamespace(value="observer")),
        "csrf": "fixture-csrf",
        "message": None,
    }


def test_page_template_uses_lattice_in_order_and_carries_csrf() -> None:
    rendered = templates.get_template("page.html").render(
        **base_context("/ui"),
        heading="Status",
        intro="Fixture",
        sections=[
            {
                "title": "Action",
                "form": {
                    "action": "/ui/actions/harness",
                    "label": "Apply",
                    "fields": [{"name": "reason", "label": "Reason", "required": True}],
                },
            }
        ],
        badge=None,
    )
    assert rendered.index("/ui/static/tokens.css") < rendered.index("/ui/static/lattice.css")
    assert 'data-theme="dark"' in rendered
    assert 'name="csrf" value="fixture-csrf"' in rendered
    assert "<script" not in rendered


def test_login_template_has_refresh_fallback_and_only_polling_script() -> None:
    context = base_context("/ui/credentials/codex/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    rendered = templates.get_template("login.html").render(
        **context,
        harness="codex",
        login={
            "state": "starting",
            "window": "Device flow",
            "url": None,
            "code": None,
            "output_tail": [],
            "error": None,
        },
    )
    assert "Refresh status" in rendered
    assert rendered.count("<script>") == 1
    assert "setTimeout" in rendered


def test_reader_login_page_has_state_but_no_operator_controls() -> None:
    rendered = templates.get_template("login.html").render(
        **base_context("/ui/credentials/codex/login"),
        harness="codex",
        login={
            "state": "waiting_for_operator",
            "window": "Device flow",
            "url": "https://example.invalid/device",
            "code": "ABCD-EFGH",
            "output_tail": ["sensitive operator output"],
            "error": None,
        },
    )
    assert "Administrator access is required" in rendered
    assert "waiting_for_operator" in rendered
    assert "ABCD-EFGH" not in rendered
    assert "sensitive operator output" not in rendered
    assert "/ui/actions/login-" not in rendered
    assert "<script>" not in rendered
