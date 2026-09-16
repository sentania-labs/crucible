"""Webhook wake delivery (17): POST with an HMAC-SHA256 signature over the raw body.

The shared secret comes from the environment and is never stored, logged, or echoed.
The standard library does the POST, so this adds no dependency."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import urllib.error
import urllib.request

from crucible.ports.notification import DeliveryResult

log = logging.getLogger("crucible.wakes")

SIGNATURE_HEADER = "X-Crucible-Signature-256"
CONTENT_TYPE = "application/json"


def sign(secret: str, body: bytes) -> str:
    """The value of X-Crucible-Signature-256 for this body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookWakeDeliverer:
    def __init__(
        self, url: str | None, secret: str | None, *, timeout_seconds: float = 5.0
    ) -> None:
        self._url = url
        self._secret = secret
        self._timeout = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def _post(self, body: bytes) -> DeliveryResult:
        assert self._url is not None
        headers = {"Content-Type": CONTENT_TYPE}
        if self._secret:
            headers[SIGNATURE_HEADER] = sign(self._secret, body)
        request = urllib.request.Request(self._url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            return DeliveryResult(False, f"HTTP {exc.code}")
        except Exception as exc:
            return DeliveryResult(False, f"{type(exc).__name__}: {exc}")
        if 200 <= status < 300:
            return DeliveryResult(True, f"HTTP {status}")
        return DeliveryResult(False, f"HTTP {status}")

    async def deliver(self, body: bytes) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(False, "no wake webhook configured; poll only")
        return await asyncio.to_thread(self._post, body)
