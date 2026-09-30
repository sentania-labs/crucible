"""Administrative UI cookies refer to revocable server-side sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import RedirectResponse

from crucible.adapters.ui import router as ui
from crucible.domain.entities import Principal, Role, UiSession

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PRINCIPAL = Principal("01K6H9ZH2J7F0X7M6C1Y8D3P4Q", "admin", Role.ADMIN, NOW)


class Sessions:
    def __init__(self) -> None:
        self.rows: dict[str, UiSession] = {}

    def create(self, session: UiSession) -> None:
        self.rows[session.id] = session

    def get(self, session_id: str) -> UiSession | None:
        return self.rows.get(session_id)

    def delete(self, session_id: str) -> None:
        self.rows.pop(session_id, None)

    def delete_expired(self, now: datetime) -> int:
        expired = [key for key, row in self.rows.items() if row.expires_at <= now]
        for key in expired:
            del self.rows[key]
        return len(expired)

    def touch(self, session_id: str, last_seen_at: datetime) -> None:
        self.rows[session_id].last_seen_at = last_seen_at


class Principals:
    def get(self, principal_id: str) -> Principal | None:
        return PRINCIPAL if principal_id == PRINCIPAL.id else None


class Uow:
    def __init__(self) -> None:
        self.ui_sessions = Sessions()
        self.principals = Principals()
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def context() -> Any:
    return SimpleNamespace(
        ui_signing_key=b"unit-test-signing-key",
        clock=SimpleNamespace(now=lambda: NOW),
        first_run=None,
    )


def request(*, cookie: str = "", body: str = "", path: str = "/ui") -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "POST", "path": path, "headers": headers}, receive)


def session_cookie(ctx: Any, session_id: str) -> str:
    return f"{ui.COOKIE}={ui._serializer(ctx).dumps(session_id)}"


def add_session(uow: Uow, *, expires_at: datetime = NOW + timedelta(hours=1)) -> UiSession:
    row = UiSession("ab" * 32, PRINCIPAL.id, "fixture-csrf", NOW, expires_at, NOW)
    uow.ui_sessions.create(row)
    return row


@pytest.mark.asyncio
async def test_sign_in_cookie_contains_no_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, uow = context(), Uow()
    token = "faketok-known-token-bytes"
    preauth = ui._preauth_serializer(ctx).dumps({"csrf": "preauth-csrf"})
    monkeypatch.setattr(
        ui, "authenticate", lambda _uow, supplied: PRINCIPAL if supplied == token else None
    )
    response = await ui.sign_in(
        request(
            cookie=f"{ui.PREAUTH_COOKIE}={preauth}",
            body=f"csrf=preauth-csrf&token={token}",
            path="/ui/sign-in",
        ),
        ctx,
        uow,  # type: ignore[arg-type]
    )

    assert token.encode() not in b"\n".join(
        value.encode() for value in response.headers.getlist("set-cookie")
    )
    assert len(uow.ui_sessions.rows) == 1
    assert len(next(iter(uow.ui_sessions.rows))) == 64


def test_deleted_session_redirects_to_sign_in() -> None:
    ctx, uow = context(), Uow()
    row = add_session(uow)
    uow.ui_sessions.delete(row.id)

    response = ui._require(request(cookie=session_cookie(ctx, row.id)), ctx, uow)  # type: ignore[arg-type]

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/sign-in?next=/ui"


def test_expired_session_is_refused() -> None:
    ctx, uow = context(), Uow()
    row = add_session(uow, expires_at=NOW - timedelta(seconds=1))

    assert ui._session(request(cookie=session_cookie(ctx, row.id)), ctx, uow) is None  # type: ignore[arg-type]


def test_well_formed_unknown_session_is_refused() -> None:
    ctx, uow = context(), Uow()

    assert ui._session(request(cookie=session_cookie(ctx, "cd" * 32)), ctx, uow) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_sign_out_deletes_session_row() -> None:
    ctx, uow = context(), Uow()
    row = add_session(uow)

    response = await ui.sign_out(
        request(cookie=session_cookie(ctx, row.id), body="csrf=fixture-csrf", path="/ui/sign-out"),
        ctx,
        uow,  # type: ignore[arg-type]
    )

    assert response.status_code == 303
    assert uow.ui_sessions.get(row.id) is None
