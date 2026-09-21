"""Small template checks for the script-free administrative surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui.router import _readiness_gaps, templates
from crucible.application.admin import status as status_service
from crucible.application.queries import supervisor_view
from crucible.domain.entities import Lease, SupervisorStatus


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


NOW = datetime(2026, 9, 21, 6, 30, tzinfo=UTC)


def _supervisor_document(
    lease: Lease | None, supervisor_status: SupervisorStatus
) -> dict[str, Any]:
    uow = SimpleNamespace(
        leases=SimpleNamespace(get_supervisor=lambda: lease),
        supervisor_status=SimpleNamespace(get=lambda: supervisor_status),
        wakes=SimpleNamespace(count_unacked=lambda: 0),
        pull_requests=SimpleNamespace(list_in_states=lambda _states: []),
        github_deliveries=SimpleNamespace(count_unprocessed=lambda: 0),
    )
    return supervisor_view(uow, [], NOW, 30).model_dump(mode="json")


def _lease() -> Lease:
    return Lease(
        id="supervisor",
        kind="supervisor",
        key="singleton",
        holder="test-supervisor",
        fenced_token=1,
        expires_at=NOW + timedelta(seconds=30),
    )


def _status(**overrides: Any) -> SupervisorStatus:
    values: dict[str, Any] = {
        "holder": "test-supervisor",
        "last_tick_at": NOW,
        "tick_ms": 5,
        "counts": {},
        "last_success_at": NOW,
        "last_error_at": None,
        "last_error": None,
    }
    values.update(overrides)
    return SupervisorStatus(**values)


def test_healthy_supervisor_adds_no_readiness_gap() -> None:
    document = {"supervisor": _supervisor_document(_lease(), _status()), "harnesses": []}

    assert _readiness_gaps(document, repository_registered=True) == []


@pytest.mark.parametrize(
    ("lease", "supervisor_status", "cause"),
    [
        (None, _status(), "no supervisor lease"),
        (
            _lease(),
            _status(last_success_at=NOW - timedelta(seconds=31)),
            "no successful tick within the lease window",
        ),
        (
            _lease(),
            _status(
                last_success_at=NOW - timedelta(seconds=1),
                last_error_at=NOW,
                last_error="RuntimeError: test failure",
            ),
            "last tick failed: RuntimeError: test failure",
        ),
    ],
)
def test_each_unhealthy_supervisor_shape_adds_one_cause_specific_gap(
    lease: Lease | None, supervisor_status: SupervisorStatus, cause: str
) -> None:
    document = {
        "supervisor": _supervisor_document(lease, supervisor_status),
        "harnesses": [],
    }

    gaps = _readiness_gaps(document, repository_registered=True)

    assert len(gaps) == 1
    assert cause in gaps[0][0]


class _StrictStatusDict(dict[str, Any]):
    """Make optional access to an undefined status field fail like indexed access."""

    def __getitem__(self, key: str) -> Any:
        if key not in self:
            raise AssertionError(f"gap logic read undefined status field {key!r}")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            raise AssertionError(f"gap logic read undefined status field {key!r}")
        return super().get(key, default)


def _strict_status(value: Any) -> Any:
    if isinstance(value, dict):
        return _StrictStatusDict({key: _strict_status(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_strict_status(item) for item in value]
    return value


@pytest.mark.asyncio
async def test_gap_logic_reads_only_keys_from_real_status_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def list_images(_ctx: Any) -> list[Any]:
        return []

    async def provider_status(_ctx: Any) -> dict[str, Any]:
        return {}

    harnesses = [
        {
            "name": "disabled",
            "enabled": False,
            "enabled_by_configuration": True,
            "credential": {"state": "absent"},
            "images": [],
        },
        {
            "name": "unpromoted",
            "enabled": True,
            "enabled_by_configuration": True,
            "credential": {"state": "valid"},
            "images": [{"promotion_state": "candidate"}],
        },
    ]
    supervisor = _supervisor_document(None, _status())
    status_module = cast(Any, status_service)
    monkeypatch.setattr(status_service, "list_images", list_images)
    monkeypatch.setattr(status_service, "list_harnesses", lambda _ctx, _uow, _images: harnesses)
    monkeypatch.setattr(status_service, "providers_status", provider_status)
    monkeypatch.setattr(
        status_service,
        "supervisor_view",
        lambda *_args: SimpleNamespace(model_dump=lambda **_kwargs: supervisor),
    )
    monkeypatch.setattr(status_module.github, "status", lambda _ctx, _uow: {})
    monkeypatch.setattr(status_service, "workers", lambda _uow: [])
    monkeypatch.setattr(status_service, "tasks", lambda _uow: {})
    monkeypatch.setattr(status_service, "wakes", lambda _uow: {})
    monkeypatch.setattr(status_service, "retention", lambda _uow: {})
    monkeypatch.setattr(status_module.bootstrap, "status_part", lambda _uow: {})
    monkeypatch.setattr(status_module.audit, "tail", lambda _uow, **_kwargs: {"next_cursor": None})
    document = await status_service.status(
        cast(
            Any,
            SimpleNamespace(
                providers={}, clock=SimpleNamespace(now=lambda: NOW), lease_ttl_seconds=30
            ),
        ),
        cast(Any, SimpleNamespace()),
    )

    gaps = _readiness_gaps(_strict_status(document), repository_registered=False)

    assert len(gaps) == 5
