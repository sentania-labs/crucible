"""Content-addressed artifact store (03, 14): bytes live outside the database."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """Where the bytes landed, relative to the artifact root, plus their digest."""

    path: str
    sha256: str
    size: int


class SecretInArtifactError(Exception):
    """The scanner matched a secret pattern; the bytes are refused before storage (12).

    Carries the pattern name and the offset only, never the matched value."""

    def __init__(self, pattern: str, where: str) -> None:
        super().__init__(f"secret pattern {pattern} matched at {where}")
        self.pattern = pattern
        self.where = where


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> StoredBlob:
        """Scan, then store content-addressed. Raises SecretInArtifactError on a match."""
        ...

    def get(self, path: str) -> bytes: ...

    def exists(self, path: str) -> bool: ...
