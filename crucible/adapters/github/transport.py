"""A small REST transport for api.github.com (23).

Blocking, one connection per request, no third-party HTTP dependency. It knows three
things the rest of the adapter should not have to: how to hold a bearer token for the
length of one call without putting it anywhere else, how to follow `Link` pagination,
and what to do about the rate limit.

Rate limiting: a `403` or `429` carrying `x-ratelimit-remaining: 0` (or `retry-after`)
is raised at once as a `rate_limited` GitHubError carrying how long GitHub asked the
caller to wait. The transport never sleeps: it runs inside the supervisor's tick, and a
sleep there stalls every task, so the caller defers the work to a later tick instead
(hades FDY-0139). A `retry-after` header wins over the reset header, as GitHub's
secondary limits use it.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Callable, Mapping
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from crucible.ports.github import GitHubError

log = logging.getLogger("crucible.github.transport")

API_VERSION = "2022-11-28"
USER_AGENT = "crucible"
DEFAULT_TIMEOUT = 20.0
MAX_RATE_LIMIT_WAIT = 3600.0
MAX_PAGES = 20
PER_PAGE = 100
DOWNLOAD_CHUNK = 64 * 1024


class RestTransport:
    """One host, one API version. `bearer` is passed per call and never stored."""

    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connection_factory: Callable[[str, int, float], Any] | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        self.scheme = parts.scheme or "https"
        self.host = parts.hostname or "api.github.com"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.timeout = timeout
        self._connect = connection_factory or self._default_connection
        self.rate_limit_remaining: int | None = None
        # How many calls GitHub refused for the rate limit; each was raised, not waited.
        self.rate_limited = 0

    def _default_connection(self, host: str, port: int, timeout: float) -> Any:
        if self.scheme == "https":
            return HTTPSConnection(host, port, timeout=timeout)
        return HTTPConnection(host, port, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        raw: bool = False,
    ) -> tuple[int, Any, dict[str, str]]:
        """One call. Returns (status, parsed body or bytes, lowercased headers).

        A rate-limit refusal raises at once with how long GitHub asked for; nothing here
        waits (hades FDY-0139)."""
        status, payload, headers = self._once(
            method, path, bearer=bearer, body=body, params=params, accept=accept, raw=raw
        )
        remaining = headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        if _is_rate_limited(status, headers):
            delay = _rate_limit_delay(headers)
            log.warning(
                "github rate limit reached; deferring to a later tick",
                extra={"path": _loggable(path), "seconds": delay},
            )
            self.rate_limited += 1
            raise GitHubError(
                status,
                f"rate limited; GitHub asks for {int(delay)}s before the next call",
                path=_loggable(path),
                response_class="rate_limited",
                retry_after=delay,
            )
        return status, payload, headers

    def _once(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        body: Any,
        params: Mapping[str, Any] | None,
        accept: str,
        raw: bool,
    ) -> tuple[int, Any, dict[str, str]]:
        url = f"{self.prefix}{path}"
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        }
        # The one unauthenticated call is the manifest conversion (crucible#168): the
        # code in its path is the credential, so an empty bearer sends no header.
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn = self._connect(self.host, self.port, self.timeout)
        try:
            conn.request(method, url, body=payload, headers=headers)
            response = conn.getresponse()
            raw_body = response.read()
            received = {str(k).lower(): str(v) for k, v in response.getheaders()}
            if received.get("content-encoding") == "gzip" and raw_body:
                raw_body = gzip.decompress(raw_body)
            status = int(response.status)
        except OSError as exc:
            raise GitHubError(
                0,
                f"{type(exc).__name__}: {exc}",
                path=_loggable(path),
                response_class="transport",
            ) from exc
        finally:
            conn.close()
            headers.clear()
        if raw:
            return status, raw_body, received
        if not raw_body:
            return status, None, received
        try:
            return status, json.loads(raw_body.decode("utf-8")), received
        except (ValueError, UnicodeDecodeError):
            return status, None, received

    def get(self, path: str, *, bearer: str, params: Mapping[str, Any] | None = None) -> Any:
        status, payload, _ = self.request("GET", path, bearer=bearer, params=params)
        if status >= 400:
            raise GitHubError(status, _message(payload), path=path)
        return payload

    def paginate(
        self,
        path: str,
        *,
        bearer: str,
        params: Mapping[str, Any] | None = None,
        key: str | None = None,
    ) -> list[Any]:
        """Every page of a list endpoint, bounded. GitHub's `Link` header is the cursor.

        `key` names the list inside an object-shaped page, for the endpoints that wrap
        their items (`check_runs`, `workflow_runs`, `jobs`): those are paginated the same
        way, and reading only their first page silently drops everything past it."""
        merged = {"per_page": PER_PAGE, **(params or {})}
        out: list[Any] = []
        page = 1
        while page <= MAX_PAGES:
            status, payload, headers = self.request(
                "GET", path, bearer=bearer, params={**merged, "page": page}
            )
            if status >= 400:
                raise GitHubError(status, _message(payload), path=path)
            if key is not None:
                payload = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(payload, list):
                break
            out.extend(payload)
            if 'rel="next"' not in headers.get("link", ""):
                break
            page += 1
        return out

    def download(self, url: str, *, limit_bytes: int) -> bytes:
        """The last `limit_bytes` of what a redirect target serves (hades FDY-0139).

        GitHub answers a log request with a redirect to a short-lived signed URL on
        another host. That URL is its own credential, so no Authorization header goes
        with it, and it must be https unless it is on the API host itself (the fake in
        the integration tier). The tail is what is kept: a failing job prints its failure
        last."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        same_host = host == self.host and (parts.scheme or self.scheme) == self.scheme
        if not host or (parts.scheme != "https" and not same_host):
            raise GitHubError(0, "refusing a log redirect that is not https", path="[redirect]")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        conn: Any = (
            self._connect(host, port, self.timeout)
            if same_host
            else HTTPSConnection(host, port, timeout=self.timeout)
        )
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        limit = max(1, limit_bytes)
        tail = b""
        try:
            conn.request("GET", target, headers={"User-Agent": USER_AGENT})
            response = conn.getresponse()
            status = int(response.status)
            while True:
                chunk = response.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                tail = (tail + chunk)[-limit:]
        except (OSError, HTTPException) as exc:
            # A connection the log host drops partway (IncompleteRead) is a failed read
            # like any other, never an exception that escapes the tick.
            raise GitHubError(
                0, f"{type(exc).__name__}: {exc}", path="[redirect]", response_class="transport"
            ) from exc
        finally:
            conn.close()
        if status >= 400:
            raise GitHubError(status, "the log download was refused", path="[redirect]")
        return tail


def _loggable(path: str) -> str:
    """The path as a log line or an error may carry it: a manifest code is a one-time
    credential for a new App's key (crucible#168), so it is left out."""
    if path.startswith("/app-manifests/"):
        return "/app-manifests/[code]/conversions"
    return path


def _is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


def _rate_limit_delay(headers: Mapping[str, str]) -> float:
    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(float(retry_after), MAX_RATE_LIMIT_WAIT)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return max(1.0, min(float(int(reset) - time.time()), MAX_RATE_LIMIT_WAIT))
    return 60.0


def _message(payload: Any) -> str:
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return "request failed"
