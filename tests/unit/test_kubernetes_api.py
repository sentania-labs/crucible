from __future__ import annotations

from typing import Any

from crucible.adapters.execution.k8sapi import _exec_over_websocket


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
