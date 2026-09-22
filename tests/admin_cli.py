"""Drive `crucible admin` the way the tests drove `crucible-admin`: in process, reading
the envelope it prints."""

from __future__ import annotations

import json
from typing import Any

import pytest

from crucible.cli.main import run


def admin_main(argv: list[str]) -> None:
    """`crucible admin ARGV`; a non-zero exit raises SystemExit as the old entry point did."""
    code = run(["admin", *argv])
    if code:
        raise SystemExit(code)


def envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip().splitlines()
    document = json.loads(out[-1])
    assert isinstance(document, dict)
    return document


def envelope_data(capsys: pytest.CaptureFixture[str]) -> Any:
    document = envelope(capsys)
    assert document["ok"] is True, document
    return document["data"]
