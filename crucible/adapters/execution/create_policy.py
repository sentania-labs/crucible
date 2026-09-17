"""The Docker create-request policy (13).

The socket proxy narrows the API surface, not the request bodies: it cannot refuse a
create that asks for `Privileged`, a host namespace, or a bind of `/`. This module is
what refuses those, before the request is made. It protects against Crucible bugs, not
against a hostile Crucible, and every rule here has a unit test (18).

Pure functions over the create body: no I/O, no clock, no daemon.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

FORBIDDEN_HOST_CONFIG_FLAGS: tuple[str, ...] = (
    "Privileged",
    "PublishAllPorts",
)
NAMESPACE_KEYS: tuple[str, ...] = ("PidMode", "NetworkMode", "IpcMode", "UTSMode", "UsernsMode")
HOST_NAMESPACE_VALUES: frozenset[str] = frozenset({"host"})


class CreateRequestRefusedError(Exception):
    """Crucible refused to emit a container create request (13)."""

    def __init__(self, violations: Sequence[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = tuple(violations)


@dataclass(frozen=True, slots=True)
class CreatePolicy:
    """What a create request may ask for. Roots are absolute, daemon-visible paths."""

    image_allowlist: tuple[str, ...]
    artifact_root: str
    credential_root: str | None = None
    allowed_volumes: tuple[str, ...] = ()
    extra_readonly_paths: tuple[str, ...] = field(default=())

    @property
    def roots(self) -> tuple[str, ...]:
        roots = [self.artifact_root]
        if self.credential_root:
            roots.append(self.credential_root)
        roots.extend(self.extra_readonly_paths)
        return tuple(posixpath.normpath(r) for r in roots if r)


def _glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    for char in pattern:
        if char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        else:
            out.append(re.escape(char))
    return re.compile("".join(out) + r"\Z")


def image_allowed(image: str, allowlist: Sequence[str]) -> bool:
    """An image reference is allowed when it matches a pattern verbatim or by digest.

    `crucible-worker:*` covers `crucible-worker:script-1` and the same name pinned by
    digest, because the digest form still names the tag Crucible resolved."""
    candidates = [image]
    if "@" in image:
        candidates.append(image.split("@", 1)[0])
    return any(_glob(pattern).match(candidate) for pattern in allowlist for candidate in candidates)


def _inside(path: str, roots: Sequence[str]) -> bool:
    normalized = posixpath.normpath(path)
    for root in roots:
        if normalized == root or normalized.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _bind_sources(host_config: Mapping[str, Any]) -> list[str]:
    sources: list[str] = []
    for entry in host_config.get("Binds") or []:
        text = str(entry)
        # "src:dst[:opts]"; a Windows-style drive letter cannot occur on this daemon.
        sources.append(text.split(":", 1)[0])
    for mount in host_config.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        if str(mount.get("Type")) == "bind":
            sources.append(str(mount.get("Source", "")))
    return sources


def _volume_names(host_config: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for mount in host_config.get("Mounts") or []:
        if isinstance(mount, dict) and str(mount.get("Type")) == "volume":
            names.append(str(mount.get("Source", "")))
    return names


def violations(body: Mapping[str, Any], policy: CreatePolicy) -> list[str]:
    """Every rule of 13 that this create body breaks, in a stable order."""
    found: list[str] = []
    image = str(body.get("Image", ""))
    if not image:
        found.append("the create request names no image")
    elif not image_allowed(image, policy.image_allowlist):
        found.append(f"image {image!r} is not in the allowlist {list(policy.image_allowlist)}")

    host_config = body.get("HostConfig") or {}
    if not isinstance(host_config, Mapping):
        return [*found, "HostConfig is not an object"]

    for flag in FORBIDDEN_HOST_CONFIG_FLAGS:
        if host_config.get(flag):
            found.append(f"HostConfig.{flag} is never permitted")
    for key in NAMESPACE_KEYS:
        value = str(host_config.get(key) or "")
        head = value.split(":", 1)[0]
        if head in HOST_NAMESPACE_VALUES:
            found.append(f"HostConfig.{key}={value!r} shares a host namespace")
        if head == "container" and key != "NetworkMode":
            found.append(f"HostConfig.{key}={value!r} joins another container's namespace")
    if host_config.get("CapAdd"):
        found.append(f"HostConfig.CapAdd={list(host_config['CapAdd'])} adds capabilities")
    cap_drop = [str(c).upper() for c in (host_config.get("CapDrop") or [])]
    if "ALL" not in cap_drop:
        found.append("HostConfig.CapDrop must contain ALL")
    if not host_config.get("ReadonlyRootfs"):
        found.append("HostConfig.ReadonlyRootfs must be true")
    security_opt = [str(o) for o in (host_config.get("SecurityOpt") or [])]
    if "no-new-privileges:true" not in security_opt and "no-new-privileges" not in security_opt:
        found.append("HostConfig.SecurityOpt must contain no-new-privileges")
    if any(
        o.startswith("seccomp=unconfined") or o.startswith("apparmor=unconfined")
        for o in security_opt
    ):
        found.append("HostConfig.SecurityOpt must not unconfine seccomp or AppArmor")
    if host_config.get("Devices"):
        found.append("HostConfig.Devices is never permitted")
    if host_config.get("Sysctls"):
        found.append("HostConfig.Sysctls is never permitted")
    if host_config.get("Init") is not True:
        # S5: a harness as PID 1 ignores SIGTERM, so drain would always end in SIGKILL.
        found.append("HostConfig.Init must be true")
    if str(body.get("User", "")) != "1000:1000":
        found.append(f"User must be 1000:1000, not {body.get('User')!r}")

    for source in _bind_sources(host_config):
        if not source.startswith("/"):
            found.append(f"bind source {source!r} is not an absolute path")
        elif not _inside(source, policy.roots):
            found.append(f"bind source {source!r} is outside the artifact and credential roots")
    for name in _volume_names(host_config):
        if policy.allowed_volumes and name not in policy.allowed_volumes:
            found.append(f"volume {name!r} is not one Crucible owns")
    return found


def check(body: Mapping[str, Any], policy: CreatePolicy) -> None:
    """Raise unless the create body satisfies every rule of 13."""
    found = violations(body, policy)
    if found:
        raise CreateRequestRefusedError(found)
