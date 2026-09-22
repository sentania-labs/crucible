"""The one HTTP transport both command groups use against `/v1`.

Carried from the two clients it replaces: `crucible-admin`'s remote mode and Foundry's
`foundry-crucible`. Redirects are refused, so a bearer token never crosses to another
origin. A refusal comes back as a ClientError carrying the API's problem detail whole;
the token in use is redacted from every message before it leaves this module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from crucible.client.envelope import ClientError, UsageError, redact, redact_text, refusal

# Foundry's client used 30 seconds; the admin client 300, because a credential probe
# runs a bounded container and a login waits on a person.
ORCHESTRATOR_TIMEOUT_SECONDS = 30.0
ADMIN_TIMEOUT_SECONDS = 300.0


def validate_base_url(value: str, *, source: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError(f"{source} must be an http or https URL", code="config")
    if parsed.query or parsed.fragment:
        raise UsageError(f"{source} must not contain a query or fragment", code="config")
    if parsed.username or parsed.password:
        # The URL is echoed in `next` commands; a credential in it would be printed.
        raise UsageError(
            f"{source} must not carry credentials; the token comes from the environment",
            code="config",
        )
    return value.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """Refuse redirects so a bearer token never crosses to another origin."""
        return


class Api:
    """`/v1` over HTTP with a bearer token."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _redact(self, text: str) -> str:
        return redact_text(text, [self.token])

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        reason: str | None = None,
        versioned: bool = False,
        timeout: float = ADMIN_TIMEOUT_SECONDS,
    ) -> Any:
        """One request. `path` starts with `/v1`. `versioned` holds the response to the
        orchestrator contract: a JSON object with a `schema_version` (04)."""
        data = None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if reason is not None:
            headers["X-Foundry-Reason"] = reason
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = self._redact(exc.read().decode("utf-8", "replace"))
            try:
                problem = json.loads(raw) if raw else None
            except ValueError:
                problem = None
            raise refusal(exc.code, redact(problem, [self.token]), raw) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClientError(
                "unreachable", self._redact(f"cannot reach {self.base_url}: {exc}")
            ) from None
        if not raw:
            if versioned:
                raise ClientError("protocol", "Crucible returned an empty success response")
            return None
        try:
            document = json.loads(raw)
        except ValueError:
            raise ClientError("protocol", "Crucible returned a non-JSON response") from None
        if versioned:
            if isinstance(document, dict) and "type" in document and "detail" in document:
                raise ClientError(
                    "protocol", "Crucible returned a problem document with a 2xx status"
                )
            if not isinstance(document, dict) or not isinstance(
                document.get("schema_version"), str
            ):
                raise ClientError(
                    "protocol",
                    "Crucible returned a 2xx response without a versioned resource envelope",
                )
        return redact(document, [self.token])

    def all_pages(self, path: str) -> dict[str, Any]:
        """Every page of a cursor-paginated list, as one list with `next_cursor` null."""
        document = self.call("GET", path, versioned=True, timeout=ORCHESTRATOR_TIMEOUT_SECONDS)
        if not isinstance(document.get("items"), list) or "next_cursor" not in document:
            raise ClientError("protocol", "Crucible returned a list without items and next_cursor")
        items = list(document["items"])
        cursor = document["next_cursor"]
        seen: set[str] = set()
        while cursor is not None:
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ClientError(
                    "protocol", "Crucible returned an invalid or repeated pagination cursor"
                )
            seen.add(cursor)
            separator = "&" if "?" in path else "?"
            page = self.call(
                "GET",
                f"{path}{separator}{urllib.parse.urlencode({'cursor': cursor})}",
                versioned=True,
                timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
            )
            if not isinstance(page.get("items"), list) or "next_cursor" not in page:
                raise ClientError(
                    "protocol", "Crucible returned a page without items and next_cursor"
                )
            items.extend(page["items"])
            cursor = page["next_cursor"]
        return {**document, "items": items, "next_cursor": None}
