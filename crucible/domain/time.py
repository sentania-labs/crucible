"""Time helpers. Everything is timezone-aware UTC; rendering is RFC 3339 with an offset."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not allowed; timestamps must carry an offset")
    return value.astimezone(UTC)


def rfc3339(value: datetime) -> str:
    """Render with an explicit numeric offset (never 'Z', never an epoch)."""
    return ensure_utc(value).isoformat(timespec="microseconds")
