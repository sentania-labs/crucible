"""Resolving a worker image through the container registry (11, 13, 26).

The Docker provider asks the daemon what an image reference is: the daemon holds the
image, so `inspect_image` answers with a digest and the `crucible.harness` labels that
decide whether the reference may run with a harness's credential. A cluster holds no
image on Crucible's side. 26 therefore resolves through the registry the release
publishes to, before the Job is created, because the version refusal (07) has to happen
before anything is seeded or scheduled, not after a kubelet has already pulled.

This speaks the OCI distribution API and nothing else: a manifest read and a config blob
read, both by GET, with the anonymous bearer-token dance registries answer 401 with. It
never pushes, never deletes, and never sends a credential anywhere but the realm the
registry's own challenge named.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.client import HTTPSConnection
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from crucible.ports.execution import ImageInfo

DOCKER_HUB = "docker.io"
DOCKER_HUB_ENDPOINT = "registry-1.docker.io"
DEFAULT_TIMEOUT = 20.0

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)


class RegistryError(Exception):
    """The registry could not answer for a reference. The launch is refused (13)."""


@dataclass(frozen=True, slots=True)
class ImageReference:
    """A parsed reference: where the registry is, what the repository is called, and
    whether the caller named a tag or already pinned a digest."""

    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def wire_reference(self) -> str:
        return self.digest or self.tag or "latest"

    def pinned(self, digest: str) -> str:
        """The immutable form of this reference, which is what an attempt records.

        `repo@sha256:...`, the same shape the Docker provider records from the daemon's
        `RepoDigests`, and the shape the attempt's `image_digest` column is sized for.
        Which tag resolved to it stays on the execution row beside it (13)."""
        host = "" if self.registry == DOCKER_HUB else f"{self.registry}/"
        return f"{host}{self.repository}@{digest}"


def parse_reference(reference: str) -> ImageReference:
    remainder = reference
    digest: str | None = None
    tag: str | None = None
    if "@" in remainder:
        remainder, _, digest = remainder.partition("@")
    head, _, rest = remainder.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        registry, path = head, rest
    else:
        registry, path = DOCKER_HUB, remainder
    if digest is None and ":" in path.rsplit("/", 1)[-1]:
        path, _, tag = path.rpartition(":")
    if registry == DOCKER_HUB and "/" not in path:
        path = f"library/{path}"
    if not path:
        raise RegistryError(f"image reference {reference!r} names no repository")
    return ImageReference(registry=registry, repository=path, tag=tag, digest=digest)


class RegistryClient(Protocol):
    """What the provider needs from a registry: what a reference resolves to, and which
    tags a repository carries so `list_images` can report the promoted ones (25)."""

    def resolve(self, reference: str) -> ImageInfo: ...

    def list_tags(self, repository: str) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class RegistryAuth:
    """One registry's credential, read from the cluster's image pull Secret (26)."""

    username: str
    password: str

    def basic(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


def auths_from_dockerconfigjson(raw: bytes) -> dict[str, RegistryAuth]:
    """The `.dockerconfigjson` of a `kubernetes.io/dockerconfigjson` Secret.

    Values are read and held in memory for the life of the call that uses them; nothing
    here is logged, stored, or put on a command line (12)."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryError("the image pull secret is not JSON") from exc
    out: dict[str, RegistryAuth] = {}
    for host, entry in (document.get("auths") or {}).items():
        if not isinstance(entry, dict):
            continue
        username, password = str(entry.get("username", "")), str(entry.get("password", ""))
        if not username and entry.get("auth"):
            try:
                decoded = base64.b64decode(str(entry["auth"])).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            username, _, password = decoded.partition(":")
        if username:
            out[_host(str(host))] = RegistryAuth(username, password)
    return out


def _host(value: str) -> str:
    text = value.split("//", 1)[-1].split("/", 1)[0]
    return DOCKER_HUB if text in ("index.docker.io", "registry-1.docker.io") else text


@dataclass
class HttpRegistryClient:
    """A read-only OCI distribution client over HTTPS."""

    auths: Mapping[str, RegistryAuth] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    # Tokens the registry handed back for a scope, for the life of this client. A token
    # is a value, so it is held here and never written anywhere.
    _tokens: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    # ----- transport ---------------------------------------------------

    def _endpoint(self, registry: str) -> str:
        return DOCKER_HUB_ENDPOINT if registry == DOCKER_HUB else registry

    def _get(
        self, registry: str, path: str, accept: str, *, scope: str
    ) -> tuple[bytes, dict[str, str]]:
        for attempt in (0, 1):
            headers = {"Accept": accept, "User-Agent": "crucible"}
            token = self._tokens.get(scope)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            elif attempt == 0 and (auth := self.auths.get(registry)) is not None:
                headers["Authorization"] = auth.basic()
            conn = HTTPSConnection(self._endpoint(registry), timeout=self.timeout)
            try:
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()
                body = response.read()
                if response.status == 401 and attempt == 0:
                    challenge = response.getheader("WWW-Authenticate") or ""
                    self._tokens[scope] = self._bearer(registry, challenge, scope)
                    continue
                if response.status >= 400:
                    raise RegistryError(
                        f"{registry} answered {response.status} for {path}: "
                        f"{body.decode('utf-8', 'replace')[:200]}"
                    )
                return body, {k.lower(): v for k, v in response.getheaders()}
            except OSError as exc:
                raise RegistryError(f"{registry} is unreachable: {type(exc).__name__}") from exc
            finally:
                conn.close()
        raise RegistryError(f"{registry} refused the request for {path} twice")

    def _bearer(self, registry: str, challenge: str, scope: str) -> str:
        if not challenge.lower().startswith("bearer "):
            raise RegistryError(f"{registry} needs an authentication scheme Crucible has not got")
        fields: dict[str, str] = {}
        for part in challenge[len("bearer ") :].split(","):
            key, _, value = part.strip().partition("=")
            fields[key.strip().lower()] = value.strip().strip('"')
        realm = fields.get("realm")
        if not realm:
            raise RegistryError(f"{registry} sent a Bearer challenge with no realm")
        query = urlencode(
            {k: v for k, v in (("service", fields.get("service")), ("scope", scope)) if v}
        )
        host, _, path = realm.removeprefix("https://").partition("/")
        headers = {"Accept": "application/json", "User-Agent": "crucible"}
        if (auth := self.auths.get(registry)) is not None:
            headers["Authorization"] = auth.basic()
        conn = HTTPSConnection(host, timeout=self.timeout)
        try:
            conn.request("GET", f"/{path}?{query}", headers=headers)
            response = conn.getresponse()
            body = response.read()
            if response.status >= 400:
                raise RegistryError(f"{registry} refused a pull token: {response.status}")
            document = json.loads(body.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryError(f"{registry} token request failed: {type(exc).__name__}") from exc
        finally:
            conn.close()
        token = str(document.get("token") or document.get("access_token") or "")
        if not token:
            raise RegistryError(f"{registry} returned no pull token")
        return token

    # ----- the two calls the provider makes -----------------------------

    def resolve(self, reference: str) -> ImageInfo:
        parsed = parse_reference(reference)
        scope = f"repository:{parsed.repository}:pull"
        path = f"/v2/{parsed.repository}/manifests/{quote(parsed.wire_reference, safe=':')}"
        body, headers = self._get(parsed.registry, path, ", ".join(MANIFEST_TYPES), scope=scope)
        digest = headers.get("docker-content-digest", "") or parsed.digest or ""
        manifest = _document(body, parsed.repository)
        if str(manifest.get("mediaType", "")) in INDEX_TYPES:
            child = _linux_amd64(manifest, parsed.repository)
            body, _ = self._get(
                parsed.registry,
                f"/v2/{parsed.repository}/manifests/{quote(child, safe=':')}",
                ", ".join(MANIFEST_TYPES),
                scope=scope,
            )
            manifest = _document(body, parsed.repository)
        config_digest = str((manifest.get("config") or {}).get("digest", ""))
        if not config_digest:
            raise RegistryError(f"{reference!r} has a manifest with no config descriptor")
        blob, _ = self._get(
            parsed.registry,
            f"/v2/{parsed.repository}/blobs/{quote(config_digest, safe=':')}",
            "application/json",
            scope=scope,
        )
        config = _document(blob, parsed.repository)
        labels = {
            str(k): str(v) for k, v in ((config.get("config") or {}).get("Labels") or {}).items()
        }
        if not digest:
            raise RegistryError(f"{reference!r} resolved to no digest")
        return ImageInfo(
            reference=parsed.pinned(digest),
            digest=digest,
            harness=labels.get("crucible.harness"),
            harness_version=labels.get("crucible.harness_version"),
            labels=labels,
        )

    def list_tags(self, repository: str) -> list[str]:
        parsed = parse_reference(repository)
        body, _ = self._get(
            parsed.registry,
            f"/v2/{parsed.repository}/tags/list",
            "application/json",
            scope=f"repository:{parsed.repository}:pull",
        )
        document = _document(body, parsed.repository)
        return [str(tag) for tag in (document.get("tags") or [])]


def _document(raw: bytes, repository: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistryError(f"{repository} returned something that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise RegistryError(f"{repository} returned a document that is not an object")
    return parsed


def _linux_amd64(index: Mapping[str, Any], repository: str) -> str:
    """The one manifest of a multi-platform index a lab node runs.

    A cluster of one architecture is what 26 describes; picking deliberately rather
    than taking the first entry keeps the recorded digest the one that will run."""
    entries = [m for m in (index.get("manifests") or []) if isinstance(m, dict)]
    for entry in entries:
        platform = entry.get("platform") or {}
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            return str(entry.get("digest", ""))
    for entry in entries:
        platform = entry.get("platform") or {}
        if platform.get("os") == "linux":
            return str(entry.get("digest", ""))
    raise RegistryError(f"{repository} has no linux manifest in its index")
