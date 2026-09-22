"""The release pushes the worker images as built and never over another digest (C11)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "worker_images.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("worker_images", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["worker_images"] = module
    spec.loader.exec_module(module)
    return module


wi = _module()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_archive(path: Path, *, tamper: bool = False) -> str:
    """A one-image OCI layout tarball like build.sh writes; returns its manifest digest."""
    config = b'{"config":{"Labels":{"crucible.harnesses":"codex"}}}'
    layer = b"layer bytes"
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": _digest(config), "size": len(config)},
            "layers": [{"digest": _digest(layer), "size": len(layer)}],
        }
    ).encode()
    index = json.dumps(
        {
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": _digest(manifest),
                }
            ]
        }
    ).encode()
    blobs = {config: config, layer: b"other bytes" if tamper else layer, manifest: manifest}
    with tarfile.open(path, "w") as archive:
        for name, data in [("index.json", index)] + [
            (f"blobs/sha256/{_digest(key)[7:]}", value) for key, value in blobs.items()
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return _digest(manifest)


def write_manifest(path: Path, tag: str, digest: str) -> Path:
    path.write_text(
        f"# header\nWORKER={tag}\nWORKER_DIGEST={digest}\nWORKER_HARNESSES=codex:0.156.0\n",
        encoding="utf-8",
    )
    return path


class FakeRegistry:
    """Records what publish would send; `existing` is what the tag already carries."""

    def __init__(self, existing: str | None) -> None:
        self.existing = existing
        self.blobs: list[str] = []
        self.manifests: list[tuple[str, str, str]] = []

    def published_digest(self, tag: str) -> str | None:
        return self.existing

    def push_blob(self, digest: str, data: bytes) -> None:
        assert _digest(data) == digest
        self.blobs.append(digest)

    def push_manifest(self, tag: str, media_type: str, data: bytes) -> None:
        self.manifests.append((tag, media_type, _digest(data)))


@pytest.fixture
def published(tmp_path: Path) -> tuple[Path, Path, str]:
    archives = tmp_path / "out"
    archives.mkdir()
    tag = "20260916-aaaaaaaaaaaa"
    digest = write_archive(archives / f"crucible-worker-{tag}.oci.tar")
    return (
        write_manifest(tmp_path / "manifest.env", f"crucible-worker:{tag}", digest),
        archives,
        digest,
    )


def test_the_manifest_names_each_image_tag_and_digest(published: tuple[Path, Path, str]) -> None:
    manifest, _, digest = published
    (image,) = wi.declared_images(manifest)
    assert (image.key, image.tag, image.digest) == ("WORKER", "20260916-aaaaaaaaaaaa", digest)
    assert image.archive_name == "crucible-worker-20260916-aaaaaaaaaaaa.oci.tar"


def test_an_absent_tag_is_pushed_byte_for_byte(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, archives, digest = published
    registry = FakeRegistry(existing=None)
    monkeypatch.setattr(wi, "Registry", lambda _repository: registry)
    wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker")
    assert len(registry.blobs) == 2
    assert registry.manifests == [
        ("20260916-aaaaaaaaaaaa", "application/vnd.oci.image.manifest.v1+json", digest)
    ]


def test_the_declared_digest_already_published_is_a_rerun(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, archives, digest = published
    registry = FakeRegistry(existing=digest)
    monkeypatch.setattr(wi, "Registry", lambda _repository: registry)
    wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker")
    assert registry.blobs == [] and registry.manifests == []


def test_a_tag_with_another_digest_is_never_overwritten(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, archives, _ = published
    registry = FakeRegistry(existing="sha256:" + "9" * 64)
    monkeypatch.setattr(wi, "Registry", lambda _repository: registry)
    with pytest.raises(wi.PublishError, match="never overwritten"):
        wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker")
    assert registry.blobs == [] and registry.manifests == []


def test_an_archive_that_is_not_the_declared_image_is_refused_before_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archives = tmp_path / "out"
    archives.mkdir()
    tag = "20260916-aaaaaaaaaaaa"
    write_archive(archives / f"crucible-worker-{tag}.oci.tar")
    manifest = write_manifest(
        tmp_path / "manifest.env", f"crucible-worker:{tag}", "sha256:" + "1" * 64
    )
    registry = FakeRegistry(existing=None)
    monkeypatch.setattr(wi, "Registry", lambda _repository: registry)
    with pytest.raises(wi.PublishError, match="refusing to publish"):
        wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker")
    assert registry.blobs == [] and registry.manifests == []


def test_a_blob_that_does_not_hash_to_its_name_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.oci.tar"
    write_archive(path, tamper=True)
    archive = wi.OciArchive(path)
    with pytest.raises(wi.PublishError, match="does not hash to its name"):
        for digest in archive.blobs:
            archive.blob(digest)


def test_verify_fails_on_any_tag_without_the_declared_digest(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, digest = published
    monkeypatch.setattr(wi, "Registry", lambda _repository: FakeRegistry(existing=None))
    with pytest.raises(wi.PublishError, match="carries nothing"):
        wi.verify(manifest, "ghcr.io/sentania-labs/crucible-worker")
    monkeypatch.setattr(wi, "Registry", lambda _repository: FakeRegistry(existing=digest))
    wi.verify(manifest, "ghcr.io/sentania-labs/crucible-worker")
