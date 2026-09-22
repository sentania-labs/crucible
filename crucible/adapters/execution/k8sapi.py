"""A small Kubernetes API client for the one namespace the provider owns (26).

The Docker provider talks to a socket proxy that narrows the API surface for it. There
is no proxy on the cluster: the narrowing is the supervisor's ServiceAccount, bound to a
Role in the workers namespace and nothing else (26). This client is the second half of
that discipline. It knows the eight resource kinds 26 names, `pods/log`, and
`pods/exec`, and it has no call for anything cluster-scoped, no `create` on a
Deployment, and no way to name a namespace other than the one it was built with.

Blocking, like the Docker client: the provider calls it from a thread. One connection
per request.

`exec` is here because bytes have to leave a Pod without passing through its log. The
kubelet writes pod logs to the node's disk, so a rotated credential read back through a
log would be a credential on a node (12). The exec stream is the same channel
`kubectl cp` uses and it is what the reader Pod hands the collected outputs and the
rotated auth files back on.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import yaml

DEFAULT_TIMEOUT = 30.0
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# The API groups each kind lives in. Nothing cluster-scoped is reachable from here.
_KINDS: dict[str, tuple[str, str]] = {
    "pods": ("", "v1"),
    "secrets": ("", "v1"),
    "configmaps": ("", "v1"),
    "persistentvolumeclaims": ("", "v1"),
    "resourcequotas": ("", "v1"),
    "events": ("", "v1"),
    "jobs": ("batch", "v1"),
    "networkpolicies": ("networking.k8s.io", "v1"),
}


class KubernetesApiError(Exception):
    """An API call failed. Carries the status so a caller can tell 404 from 409."""

    def __init__(self, status: int, message: str, *, path: str = "") -> None:
        super().__init__(f"{status} on {path}: {message}" if path else f"{status}: {message}")
        self.status = status
        self.message = message
        self.path = path


@dataclass(frozen=True, slots=True)
class ExecResult:
    """What one `pods/exec` produced: the two streams and the command's exit status.

    `exit_code` is None when the API server reported no status at all, which is a
    failed exec rather than a command that failed."""

    stdout: bytes
    stderr: bytes
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class LogFrame:
    """One pod log payload, shaped like the Docker client's frame so the shared resume
    logic (10) reads both without knowing which provider produced them."""

    stream: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class ClusterAccess:
    """Where the API server is and how this process authenticates to it.

    `token_path` rather than a token: an in-cluster ServiceAccount token is rotated by
    the kubelet, so it is read per request and never held (12's file-not-value rule
    applied to Crucible's own credential)."""

    server: str
    token_path: str | None = None
    token: str | None = None
    ca_cert_path: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    verify: bool = True

    def bearer(self) -> str | None:
        if self.token_path:
            try:
                return open(self.token_path, encoding="utf-8").read().strip()
            except OSError:
                return None
        return self.token


def in_cluster_access() -> ClusterAccess:
    """The ServiceAccount arrangement a Deployment in the `crucible` namespace gets."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise KubernetesApiError(0, "not running in a cluster: KUBERNETES_SERVICE_HOST is unset")
    return ClusterAccess(
        server=f"https://{host}:{port}",
        token_path=f"{SERVICE_ACCOUNT_DIR}/token",
        ca_cert_path=f"{SERVICE_ACCOUNT_DIR}/ca.crt",
    )


def kubeconfig_access(path: str, context: str | None = None) -> ClusterAccess:
    """The developer arrangement: a kubeconfig file, one context, no interactive auth.

    Only the static forms are read. An `exec` credential plugin would run a program this
    process chose from a file, which is not something a supervisor does unattended."""
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise KubernetesApiError(0, f"kubeconfig {path!r} is not a mapping")
    wanted = context or str(document.get("current-context") or "")
    entry = _named(document.get("contexts"), wanted, "context")
    cluster = _named(document.get("clusters"), str(entry.get("cluster", "")), "cluster")
    user = _named(document.get("users"), str(entry.get("user", "")), "user")
    server = str(cluster.get("server", ""))
    if not server:
        raise KubernetesApiError(0, f"kubeconfig context {wanted!r} names no server")
    if "exec" in user or "auth-provider" in user:
        raise KubernetesApiError(
            0,
            f"kubeconfig user for context {wanted!r} needs a credential plugin; "
            "Crucible authenticates with a token or a client certificate only",
        )
    # A kubeconfig holds its certificates either as a path or inline as base64 under the
    # `-data` twin. kind writes the inline form, so a parser that reads only paths gets
    # the system CA and no client certificate, and cannot connect at all.
    return ClusterAccess(
        server=server,
        token=str(user["token"]) if user.get("token") else None,
        ca_cert_path=_material(cluster, "certificate-authority", "crucible-ca"),
        client_cert_path=_material(user, "client-certificate", "crucible-cert"),
        client_key_path=_material(user, "client-key", "crucible-key"),
        verify=not bool(cluster.get("insecure-skip-tls-verify")),
    )


def _material(entry: Mapping[str, Any], key: str, prefix: str) -> str | None:
    """A certificate as a path, taking the inline `<key>-data` form when that is what
    the kubeconfig carries.

    Inline material is written to a private temporary file, because `ssl` loads a chain
    from a path and nothing else. The file is mode 0600 and lives for the process."""
    path = entry.get(key)
    if path:
        return str(path)
    raw = entry.get(f"{key}-data")
    if not raw:
        return None
    try:
        decoded = base64.b64decode(str(raw))
    except ValueError as exc:
        raise KubernetesApiError(0, f"kubeconfig {key}-data is not base64") from exc
    handle, name = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".pem")
    try:
        os.fchmod(handle, 0o600)
        os.write(handle, decoded)
    finally:
        os.close(handle)
    _MATERIAL_FILES.append(name)
    return name


# What `_material` wrote, so a caller can remove it and so the files are not collected
# while a connection still needs them.
_MATERIAL_FILES: list[str] = []


def _named(entries: Any, name: str, kind: str) -> dict[str, Any]:
    for entry in entries or []:
        if isinstance(entry, dict) and str(entry.get("name")) == name:
            body = entry.get(kind)
            return body if isinstance(body, dict) else {}
    raise KubernetesApiError(0, f"kubeconfig has no {kind} named {name!r}")


class KubernetesClient:
    """Namespaced calls only. The namespace is fixed at construction on purpose: 26
    gives the supervisor a Role in `crucible-workers` and nothing anywhere else, and a
    client that cannot spell another namespace cannot drift past that."""

    def __init__(
        self, access: ClusterAccess, namespace: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self.access = access
        self.namespace = namespace
        self.timeout = timeout

    # ----- transport ---------------------------------------------------

    def _context(self) -> ssl.SSLContext | None:
        parsed = urlsplit(self.access.server)
        if parsed.scheme != "https":
            return None
        context = ssl.create_default_context(cafile=self.access.ca_cert_path)
        if not self.access.verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if self.access.client_cert_path:
            context.load_cert_chain(self.access.client_cert_path, self.access.client_key_path)
        return context

    def _connect(self, timeout: float | None = None) -> HTTPConnection:
        parsed = urlsplit(self.access.server)
        wait = self.timeout if timeout is None else timeout
        host = parsed.hostname or ""
        context = self._context()
        if context is None:
            return HTTPConnection(host, parsed.port or 80, timeout=wait)
        return HTTPSConnection(host, parsed.port or 443, timeout=wait, context=context)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        bearer = self.access.bearer()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

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
        url = path
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connect(timeout)
        try:
            conn.request(method, url, body=payload, headers=headers)
            response = conn.getresponse()
            if response.status >= 400:
                raw = response.read().decode("utf-8", "replace")
                raise KubernetesApiError(response.status, _message(raw), path=url)
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
        return json.loads(raw.decode("utf-8")) if raw else None

    # ----- paths -------------------------------------------------------

    def _base(self, kind: str) -> str:
        try:
            group, version = _KINDS[kind]
        except KeyError:
            raise KubernetesApiError(0, f"the client has no call for {kind!r}") from None
        root = f"/api/{version}" if not group else f"/apis/{group}/{version}"
        return f"{root}/namespaces/{quote(self.namespace, safe='')}/{kind}"

    # ----- calls -------------------------------------------------------

    def version(self) -> str:
        data = self._json("GET", "/version")
        return str(data.get("gitVersion", "")) if isinstance(data, dict) else ""

    def create(self, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
        data = self._json("POST", self._base(kind), body=body)
        assert isinstance(data, dict)
        return data

    def get(self, kind: str, name: str) -> dict[str, Any]:
        data = self._json("GET", f"{self._base(kind)}/{quote(name, safe='')}")
        assert isinstance(data, dict)
        return data

    def list_objects(
        self, kind: str, *, label_selector: str | None = None, field_selector: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if label_selector:
            params["labelSelector"] = label_selector
        if field_selector:
            params["fieldSelector"] = field_selector
        data = self._json("GET", self._base(kind), params=params)
        items = data.get("items") if isinstance(data, dict) else None
        return [row for row in (items or []) if isinstance(row, dict)]

    def delete(
        self,
        kind: str,
        name: str,
        *,
        grace_period_seconds: int | None = None,
        propagation: str = "Background",
    ) -> None:
        """Delete one object. A 404 is the state delete was asked to produce.

        `propagation` is Background by default so deleting a Job takes its Pod with it;
        Orphan would leave a worker Pod running with nothing tracking it (26)."""
        body: dict[str, Any] = {
            "apiVersion": "meta.k8s.io/v1",
            "kind": "DeleteOptions",
            "propagationPolicy": propagation,
        }
        if grace_period_seconds is not None:
            body["gracePeriodSeconds"] = grace_period_seconds
        try:
            self._json("DELETE", f"{self._base(kind)}/{quote(name, safe='')}", body=body)
        except KubernetesApiError as exc:
            if exc.status != 404:
                raise

    def patch(self, kind: str, name: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """A JSON merge patch on one object. The only mutation this client makes to an
        object it did not create, and the only thing it patches is the harness
        credential Secret's data on a validated sync-back (12)."""
        url = f"{self._base(kind)}/{quote(name, safe='')}"
        headers = {**self._headers(), "Content-Type": "application/merge-patch+json"}
        conn = self._connect()
        try:
            conn.request("PATCH", url, body=json.dumps(body).encode("utf-8"), headers=headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise KubernetesApiError(
                    response.status, _message(raw.decode("utf-8", "replace")), path=url
                )
        finally:
            conn.close()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        return data if isinstance(data, dict) else {}

    def pod_log(
        self,
        name: str,
        *,
        container: str | None = None,
        since_time: str | None = None,
        timestamps: bool = True,
        timeout: float | None = None,
    ) -> list[LogFrame]:
        """`pods/log` with timestamps and an RFC 3339 `sinceTime` bound (26).

        Kubernetes merges stdout and stderr into one stream and does not say which a
        line came from, so every line is reported on stdout. The resume position (10)
        is a timestamp and a line hash, neither of which depends on the stream name.
        `sinceTime` has one-second granularity and is inclusive, which is exactly the
        overlap the strict-after resume already exists to drop."""
        params: dict[str, Any] = {"timestamps": "true" if timestamps else "false"}
        if container:
            params["container"] = container
        if since_time:
            params["sinceTime"] = since_time
        path = f"{self._base('pods')}/{quote(name, safe='')}/log"
        try:
            with self._request("GET", path, params=params, timeout=timeout) as response:
                raw = response.read()
        except KubernetesApiError as exc:
            if exc.status in (400, 404):
                # 400 is what a Pod that has not started a container answers.
                return []
            raise
        return [LogFrame("stdout", raw)] if raw else []

    def pod_exec(
        self,
        name: str,
        command: Sequence[str],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 64 * 1024 * 1024,
    ) -> ExecResult:
        """Run one command in a Pod and return both streams and its exit status.

        The WebSocket form of `pods/exec` (`v4.channel.k8s.io`), which is the transport
        `kubectl cp` uses. No stdin is opened: this client never writes to a Pod, so a
        value cannot travel into one this way."""
        params: dict[str, Any] = {"stdout": "true", "stderr": "true", "stdin": "false"}
        if container:
            params["container"] = container
        query = urlencode([*params.items(), *[("command", c) for c in command]])
        path = f"{self._base('pods')}/{quote(name, safe='')}/exec?{query}"
        return _exec_over_websocket(
            self._connect(timeout), self._headers(), self.access.server, path, limit=limit
        )


# ----- the exec stream ---------------------------------------------------

_CHANNEL_STDOUT = 1
_CHANNEL_STDERR = 2
_CHANNEL_ERROR = 3
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _exec_over_websocket(
    conn: HTTPConnection,
    headers: Mapping[str, str],
    server: str,
    path: str,
    *,
    limit: int,
) -> ExecResult:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    parsed = urlsplit(server)
    request_headers = {
        **headers,
        # The ordinary API calls ask for JSON. An exec upgrade has no JSON
        # representation, and a real API server answers 406 when that Accept header
        # leaks into the WebSocket handshake.
        "Accept": "*/*",
        "Host": parsed.netloc,
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Protocol": "v4.channel.k8s.io",
    }
    try:
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        for header, value in request_headers.items():
            conn.putheader(header, value)
        conn.endheaders()
        response = conn.getresponse()
        if response.status != 101:
            raw = response.read().decode("utf-8", "replace")
            raise KubernetesApiError(response.status, _message(raw), path=path)
        sock = conn.sock
        if sock is None:
            raise KubernetesApiError(0, "the exec upgrade carried no socket", path=path)
        return _read_exec_channels(sock, limit=limit)
    finally:
        conn.close()


def _read_exec_channels(sock: Any, *, limit: int) -> ExecResult:
    streams: dict[int, bytearray] = {
        _CHANNEL_STDOUT: bytearray(),
        _CHANNEL_STDERR: bytearray(),
        _CHANNEL_ERROR: bytearray(),
    }
    total = 0
    for payload in _websocket_frames(sock):
        if not payload:
            continue
        channel, body = payload[0], payload[1:]
        buffer = streams.get(channel)
        if buffer is None or not body:
            continue
        # Bounded before anything is parsed: a Pod owns what it writes and the reader
        # asks for a file a worker could have replaced with anything (12).
        room = max(0, limit - total)
        buffer.extend(body[:room])
        total += len(body)
        if total > limit:
            break
    return ExecResult(
        stdout=bytes(streams[_CHANNEL_STDOUT]),
        stderr=bytes(streams[_CHANNEL_STDERR]),
        exit_code=_exit_status(bytes(streams[_CHANNEL_ERROR])),
    )


def _exit_status(raw: bytes) -> int | None:
    """The error channel carries a metav1.Status: `Success`, or a non-zero exit code in
    its `details.causes`. No status at all is an exec that never ran the command."""
    if not raw:
        return None
    try:
        document = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    if str(document.get("status", "")) == "Success":
        return 0
    for cause in (document.get("details") or {}).get("causes") or []:
        if isinstance(cause, dict) and str(cause.get("reason")) == "ExitCode":
            try:
                return int(str(cause.get("message")))
            except ValueError:
                return None
    return None


def _websocket_frames(sock: Any) -> Iterator[bytes]:
    """RFC 6455 frames from a server, which are never masked. Continuations are joined;
    a close, an empty read, or a socket error ends the stream."""
    buffer = bytearray()
    pending = bytearray()

    def need(count: int) -> bool:
        while len(buffer) < count:
            try:
                chunk = sock.recv(65536)
            except (OSError, ssl.SSLError):
                return False
            if not chunk:
                return False
            buffer.extend(chunk)
        return True

    while True:
        if not need(2):
            return
        first, second = buffer[0], buffer[1]
        opcode = first & 0x0F
        final = bool(first & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if not need(4):
                return
            length = int.from_bytes(buffer[2:4], "big")
            offset = 4
        elif length == 127:
            if not need(10):
                return
            length = int.from_bytes(buffer[2:10], "big")
            offset = 10
        if second & 0x80:
            # A masked frame from a server is a protocol violation; nothing is parsed.
            return
        if not need(offset + length):
            return
        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]
        if opcode == 0x8:
            return
        if opcode in (0x9, 0xA):
            continue
        pending.extend(payload)
        if final:
            yield bytes(pending)
            pending.clear()


def _message(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(parsed, dict) and "message" in parsed:
        return str(parsed["message"])
    return raw.strip()
