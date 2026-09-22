from __future__ import annotations

import base64
import stat
from pathlib import Path
from typing import Any

import yaml

from crucible.adapters.execution.k8sapi import _exec_over_websocket, kubeconfig_access


class _Response:
    status = 101


class _Socket:
    def recv(self, _count: int) -> bytes:
        return b"\x88\x00"


class _Connection:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.sock = _Socket()

    def putrequest(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def putheader(self, key: str, value: str) -> None:
        self.headers[key] = value

    def endheaders(self) -> None:
        pass

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        pass


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


def test_exec_websocket_does_not_ask_for_a_json_representation() -> None:
    connection = _Connection()

    result = _exec_over_websocket(
        connection,  # type: ignore[arg-type]
        {"Accept": "application/json"},
        "https://127.0.0.1:6443",
        "/api/v1/namespaces/workers/pods/reader/exec",
        limit=1024,
    )

    assert connection.headers["Accept"] == "*/*"
    assert connection.headers["Sec-WebSocket-Protocol"] == "v4.channel.k8s.io"
    assert result.exit_code is None
