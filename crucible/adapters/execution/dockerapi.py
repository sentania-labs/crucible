"""A small Docker Engine API client over the socket proxy (13, ADR 0004).

Crucible never sees the raw socket: the endpoint is the proxy's HTTP address. The
client speaks one pinned API version, blocks (the provider calls it from a thread),
and knows only the calls the proxy permits: containers, images, networks, volumes.

There is deliberately no `exec`, no `build`, and no `system` call here. S9 found that
the reference proxy image lets `POST /containers/{id}/exec` create an exec instance
even with `EXEC=0`, so the client not having the call is what keeps Crucible honest.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse
from typing import Any
from urllib.parse import quote, urlencode

API_VERSION = "v1.47"
"""Pinned Engine API version (08). Docker 28 speaks v1.51; pinning keeps the request
shape stable when the daemon moves."""

DEFAULT_TIMEOUT = 30.0


class DockerApiError(Exception):
    """A Docker API call failed. Carries the status so callers can tell 404 from 409."""

    def __init__(self, status: int, message: str, *, path: str = "") -> None:
        super().__init__(f"{status} on {path}: {message}" if path else f"{status}: {message}")
        self.status = status
        self.message = message
        self.path = path


class _UnixConnection(HTTPConnection):
    """HTTPConnection over an AF_UNIX socket. The host header is a placeholder."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


@dataclass(frozen=True, slots=True)
class LogFrame:
    """One demultiplexed frame of a container log stream."""

    stream: str
    payload: bytes


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    """Split a DOCKER_HOST-shaped endpoint into (scheme, target)."""
    if endpoint.startswith("unix://"):
        return "unix", endpoint[len("unix://") :]
    if endpoint.startswith(("tcp://", "http://")):
        return "tcp", endpoint.split("://", 1)[1]
    if endpoint.startswith("/"):
        return "unix", endpoint
    raise ValueError(f"unsupported docker endpoint {endpoint!r}")


class DockerClient:
    """Blocking client. One connection per request: the proxy is HTTP/1.1 and the call
    volume is a handful per tick."""

    def __init__(self, endpoint: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.endpoint = endpoint
        self._scheme, self._target = parse_endpoint(endpoint)
        self.timeout = timeout

    # ----- transport ---------------------------------------------------

    def _connect(self, timeout: float | None = None) -> HTTPConnection:
        wait = self.timeout if timeout is None else timeout
        if self._scheme == "unix":
            return _UnixConnection(self._target, wait)
        host, _, port = self._target.partition(":")
        return HTTPConnection(host, int(port or 2375), timeout=wait)

    @contextmanager
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Iterator[HTTPResponse]:
        url = f"/{API_VERSION}{path}"
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "Host": "docker"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connect(timeout)
        try:
            conn.request(method, url, body=payload, headers=headers)
            response = conn.getresponse()
            if response.status >= 400:
                raw = response.read().decode("utf-8", "replace")
                raise DockerApiError(response.status, _message(raw), path=url)
            yield response
        finally:
            conn.close()

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        with self._request(method, path, params=params, body=body, timeout=timeout) as response:
            raw = response.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    # ----- calls -------------------------------------------------------

    def ping(self) -> str:
        """The daemon's own version, through the proxy. Used by the launch preflight."""
        data = self._json("GET", "/version")
        return str(data.get("ApiVersion", "")) if isinstance(data, dict) else ""

    def inspect_image(self, reference: str) -> dict[str, Any]:
        data = self._json("GET", f"/images/{quote(reference, safe='')}/json")
        assert isinstance(data, dict)
        return data

    def create_container(self, name: str, body: Mapping[str, Any]) -> str:
        data = self._json("POST", "/containers/create", params={"name": name}, body=body)
        assert isinstance(data, dict)
        return str(data["Id"])

    def start_container(self, container_id: str) -> None:
        self._json("POST", f"/containers/{container_id}/start")

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        data = self._json("GET", f"/containers/{container_id}/json")
        assert isinstance(data, dict)
        return data

    def stop_container(self, container_id: str, *, timeout_seconds: int) -> None:
        try:
            self._json(
                "POST",
                f"/containers/{container_id}/stop",
                params={"t": timeout_seconds},
                timeout=self.timeout + timeout_seconds,
            )
        except DockerApiError as exc:
            if exc.status not in (304, 404, 409):
                raise

    def kill_container(self, container_id: str, *, signal: str = "SIGKILL") -> None:
        try:
            self._json("POST", f"/containers/{container_id}/kill", params={"signal": signal})
        except DockerApiError as exc:
            # 409 is "not running", which is the state kill was asked to produce.
            if exc.status not in (404, 409):
                raise

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        try:
            self._json("DELETE", f"/containers/{container_id}", params={"force": force, "v": True})
        except DockerApiError as exc:
            if exc.status != 404:
                raise

    def wait_container(self, container_id: str, *, timeout: float) -> int:
        data = self._json("POST", f"/containers/{container_id}/wait", timeout=timeout)
        assert isinstance(data, dict)
        return int(data.get("StatusCode", -1))

    def list_containers(
        self, *, all_states: bool = True, filters: Mapping[str, Sequence[str]] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"all": "1" if all_states else "0"}
        if filters:
            params["filters"] = json.dumps({k: list(v) for k, v in filters.items()})
        data = self._json("GET", "/containers/json", params=params)
        assert isinstance(data, list)
        return [row for row in data if isinstance(row, dict)]

    def container_logs(
        self,
        container_id: str,
        *,
        since: str | None = None,
        timeout: float | None = None,
    ) -> list[LogFrame]:
        """stdout and stderr with timestamps, demultiplexed.

        `since` is an RFC 3339 timestamp and Docker treats it as inclusive (S8); the
        caller drops the overlap by line hash, never by trusting the bound.
        """
        params: dict[str, Any] = {"stdout": "1", "stderr": "1", "timestamps": "1"}
        if since:
            params["since"] = since
        with self._request(
            "GET", f"/containers/{container_id}/logs", params=params, timeout=timeout
        ) as response:
            raw = response.read()
            tty = response.getheader("Content-Type") == "application/vnd.docker.raw-stream"
        return _demux(raw) if not tty else [LogFrame("stdout", raw)]

    def create_network(self, name: str, *, internal: bool) -> str:
        body = {"Name": name, "Driver": "bridge", "Internal": internal, "CheckDuplicate": True}
        try:
            data = self._json("POST", "/networks/create", body=body)
        except DockerApiError as exc:
            if exc.status != 409:
                raise
            return self.inspect_network(name)["Id"]  # type: ignore[no-any-return]
        assert isinstance(data, dict)
        return str(data["Id"])

    def inspect_network(self, name: str) -> dict[str, Any]:
        data = self._json("GET", f"/networks/{quote(name, safe='')}")
        assert isinstance(data, dict)
        return data

    def create_volume(self, name: str, labels: Mapping[str, str]) -> None:
        self._json("POST", "/volumes/create", body={"Name": name, "Labels": dict(labels)})

    def remove_volume(self, name: str) -> None:
        try:
            self._json("DELETE", f"/volumes/{quote(name, safe='')}", params={"force": True})
        except DockerApiError as exc:
            if exc.status != 404:
                raise


def _message(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(parsed, dict) and "message" in parsed:
        return str(parsed["message"])
    return raw.strip()


_STREAMS = {0: "stdin", 1: "stdout", 2: "stderr"}


def _demux(raw: bytes) -> list[LogFrame]:
    """Split Docker's 8-byte-header multiplexed stream into frames."""
    frames: list[LogFrame] = []
    index = 0
    while index + 8 <= len(raw):
        stream = _STREAMS.get(raw[index], "stdout")
        size = int.from_bytes(raw[index + 4 : index + 8], "big")
        start = index + 8
        end = start + size
        if end > len(raw):
            break
        frames.append(LogFrame("stderr" if stream == "stderr" else "stdout", raw[start:end]))
        index = end
    return frames
