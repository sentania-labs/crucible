"""Shared fixtures for admin UI tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

from crucible.adapters.ui.render import (
    _localize,
    templates,
)
from crucible.application.admin import credentials as credentials_service
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
        tasks=SimpleNamespace(list_by_state=lambda _state: []),
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


READINESS = status_service.readiness


def _readiness(
    document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    repositories: list[Any] | None = None,
    credential_state: str = "validated",
    enabled_models: set[str] | None = None,
    endpoint: str | None = "https://llm.example.invalid/v1",
) -> dict[str, Any]:
    """crucible#123's readiness over a status document, with the credential, routing
    and gateway reads it makes replaced by fixed answers."""
    monkeypatch.setattr(
        credentials_service,
        "state_view",
        lambda _ctx, _uow, _name, _secret=None: {
            "state": credential_state,
            "last_launch_outcome": None,
        },
    )
    monkeypatch.setattr(status_service, "_enabled_models", lambda _uow: enabled_models or set())
    monkeypatch.setattr(status_service, "gateway_url", lambda _uow: (endpoint, "routing"))
    ctx = SimpleNamespace(
        harnesses=SimpleNamespace(
            get=lambda name: SimpleNamespace(test_fixture=name == "script-harness")
        )
    )
    uow = SimpleNamespace(
        repositories=SimpleNamespace(list_all=lambda: repositories if repositories else [])
    )
    return READINESS(cast(Any, ctx), cast(Any, uow), document)


def _harness(name: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "enabled": True,
        "enabled_by_configuration": True,
        "enabled_by_administrator": True,
        "reason": "",
        "credential": {"state": "validated"},
        "images": [{"promotion_state": "default"}],
        "default_image": {"reference": "w:1", "digest": "sha256:w", "version": "1"},
    }
    item.update(overrides)
    return item


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
def _render_documents(sections: list[dict[str, Any]]) -> str:
    return templates.get_template("page.html").render(
        **base_context("/ui"),
        heading="Readable panels",
        intro="Fixture",
        sections=_localize(sections, "America/Chicago"),
        badge=None,
    )


def _panel_leaves(panel: dict[str, Any]) -> list[Any]:
    if panel["kind"] == "fields":
        leaves: list[Any] = []
        for item in panel["items"]:
            leaves.extend(_panel_leaves(item["panel"]) if "panel" in item else [item["value"]])
        return leaves
    if panel["kind"] == "table":
        return [
            leaf
            for row in panel["rows"]
            for cell in row
            for leaf in (
                _panel_leaves(cell)
                if isinstance(cell, dict) and "kind" in cell
                else _document_leaves(cell)
            )
        ]
    if panel["kind"] == "values":
        return [leaf for item in panel["items"] for leaf in _document_leaves(item)]
    if panel["kind"] == "empty":
        return []
    return [panel["value"]]


def _document_leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _document_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _document_leaves(item)]
    return [value]
