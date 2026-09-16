"""RFC 9457 problem details with a stable type URI per error class (04)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_TYPE_PREFIX = "urn:crucible:problem:"


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


def problem_type(slug: str) -> str:
    return PROBLEM_TYPE_PREFIX + slug
