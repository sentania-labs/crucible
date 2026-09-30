from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from crucible.application.admin import routing
from crucible.application.admin.context import AdminContext
from crucible.domain.entities import Principal, Role


class _Versions:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list_versions(self, name: str) -> list[Any]:
        return [row for row in self.rows if row.name == name]


@pytest.mark.parametrize("pinned, expected", [(False, 4), (True, None)])
def test_delivery_policy_follows_routing_unless_deliberately_pinned(
    monkeypatch: pytest.MonkeyPatch, pinned: bool, expected: int | None
) -> None:
    policy = SimpleNamespace(
        name="delivery",
        version=3,
        document={
            "version": 3,
            "description": "Delivery policy",
            "routing": {
                "policy": {"name": "route", "version": 7, "pinned": pinned},
            },
        },
    )
    route = SimpleNamespace(name="route", version=7, document={"version": 7})
    uow = SimpleNamespace(policies=_Versions([policy]), routing_policies=_Versions([route]))
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(routing, "put_routing_policy", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        routing,
        "put_policy",
        lambda *args, **kwargs: saved.append(deepcopy(kwargs)),
    )
    ctx = SimpleNamespace(
        clock=SimpleNamespace(now=lambda: datetime(2026, 9, 30, tzinfo=UTC)),
        proxy_config_path=None,
        proxy_subnet="",
        proxy_hosts=(),
        providers={},
    )

    policy_version, routing_version = routing.publish_routing(
        cast(AdminContext, ctx),
        uow,
        principal=Principal(
            id="p",
            name="operator",
            role=Role.ADMIN,
            created_at=datetime(2026, 9, 30, tzinfo=UTC),
        ),
        policy=policy,
        routing=route,
        routing_document={"version": 7},
        reason="operator choice",
        note="Routing update",
    )

    assert routing_version == 8
    assert policy_version == (expected or 3)
    assert ([item["version"] for item in saved] if saved else None) == (
        [expected] if expected else None
    )
    if saved:
        assert saved[0]["document"]["routing"] == {
            "policy": {"name": "route", "version": 8, "pinned": False}
        }
