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


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, including Docker's nanosecond form.

    Python's `fromisoformat` takes at most microseconds, and Docker's log timestamps
    carry nine fractional digits, so the tail is truncated rather than rejected."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        index = 0
        while index < len(rest) and rest[index].isdigit():
            digits += rest[index]
            index += 1
        text = f"{head}.{digits[:6]:0<6}{rest[index:]}"
    return ensure_utc(datetime.fromisoformat(text))
