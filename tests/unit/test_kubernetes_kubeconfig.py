from __future__ import annotations

import base64
import os
import ssl
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from crucible.adapters.execution import k8sapi
from crucible.adapters.execution.k8sapi import (
    ClusterAccess,
    KubernetesApiError,
    KubernetesClient,
    kubeconfig_access,
)


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary directory nothing else on the machine writes to, so the tests can
    see that the client wrote nothing there."""
    directory = tmp_path / "tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def test_kind_inline_kubeconfig_material_is_held_in_memory(tmp_path: Path) -> None:
    material = {
        "ca": b"kind ca certificate\n",
        "cert": b"kind client certificate\n",
        "key": b"kind client key\n",
    }
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "kind-crucible",
        "clusters": [
            {
                "name": "kind-crucible",
                "cluster": {
                    "server": "https://127.0.0.1:49123",
                    "certificate-authority-data": base64.b64encode(material["ca"]).decode(),
                },
            }
        ],
        "contexts": [
            {
                "name": "kind-crucible",
                "context": {"cluster": "kind-crucible", "user": "kind-crucible"},
            }
        ],
        "users": [
            {
                "name": "kind-crucible",
                "user": {
                    "client-certificate-data": base64.b64encode(material["cert"]).decode(),
                    "client-key-data": base64.b64encode(material["key"]).decode(),
                },
            }
        ],
    }
    path = tmp_path / "kind-kubeconfig"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    before = _temp_entries()
    access = kubeconfig_access(str(path))

    assert access.server == "https://127.0.0.1:49123"
    assert access.ca_cert_path is None
    assert access.client_cert_path is None
    assert access.client_key_path is None
    assert access.ca_cert_data == material["ca"]
    assert access.client_cert_data == material["cert"]
    assert access.client_key_data == material["key"]
    # crucible#65: nothing was written to the temporary directory, and the key never
    # shows in a repr that a log line or traceback could carry.
    assert _temp_entries() == before
    assert "kind client key" not in repr(access)


def _temp_entries() -> set[str]:
    return set(os.listdir(tempfile.gettempdir()))


def _self_signed(tmp_path: Path) -> tuple[bytes, bytes]:
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=crucible-test", "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    data = cert.read_bytes(), key.read_bytes()
    key.unlink()
    cert.unlink()
    return data


def test_inline_client_key_loads_into_tls_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, key = _self_signed(tmp_path)
    loaded: list[tuple[str, str, bytes]] = []
    real = ssl.SSLContext.load_cert_chain

    def spy(self: ssl.SSLContext, certfile: str, keyfile: str | None = None) -> None:
        # The key is readable at the moment the context loads it, and from where.
        assert keyfile is not None
        loaded.append((certfile, keyfile, Path(keyfile).read_bytes()))
        real(self, certfile, keyfile)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    access = ClusterAccess(
        server="https://127.0.0.1:6443",
        ca_cert_data=cert,
        client_cert_data=cert,
        client_key_data=key,
    )
    before = _temp_entries()

    context = KubernetesClient(access, "crucible-workers")._context()

    assert context is not None
    assert len(loaded) == 1
    certfile, keyfile, seen = loaded[0]
    assert seen == key
    # Once the context is built the path that held the key reads nothing.
    for gone in (certfile, keyfile):
        assert not os.path.exists(gone) or Path(gone).read_bytes() != key
    assert _temp_entries() == before


def test_transient_file_without_memfd_is_private_and_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "memfd_create", raising=False)
    with k8sapi._transient_file(b"secret key bytes") as name:
        assert Path(name).read_bytes() == b"secret key bytes"
        assert os.stat(name).st_mode & 0o077 == 0
        directory = os.path.dirname(name)
    assert not os.path.exists(name)
    assert not os.path.exists(directory)


def test_kubeconfig_file_path_material_is_used_without_copying(tmp_path: Path) -> None:
    material = {
        "ca": tmp_path / "ca.pem",
        "cert": tmp_path / "client.pem",
        "key": tmp_path / "client-key.pem",
    }
    for name, material_path in material.items():
        material_path.write_text(f"{name} material\n", encoding="utf-8")
    document = {
        "current-context": "file-backed",
        "clusters": [
            {
                "name": "cluster",
                "cluster": {
                    "server": "https://127.0.0.1:6443",
                    "certificate-authority": str(material["ca"]),
                },
            }
        ],
        "contexts": [{"name": "file-backed", "context": {"cluster": "cluster", "user": "user"}}],
        "users": [
            {
                "name": "user",
                "user": {
                    "client-certificate": str(material["cert"]),
                    "client-key": str(material["key"]),
                },
            }
        ],
    }
    path = tmp_path / "file-backed-kubeconfig"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    access = kubeconfig_access(str(path))

    assert access.server == "https://127.0.0.1:6443"
    assert access.ca_cert_path == str(material["ca"])
    assert access.client_cert_path == str(material["cert"])
    assert access.client_key_path == str(material["key"])


def test_kubeconfig_refuses_a_missing_context(tmp_path: Path) -> None:
    path = tmp_path / "missing-context"
    path.write_text(yaml.safe_dump({"contexts": []}), encoding="utf-8")

    with pytest.raises(KubernetesApiError, match="has no context named ''"):
        kubeconfig_access(str(path))


def test_kubeconfig_refuses_an_unmatched_current_context(tmp_path: Path) -> None:
    path = tmp_path / "unmatched-context"
    path.write_text(
        yaml.safe_dump({"current-context": "does-not-exist", "contexts": []}), encoding="utf-8"
    )

    with pytest.raises(KubernetesApiError, match="has no context named 'does-not-exist'"):
        kubeconfig_access(str(path))


def test_kubeconfig_refuses_a_credential_plugin(tmp_path: Path) -> None:
    document = {
        "current-context": "plugin",
        "clusters": [{"name": "cluster", "cluster": {"server": "https://127.0.0.1:6443"}}],
        "contexts": [{"name": "plugin", "context": {"cluster": "cluster", "user": "user"}}],
        "users": [{"name": "user", "user": {"exec": {"command": "unsafe-command"}}}],
    }
    path = tmp_path / "credential-plugin"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(KubernetesApiError, match="needs a credential plugin"):
        kubeconfig_access(str(path))
