"""Docker-free checks of integration server sharing and controller lifetime."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from tests.integration import conftest as fixtures


def test_lock_and_share_starts_once(tmp_path: Path) -> None:
    ready = Barrier(2)
    start = Mock(return_value="postgresql+psycopg://localhost/test")

    def worker() -> str:
        ready.wait(timeout=5)
        return fixtures.lock_and_share(tmp_path, start)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        urls = [future.result(timeout=5) for future in futures]
    assert urls == [start.return_value, start.return_value]
    start.assert_called_once_with()


def test_failed_start_does_not_publish_url(tmp_path: Path) -> None:
    start = Mock(side_effect=[RuntimeError("startup failed"), "postgresql://localhost/test"])
    with pytest.raises(RuntimeError, match="startup failed"):
        fixtures.lock_and_share(tmp_path, start)
    assert not (tmp_path / "postgres.url").exists()
    assert fixtures.lock_and_share(tmp_path, start) == "postgresql://localhost/test"
    assert start.call_count == 2


@pytest.mark.parametrize("workers", [0, 2])
def test_controller_owns_container_until_shutdown(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    monkeypatch.delenv("CRUCIBLE_TEST_DATABASE_URL", raising=False)
    pg = Mock()
    pg.get_connection_url.return_value = "postgresql+psycopg://localhost/test"
    constructor = Mock(return_value=pg)
    monkeypatch.setattr("docker.from_env", MagicMock())
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    controller: Any = SimpleNamespace(
        stash=pytest.Stash(), option=SimpleNamespace(collectonly=False)
    )
    fixtures.pytest_configure(controller)
    for _ in range(workers):
        node = SimpleNamespace(config=controller, workerinput={})
        fixtures.pytest_configure_node(node)
        worker: Any = SimpleNamespace(stash=pytest.Stash(), workerinput=node.workerinput)
        fixtures.pytest_configure(worker)
        assert worker.stash[fixtures.SERVER_URL] == pg.get_connection_url.return_value
        fixtures.pytest_unconfigure(worker)
        pg.stop.assert_not_called()
    constructor.assert_called_once_with(fixtures.POSTGRES_IMAGE, driver="psycopg")
    pg.start.assert_called_once_with()
    fixtures.pytest_unconfigure(controller)
    fixtures.pytest_unconfigure(controller)
    pg.stop.assert_called_once_with()


def test_external_server_does_not_start_container(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://localhost/external"
    monkeypatch.setenv("CRUCIBLE_TEST_DATABASE_URL", url)
    constructor = Mock()
    monkeypatch.setattr("testcontainers.postgres.PostgresContainer", constructor)
    config: Any = SimpleNamespace(stash=pytest.Stash(), option=SimpleNamespace(collectonly=False))
    fixtures.pytest_configure(config)
    assert config.stash[fixtures.SERVER_URL] == url
    fixtures.pytest_unconfigure(config)
    constructor.assert_not_called()
