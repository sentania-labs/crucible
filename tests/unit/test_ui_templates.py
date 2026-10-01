"""Template checks for the script-free administrative surface."""

from __future__ import annotations

from types import SimpleNamespace

from crucible.adapters.ui.render import templates
from tests.unit.admin_ui_fixtures import base_context


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


def test_a_finishing_login_says_it_is_cleaning_up_and_offers_no_cancel() -> None:
    context = base_context("/ui/credentials/codex/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    rendered = templates.get_template("login.html").render(
        **context,
        harness="codex",
        login={
            "state": "finishing",
            "window": "Device flow",
            "url": None,
            "code": None,
            "output_tail": [],
            "error": None,
        },
    )
    assert "nothing left to cancel" in rendered
    assert "/ui/actions/login-cancel" not in rendered
    assert "/ui/actions/login-code" not in rendered
    assert "/ui/actions/login-finish" not in rendered
    assert "setTimeout" in rendered


def test_a_login_waiting_for_a_code_shows_the_link_the_prompt_and_the_deadline() -> None:
    """hades #173: the URL itself, the CLI's own prompt, what to do, and when AGY stops
    waiting; the page does not reload under an operator typing a code."""
    context = base_context("/ui/credentials/agy/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    rendered = templates.get_template("login.html").render(
        **context,
        harness="agy",
        login={
            "state": "waiting_for_code",
            "window": "AGY",
            "url": "https://accounts.google.com/o/oauth2/auth?x=1",
            "code": None,
            "prompt": "Or, paste the authorization code here and press Enter:",
            "guidance": ["Copy the value after code= from the address bar."],
            "code_wait_ends_at": "2026-09-29T02:15:00+00:00",
            "code_wait_local": "2026-09-28 09:15:00 PM CDT",
            "code_wait_seconds_left": 42,
            "output_tail": [],
            "error": None,
        },
    )
    assert "https://accounts.google.com/o/oauth2/auth?x=1</code>" in rendered
    assert "Or, paste the authorization code here and press Enter:" in rendered
    assert "Copy the value after code= from the address bar." in rendered
    assert "09:15:00 PM CDT" in rendered and ">42</span> seconds left" in rendered
    assert 'name="code"' in rendered
    assert "activeElement" in rendered and rendered.count("<script>") == 1


def test_an_ended_login_can_be_started_again_from_the_page() -> None:
    """hades #173: a login that failed (AGY's minute ran out) restarts from its page."""
    context = base_context("/ui/credentials/agy/login")
    context["principal"] = SimpleNamespace(name="admin", role=SimpleNamespace(value="admin"))
    for state in ("failed", "finished"):
        rendered = templates.get_template("login.html").render(
            **context,
            harness="agy",
            login={
                "state": state,
                "window": "AGY",
                "url": "https://accounts.google.com/o/oauth2/auth?x=1",
                "output_tail": [],
                "error": "AGY stopped waiting for the code",
            },
        )
        assert "/ui/actions/login-start" in rendered and "Start a new login" in rendered
        assert "/ui/actions/login-finish" in rendered
        assert "/ui/actions/login-code" not in rendered
        assert "<script>" not in rendered


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
