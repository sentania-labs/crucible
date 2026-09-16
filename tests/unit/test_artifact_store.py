"""Content-addressed artifact store and its secret refusal (12, 14).

Every secret-shaped fixture is built at runtime so nothing secret-shaped is committed."""

from __future__ import annotations

from pathlib import Path

import pytest

from crucible.adapters.storage.disk import DiskArtifactStore
from crucible.ports.artifacts import SecretInArtifactError


def token_like() -> bytes:
    """A GitHub-token-shaped string assembled here, never written to a file."""
    return ("gh" + "p_" + "b" * 36).encode()


def test_content_addressing_is_stable_and_deduplicates(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    first = store.put(b"hello evidence")
    second = store.put(b"hello evidence")
    assert first.sha256 == second.sha256 and first.path == second.path
    assert first.size == len(b"hello evidence")
    assert store.exists(first.path)
    assert store.get(first.path) == b"hello evidence"
    assert len(list((tmp_path / "blobs").rglob("*"))) == 3  # two directories and one blob


def test_the_path_is_the_digest(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    blob = store.put(b"x")
    assert blob.path.endswith(blob.sha256)
    assert f"/{blob.sha256[:2]}/{blob.sha256[2:4]}/" in blob.path


def test_a_secret_is_refused_before_storage(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    with pytest.raises(SecretInArtifactError) as exc:
        store.put(b"log line\n" + token_like() + b"\nmore")
    assert exc.value.pattern == "github_token"
    assert "offset" in exc.value.where
    # Nothing was written, and the message carries no value.
    assert not (tmp_path / "blobs").exists()
    assert "ghp_" not in str(exc.value)


def test_a_private_key_header_is_refused(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    with pytest.raises(SecretInArtifactError) as exc:
        store.put(header.encode())
    assert exc.value.pattern == "private_key_header"


def test_binary_content_is_stored(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    blob = store.put(bytes(range(256)))
    assert store.get(blob.path) == bytes(range(256))


def test_missing_blob_reports_absent(tmp_path: Path) -> None:
    store = DiskArtifactStore(tmp_path)
    assert store.exists("blobs/aa/bb/" + "0" * 64) is False
