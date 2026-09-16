"""Content-addressed artifact store on disk, under the configured artifact root (14).

Bytes live outside the database. The path is the digest, so identical content is stored
once and a rewrite cannot change what a recorded sha256 refers to. Every blob passes the
secret scanner before it is written: a match is refused with the pattern name and the
offset, never the value (12)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from crucible.domain.secrets import scan_text
from crucible.ports.artifacts import SecretInArtifactError, StoredBlob

BLOB_DIR = "blobs"
# Enough of the file to find a credential without holding a huge artifact in memory
# twice; the scanner patterns are all far shorter than this.
SCAN_CHUNK = 1 << 20


class DiskArtifactStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _blob_path(self, digest: str) -> Path:
        return self.root / BLOB_DIR / digest[:2] / digest[2:4] / digest

    @staticmethod
    def relative(digest: str) -> str:
        return f"{BLOB_DIR}/{digest[:2]}/{digest[2:4]}/{digest}"

    @staticmethod
    def scan(content: bytes) -> None:
        """Raise SecretInArtifactError when the content matches a secret pattern."""
        for offset in range(0, max(len(content), 1), SCAN_CHUNK):
            # Overlap by a pattern's worth so a match on a chunk boundary is still seen.
            chunk = content[max(0, offset - 512) : offset + SCAN_CHUNK]
            hit = scan_text(chunk.decode("utf-8", "replace"))
            if hit is not None:
                raise SecretInArtifactError(hit, f"byte offset {offset}")

    def put(self, content: bytes) -> StoredBlob:
        self.scan(content)
        digest = hashlib.sha256(content).hexdigest()
        target = self._blob_path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file in the same directory, then rename, so a reader
            # never sees a half-written blob.
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(tmp, target)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return StoredBlob(path=self.relative(digest), sha256=digest, size=len(content))

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def exists(self, path: str) -> bool:
        return (self.root / path).is_file()
