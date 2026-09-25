"""The first-run administrator token's two deliveries (crucible#122, ADR 0016)."""

from __future__ import annotations

import base64
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.first_run import SECRET_NAME, FileDelivery, SecretDelivery
from crucible.application.first_run import discard_after_use, is_first_run
from crucible.domain.entities import Principal, Role

TOKEN = "cru_" + "0" * 26 + "." + "s" * 40


def _principal(name: str) -> Principal:
    return Principal(id="p1", name=name, role=Role.ADMIN, created_at=datetime.now(UTC))


def test_the_secret_holds_the_token_and_names_only_where_it_is() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]

    delivery.deliver(TOKEN)

    secret = api.get("secrets", SECRET_NAME)
    assert base64.b64decode(secret["data"]["token"]).decode() == TOKEN
    assert secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "crucible"
    where = delivery.where()
    assert "crucible/crucible-first-run-admin" in where and TOKEN not in where


def test_a_second_delivery_replaces_a_stale_secret_whole() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]
    delivery.deliver("cru_stale.value")
    delivery.deliver(TOKEN)
    secret = api.get("secrets", SECRET_NAME)
    assert base64.b64decode(secret["data"]["token"]).decode() == TOKEN


def test_discard_deletes_the_secret_and_tolerates_it_being_gone() -> None:
    api = FakeKubernetesApi(namespace="crucible")
    delivery = SecretDelivery(api)  # type: ignore[arg-type]
    delivery.deliver(TOKEN)
    delivery.discard()
    with pytest.raises(KubernetesApiError):
        api.get("secrets", SECRET_NAME)
    delivery.discard()


def test_the_file_is_private_and_replaced_atomically(tmp_path: Path) -> None:
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    delivery.deliver("cru_stale.value")
    delivery.deliver(TOKEN)
    assert delivery.path.read_text(encoding="utf-8") == TOKEN + "\n"
    assert os.stat(delivery.path).st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["first-run-admin-token"]
    assert "docker compose exec crucible cat" in delivery.where()
    delivery.discard()
    delivery.discard()
    assert not delivery.path.exists()


def test_only_the_first_run_principal_discards(tmp_path: Path) -> None:
    delivery = FileDelivery(tmp_path / "first-run-admin-token")
    delivery.deliver(TOKEN)
    discard_after_use(delivery, _principal("operator-admin"))
    assert delivery.path.exists()
    discard_after_use(None, _principal("first-run-admin"))
    discard_after_use(delivery, _principal("first-run-admin-1a2b3c4d"))
    assert not delivery.path.exists()
    assert is_first_run("first-run-admin") and not is_first_run("admin-first-run-admin")


def test_a_failed_discard_is_logged_without_the_token(caplog: pytest.LogCaptureFixture) -> None:
    class Failing:
        def where(self) -> str:
            return "the Secret crucible/crucible-first-run-admin"

        def deliver(self, token: str) -> None:
            raise AssertionError("never called")

        def discard(self) -> None:
            raise KubernetesApiError(403, "forbidden")

    with caplog.at_level(logging.WARNING):
        discard_after_use(Failing(), _principal("first-run-admin"))
    assert "could not be removed" in caplog.text and "cru_" not in caplog.text
