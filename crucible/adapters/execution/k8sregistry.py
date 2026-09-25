"""Resolving a worker image through the container registry (11, 13, 26).

The Docker provider asks the daemon what an image reference is: the daemon holds the
image, so `inspect_image` answers with a digest and the harness labels that decide
whether the reference may run with a harness's credential. A cluster holds no
image on Crucible's side. 26 therefore resolves through the registry the release
publishes to, before the Job is created, because the version refusal (07) has to happen
before anything is seeded or scheduled, not after a kubelet has already pulled.

The registry is read with `crane` (go-containerregistry), which the service image ships
at a pinned version. Registries differ in how they authenticate and where they serve a
blob from (GHCR answers a blob GET with a 307 to another host, 108), and a widely used
client already handles those differences; a hand-written one did not (the operator's
decision, 2026-09-24). Only `crane digest`, `crane config` and `crane ls` are run: it
never pushes and never deletes.

The credential is the image pull Secret's, written for the one call into a private
`DOCKER_CONFIG` directory that is removed when the call returns, whatever happened. It
is never on a command line and never logged (12).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Protocol

from crucible.ports.execution import ImageInfo

DOCKER_HUB = "docker.io"
# The key Docker Hub's credential is stored under in a Docker config file.
DOCKER_HUB_AUTH_KEY = "https://index.docker.io/v1/"
CRANE = "crane"
# Per crane call. One resolve is two calls, so a registry that never answers holds a
# launch for at most twice this.
DEFAULT_TIMEOUT = 20.0
# The one platform a lab node runs. For an index the recorded digest is still the
# index's own; only the labels are read from this platform's image config.
PLATFORM = "linux/amd64"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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
    def canonical(self) -> str:
        """The form handed to crane: registry named, and a digest or a tag, never both."""
        suffix = f"@{self.digest}" if self.digest else f":{self.tag or 'latest'}"
        return f"{self.registry}/{self.repository}{suffix}"

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
    if ":" in path.rsplit("/", 1)[-1]:
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

    def encoded(self) -> str:
        return base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")


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
class CraneRegistryClient:
    """A read-only registry client that runs the `crane` binary for each read."""

    auths: dict[str, RegistryAuth] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    binary: str = CRANE

    def resolve(self, reference: str) -> ImageInfo:
        """The reference's own digest (an index's, when it is one) and the labels of
        its linux/amd64 image config. The config is read by that digest, so a tag that
        moves between the two calls cannot pair one image's digest with another's
        labels."""
        parsed = parse_reference(reference)
        digest = self._crane(parsed.registry, "digest", parsed.canonical).strip()
        if not DIGEST.fullmatch(digest):
            raise RegistryError(f"{reference!r} resolved to no digest")
        by_digest = f"{parsed.registry}/{parsed.repository}@{digest}"
        raw = self._crane(parsed.registry, "config", "--platform", PLATFORM, by_digest)
        config = _document(raw, parsed.repository)
        labels = {
            str(k): str(v) for k, v in ((config.get("config") or {}).get("Labels") or {}).items()
        }
        return ImageInfo.from_labels(parsed.pinned(digest), digest, labels)

    def list_tags(self, repository: str) -> list[str]:
        parsed = parse_reference(repository)
        out = self._crane(parsed.registry, "ls", f"{parsed.registry}/{parsed.repository}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    # ----- running crane ------------------------------------------------

    def _crane(self, registry: str, *args: str) -> str:
        # mkdtemp creates the directory 0700 and owned by this process's user.
        config_dir = tempfile.mkdtemp(prefix="crucible-crane-")
        try:
            _write_docker_config(config_dir, registry, self.auths.get(registry))
            # The service's own environment, so crane trusts what the service trusts
            # (SSL_CERT_FILE, the system store with the lab CA) and takes the same proxy.
            env = {**os.environ, "DOCKER_CONFIG": config_dir}
            try:
                done = subprocess.run(
                    [self.binary, *args],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise RegistryError(
                    f"the registry client {self.binary!r} is not installed "
                    "(the service image ships it)"
                ) from exc
            except subprocess.TimeoutExpired:
                raise RegistryError(f"{registry} did not answer within {self.timeout:g}s") from None
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)
        if done.returncode != 0:
            raise RegistryError(f"{registry}: {self._reason(done.stderr)}")
        return done.stdout.decode("utf-8", "replace")

    def _reason(self, stderr: bytes) -> str:
        """crane's own `Error:` line, which names the request and the registry's answer
        (`MANIFEST_UNKNOWN`, `UNAUTHORIZED`, `DENIED`). It carries no credential, and any
        credential value that ever appeared in it is replaced anyway."""
        lines = [line.strip() for line in stderr.decode("utf-8", "replace").splitlines()]
        lines = [line for line in lines if line]
        errors = [line.removeprefix("Error: ") for line in lines if line.startswith("Error: ")]
        text = (errors or lines or ["crane failed with no message"])[-1]
        for auth in self.auths.values():
            for secret in (auth.password, auth.encoded()):
                if secret:
                    text = text.replace(secret, "[redacted]")
        return text[:300]


def _write_docker_config(directory: str, registry: str, auth: RegistryAuth | None) -> None:
    """A Docker config holding at most the one registry's credential, file mode 0600.

    Written even when there is no credential, so crane never reads a config from the
    service user's home instead."""
    key = DOCKER_HUB_AUTH_KEY if registry == DOCKER_HUB else registry
    auths = {key: {"auth": auth.encoded()}} if auth is not None else {}
    path = os.path.join(directory, "config.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"auths": auths}, handle)


def _document(raw: str, repository: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise RegistryError(f"{repository} returned something that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise RegistryError(f"{repository} returned a document that is not an object")
    return parsed
