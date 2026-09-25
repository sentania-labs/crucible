"""The real crane against a registry that redirects its blobs to another host (108).

GHCR answers a config blob GET with a 307 to a different host, and the hand-written
client this replaced parsed the redirect as the blob. The registry here does the same:
it serves manifests and tag lists on `127.0.0.1`, answers every blob GET with a 307 to a
second server named `localhost`, and demands a password for everything under `/v2/`,
which reaches crane only through the adapter's private DOCKER_CONFIG. The redirect names
a host, as GHCR's does: crane refuses a redirect to a private IP literal (its SSRF guard,
which the implementation notes record), and allows one to a name. The blob host records
the headers it was sent, so the test also proves the credential was not forwarded to it.

`make registry-check` runs this with the pinned crane on PATH; so does CI.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from crucible.adapters.execution.k8sregistry import (
    CraneRegistryClient,
    RegistryAuth,
    RegistryError,
)

pytestmark = [
    pytest.mark.e2e_registry,
    pytest.mark.skipif(
        not os.environ.get("CRUCIBLE_E2E_REGISTRY"), reason="needs make registry-check"
    ),
]

USER, PASSWORD = "tester", "stub-registry-password"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _config(architecture: str, labels: dict[str, str]) -> bytes:
    return json.dumps(
        {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": labels},
            "rootfs": {"type": "layers", "diff_ids": []},
        }
    ).encode()


def _manifest(media_type: str, config: bytes) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": media_type,
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json"
                if media_type == DOCKER_MANIFEST
                else "application/vnd.oci.image.config.v1+json",
                "digest": _digest(config),
                "size": len(config),
            },
            "layers": [],
        }
    ).encode()


@dataclass
class Content:
    """What the stub registry holds: manifests by repository and reference, and blobs."""

    manifests: dict[tuple[str, str], tuple[str, bytes]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    blob_requests: list[dict[str, str]] = field(default_factory=list)

    def add_manifest(self, repository: str, tag: str, media_type: str, raw: bytes) -> str:
        digest = _digest(raw)
        self.manifests[(repository, tag)] = (media_type, raw)
        self.manifests[(repository, digest)] = (media_type, raw)
        return digest

    def tags(self, repository: str) -> list[str]:
        return sorted(ref for repo, ref in self.manifests if repo == repository and ":" not in ref)


def _serve(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def registry() -> Iterator[tuple[str, Content]]:
    content = Content()

    class BlobHost(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content.blob_requests.append({k.lower(): v for k, v in self.headers.items()})
            blob = content.blobs.get(self.path.rsplit("/", 1)[-1])
            if blob is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *_args: Any) -> None:
            pass

    blob_host = _serve(BlobHost)
    blob_port = blob_host.server_address[1]
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    class Registry(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="stub"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _answer(self, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(200)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._authorized():
                return
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if parts == ["v2"]:
                self._answer(b"{}", {"Content-Type": "application/json"})
            elif len(parts) >= 4 and parts[-2] == "manifests":
                found = content.manifests.get(("/".join(parts[1:-2]), parts[-1]))
                if found is None:
                    self.send_error(404)
                    return
                media_type, raw = found
                self._answer(
                    raw, {"Content-Type": media_type, "Docker-Content-Digest": _digest(raw)}
                )
            elif len(parts) >= 4 and parts[-2] == "blobs":
                # What GHCR does: the blob lives on another host.
                self.send_response(307)
                self.send_header("Location", f"http://localhost:{blob_port}/blob/{parts[-1]}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif parts[-2:] == ["tags", "list"]:
                repository = "/".join(parts[1:-2])
                body = json.dumps({"name": repository, "tags": content.tags(repository)})
                self._answer(body.encode(), {"Content-Type": "application/json"})
            else:
                self.send_error(404)

        def log_message(self, *_args: Any) -> None:
            pass

    server = _serve(Registry)
    try:
        yield f"127.0.0.1:{server.server_address[1]}", content
    finally:
        server.shutdown()
        blob_host.shutdown()


def _client(host: str) -> CraneRegistryClient:
    return CraneRegistryClient(auths={host: RegistryAuth(USER, PASSWORD)}, timeout=15)


def test_resolve_follows_a_blob_redirect_to_another_host(
    registry: tuple[str, Content],
) -> None:
    host, content = registry
    labels = {"crucible.harnesses": "codex", "crucible.harness.codex.version": "0.156.0"}
    config = _config("amd64", labels)
    content.blobs[_digest(config)] = config
    digest = content.add_manifest(
        "sentania-labs/crucible-worker",
        "0.5.3",
        DOCKER_MANIFEST,
        _manifest(DOCKER_MANIFEST, config),
    )

    info = _client(host).resolve(f"{host}/sentania-labs/crucible-worker:0.5.3")

    assert info.digest == digest
    assert info.reference == f"{host}/sentania-labs/crucible-worker@{digest}"
    assert info.harnesses == {"codex": "0.156.0"}
    # The config really came from the other host, and the password did not go with it.
    assert content.blob_requests
    assert all("authorization" not in headers for headers in content.blob_requests)


def test_an_index_pins_its_own_digest_and_reads_the_amd64_labels(
    registry: tuple[str, Content],
) -> None:
    host, content = registry
    manifests = []
    for architecture, version in (("arm64", "9.9.9"), ("amd64", "1.2.8")):
        config = _config(
            architecture, {"crucible.harnesses": "agy", "crucible.harness.agy.version": version}
        )
        content.blobs[_digest(config)] = config
        raw = _manifest(OCI_MANIFEST, config)
        content.add_manifest("worker", _digest(raw), OCI_MANIFEST, raw)
        manifests.append(
            {
                "mediaType": OCI_MANIFEST,
                "digest": _digest(raw),
                "size": len(raw),
                "platform": {"architecture": architecture, "os": "linux"},
            }
        )
    index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests})
    index_digest = content.add_manifest("worker", "multi", OCI_INDEX, index.encode())

    info = _client(host).resolve(f"{host}/worker:multi")

    assert info.digest == index_digest
    assert info.harnesses == {"agy": "1.2.8"}


def test_tags_are_listed_with_the_credential(registry: tuple[str, Content]) -> None:
    host, content = registry
    config = _config("amd64", {})
    content.blobs[_digest(config)] = config
    for tag in ("0.5.3", "latest"):
        content.add_manifest("worker", tag, DOCKER_MANIFEST, _manifest(DOCKER_MANIFEST, config))

    assert _client(host).list_tags(f"{host}/worker") == ["0.5.3", "latest"]


def test_without_the_credential_the_registry_refuses(registry: tuple[str, Content]) -> None:
    host, _ = registry

    with pytest.raises(RegistryError, match=r"UNAUTHORIZED|401"):
        CraneRegistryClient(timeout=15).list_tags(f"{host}/worker")


def test_a_missing_tag_names_the_registry_and_the_reference(
    registry: tuple[str, Content],
) -> None:
    host, _ = registry

    with pytest.raises(RegistryError) as caught:
        _client(host).resolve(f"{host}/worker:nope")

    assert str(caught.value).startswith(f"{host}: ")
    assert "worker/manifests/nope" in str(caught.value)
