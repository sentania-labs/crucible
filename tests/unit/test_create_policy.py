"""Every forbidden create option is refused (13, 18).

The socket proxy narrows the API surface, not the request bodies, so this is the only
thing that stops Crucible asking for `Privileged`, a host namespace, or a bind of `/`.
"""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.execution.create_policy import (
    CreatePolicy,
    CreateRequestRefusedError,
    check,
    image_allowed,
    violations,
)

POLICY = CreatePolicy(
    image_allowlist=("crucible-worker:*", "ghcr.io/sentania-labs/crucible-worker:*"),
    artifact_root="/var/lib/crucible/artifacts",
    credential_root="/var/lib/crucible/credentials",
    allowed_volumes=("crucible-artifacts",),
)


def body(**overrides: Any) -> dict[str, Any]:
    """The worker shape of 08, which must pass."""
    host: dict[str, Any] = {
        "Init": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": "crucible-workers",
        "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=512m"},
        "Memory": 4 * 1024**3,
        "PidsLimit": 512,
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/var/lib/crucible/artifacts/workspaces/01AB/repo",
                "Target": "/crucible/repo",
                "ReadOnly": False,
            }
        ],
    }
    document: dict[str, Any] = {
        "Image": "crucible-worker:script-harness-1.0.0-abc",
        "User": "1000:1000",
        "HostConfig": host,
    }
    for key, value in overrides.items():
        if key.startswith("host_"):
            host[key[5:]] = value
        else:
            document[key] = value
    return document


def test_the_worker_shape_of_08_passes() -> None:
    assert violations(body(), POLICY) == []
    check(body(), POLICY)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"host_Privileged": True}, "Privileged"),
        ({"host_PidMode": "host"}, "PidMode"),
        ({"host_NetworkMode": "host"}, "NetworkMode"),
        ({"host_IpcMode": "host"}, "IpcMode"),
        ({"host_UTSMode": "host"}, "UTSMode"),
        ({"host_UsernsMode": "host"}, "UsernsMode"),
        ({"host_PidMode": "container:other"}, "another container's namespace"),
        ({"host_CapAdd": ["SYS_ADMIN"]}, "adds capabilities"),
        ({"host_CapDrop": []}, "CapDrop must contain ALL"),
        ({"host_ReadonlyRootfs": False}, "ReadonlyRootfs must be true"),
        ({"host_SecurityOpt": []}, "no-new-privileges"),
        ({"host_SecurityOpt": ["seccomp=unconfined"]}, "unconfine"),
        ({"host_Devices": [{"PathOnHost": "/dev/sda"}]}, "Devices"),
        ({"host_Sysctls": {"net.ipv4.ip_forward": "1"}}, "Sysctls"),
        ({"host_Init": False}, "Init must be true"),
        ({"host_PublishAllPorts": True}, "PublishAllPorts"),
        ({"User": "0:0"}, "User must be 1000:1000"),
        ({"Image": "alpine:latest"}, "not in the allowlist"),
        ({"Image": ""}, "names no image"),
    ],
)
def test_each_forbidden_option_is_refused(overrides: dict[str, Any], expected: str) -> None:
    found = violations(body(**overrides), POLICY)
    assert any(expected in problem for problem in found), found
    with pytest.raises(CreateRequestRefusedError):
        check(body(**overrides), POLICY)


@pytest.mark.parametrize(
    "source",
    ["/", "/etc", "/var/run/docker.sock", "/home/operator/.claude", "relative/path"],
)
def test_a_bind_outside_the_roots_is_refused(source: str) -> None:
    document = body()
    document["HostConfig"]["Binds"] = [f"{source}:/crucible/x:ro"]
    found = violations(document, POLICY)
    assert any("bind source" in problem for problem in found), found


def test_a_mount_bind_outside_the_roots_is_refused() -> None:
    document = body()
    document["HostConfig"]["Mounts"] = [
        {"Type": "bind", "Source": "/", "Target": "/host", "ReadOnly": True}
    ]
    assert any("outside the artifact" in p for p in violations(document, POLICY))


def test_the_credential_root_is_an_allowed_bind_source() -> None:
    document = body()
    document["HostConfig"]["Mounts"].append(
        {
            "Type": "bind",
            "Source": "/var/lib/crucible/credentials/codex",
            "Target": "/home/worker/.codex",
            "ReadOnly": True,
        }
    )
    assert violations(document, POLICY) == []


def test_a_volume_crucible_does_not_own_is_refused() -> None:
    document = body()
    document["HostConfig"]["Mounts"] = [
        {"Type": "volume", "Source": "someone-elses", "Target": "/crucible/repo"}
    ]
    assert any("is not one Crucible owns" in p for p in violations(document, POLICY))


def test_a_path_prefix_is_not_a_directory_prefix() -> None:
    """`/var/lib/crucible/artifacts-other` is not inside `/var/lib/crucible/artifacts`."""
    document = body()
    document["HostConfig"]["Binds"] = ["/var/lib/crucible/artifacts-other:/crucible/x"]
    assert any("outside the artifact" in p for p in violations(document, POLICY))


def test_a_traversal_out_of_the_root_is_refused() -> None:
    document = body()
    document["HostConfig"]["Binds"] = ["/var/lib/crucible/artifacts/../../..:/crucible/x"]
    assert any("outside the artifact" in p for p in violations(document, POLICY))


def test_image_allowlist_matching() -> None:
    allowlist = ["crucible-worker:*"]
    assert image_allowed("crucible-worker:codex-0.153.4-abc", allowlist)
    # A tag Crucible resolved to a digest still names the tag it came from.
    assert image_allowed("crucible-worker:codex-0.153.4-abc@sha256:" + "a" * 64, allowlist)
    assert not image_allowed("evil/crucible-worker:latest", allowlist)
    assert not image_allowed("crucible-workers:x", ["crucible-worker:*"])
