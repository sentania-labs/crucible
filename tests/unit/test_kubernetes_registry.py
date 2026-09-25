"""The crane registry adapter (26, 108) against a stub `crane`.

The stub records what it was run with (argv, the DOCKER_CONFIG it was given, that
directory's mode and the config file's mode and content) and answers the way the test
asks it to. The real binary is exercised by tests/e2e/test_registry.py and the kind tier.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution.k8sregistry import (
    CraneRegistryClient,
    RegistryAuth,
    RegistryError,
    auths_from_dockerconfigjson,
)

DIGEST = "sha256:" + "a" * 64
CONFIG = {
    "config": {"Labels": {"crucible.harnesses": "codex", "crucible.harness.codex.version": "0.1"}}
}

STUB = """#!{python}
import json, os, stat, sys, time
directory = os.environ["DOCKER_CONFIG"]
path = os.path.join(directory, "config.json")
record = {{
    "argv": sys.argv[1:],
    "dir_mode": stat.S_IMODE(os.stat(directory).st_mode),
    "file_mode": stat.S_IMODE(os.stat(path).st_mode),
    "config": json.load(open(path)),
    "docker_config": directory,
}}
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
mode = os.environ.get("STUB_MODE", "ok")
command = sys.argv[1]
if mode == "sleep":
    time.sleep(30)
if mode == "fail" or (mode == "fail-config" and command == "config"):
    sys.stderr.write(os.environ["STUB_STDERR"])
    sys.exit(1)
if command == "digest":
    print(os.environ.get("STUB_DIGEST", "{digest}"))
elif command == "config":
    sys.stdout.write(os.environ.get("STUB_CONFIG", {config!r}))
elif command == "ls":
    print("0.5.3")
    print("latest")
    print("")
"""


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "crane"
    binary.write_text(STUB.format(python=sys.executable, digest=DIGEST, config=json.dumps(CONFIG)))
    binary.chmod(0o755)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("STUB_LOG", str(log))
    return binary


def _calls(stub: Path) -> list[dict[str, Any]]:
    log = stub.parent / "log.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _client(stub: Path, **kwargs: Any) -> CraneRegistryClient:
    return CraneRegistryClient(binary=str(stub), **kwargs)


# ----- argument shape -------------------------------------------------------


def test_resolve_reads_the_digest_then_the_amd64_config_by_that_digest(stub: Path) -> None:
    info = _client(stub).resolve("ghcr.io/sentania-labs/crucible-worker:0.5.3")

    assert [call["argv"] for call in _calls(stub)] == [
        ["digest", "ghcr.io/sentania-labs/crucible-worker:0.5.3"],
        [
            "config",
            "--platform",
            "linux/amd64",
            f"ghcr.io/sentania-labs/crucible-worker@{DIGEST}",
        ],
    ]
    assert info.digest == DIGEST
    assert info.reference == f"ghcr.io/sentania-labs/crucible-worker@{DIGEST}"
    assert info.harnesses == {"codex": "0.1"}


def test_a_docker_hub_short_name_is_named_in_full_and_pinned_without_the_host(
    stub: Path,
) -> None:
    info = _client(stub).resolve("busybox")

    assert _calls(stub)[0]["argv"] == ["digest", "docker.io/library/busybox:latest"]
    assert info.reference == f"library/busybox@{DIGEST}"


def test_a_reference_with_a_tag_and_a_digest_resolves_by_the_digest(stub: Path) -> None:
    _client(stub).resolve(f"ghcr.io/o/r:1.0@{DIGEST}")

    assert _calls(stub)[0]["argv"] == ["digest", f"ghcr.io/o/r@{DIGEST}"]


def test_list_tags_runs_ls_on_the_repository_and_drops_blank_lines(stub: Path) -> None:
    tags = _client(stub).list_tags("ghcr.io/sentania-labs/crucible-worker")

    assert tags == ["0.5.3", "latest"]
    assert _calls(stub)[0]["argv"] == ["ls", "ghcr.io/sentania-labs/crucible-worker"]


# ----- the credential directory -------------------------------------------


def test_the_credential_reaches_crane_only_through_a_private_docker_config(
    stub: Path,
) -> None:
    client = _client(
        stub,
        auths={
            "ghcr.io": RegistryAuth("robot", "hunter2-secret"),
            "quay.io": RegistryAuth("other", "not-for-ghcr"),
        },
    )
    client.resolve("ghcr.io/o/r:1")

    for call in _calls(stub):
        assert call["dir_mode"] == 0o700
        assert call["file_mode"] == 0o600
        # Only the registry being read; never the argv.
        assert call["config"] == {
            "auths": {"ghcr.io": {"auth": base64.b64encode(b"robot:hunter2-secret").decode()}}
        }
        assert not any("hunter2" in arg or "robot" in arg for arg in call["argv"])
        assert not os.path.exists(call["docker_config"])


def test_docker_hub_credentials_use_the_key_crane_looks_up(stub: Path) -> None:
    _client(stub, auths={"docker.io": RegistryAuth("u", "p")}).list_tags("library/busybox")

    assert list(_calls(stub)[0]["config"]["auths"]) == ["https://index.docker.io/v1/"]


def test_no_credential_still_gets_an_empty_private_config(stub: Path) -> None:
    _client(stub).list_tags("ghcr.io/o/r")

    call = _calls(stub)[0]
    assert call["config"] == {"auths": {}}
    assert call["docker_config"] != os.path.expanduser("~/.docker")


def test_the_credential_directory_is_removed_when_crane_fails(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv("STUB_STDERR", "Error: GET https://ghcr.io/v2/o/r/manifests/x: DENIED\n")
    with pytest.raises(RegistryError):
        _client(stub, auths={"ghcr.io": RegistryAuth("u", "p")}).resolve("ghcr.io/o/r:x")

    assert not os.path.exists(_calls(stub)[0]["docker_config"])


def test_the_credential_directory_is_removed_when_crane_times_out(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep")
    with pytest.raises(RegistryError):
        _client(stub, timeout=0.5).resolve("ghcr.io/o/r:x")

    assert not os.path.exists(_calls(stub)[0]["docker_config"])


def test_the_credential_directory_is_removed_when_crane_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    client = CraneRegistryClient(binary=str(tmp_path / "no-such-crane"))
    with pytest.raises(RegistryError, match="is not installed"):
        client.resolve("ghcr.io/o/r:1")

    assert list(tmp_path.iterdir()) == []


# ----- error mapping ------------------------------------------------------


def test_crane_error_line_is_the_reason_with_the_registry_named(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv(
        "STUB_STDERR",
        "2026/09/24 22:12:58 HEAD request failed, falling back on GET\n"
        "Error: GET https://ghcr.io/v2/o/r/manifests/nope: MANIFEST_UNKNOWN: manifest unknown\n",
    )
    with pytest.raises(RegistryError) as caught:
        _client(stub).resolve("ghcr.io/o/r:nope")

    assert str(caught.value) == (
        "ghcr.io: GET https://ghcr.io/v2/o/r/manifests/nope: MANIFEST_UNKNOWN: manifest unknown"
    )


def test_a_credential_value_in_crane_output_is_redacted(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = RegistryAuth("robot", "hunter2-secret")
    monkeypatch.setenv("STUB_MODE", "fail")
    monkeypatch.setenv("STUB_STDERR", f"Error: hunter2-secret and {auth.encoded()}\n")
    with pytest.raises(RegistryError) as caught:
        _client(stub, auths={"ghcr.io": auth}).resolve("ghcr.io/o/r:1")

    assert "hunter2" not in str(caught.value)
    assert auth.encoded() not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_a_failed_config_read_is_a_registry_error(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "fail-config")
    monkeypatch.setenv(
        "STUB_STDERR", "Error: no child with platform linux/amd64 in index ghcr.io/o/r\n"
    )
    with pytest.raises(RegistryError, match="no child with platform linux/amd64"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_output_that_is_not_a_digest_is_refused(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_DIGEST", "<html>moved</html>")
    with pytest.raises(RegistryError, match="resolved to no digest"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_a_config_that_is_not_json_is_refused(stub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_CONFIG", "not json")
    with pytest.raises(RegistryError, match="not JSON"):
        _client(stub).resolve("ghcr.io/o/r:1")


def test_a_crane_call_is_bounded_by_the_timeout(
    stub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_MODE", "sleep")
    started = time.monotonic()
    with pytest.raises(RegistryError, match=r"ghcr.io did not answer within 0.5s"):
        _client(stub, timeout=0.5).list_tags("ghcr.io/o/r")

    assert time.monotonic() - started < 10


# ----- the pull Secret ------------------------------------------------------


def test_pull_secret_entries_are_read_from_either_form() -> None:
    raw = json.dumps(
        {
            "auths": {
                "https://index.docker.io/v1/": {"auth": base64.b64encode(b"hub:pw").decode()},
                "ghcr.io": {"username": "robot", "password": "tok"},
            }
        }
    ).encode()

    assert auths_from_dockerconfigjson(raw) == {
        "docker.io": RegistryAuth("hub", "pw"),
        "ghcr.io": RegistryAuth("robot", "tok"),
    }
