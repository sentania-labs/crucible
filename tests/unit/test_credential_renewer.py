from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    InvalidGrantError,
)
from crucible.domain.events import EventKind


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def _jwt(expiry: datetime) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': expiry.timestamp()})}.signature"


def _login(path: Path, clock: FakeClock) -> None:
    path.write_text(
        json.dumps(
            {
                "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
                "tokens": {
                    "access_token": _jwt(clock.now() + timedelta(hours=1)),
                    "refresh_token": "fixture-refresh-value",
                    "account_id": "account-fixture",
                },
            }
        )
    )


def test_t_auth_3_two_callers_make_one_grant_and_propagate(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = tmp_path / "auth.json"
    _login(login, clock)
    grants = 0
    projections: list[dict[str, str]] = []
    events: list[EventKind] = []
    barrier = threading.Barrier(2)

    def grant(_refresh: str) -> dict[str, str]:
        nonlocal grants
        grants += 1
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        login,
        grant=grant,
        clock=clock,
        propagate=lambda value: projections.append(dict(value)),
        record=lambda kind, _payload: events.append(kind),
    )

    def call() -> None:
        barrier.wait()
        renewer.refresh("worker rejected token")

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert grants == 1
    assert len(projections) == 2
    assert events == [EventKind.CREDENTIAL_REFRESHED]
    assert login.stat().st_mode & 0o777 == 0o600
    assert set(projections[-1]) == {"access_token", "account_id", "expires_at"}


def test_t_auth_4_invalid_grant_marks_dead_and_wakes_once(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = tmp_path / "auth.json"
    _login(login, clock)
    grants = 0
    events: list[EventKind] = []
    wakes: list[str] = []

    def grant(_refresh: str) -> dict[str, str]:
        nonlocal grants
        grants += 1
        raise InvalidGrantError("invalid_grant")

    renewer = CodexCredentialRenewer(
        login,
        grant=grant,
        clock=clock,
        record=lambda kind, _payload: events.append(kind),
        wake=wakes.append,
    )
    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")
    with pytest.raises(InvalidGrantError):
        renewer.refresh("timer")

    assert renewer.dead
    assert grants == 1
    assert events == [EventKind.CREDENTIAL_REFRESH_FAILED]
    assert len(wakes) == 1
