"""ULID identifiers for every entity."""

from __future__ import annotations

from ulid import ULID

ULID_LENGTH = 26


def new_id() -> str:
    """Return a fresh ULID as its 26-character string."""
    return str(ULID())


def is_ulid(value: str) -> bool:
    if len(value) != ULID_LENGTH:
        return False
    try:
        ULID.from_str(value)
    except ValueError:
        return False
    return True
