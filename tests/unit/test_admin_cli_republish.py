"""The administrative CLI exposes the same manual publication retry as the API."""

from __future__ import annotations

import argparse
from typing import Any

from crucible.cli.admin import _remote, build_parser


class RecordingRemote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path, body))
        return {"state": "publishing"}


def test_remote_cli_republish_calls_the_task_api() -> None:
    args = build_parser(argparse.ArgumentParser()).parse_args(
        ["task", "republish", "01TASK", "--reason", "service recovered"]
    )
    remote = RecordingRemote()
    document = _remote(args, remote)  # type: ignore[arg-type]
    assert remote.calls == [
        (
            "POST",
            "/v1/tasks/01TASK/republish",
            {"reason": "service recovered"},
        )
    ]
    assert document == {"state": "publishing"}
