"""Validation shared by the launch contracts for subscription and local endpoints."""

from __future__ import annotations

from urllib.parse import urlsplit


def validate_endpoint(endpoint: str, endpoint_url: str | None) -> None:
    if endpoint == "subscription":
        if endpoint_url is not None:
            raise ValueError("subscription endpoint forbids endpoint_url")
        return
    if endpoint != "local":
        raise ValueError(f"unknown endpoint kind {endpoint!r}")
    if not endpoint_url:
        raise ValueError("local endpoint requires endpoint_url")
    parsed = urlsplit(endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1"
    ):
        raise ValueError("local endpoint_url must be an http(s) base URL ending in /v1")
