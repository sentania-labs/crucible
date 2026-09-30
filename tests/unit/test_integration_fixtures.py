"""Unit tests for the lock-and-share helper used by the integration tier (hades #219).

These tests exercise the file-lock coordination logic in
``tests.integration.conftest``: two concurrent threads call the helper, both
receive the same URL, and the start callback is invoked exactly once.

The PostgresContainer itself is mocked so these tests do not require Docker.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from tests.integration.conftest import _lock_and_share, _release


class _FakeTmpPathFactory:
    """Minimal stand-in for pytest's ``tmp_path_factory``."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def getbasetemp(self) -> Path:
        return self._tmp_path


class _FakeResource:
    """Mock resource that tracks how many times start() was called."""

    _counter: int

    def __init__(self) -> None:
        self._counter = 0

    def start(self) -> None:
        self._counter += 1

    def stop(self) -> None:
        pass

    def get_connection_url(self) -> str:
        return "postgres://fake:fake@localhost:5432/fake"


def _make_resource() -> tuple[str, Any]:
    r = _FakeResource()
    r.start()
    return r.get_connection_url(), r


class TestLockAndShare:
    """Two threads call ``_lock_and_share`` concurrently and get the same URL."""

    def _make_isolated_factory(self, tmp_path: Path, tag: str) -> _FakeTmpPathFactory:
        """Return a factory whose coord dir is unique."""
        coord = tmp_path / f"coord_{tag}"
        coord.mkdir()
        inner = coord / "inner"
        inner.mkdir()
        return _FakeTmpPathFactory(inner)

    def test_shared_url_across_threads(self, tmp_path: Path) -> None:
        factory = self._make_isolated_factory(tmp_path, "thread1")

        results: list[tuple[str, Any]] = []

        def worker() -> None:
            url, pg_ref = _lock_and_share(factory, _make_resource)
            results.append((url, pg_ref))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        time.sleep(0.05)
        t2.start()

        t1.join(timeout=30)
        t2.join(timeout=30)

        assert len(results) == 2

        # Both threads get the same URL.
        url1, _pg_ref1 = results[0]
        url2, _pg_ref2 = results[1]
        assert url1 == url2 and url1 != "", "both threads must get the same URL"

    def test_start_callback_runs_once(self, tmp_path: Path) -> None:
        """Only one thread creates the resource (pg_ref is not None)."""
        factory = self._make_isolated_factory(tmp_path, "once")

        results: list[tuple[str, Any]] = []

        def worker() -> None:
            url, pg_ref = _lock_and_share(factory, _make_resource)
            results.append((url, pg_ref))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)

        t1.start()
        time.sleep(0.05)
        t2.start()

        t1.join(timeout=30)
        t2.join(timeout=30)

        # Exactly one thread got a pg_ref (the one that started the resource).
        pg_refs = [r for _, r in results if r is not None]
        assert len(pg_refs) == 1, "exactly one thread should start the resource"

    def test_release_decrements_to_zero(self, tmp_path: Path) -> None:
        """After _release decrements to zero, the counter file is zero."""
        factory = self._make_isolated_factory(tmp_path, "release")

        _unused_url, pg_ref = _lock_and_share(factory, _make_resource)
        assert pg_ref is not None, "first worker must get a pg_ref"

        # Release our reference -- count goes from 1 to 0.
        _release(factory, pg_ref)

        coord = tmp_path / "coord_release"
        refs_path = coord / ".pg.refs"
        count = int(refs_path.read_text(encoding="utf-8").strip())
        assert count == 0, "the reference count must reach zero after release"
