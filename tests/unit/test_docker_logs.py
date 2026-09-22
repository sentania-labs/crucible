"""Log resume is strict-after by (timestamp, line hash), never by timestamp alone (10, S8)."""

from __future__ import annotations

import hashlib

from crucible.adapters.execution.docker import _since_param
from crucible.adapters.execution.dockerapi import LogFrame, demultiplex
from crucible.adapters.execution.logstream import chunks as _chunks
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


REPEATS = frame(
    "stdout",
    "2026-09-16T16:49:49.000000000Z same",
    "2026-09-16T16:49:49.000000000Z same",
    "2026-09-16T16:49:49.000000000Z same",
    "2026-09-16T16:49:49.000000000Z same",
    "2026-09-16T16:49:50.000000000Z after",
)
SAME = hashlib.sha256(b"same").hexdigest()
INSTANT = "2026-09-16T16:49:49.000000000Z"


def test_the_boundary_is_matched_at_its_occurrence_not_at_the_last_hash() -> None:
    """Four identical lines in one instant: resuming after the first must return the
    other three, not only what came after the last of them."""
    offset = LogOffset(timestamp=INSTANT, line_sha256=SAME, occurrence=0)
    body = b"".join(c.content for c in _chunks([REPEATS], offset))
    assert body == b"same\nsame\nsame\nafter\n"

    offset = LogOffset(timestamp=INSTANT, line_sha256=SAME, occurrence=2)
    body = b"".join(c.content for c in _chunks([REPEATS], offset))
    assert body == b"same\nafter\n"

    offset = LogOffset(timestamp=INSTANT, line_sha256=SAME, occurrence=3)
    body = b"".join(c.content for c in _chunks([REPEATS], offset))
    assert body == b"after\n"


def test_the_occurrence_of_the_new_boundary_continues_the_count() -> None:
    """The position the next pull stores has to be absolute within its instant, or the
    pull after that drops the wrong number of repeats."""
    first = _chunks([REPEATS], LogOffset())[-1]
    assert first.occurrence == 0 and first.ts is not None

    offset = LogOffset(timestamp=INSTANT, line_sha256=SAME, occurrence=0)
    chunk = _chunks([REPEATS], offset)[-1]
    # The last line of the batch is `after`, alone in its own instant.
    assert chunk.occurrence == 0

    only_repeats = frame(
        "stdout",
        "2026-09-16T16:49:49.000000000Z same",
        "2026-09-16T16:49:49.000000000Z same",
        "2026-09-16T16:49:49.000000000Z same",
    )
    chunk = _chunks([only_repeats], LogOffset())[-1]
    assert chunk.occurrence == 2
    # Resuming from it returns nothing, and a second pull of the same batch is empty.
    resumed = LogOffset(timestamp=INSTANT, line_sha256=SAME, occurrence=chunk.occurrence)
    assert _chunks([only_repeats], resumed) == []


def test_repeated_pulls_never_duplicate_or_drop_a_repeated_line() -> None:
    """Pull the same growing stream one line at a time; the concatenation is exact."""
    stamps = [f"2026-09-16T16:49:49.00000000{i}Z same" for i in (0, 0, 0)]
    stamps += ["2026-09-16T16:49:49.000000001Z same", "2026-09-16T16:49:50.000000000Z last"]
    offset = LogOffset()
    seen = b""
    for size in range(1, len(stamps) + 1):
        batch = frame("stdout", *stamps[:size])
        for chunk in _chunks([batch], offset):
            seen += chunk.content
            offset = LogOffset(
                timestamp=chunk.ts.isoformat() if chunk.ts else None,
                line_sha256=chunk.line_sha256,
                occurrence=chunk.occurrence,
            )
    assert seen == b"same\nsame\nsame\nsame\nlast\n"


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


def multiplexed(*parts: tuple[int, bytes]) -> bytes:
    """A captured-shape body: 8-byte header, big-endian length, payload."""
    return b"".join(
        bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
        for stream, payload in parts
    )


# What `GET /containers/{id}/logs?timestamps=1` returns for a Tty=false container: the
# content type says `application/vnd.docker.raw-stream` on daemons before API 1.42 and
# `application/vnd.docker.multiplexed-stream` after, and the body is framed either way.
SAMPLE = multiplexed(
    (1, b"2026-09-16T16:49:48.161000000Z read identity bundle 2631 bytes\n"),
    (2, b"2026-09-16T16:49:48.500000000Z warning: nothing to commit\n"),
    (1, b"2026-09-16T16:49:49.162482250Z wrote report.yaml\n"),
)


def test_a_multiplexed_body_is_demultiplexed_whatever_the_content_type_said() -> None:
    frames = demultiplex(SAMPLE)
    assert [f.stream for f in frames] == ["stdout", "stderr", "stdout"]
    assert frames[1].payload.endswith(b"nothing to commit\n")


def test_timestamps_and_streams_survive_the_whole_path() -> None:
    """Attribution and parsing together, on the captured sample."""
    chunks = _chunks(demultiplex(SAMPLE), LogOffset())
    assert [(c.stream, c.content) for c in chunks] == [
        ("stdout", b"read identity bundle 2631 bytes\n"),
        ("stderr", b"warning: nothing to commit\n"),
        ("stdout", b"wrote report.yaml\n"),
    ]
    assert [c.ts.isoformat() if c.ts else None for c in chunks] == [
        "2026-09-16T16:49:48.161000+00:00",
        "2026-09-16T16:49:48.500000+00:00",
        "2026-09-16T16:49:49.162482+00:00",
    ]
    assert all(c.line_sha256 and c.lines == 1 for c in chunks)


def test_a_body_that_is_not_framed_is_kept_rather_than_dropped() -> None:
    """A TTY container, which Crucible never creates, or a daemon that answered
    differently: losing the output silently would be worse than attributing it."""
    raw = b"2026-09-16T16:49:48.161000000Z plain\n"
    assert [(f.stream, f.payload) for f in demultiplex(raw)] == [("stdout", raw)]
    assert demultiplex(b"") == []


def test_a_truncated_final_frame_keeps_what_arrived() -> None:
    raw = bytes([1, 0, 0, 0]) + (99).to_bytes(4, "big") + b"short"
    assert [(f.stream, f.payload) for f in demultiplex(raw)] == [("stdout", b"short")]
