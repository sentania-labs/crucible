"""Read the gateway readiness endpoint without starting another worker."""

from __future__ import annotations

import asyncio
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urlsplit


def _probe_model_endpoint(endpoint_url: str) -> bool:
    connection: HTTPConnection | None = None
    try:
        parts = urlsplit(endpoint_url)
        if parts.scheme not in {"http", "https"} or parts.hostname is None:
            return False
        connection_type = HTTPSConnection if parts.scheme == "https" else HTTPConnection
        connection = connection_type(parts.hostname, parts.port, timeout=5)
        prefix = parts.path.rstrip("/")
        if prefix.endswith("/v1"):
            prefix = prefix[:-3]
        connection.request("GET", prefix + "/health/readiness")
        with connection.getresponse() as response:
            return response.status == 200
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


async def probe_model_endpoint(endpoint_url: str) -> bool:
    # Network I/O must not stall the supervisor's event loop during an outage.
    try:
        async with asyncio.timeout(5):
            return await asyncio.to_thread(_probe_model_endpoint, endpoint_url)
    except TimeoutError:
        return False
