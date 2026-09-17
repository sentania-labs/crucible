"""A small Docker Engine API client over the socket proxy (13, ADR 0004).

Crucible never sees the raw socket: the endpoint is the proxy's HTTP address. The
client speaks one pinned API version, blocks (the provider calls it from a thread),
and knows only the calls the proxy permits: containers, images, networks, volumes.

There is deliberately no `exec`, no `build`, and no `system` call here. S9 found that
the reference proxy image lets `POST /containers/{id}/exec` create an exec instance
even with `EXEC=0`, so the client not having the call is what keeps Crucible honest.
"""

from __future__ import annotations

import contextlib
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

        `since` is a `<seconds>.<nanoseconds>` bound and Docker treats it as inclusive
        (S8); the caller drops the overlap by line hash, never by trusting the bound.

        Every container Crucible creates has `Tty: false`, so the body is always the
        8-byte-header multiplexed stream. The content type does not distinguish it:
        daemons before API 1.42 answer `application/vnd.docker.raw-stream` for both the
        multiplexed and the TTY case, which is why the header is not what decides.
        """
        params: dict[str, Any] = {"stdout": "1", "stderr": "1", "timestamps": "1"}
        if since:
            params["since"] = since
        with self._request(
            "GET", f"/containers/{container_id}/logs", params=params, timeout=timeout
        ) as response:
            raw = response.read()
        return demultiplex(raw)

    def write_stdin(self, container_id: str, payload: bytes) -> None:
        """Hand a value to a running container on its stdin and close the write side.

        This is `docker run -i` through the API: the attach endpoint hijacks the
        connection and whatever is written reaches the container's stdin. It is how the
        publisher receives its installation token, because `docker cp` cannot reach a
        tmpfs inside a `--read-only` container and even without that flag it lands the
        value on the writable layer, which is disk (S10).

        The value is written to the socket and the socket is closed. It is never in
        `Env`, in `Cmd`, in a bind source, or in this process's argv.
        """
        url = f"/{API_VERSION}/containers/{container_id}/attach?stream=1&stdin=1&stdout=0&stderr=0"
        conn = self._connect()
        try:
            conn.putrequest("POST", url, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", "docker")
            conn.putheader("Content-Type", "application/vnd.docker.raw-stream")
            conn.putheader("Connection", "Upgrade")
            conn.putheader("Upgrade", "tcp")
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            response = conn.getresponse()
            if response.status not in (101, 200):
                raw = response.read().decode("utf-8", "replace")
                raise DockerApiError(response.status, _message(raw), path=url)
            sock = conn.sock
            if sock is None:
                raise DockerApiError(0, "the attach connection carried no socket", path=url)
            sock.sendall(payload)
            # Some proxies half-close on their own; the container still sees EOF when
            # the connection closes below, so a refusal here is not an error.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_WR)
        finally:
            conn.close()

    def put_archive(self, container_id: str, path: str, tar: bytes) -> None:
        """Extract a tar into a container path (`docker cp -` by another name).

        This is how the per-attempt credential copy is seeded (12): the container is
        created, not started, its credential mount is in place, and the daemon extracts
        the named auth files into it with the ownership the tar headers carry. The value
        travels in the request body and nowhere else: not in `Env`, not in `Cmd`, not on
        a command line, not through this process's argv."""
        url = f"/{API_VERSION}/containers/{container_id}/archive?{urlencode({'path': path})}"
        conn = self._connect()
        try:
            conn.request(
                "PUT",
                url,
                body=tar,
                headers={"Content-Type": "application/x-tar", "Host": "docker"},
            )
            response = conn.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise DockerApiError(
                    response.status, _message(raw.decode("utf-8", "replace")), path=url
                )
        finally:
            conn.close()

    def get_archive(self, container_id: str, path: str) -> bytes | None:
        """A tar of one path inside a container, or None when it is not there.

        Works on a stopped container: the daemon mounts its volumes and binds for the
        copy. This is how the named auth files come back for the sync-back (12)."""
        try:
            with self._request(
                "GET", f"/containers/{container_id}/archive", params={"path": path}
            ) as response:
                return response.read()
        except DockerApiError as exc:
            if exc.status == 404:
                return None
            raise

    def list_images(
        self, filters: Mapping[str, Sequence[str]] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if filters:
            params["filters"] = json.dumps({k: list(v) for k, v in filters.items()})
        data = self._json("GET", "/images/json", params=params)
        assert isinstance(data, list)
        return [row for row in data if isinstance(row, dict)]

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


def demultiplex(raw: bytes) -> list[LogFrame]:
    """Split Docker's 8-byte-header multiplexed stream into frames.

    A body that is not framed at all (a TTY container, which Crucible never creates,
    or a daemon that answered differently) is returned whole on stdout rather than
    dropped: losing a worker's output silently is worse than attributing it loosely.
    """
    frames: list[LogFrame] = []
    index = 0
    while index + 8 <= len(raw):
        header = raw[index]
        if header not in _STREAMS or raw[index + 1 : index + 4] != b"\x00\x00\x00":
            # Not a frame header. The body is raw, so nothing here is trustworthy.
            return [LogFrame("stdout", raw)] if raw else []
        size = int.from_bytes(raw[index + 4 : index + 8], "big")
        start = index + 8
        end = start + size
        if end > len(raw):
            # A truncated final frame: keep what arrived rather than drop the batch.
            if start < len(raw):
                frames.append(_frame(header, raw[start:]))
            break
        frames.append(_frame(header, raw[start:end]))
        index = end
    if not frames and raw:
        return [LogFrame("stdout", raw)]
    return frames


def _frame(header: int, payload: bytes) -> LogFrame:
    return LogFrame("stderr" if _STREAMS.get(header) == "stderr" else "stdout", payload)
