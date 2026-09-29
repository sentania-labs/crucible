"""Transport failures reach the provider as one error type it handles (lab findings of
2026-09-29): a refused, reset or timed-out connection, and an API server that answers
503, are a `KubernetesUnavailableError`, which is a `ProviderError`, never an OSError
that escapes every handler."""

from __future__ import annotations

import io
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from crucible.adapters.execution.k8sapi import (
    ClusterAccess,
    KubernetesApiError,
    KubernetesClient,
    KubernetesUnavailableError,
    _read_exec_channels,
)
from crucible.ports.execution import ProviderError, ProviderUnavailableError


class _Handler(BaseHTTPRequestHandler):
    status = 200
    hang = False

    def do_GET(self) -> None:
        if type(self).hang:
            threading.Event().wait(2)
            return
        body = json.dumps({"kind": "Status", "message": f"answered {type(self).status}"})
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def server() -> Iterator[tuple[HTTPServer, type[_Handler]]]:
    handler = type("Handler", (_Handler,), {"status": 200, "hang": False})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, handler
    httpd.shutdown()
    httpd.server_close()


def _client(port: int, timeout: float = 5.0) -> KubernetesClient:
    return KubernetesClient(
        ClusterAccess(server=f"http://127.0.0.1:{port}"), "crucible-workers", timeout=timeout
    )


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_refused_connection_is_an_unavailable_provider_error() -> None:
    client = _client(_closed_port())
    with pytest.raises(KubernetesUnavailableError) as raised:
        client.get("pods", "worker-1")
    assert raised.value.status == 0
    assert "ConnectionRefusedError" in str(raised.value)
    assert isinstance(raised.value, ProviderError)
    assert isinstance(raised.value, ProviderUnavailableError)


def test_a_timed_out_request_is_an_unavailable_provider_error(
    server: tuple[HTTPServer, type[_Handler]],
) -> None:
    httpd, handler = server
    handler.hang = True
    client = _client(httpd.server_address[1], timeout=0.3)
    with pytest.raises(KubernetesUnavailableError, match="timed out"):
        client.list_objects("pods")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_an_overloaded_api_server_is_unavailable(
    server: tuple[HTTPServer, type[_Handler]], status: int
) -> None:
    httpd, handler = server
    handler.status = status
    with pytest.raises(KubernetesUnavailableError) as raised:
        _client(httpd.server_address[1]).get("pods", "worker-1")
    assert raised.value.status == status


@pytest.mark.parametrize("status", [403, 404, 409, 422])
def test_an_answer_is_not_unavailability(
    server: tuple[HTTPServer, type[_Handler]], status: int
) -> None:
    """A 404 is the answer "not there" and a 403 the answer "not allowed": callers
    decide on those, so they stay plain API errors (still ProviderErrors)."""
    httpd, handler = server
    handler.status = status
    with pytest.raises(KubernetesApiError) as raised:
        _client(httpd.server_address[1]).get("pods", "worker-1")
    assert raised.value.status == status
    assert not isinstance(raised.value, KubernetesUnavailableError)
    assert isinstance(raised.value, ProviderError)


class _Frames:
    """A socket that hands back server frames, one stdout channel frame each."""

    def __init__(self, payloads: list[bytes]) -> None:
        raw = bytearray()
        for payload in payloads:
            body = bytes([1]) + payload
            if len(body) < 126:
                raw += bytes([0x82, len(body)]) + body
            else:
                raw += bytes([0x82, 126]) + len(body).to_bytes(2, "big") + body
        status = json.dumps({"status": "Success"}).encode()
        raw += bytes([0x82, len(status) + 1, 3]) + status
        self._raw = bytes(raw)

    def recv(self, count: int) -> bytes:
        chunk, self._raw = self._raw[:count], self._raw[count:]
        return chunk


def test_an_exec_can_write_stdout_to_a_file_instead_of_memory() -> None:
    """The collected archive is streamed to disk: nothing of it is held in the result."""
    sink = io.BytesIO()
    result = _read_exec_channels(_Frames([b"a" * 200, b"b" * 200]), limit=1024, stdout=sink)
    assert result.stdout == b""
    assert result.exit_code == 0
    assert result.stdout_size == 400
    assert sink.getvalue() == b"a" * 200 + b"b" * 200


def test_a_streamed_exec_past_its_limit_stops_and_says_how_much_came() -> None:
    """Past the limit the read stops: the file holds the limit and no more, and
    `stdout_size` is at least the limit, which is how the collector knows it was cut."""
    sink = io.BytesIO()
    result = _read_exec_channels(_Frames([b"a" * 200, b"b" * 200]), limit=300, stdout=sink)
    assert sink.getvalue() == b"a" * 200 + b"b" * 100
    assert result.stdout_size >= 300


def test_an_exec_without_a_file_still_returns_stdout() -> None:
    result = _read_exec_channels(_Frames([b"hello"]), limit=1024)
    assert result.stdout == b"hello"
    assert result.stdout_size == 5
