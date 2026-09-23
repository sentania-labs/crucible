"""The release pushes the worker images as built and never over another digest (C11)."""

from __future__ import annotations

import base64
import hashlib
import http.server
import importlib.util
import io
import json
import secrets
import ssl
import subprocess
import sys
import tarfile
import threading
import urllib.parse
import uuid
from collections.abc import Iterator
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

    def push_blob(self, digest: str, chunks: Any) -> None:
        data = b"".join(chunks)
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


def test_streamed_chunks_of_a_tampered_blob_are_all_yielded_then_refused(tmp_path: Path) -> None:
    """FDY-0074: blob_chunks() cannot know a blob is tampered until the last chunk, so
    every chunk it read is still handed to the caller before it raises."""
    path = tmp_path / "bad.oci.tar"
    write_archive(path, tamper=True)
    archive = wi.OciArchive(path)
    good, bad = 0, 0
    for digest in archive.blobs:
        try:
            chunks = list(archive.blob_chunks(digest, chunk_size=4))
            assert b"".join(chunks)
            good += 1
        except wi.PublishError as exc:
            assert "does not hash to its name" in str(exc)
            bad += 1
    assert (good, bad) == (1, 1)


def test_verify_fails_on_any_tag_without_the_declared_digest(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, digest = published
    monkeypatch.setattr(wi, "Registry", lambda _repository: FakeRegistry(existing=None))
    with pytest.raises(wi.PublishError, match="carries nothing"):
        wi.verify(manifest, "ghcr.io/sentania-labs/crucible-worker")
    monkeypatch.setattr(wi, "Registry", lambda _repository: FakeRegistry(existing=digest))
    wi.verify(manifest, "ghcr.io/sentania-labs/crucible-worker")


# Below: the real Registry class driven over real HTTPS against a fake registry server,
# not the FakeRegistry stub above. This is what proves the bearer-token challenge (401,
# WWW-Authenticate, token fetch, retry) and the chunked POST/PATCH/PUT upload sequence
# themselves, end to end, the way FakeRegistry's in-process substitution cannot (FDY-0074).


def _self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    key = tmp_path / "fake-registry.key"
    cert = tmp_path / "fake-registry.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


class _FakeRegistryServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    name = "sentania-labs/crucible-worker"

    def __init__(self, certfile: str, keyfile: str) -> None:
        super().__init__(("127.0.0.1", 0), _FakeRegistryHandler)
        self.username = "publisher"
        self.password = "s3cret"  # test-only, thrown away with the server (FDY-0074)
        self.token = secrets.token_hex(16)
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, tuple[str, bytes]] = {}
        self.uploads: dict[str, bytearray] = {}
        self.chunk_sizes: list[int] = []
        self.blob_finalize_attempts = 0
        self.redirect_off_host = False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self.socket = context.wrap_socket(self.socket, server_side=True)


# What GHCR itself answered in CI run 35831556330 (FDY-0090), and so what the fake does:
# an upload's Location is /v2/<name>/blobs/upload/<n>.<uuid> (singular `upload`, not the
# POST's `uploads/`), echoed in Docker-Upload-UUID, with an inclusive Range; and a PATCH
# or finalizing PUT whose Content-Type is not application/octet-stream is refused with
# 404 BLOB_UPLOAD_INVALID "invalid content-type". registry:2 accepts the latter, which is
# how the v0.5.0 release reached GHCR with a publish that had only ever passed locally.
_GHCR_INVALID_CONTENT_TYPE = json.dumps(
    {"errors": [{"code": "BLOB_UPLOAD_INVALID", "message": "invalid content-type"}]}
).encode()


class _FakeRegistryHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeRegistryServer

    def log_message(self, format_: str, *args: Any) -> None:  # quiet the test run
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _chunked_body(self) -> list[bytes] | None:
        """A chunked-transfer-encoded body, piece by piece as it crossed the wire, or
        None when the stream ends before its terminating chunk (the client aborted)."""
        pieces: list[bytes] = []
        while True:
            size_line = self.rfile.readline()
            if not size_line:
                return None
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                self.rfile.readline()
                return pieces
            piece = self.rfile.read(size)
            if len(piece) != size or self.rfile.readline() != b"\r\n":
                return None
            pieces.append(piece)

    def _send(self, status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _challenge(self) -> None:
        realm = f"https://127.0.0.1:{self.server.server_port}/token"
        scope = f"repository:{self.server.name}:pull,push"
        self._send(
            401,
            {"Www-Authenticate": f'Bearer realm="{realm}",service="fake-registry",scope="{scope}"'},
        )

    def _authorized(self) -> bool:
        if self.path.startswith("/token"):
            return True
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._challenge()
        return False

    def do_GET(self) -> None:
        if self.path.startswith("/token"):
            expected = (
                "Basic "
                + base64.b64encode(
                    f"{self.server.username}:{self.server.password}".encode()
                ).decode()
            )
            if self.headers.get("Authorization") != expected:
                self._send(401)
                return
            self._send(
                200,
                {"Content-Type": "application/json"},
                json.dumps({"token": self.server.token}).encode(),
            )
            return
        if not self._require_auth():
            return
        if self.path.startswith(f"/v2/{self.server.name}/manifests/"):
            tag = self.path.rsplit("/", 1)[-1]
            entry = self.server.manifests.get(tag)
            if entry is None:
                self._send(404)
                return
            digest, body = entry
            self._send(200, {"Docker-Content-Digest": digest}, body)
            return
        self._send(404)

    def do_HEAD(self) -> None:
        if not self._require_auth():
            return
        digest = self.path.rsplit("/", 1)[-1]
        self._send(200 if digest in self.server.blobs else 404)

    def _upload_location(self, upload_id: str) -> str:
        return f"/v2/{self.server.name}/blobs/upload/{upload_id}"

    def _octet_stream(self) -> bool:
        if self.headers.get("Content-Type") == "application/octet-stream":
            return True
        self._body()
        self._send(404, {"Content-Type": "application/json"}, _GHCR_INVALID_CONTENT_TYPE)
        return False

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        self._body()
        upload_id = f"1.{uuid.uuid4()}"
        self.server.uploads[upload_id] = bytearray()
        if self.server.redirect_off_host:
            location = "https://evil.example/upload"
        else:
            location = self._upload_location(upload_id)
        self._send(202, {"Location": location, "Range": "0-0", "Docker-Upload-UUID": upload_id})

    def do_PATCH(self) -> None:
        if not self._require_auth():
            return
        upload_id = self.path.rsplit("/", 1)[-1]
        if not self.path.startswith(self._upload_location("")):
            self._send(404)
            return
        buf = self.server.uploads.get(upload_id)
        if buf is None:
            self._send(404)
            return
        if not self._octet_stream():
            return
        # The streamed PATCH the publisher sends, and GHCR takes: the whole blob, chunked
        # transfer encoding, no Content-Range. Each wire chunk's size is recorded, which is
        # what shows the publisher sent the blob in bounded pieces.
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            pieces = self._chunked_body()
            if pieces is None:
                self.close_connection = True
                return
            self.server.chunk_sizes.extend(len(piece) for piece in pieces)
            buf.extend(b"".join(pieces))
        else:
            start, _, end = self.headers.get("Content-Range", "").partition("-")
            body = self._body()
            if not (start.isdigit() and end.isdigit()) or int(start) != len(buf):
                self._send(416)
                return
            if int(end) - int(start) + 1 != len(body):
                self._send(416)
                return
            self.server.chunk_sizes.append(len(body))
            buf.extend(body)
        self._send(
            202,
            {
                "Location": self._upload_location(upload_id),
                "Range": f"0-{max(len(buf) - 1, 0)}",
                "Docker-Upload-UUID": upload_id,
            },
        )

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
        if self.path.startswith(self._upload_location("")):
            self.server.blob_finalize_attempts += 1
            if not self._octet_stream():
                return
            tail = self.path.rsplit("/", 1)[-1]
            upload_id, _, query = tail.partition("?")
            digest = urllib.parse.unquote(
                dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("digest", "")
            )
            buf = self.server.uploads.pop(upload_id, None)
            if buf is None:
                self._send(404)
                return
            if "sha256:" + hashlib.sha256(bytes(buf)).hexdigest() != digest:
                self._send(400)
                return
            self.server.blobs[digest] = bytes(buf)
            self._send(201, {"Docker-Content-Digest": digest})
            return
        if self.path.startswith(f"/v2/{self.server.name}/manifests/"):
            tag = self.path.rsplit("/", 1)[-1]
            body = self._body()
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            self.server.manifests[tag] = (digest, body)
            self._send(201, {"Docker-Content-Digest": digest})
            return
        self._send(404)


@pytest.fixture
def fake_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeRegistryServer]:
    certfile, keyfile = _self_signed_cert(tmp_path)
    server = _FakeRegistryServer(certfile, keyfile)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("REGISTRY_CA_FILE", certfile)
    monkeypatch.setenv("REGISTRY_USERNAME", server.username)
    monkeypatch.setenv("REGISTRY_PASSWORD", server.password)
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _client(server: _FakeRegistryServer) -> Any:
    return wi.Registry(f"127.0.0.1:{server.server_port}/{server.name}")


def test_a_blob_push_answers_the_bearer_challenge_and_streams_bounded_chunks(
    fake_registry: _FakeRegistryServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wi, "CHUNK_SIZE", 16)
    data = bytes(range(256)) * 4
    digest = _digest(data)

    def chunks() -> Iterator[bytes]:
        for i in range(0, len(data), 16):
            yield data[i : i + 16]

    registry = _client(fake_registry)
    registry.push_blob(digest, chunks())
    assert fake_registry.blobs[digest] == data
    assert fake_registry.chunk_sizes and all(size <= 16 for size in fake_registry.chunk_sizes)
    assert len(fake_registry.chunk_sizes) > 1


def test_an_already_present_blob_still_has_its_chunks_drained_but_not_sent(
    fake_registry: _FakeRegistryServer,
) -> None:
    data = b"already published"
    digest = _digest(data)
    fake_registry.blobs[digest] = data
    registry = _client(fake_registry)
    drained: list[bytes] = []

    def chunks() -> Iterator[bytes]:
        drained.append(data)
        yield data

    registry.push_blob(digest, chunks())
    assert drained == [data]
    assert fake_registry.chunk_sizes == []


def test_a_chunk_stream_that_does_not_hash_to_the_declared_digest_is_never_finalized(
    fake_registry: _FakeRegistryServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wi, "CHUNK_SIZE", 16)
    data = b"x" * 64
    wrong_digest = _digest(b"not the data that was streamed")
    registry = _client(fake_registry)
    with pytest.raises(wi.PublishError):
        registry.push_blob(wrong_digest, iter([data[i : i + 16] for i in range(0, len(data), 16)]))
    assert wrong_digest not in fake_registry.blobs


def test_a_tampered_archive_blob_never_reaches_the_finalizing_put(
    fake_registry: _FakeRegistryServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive's own tamper check, not just a caller passing a wrong digest, must
    stop the upload before the finalizing PUT (FDY-0074 review round)."""
    monkeypatch.setattr(wi, "CHUNK_SIZE", 4)
    path = tmp_path / "bad.oci.tar"
    write_archive(path, tamper=True)
    archive = wi.OciArchive(path)
    layer_digest = archive.blobs[-1]
    registry = _client(fake_registry)
    with pytest.raises(wi.PublishError, match="does not hash to its name"):
        registry.push_blob(layer_digest, archive.blob_chunks(layer_digest, chunk_size=4))
    assert fake_registry.blob_finalize_attempts == 0
    assert layer_digest not in fake_registry.blobs


def test_an_upload_redirected_off_the_registry_host_is_refused(
    fake_registry: _FakeRegistryServer,
) -> None:
    fake_registry.redirect_off_host = True
    registry = _client(fake_registry)
    with pytest.raises(wi.PublishError, match="sent the upload elsewhere"):
        registry.push_blob(_digest(b"x"), iter([b"x"]))


def test_a_manifest_push_answers_the_bearer_challenge(fake_registry: _FakeRegistryServer) -> None:
    data = b'{"schemaVersion":2}'
    registry = _client(fake_registry)
    registry.push_manifest("a-tag", "application/vnd.oci.image.manifest.v1+json", data)
    digest, body = fake_registry.manifests["a-tag"]
    assert body == data and digest == _digest(data)
    assert registry.published_digest("a-tag") == _digest(data)
    assert registry.published_digest("missing-tag") is None


def test_the_fake_refuses_what_ghcr_refused_in_the_v0_5_0_release(
    fake_registry: _FakeRegistryServer,
) -> None:
    """The fake must fail the way GHCR did, or the tests above prove nothing about GHCR:
    a finalizing PUT without an octet-stream Content-Type (what urllib sends for an
    empty body when none is given) is 404 BLOB_UPLOAD_INVALID (FDY-0090)."""
    registry = _client(fake_registry)
    _, reply, _ = registry.request(
        "POST",
        f"/v2/{fake_registry.name}/blobs/uploads/",
        body=b"",
        headers=wi.UPLOAD_HEADERS,
        ok=(202,),
    )
    assert reply["location"].startswith(f"/v2/{fake_registry.name}/blobs/upload/1.")
    assert reply["docker-upload-uuid"] == reply["location"].rsplit("/", 1)[-1]
    with pytest.raises(wi.PublishError, match=r"404 .*invalid content-type"):
        registry.request("PUT", f"{reply['location']}?digest={_digest(b'')}", body=b"", ok=(201,))


def test_every_upload_request_logs_its_answer_and_never_a_credential(
    fake_registry: _FakeRegistryServer, capsys: pytest.CaptureFixture[str]
) -> None:
    data = b"logged upload"
    registry = _client(fake_registry)
    registry.push_blob(_digest(data), iter([data]))
    log = capsys.readouterr().err
    assert "POST /v2/" in log and "PATCH /v2/" in log and "PUT /v2/" in log
    assert "docker-upload-uuid=1." in log and "range=0-12" in log
    assert fake_registry.token not in log and fake_registry.password not in log
    assert "Bearer" not in log and "Basic" not in log


def test_a_signed_upload_state_in_a_location_is_not_logged() -> None:
    assert (
        wi.redact("/v2/a/blobs/upload/1.x?_state=c2VjcmV0&digest=sha256%3Aab")
        == "/v2/a/blobs/upload/1.x?_state=<8 chars>&digest=sha256%3Aab"
    )
    assert wi.redact("/v2/a/blobs/upload/1.x") == "/v2/a/blobs/upload/1.x"


def test_userinfo_and_a_query_in_a_traced_location_are_dropped() -> None:
    """A hostile registry's Location can carry a credential ahead of the `_url()`
    check that would otherwise refuse it; only scheme, host and path are ever traced."""
    assert (
        wi.redact_location("https://user:secret@evil.example/v2/a/blobs/upload/1.x?_state=c2VjcmV0")
        == "https://evil.example/v2/a/blobs/upload/1.x"
    )
    assert wi.redact_location("/v2/a/blobs/upload/1.x?_state=c2VjcmV0") == "/v2/a/blobs/upload/1.x"


def test_userinfo_without_a_scheme_in_a_traced_location_is_still_dropped() -> None:
    """A Location with no `https://` still has a colon in front of the `@`, which
    `urlsplit` would misread as the scheme rather than as userinfo."""
    assert wi.redact_location("user:secret@evil.example/upload") == "evil.example/upload"
    assert wi.redact_location("ghp_abc123:x@evil.example/upload") == "evil.example/upload"


def test_a_tag_prefix_publishes_and_verifies_the_prefixed_tag_only(
    published: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, archives, _ = published
    registry = FakeRegistry(existing=None)
    monkeypatch.setattr(wi, "Registry", lambda _repository: registry)
    wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker", "ci-0123abcd-")
    assert [tag for tag, _, _ in registry.manifests] == ["ci-0123abcd-20260916-aaaaaaaaaaaa"]
    with pytest.raises(wi.PublishError, match="would not make a valid tag"):
        wi.publish(manifest, archives, "ghcr.io/sentania-labs/crucible-worker", "-bad/")


def test_an_empty_blob_streams_and_finalizes(fake_registry: _FakeRegistryServer) -> None:
    digest = _digest(b"")
    registry = _client(fake_registry)
    registry.push_blob(digest, iter([]))
    assert fake_registry.blobs[digest] == b""


def test_a_location_that_is_not_an_absolute_path_is_refused(
    fake_registry: _FakeRegistryServer,
) -> None:
    """`@evil.example/x` glued onto the registry host would be userinfo plus another host."""
    registry = _client(fake_registry)
    for location in ("@evil.example/upload", "//evil.example/upload", ":8443/upload"):
        with pytest.raises(wi.PublishError, match="not a path"):
            registry.request("PATCH", location, body=b"", ok=(202,))


def test_an_off_host_location_is_refused_without_echoing_its_userinfo(
    fake_registry: _FakeRegistryServer,
) -> None:
    registry = _client(fake_registry)
    with pytest.raises(wi.PublishError, match="elsewhere") as refused:
        registry.request("PATCH", "https://user:hunter2@evil.example/x", body=b"", ok=(202,))
    assert "hunter2" not in str(refused.value) and "evil.example" in str(refused.value)
