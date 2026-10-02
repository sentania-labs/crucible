"""Read the gateway readiness endpoint without starting another worker."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx


async def probe_model_endpoint(endpoint_url: str) -> bool:
    parts = urlsplit(endpoint_url)
    url = urlunsplit((parts.scheme, parts.netloc, "/health/readiness", "", ""))
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(url)
        return response.status_code == 200
    except httpx.HTTPError:
        return False
