"""Log resume is strict-after by (timestamp, line hash), never by timestamp alone (10, S8)."""

from __future__ import annotations

import hashlib

from crucible.adapters.execution.docker import _chunks, _since_param
from crucible.adapters.execution.dockerapi import LogFrame, _demux
from crucible.ports.execution import LogOffset


def frame(stream: str, *lines: str) -> LogFrame:
    return LogFrame(stream, "".join(f"{line}\n" for line in lines).encode())


TICKS = frame(
    "stdout",
    "2026-09-16T16:49:48.161000000Z tick 2",
    "2026-09-16T16:49:49.162482250Z tick 3",
    "2026-09-16T16:49:50.163457571Z tick 4",
)


def test_a_first_pull_takes_everything() -> None:
    chunks = _chunks([TICKS], LogOffset())
    assert len(chunks) == 1
    assert chunks[0].content == b"tick 2\ntick 3\ntick 4\n"
    assert chunks[0].lines == 3
    assert chunks[0].line_sha256 == hashlib.sha256(b"tick 4").hexdigest()


def test_the_boundary_line_is_dropped_by_its_hash() -> None:
    """`--since` is inclusive (S8), so the line at the offset comes back every time."""
    offset = LogOffset(
        timestamp="2026-09-16T16:49:49.162482250Z",
        line_sha256=hashlib.sha256(b"tick 3").hexdigest(),
    )
    chunks = _chunks([TICKS], offset)
    assert b"".join(c.content for c in chunks) == b"tick 4\n"


def test_lines_sharing_a_timestamp_are_not_dropped_wholesale() -> None:
    """Two lines at the same instant: the hash is what separates them, not the clock."""
    same = frame(
        "stdout",
        "2026-09-16T16:49:49.000000000Z a",
        "2026-09-16T16:49:49.000000000Z b",
        "2026-09-16T16:49:49.000000000Z c",
    )
    offset = LogOffset(
        timestamp="2026-09-16T16:49:49.000000000Z",
        line_sha256=hashlib.sha256(b"a").hexdigest(),
    )
    assert b"".join(c.content for c in _chunks([same], offset)) == b"b\nc\n"


def test_an_unknown_hash_falls_back_to_strictly_after_the_timestamp() -> None:
    """A rotated or truncated stream must not replay the boundary instant."""
    offset = LogOffset(timestamp="2026-09-16T16:49:49.162482250Z", line_sha256="0" * 64)
    assert b"".join(c.content for c in _chunks([TICKS], offset)) == b"tick 4\n"


def test_streams_are_kept_apart() -> None:
    chunks = _chunks(
        [
            frame("stdout", "2026-09-16T16:49:48.000000000Z out"),
            frame("stderr", "2026-09-16T16:49:49.000000000Z err"),
        ],
        LogOffset(),
    )
    assert [(c.stream, c.content) for c in chunks] == [
        ("stdout", b"out\n"),
        ("stderr", b"err\n"),
    ]


def test_the_since_parameter_is_the_wire_form_docker_wants() -> None:
    """Docker splits `since` on the dot and parses both halves as integers."""
    assert _since_param(None) is None
    value = _since_param("2026-09-16T16:49:49.162482+00:00")
    assert value is not None
    seconds, _, nanos = value.partition(".")
    assert seconds.isdigit() and len(nanos) == 9 and nanos.isdigit()


def test_the_multiplexed_stream_is_demultiplexed() -> None:
    payload = b"hello\n"
    raw = bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
    raw += bytes([2, 0, 0, 0]) + len(b"bad\n").to_bytes(4, "big") + b"bad\n"
    assert [(f.stream, f.payload) for f in _demux(raw)] == [
        ("stdout", b"hello\n"),
        ("stderr", b"bad\n"),
    ]


def test_a_truncated_frame_is_dropped_rather_than_misread() -> None:
    raw = bytes([1, 0, 0, 0]) + (99).to_bytes(4, "big") + b"short"
    assert _demux(raw) == []
