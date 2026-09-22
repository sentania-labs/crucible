"""Resuming a timestamped log stream strictly after a stored position (10).

Docker has no byte offsets and neither does `pods/log`: both take a `since` bound that
is inclusive, so every pull reopens at the boundary instant and the overlap has to be
dropped by content. The stored position is (timestamp, occurrence, sha256) and the
resume is strict-after it, never by timestamp alone.

This module is the Docker provider's own resume logic, unchanged, in the one place both
providers read it from. Nothing here knows what produced the lines: a caller hands in
`(stream, timestamp, raw_stamp, text)` tuples and gets `LogChunk`s back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from crucible.domain.time import parse_rfc3339
from crucible.ports.execution import LogChunk, LogOffset

__all__ = ["chunks", "split_frame"]

# One log line as the chunker sees it: which stream it came from, its parsed timestamp,
# the raw stamp text, and the bytes after the stamp.
Line = tuple[str, datetime | None, str, bytes]


def split_frame(frame_stream: str, payload: bytes) -> list[Line]:
    """Split a payload of `<rfc3339> <text>` lines into the chunker's line tuples."""
    out: list[Line] = []
    for raw in payload.split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        ts: datetime | None = None
        stamp, _, rest = line.partition(" ")
        try:
            ts = parse_rfc3339(stamp)
        except ValueError:
            rest = line
            stamp = ""
        out.append((frame_stream, ts, stamp, rest.encode("utf-8")))
    return out


def chunks(frames: Sequence[Any], since: LogOffset) -> list[LogChunk]:
    """Demultiplexed frames into chunks, resuming strict-after the stored position.

    `since` is inclusive (S8), so the batch always reopens at the boundary instant.
    The stored position is (timestamp, occurrence, sha256): the occurrence is which
    line at that instant was last stored, counting from 0, and it is what separates
    two identical lines logged in the same instant. Matching the last hash in the batch
    instead would silently swallow every repeat between the two.

    If the line at the stored position is not the stored hash, the stream is not the
    one the offset came from (rotated, truncated, a different container or pod), and the
    pull falls back to strictly after the timestamp rather than guessing."""
    lines: list[Line] = []
    for frame in frames:
        lines.extend(split_frame(frame.stream, frame.payload))
    boundary: datetime | None = None
    if since.timestamp is not None:
        try:
            boundary = parse_rfc3339(since.timestamp)
        except ValueError:
            boundary = None
    carried = 0
    if boundary is not None:
        at_boundary = [index for index, entry in enumerate(lines) if entry[1] == boundary]
        position = since.occurrence
        matched = (
            position < len(at_boundary)
            and hashlib.sha256(lines[at_boundary[position]][3]).hexdigest() == since.line_sha256
        )
        if matched:
            lines = lines[at_boundary[position] + 1 :]
            carried = position + 1
        else:
            lines = [e for e in lines if e[1] is not None and e[1] > boundary]
    # Which line at its instant each kept line is. The boundary instant continues the
    # count from the offset rather than restarting it, because the lines before the
    # boundary were stored on an earlier pull.
    seen: dict[datetime | None, int] = {}
    if boundary is not None:
        seen[boundary] = carried
    occurrences: list[int] = []
    for entry in lines:
        position = seen.get(entry[1], 0)
        occurrences.append(position)
        seen[entry[1]] = position + 1

    out: list[LogChunk] = []
    buffer: list[bytes] = []
    stream = ""
    last: tuple[datetime | None, str, int] = (None, "", 0)
    count = 0
    for (line_stream, ts, _stamp, text), occurrence in zip(lines, occurrences, strict=True):
        if stream and line_stream != stream:
            out.append(_chunk(stream, buffer, last, count))
            buffer, count = [], 0
        stream = line_stream
        buffer.append(text)
        count += 1
        last = (ts, hashlib.sha256(text).hexdigest(), occurrence)
    if buffer:
        out.append(_chunk(stream, buffer, last, count))
    return out


def _chunk(
    stream: str, buffer: list[bytes], last: tuple[datetime | None, str, int], count: int
) -> LogChunk:
    content = b"\n".join(buffer) + b"\n"
    return LogChunk(
        stream="stderr" if stream == "stderr" else "stdout",
        content=content,
        ts=last[0],
        line_sha256=last[1],
        occurrence=last[2],
        lines=count,
    )
