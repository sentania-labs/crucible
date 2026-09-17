"""A small REST transport for api.github.com (23).

Blocking, one connection per request, no third-party HTTP dependency. It knows three
things the rest of the adapter should not have to: how to hold a bearer token for the
length of one call without putting it anywhere else, how to follow `Link` pagination,
and what to do about the rate limit.

Rate limiting: a `403` or `429` carrying `x-ratelimit-remaining: 0` is a wait, not a
failure. The transport sleeps until `x-ratelimit-reset` (bounded), once, and retries; a
second refusal is raised so the caller records it rather than the process stalling. A
`retry-after` header wins over the reset header, as GitHub's secondary limits use it.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Callable, Mapping
from http.client import HTTPConnection, HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from crucible.ports.github import GitHubError

log = logging.getLogger("crucible.github.transport")

API_VERSION = "2022-11-28"
USER_AGENT = "crucible"
DEFAULT_TIMEOUT = 20.0
MAX_RATE_LIMIT_SLEEP = 90.0
MAX_PAGES = 20
PER_PAGE = 100


class RestTransport:
    """One host, one API version. `bearer` is passed per call and never stored."""

    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        connection_factory: Callable[[str, int, float], Any] | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        self.scheme = parts.scheme or "https"
        self.host = parts.hostname or "api.github.com"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.timeout = timeout
        self._sleep = sleep
        self._connect = connection_factory or self._default_connection
        self.rate_limit_remaining: int | None = None
        self.waits = 0

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
        """One call. Returns (status, parsed body or bytes, lowercased headers)."""
        for attempt in (0, 1):
            status, payload, headers = self._once(
                method, path, bearer=bearer, body=body, params=params, accept=accept, raw=raw
            )
            remaining = headers.get("x-ratelimit-remaining")
            if remaining is not None and remaining.isdigit():
                self.rate_limit_remaining = int(remaining)
            if attempt == 0 and _is_rate_limited(status, headers):
                delay = _rate_limit_delay(headers)
                log.warning(
                    "github rate limit reached; waiting",
                    extra={"path": path, "seconds": delay},
                )
                self.waits += 1
                self._sleep(delay)
                continue
            return status, payload, headers
        raise GitHubError(
            429, "rate limited twice in a row", path=path, response_class="rate_limited"
        )

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
            "Authorization": f"Bearer {bearer}",
            "Accept-Encoding": "gzip",
        }
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
                0, f"{type(exc).__name__}: {exc}", path=path, response_class="transport"
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
        self, path: str, *, bearer: str, params: Mapping[str, Any] | None = None
    ) -> list[Any]:
        """Every page of a list endpoint, bounded. GitHub's `Link` header is the cursor."""
        merged = {"per_page": PER_PAGE, **(params or {})}
        out: list[Any] = []
        page = 1
        while page <= MAX_PAGES:
            status, payload, headers = self.request(
                "GET", path, bearer=bearer, params={**merged, "page": page}
            )
            if status >= 400:
                raise GitHubError(status, _message(payload), path=path)
            if not isinstance(payload, list):
                break
            out.extend(payload)
            if len(payload) < int(merged["per_page"]) or 'rel="next"' not in headers.get(
                "link", ""
            ):
                break
            page += 1
        return out


def _is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers


def _rate_limit_delay(headers: Mapping[str, str]) -> float:
    retry_after = headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(float(retry_after), MAX_RATE_LIMIT_SLEEP)
    reset = headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        return max(1.0, min(float(int(reset) - time.time()), MAX_RATE_LIMIT_SLEEP))
    return 5.0


def _message(payload: Any) -> str:
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return "request failed"
