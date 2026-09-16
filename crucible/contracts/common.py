"""Shared contract building blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

from crucible.domain.time import rfc3339

SCHEMA_VERSION = "1.0"
SUPPORTED_MAJOR = "1"

# RFC 3339 with an explicit offset in every response (01, 04).
Rfc3339 = Annotated[datetime, PlainSerializer(rfc3339, return_type=str, when_used="json")]


class StrictModel(BaseModel):
    """Unknown fields are rejected on every contract (05, 05b)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def check_major_version(value: str) -> str:
    major, _, minor = value.partition(".")
    if major != SUPPORTED_MAJOR or not minor.isdigit():
        raise ValueError(f"schema_version {value!r} is not a supported 1.x version")
    return value


def to_document(model: BaseModel) -> dict[str, Any]:
    """The JSON-shaped document stored verbatim in the database."""
    return model.model_dump(mode="json")
