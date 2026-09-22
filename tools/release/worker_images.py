#!/usr/bin/env python3
"""Publish and verify the worker images the release builds (13, 24, C11).

    worker_images.py publish --manifest images/manifest.env --archives <dir> \
        --repository ghcr.io/sentania-labs/crucible-worker
    worker_images.py verify --manifest images/manifest.env \
        --repository ghcr.io/sentania-labs/crucible-worker

`publish` pushes each image's OCI archive, exactly as images/build.sh wrote it, to
`<repository>:<tag>` over the OCI distribution API. The manifest is sent as the bytes
in the archive, so the registry stores the digest the build produced and
images/manifest.env declares; nothing is re-encoded or recompressed on the way, which
`docker push` of a loaded image would do. Before anything is sent, the archive's own
manifest digest must equal the declared one.

A published tag is never overwritten (the rule the service image has, ADR 0010):
absent means push; present with the declared digest means a re-run of the same
release, and the push is skipped; present with any other digest stops the release.
Only an explicit 404 counts as absent. Any other answer stops the job, because
treating a network blip as "absent" would push over a tag that exists.

`verify` reads every tag back and fails unless each carries the declared digest.

Stdlib only, like compose_images.py: the release job does not install the project's
Python environment. Credentials come from REGISTRY_USERNAME and REGISTRY_PASSWORD in
the environment, never an argument, and are sent only where the registry's own
challenge asks for them: its token realm, or the registry itself when it answers
Basic. REGISTRY_CA_FILE names an extra CA for a local test registry; nothing in the
release sets it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
TIMEOUT = 120.0


class PublishError(Exception):
    """A worker image could not be published or verified; the reason is operator-readable."""


@dataclass(frozen=True)
class DeclaredImage:
    """One image as images/manifest.env declares it."""

    key: str
    tag: str
    digest: str

    @property
    def archive_name(self) -> str:
        # build.sh writes crucible-worker:<version>-<build> to
        # crucible-worker-<version>-<build>.oci.tar.
        return f"crucible-worker-{self.tag}.oci.tar"


def declared_images(manifest: Path) -> list[DeclaredImage]:
    values: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    images: list[DeclaredImage] = []
    for key, value in sorted(values.items()):
        if key.endswith(("_DIGEST", "_HARNESSES")):
            continue
        repository, _, tag = value.partition(":")
        digest = values.get(f"{key}_DIGEST", "")
        if repository != "crucible-worker" or not tag:
            raise PublishError(f"{manifest}: {key}={value} is not a crucible-worker tag")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise PublishError(f"{manifest}: {key} has no valid digest")
        images.append(DeclaredImage(key, tag, digest))
    if not images:
        raise PublishError(f"{manifest} declares no image")
    return images


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class OciArchive:
    """An OCI image layout tarball holding exactly one image manifest."""

    path: Path
    manifest_bytes: bytes = b""
    media_type: str = ""
    blobs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        with tarfile.open(self.path) as archive:
            index = json.loads(self._member(archive, "index.json"))
            entries = index.get("manifests") or []
            if len(entries) != 1:
                raise PublishError(f"{self.path.name} holds {len(entries)} manifests, not one")
            entry = entries[0]
            self.media_type = str(entry.get("mediaType", ""))
            if self.media_type != "application/vnd.oci.image.manifest.v1+json":
                raise PublishError(f"{self.path.name} holds a {self.media_type}, not one image")
            self.manifest_bytes = self._blob(archive, str(entry.get("digest", "")))
            manifest = json.loads(self.manifest_bytes)
            self.blobs = [str(manifest["config"]["digest"])]
            self.blobs += [str(layer["digest"]) for layer in manifest.get("layers") or []]

    @property
    def digest(self) -> str:
        return sha256(self.manifest_bytes)

    @staticmethod
    def _member(archive: tarfile.TarFile, name: str) -> bytes:
        member = archive.extractfile(name)
        if member is None:
            raise PublishError(f"{archive.name!r} has no {name}")
        return member.read()

    def _blob(self, archive: tarfile.TarFile, digest: str) -> bytes:
        algorithm, _, hex_digest = digest.partition(":")
        if algorithm != "sha256" or len(hex_digest) != 64:
            raise PublishError(f"{self.path.name} names a blob {digest!r} that is not sha256")
        data = self._member(archive, f"blobs/sha256/{hex_digest}")
        if sha256(data) != digest:
            raise PublishError(f"{self.path.name}: blob {digest} does not hash to its name")
        return data

    def blob(self, digest: str) -> bytes:
        with tarfile.open(self.path) as archive:
            return self._blob(archive, digest)


class Registry:
    """A minimal OCI distribution client: what publishing one image needs, over HTTPS."""

    def __init__(self, repository: str) -> None:
        host, _, name = repository.partition("/")
        if not name or ("." not in host and ":" not in host and host != "localhost"):
            raise PublishError(f"{repository!r} does not name a registry host and repository")
        self.host = host
        self.name = name
        self.token: str | None = None
        # Basic credentials go to the registry itself only after it asked for them.
        self.basic_challenged = False
        self.username = os.environ.get("REGISTRY_USERNAME", "")
        self.password = os.environ.get("REGISTRY_PASSWORD", "")
        context = ssl.create_default_context()
        if ca_file := os.environ.get("REGISTRY_CA_FILE"):
            context.load_verify_locations(ca_file)
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def _url(self, path: str) -> str:
        if "://" not in path:
            return f"https://{self.host}{path}"
        # An absolute Location from the registry must stay on the registry: the request
        # carries its credential.
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme != "https" or parsed.netloc != self.host:
            raise PublishError(f"{self.host} sent the upload elsewhere: {parsed.netloc}")
        return path

    def _basic(self) -> str | None:
        if not self.username:
            return None
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _authenticate(self, challenge: str) -> None:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() == "basic":
            if self._basic() is None:
                raise PublishError(f"{self.host} wants credentials; set REGISTRY_USERNAME")
            self.token = None
            self.basic_challenged = True
            return
        if scheme.lower() != "bearer":
            raise PublishError(f"{self.host} sent an authentication scheme this cannot answer")
        fields: dict[str, str] = {}
        for part in params.split(","):
            key, _, value = part.strip().partition("=")
            fields[key.strip().lower()] = value.strip().strip('"')
        realm = fields.get("realm", "")
        if not realm.startswith("https://"):
            raise PublishError(f"{self.host} named a token realm that is not HTTPS")
        query = {"scope": f"repository:{self.name}:pull,push"}
        if fields.get("service"):
            query["service"] = fields["service"]
        request = urllib.request.Request(f"{realm}?{urllib.parse.urlencode(query)}")
        if (basic := self._basic()) is not None:
            request.add_unredirected_header("Authorization", basic)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                document = json.loads(response.read())
        except (urllib.error.URLError, ValueError) as exc:
            raise PublishError(f"{self.host} refused a push token: {exc}") from exc
        token = str(document.get("token") or document.get("access_token") or "")
        if not token:
            raise PublishError(f"{self.host} returned no token")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> tuple[int, dict[str, str], bytes]:
        """One request, re-sent once after answering a 401 challenge. A status outside
        `ok` is an error, so a caller never mistakes a refusal for an answer."""
        for attempt in (0, 1):
            request = urllib.request.Request(self._url(path), data=body, method=method)
            for key, value in (headers or {}).items():
                request.add_header(key, value)
            # Unredirected: a redirect to another host never carries the credential.
            if self.token:
                request.add_unredirected_header("Authorization", f"Bearer {self.token}")
            elif self.basic_challenged and (basic := self._basic()) is not None:
                request.add_unredirected_header("Authorization", basic)
            try:
                with self.opener.open(request, timeout=TIMEOUT) as response:
                    status, answer = response.status, response.read()
                    reply = {k.lower(): v for k, v in response.headers.items()}
            except urllib.error.HTTPError as exc:
                status, answer = exc.code, exc.read()
                reply = {k.lower(): v for k, v in exc.headers.items()}
            except urllib.error.URLError as exc:
                raise PublishError(f"{self.host} is unreachable: {exc.reason}") from exc
            if status == 401 and attempt == 0:
                self._authenticate(reply.get("www-authenticate", ""))
                continue
            if status not in ok:
                detail = answer.decode("utf-8", "replace")[:300]
                raise PublishError(f"{self.host} answered {status} to {method} {path}: {detail}")
            return status, reply, answer
        raise PublishError(f"{self.host} refused {method} {path} after authenticating")

    def published_digest(self, tag: str) -> str | None:
        """The digest `<repository>:<tag>` carries, or None on an explicit 404."""
        status, reply, body = self.request(
            "GET",
            f"/v2/{self.name}/manifests/{tag}",
            headers={"Accept": ", ".join(MANIFEST_TYPES)},
            ok=(200, 404),
        )
        if status == 404:
            return None
        digest = sha256(body)
        header = reply.get("docker-content-digest")
        if header and header != digest:
            raise PublishError(f"{self.host} reports {header} for {tag} but served {digest}")
        return digest

    def push_blob(self, digest: str, data: bytes) -> None:
        status, _, _ = self.request("HEAD", f"/v2/{self.name}/blobs/{digest}", ok=(200, 404))
        if status == 200:
            return
        _, reply, _ = self.request("POST", f"/v2/{self.name}/blobs/uploads/", body=b"", ok=(202,))
        location = reply.get("location", "")
        if not location:
            raise PublishError(f"{self.host} opened an upload with no location")
        separator = "&" if "?" in location else "?"
        self.request(
            "PUT",
            f"{location}{separator}digest={urllib.parse.quote(digest)}",
            body=data,
            headers={"Content-Type": "application/octet-stream"},
            ok=(201,),
        )

    def push_manifest(self, tag: str, media_type: str, data: bytes) -> None:
        _, reply, _ = self.request(
            "PUT",
            f"/v2/{self.name}/manifests/{tag}",
            body=data,
            headers={"Content-Type": media_type},
            ok=(201,),
        )
        stored = reply.get("docker-content-digest")
        if stored and stored != sha256(data):
            raise PublishError(f"{self.host} stored {tag} as {stored}, not {sha256(data)}")


def publish(manifest: Path, archives: Path, repository: str) -> None:
    registry = Registry(repository)
    for image in declared_images(manifest):
        reference = f"{repository}:{image.tag}"
        archive = OciArchive(archives / image.archive_name)
        if archive.digest != image.digest:
            raise PublishError(
                f"{archive.path.name} holds {archive.digest}, but images/manifest.env "
                f"declares {image.digest} for {image.key}; refusing to publish"
            )
        existing = registry.published_digest(image.tag)
        if existing == image.digest:
            print(f"{reference} is already published with {image.digest}; leaving it alone")
            continue
        if existing is not None:
            raise PublishError(
                f"{reference} is already published with {existing}, not the declared "
                f"{image.digest}. A published tag is never overwritten; change the build "
                "inputs so the image gets a new tag."
            )
        for digest in archive.blobs:
            registry.push_blob(digest, archive.blob(digest))
        registry.push_manifest(image.tag, archive.media_type, archive.manifest_bytes)
        print(f"{reference} published with {image.digest}")


def verify(manifest: Path, repository: str) -> None:
    registry = Registry(repository)
    failures: list[str] = []
    for image in declared_images(manifest):
        reference = f"{repository}:{image.tag}"
        published = registry.published_digest(image.tag)
        if published != image.digest:
            failures.append(f"{reference} carries {published or 'nothing'}, not {image.digest}")
        else:
            print(f"{reference} carries the declared {image.digest}")
    if failures:
        raise PublishError("; ".join(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    push = sub.add_parser("publish")
    push.add_argument("--manifest", type=Path, required=True)
    push.add_argument("--archives", type=Path, required=True)
    push.add_argument("--repository", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            publish(args.manifest, args.archives, args.repository)
        else:
            verify(args.manifest, args.repository)
    except PublishError as exc:
        print(f"worker_images: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
