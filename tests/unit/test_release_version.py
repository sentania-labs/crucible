"""The single highest-published-version rule the release's "move latest" step applies
before moving the service image's and the worker images' mutable `latest` tags
(FDY-0094, PR 88 review round; FDY-0096)."""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "version.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("version", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["version"] = module
    spec.loader.exec_module(module)
    return module


v = _module()


def test_a_bare_x_y_z_tag_is_a_version() -> None:
    assert v.is_version("0.5.2")
    assert v.is_version("10.0.0")


def test_a_prefixed_or_non_numeric_tag_is_not_a_version() -> None:
    assert not v.is_version("script-harness-0.5.2")
    assert not v.is_version("ci-0123abcd-20260916-aaaaaaaaaaaa")
    assert not v.is_version("latest")
    assert not v.is_version("0.5")
    assert not v.is_version("v0.5.2")


def test_a_higher_published_version_wins() -> None:
    assert v.highest_version("0.5.2", ["0.4.0", "0.5.0"]) == "0.5.2"


def test_a_lower_candidate_than_already_published_is_not_the_highest() -> None:
    assert v.highest_version("0.5.2", ["0.4.0", "0.9.0"]) == "0.9.0"


def test_the_same_candidate_already_published_is_a_no_op_highest() -> None:
    assert v.highest_version("0.5.2", ["0.5.2"]) == "0.5.2"


def test_numeric_ordering_not_lexical_ordering() -> None:
    """Lexical order would put "0.10.0" before "0.9.0"; version order must not."""
    assert v.highest_version("0.9.0", ["0.10.0"]) == "0.10.0"
    assert v.highest_version("0.10.0", ["0.9.0"]) == "0.10.0"


def test_a_non_version_tag_never_contends() -> None:
    tags = ["20260916-aaaaaaaaaaaa", "script-harness-9.9.9", "latest", "9"]
    assert v.highest_version("0.5.2", tags) == "0.5.2"


def test_nothing_published_yet_makes_the_candidate_the_highest() -> None:
    assert v.highest_version("0.1.0", []) == "0.1.0"


def test_cli_reads_tags_from_stdin() -> None:
    stdin = io.StringIO("0.4.0\n0.9.0\n\n")
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        with redirect_stdout(out):
            code = v.main(["0.5.2"])
    finally:
        sys.stdin = old_stdin
    assert code == 0
    assert out.getvalue().strip() == "0.9.0"


def test_cli_reads_tags_from_argv_when_given() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        code = v.main(["0.5.2", "0.4.0", "0.1.0"])
    assert code == 0
    assert out.getvalue().strip() == "0.5.2"
