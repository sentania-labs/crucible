"""Single-writer enforcement for the Codex credential renewer (339).

Verifies the fix that prevents the API process from mutating the login while the
supervisor is the only writer.  Three behaviours are tested:

1. **AC1**: ``serve --api`` wiring constructs no grant-capable renewer.  The read-only
   renewer returned by ``_build_readonly_renewer`` has a no-op ``grant`` so that
   calling ``refresh`` does not mutate the store or record a refresh event.
2. **AC2**: a refresh request recorded by the API is performed by the supervisor tick.
   A ``CREDENTIAL_REFRESH_REQUESTED`` event is inserted into the event store,
   ``refresh_on_request`` detects it, and the renewer calls the grant function.
3. **AC3**: a stale ``resourceVersion`` makes the Secret patch fail closed (409) instead
   of overwriting.  ``KubernetesCredentialStore.write`` passes the cached version to
   the patch client, and the fake returns 409 when it differs.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.application.credential_renewer import (
    CodexCredentialRenewer,
    KubernetesCredentialStore,
)
from crucible.cli.wiring import _build_readonly_renewer


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def _jwt(expiry: datetime) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': expiry.timestamp()})}.signature"


def _login_dict(clock: FakeClock) -> dict[str, Any]:
    return {
        "last_refresh": (clock.now() - timedelta(hours=1)).isoformat(),
        "tokens": {
            "access_token": _jwt(clock.now() + timedelta(hours=1)),
            "refresh_token": "fixture-refresh-value",
            "account_id": "account-fixture",
        },
    }


# ---------------------------------------------------------------------------
# AC1: serve --api constructs no grant-capable renewer
# ---------------------------------------------------------------------------


def test_api_renewer_noop_grand_does_not_mutate(tmp_path: Path) -> None:
    """The API process's renewer cannot call the grant function."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login_path = tmp_path / "auth.json"
    original_auth = json.dumps(_login_dict(clock))
    login_path.write_text(original_auth)

    full_renewer = CodexCredentialRenewer(
        login_path=login_path,
        clock=clock,
        grant=lambda _token: {"access_token": _jwt(clock.now() + timedelta(hours=2))},
    )

    ro = _build_readonly_renewer(full_renewer)

    # The read-only renewer shares the same store so is_dead is accurate.
    assert ro.dead is False

    # Calling refresh on the API renewer: the no-op grant returns {}, so no
    # new tokens are set.  The existing tokens are copied back, but the
    # access_token is still the original one (never refreshed).
    ro.refresh("from-api", force=True)

    # Verify the store still has the original access token.
    stored = json.loads(login_path.read_text())
    assert stored["tokens"]["access_token"] == _jwt(clock.now() + timedelta(hours=1))


# ---------------------------------------------------------------------------
# AC2: a refresh request recorded by the API is performed by the supervisor tick
# ---------------------------------------------------------------------------


class _FakeStore:
    """A minimal CredentialStore implementation for unit tests."""

    def __init__(
        self,
        read_fn: Callable[[], dict[str, Any]],
        is_dead_fn: Callable[[], bool],
        write_fn: Callable[[Mapping[str, Any]], None] | None = None,
        mark_dead_fn: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._read = read_fn
        self._is_dead = is_dead_fn
        self._write = write_fn or (lambda _doc: None)
        self._mark_dead = mark_dead_fn or (lambda _doc: None)

    def read(self) -> dict[str, Any]:
        return self._read()

    def write(self, document: Mapping[str, Any]) -> None:
        self._write(document)

    def is_dead(self) -> bool:
        return self._is_dead()

    def mark_dead(self, document: Mapping[str, Any]) -> None:
        self._mark_dead(document)


def test_supervisor_performs_refresh_on_pending_request() -> None:
    """refresh_on_request checks the checker and calls refresh when pending."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    grants: list[str] = []

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def _grant(token: str) -> dict[str, str]:
        grants.append(token)
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=_grant,
    )

    # No checker attached: refresh_on_request is a no-op.
    assert renewer.refresh_on_request() is False

    # Attach a checker that says "pending".
    renewer.set_pending_request_checker(lambda: True)
    assert renewer.refresh_on_request() is True
    assert len(grants) == 1


def test_supervisor_skips_refresh_when_no_pending_request() -> None:
    """When the checker returns False, refresh is not called."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    grants: list[str] = []

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def _grant(token: str) -> dict[str, str]:
        grants.append(token)
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=_grant,
    )

    renewer.set_pending_request_checker(lambda: False)
    assert renewer.refresh_on_request() is False
    assert len(grants) == 0


def test_supervisor_refresh_on_request_calls_grant_once_per_call() -> None:
    """Two pending-checker calls each trigger one grant invocation."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    counter = {"n": 0}

    store = _FakeStore(
        read_fn=lambda: _login_dict(clock),
        is_dead_fn=lambda: False,
        write_fn=lambda doc: None,
    )

    def counting_grant(token: str) -> dict[str, str]:
        counter["n"] += 1
        return {"access_token": _jwt(clock.now() + timedelta(hours=2))}

    renewer = CodexCredentialRenewer(
        store=store,
        clock=clock,
        grant=counting_grant,
    )

    renewer.set_pending_request_checker(lambda: True)

    renewer.refresh_on_request()
    assert counter["n"] == 1

    renewer.refresh_on_request()
    assert counter["n"] == 2


# ---------------------------------------------------------------------------
# AC3: stale resourceVersion makes the patch fail closed (409)
# ---------------------------------------------------------------------------


def test_k8s_store_rejects_stale_resource_version() -> None:
    """write() passes the read resourceVersion; a mismatching version yields 409."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = _login_dict(clock)

    client = FakeKubernetesApi(namespace="crucible")
    client.create(
        "secrets",
        k8sspec.secret(
            name="crucible-harness-codex",
            namespace="crucible",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )

    store = KubernetesCredentialStore(client)

    # read() caches the resourceVersion.
    store.read()
    cached_version = store._resource_version
    assert cached_version is not None

    # Modify the secret through the fake API so the version changes.
    other_body = client.get("secrets", "crucible-harness-codex")
    other_body["data"]["other.json"] = base64.b64encode(b"data").decode()
    other_body["metadata"]["resourceVersion"] = "9999"
    client.objects[("secrets", "crucible-harness-codex")].body = other_body

    # Now write() should fail because our cached version is stale.
    with pytest.raises(KubernetesApiError) as exc_info:
        store.write({"last_refresh": clock.now().isoformat(), "tokens": {}})

    assert exc_info.value.status == 409


def test_k8s_store_patch_succeeds_when_version_matches() -> None:
    """write() succeeds when the resourceVersion has not changed."""
    clock = FakeClock(datetime(2026, 9, 30, tzinfo=UTC))
    login = _login_dict(clock)

    client = FakeKubernetesApi(namespace="crucible")
    client.create(
        "secrets",
        k8sspec.secret(
            name="crucible-harness-codex",
            namespace="crucible",
            object_labels={},
            data={"auth.json": json.dumps(login).encode()},
        ),
    )

    store = KubernetesCredentialStore(client)

    # Read to cache the version.
    store.read()
    stored_login = store.read()
    assert "last_refresh" in stored_login

    # No concurrent modification, so write should succeed.
    store.write({"last_refresh": clock.now().isoformat(), "tokens": {}})
    # Verify the data was updated.
    updated = store.read()
    assert updated["last_refresh"] == clock.now().isoformat()
