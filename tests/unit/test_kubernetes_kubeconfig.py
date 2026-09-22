from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest
import yaml

from crucible.adapters.execution.k8sapi import KubernetesApiError, kubeconfig_access


def test_kind_inline_kubeconfig_material_is_decoded_to_private_files(tmp_path: Path) -> None:
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

    access = kubeconfig_access(str(path))

    assert access.server == "https://127.0.0.1:49123"
    for attribute, expected in (
        ("ca_cert_path", material["ca"]),
        ("client_cert_path", material["cert"]),
        ("client_key_path", material["key"]),
    ):
        decoded = Path(str(getattr(access, attribute)))
        assert decoded.read_bytes() == expected
        assert stat.S_IMODE(decoded.stat().st_mode) == 0o600


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
