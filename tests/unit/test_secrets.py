from __future__ import annotations

import pytest

from crucible.domain.secrets import find_secrets, scan_text


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("token ghp_" + "a" * 36 + " here", "github_token"),
        ("ghs_" + "B" * 36, "github_token"),
        ("github_pat_" + "x" * 30, "github_fine_grained_token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private_key_header"),
        ("-----BEGIN PRIVATE KEY-----", "private_key_header"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "bearer_token"),
        ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
        ("sk-" + "q" * 24, "openai_style_key"),
        ("xoxb-1234567890-abcdef", "slack_token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijkl", "jwt"),
        ("cru_01ARZ3NDEKTSV4RRFFQ69G5FAV." + "s" * 40, "crucible_token"),
    ],
)
def test_patterns_match(text: str, pattern: str) -> None:
    assert scan_text(text) == pattern


@pytest.mark.parametrize(
    "text",
    [
        "make lint && make test",
        "the bearer of bad news",
        "https://github.com/example-org/example-service/issues/17",
        "sk-short",
        "ghp_tooshort",
    ],
)
def test_ordinary_text_is_clean(text: str) -> None:
    assert scan_text(text) is None


def test_find_secrets_reports_path_not_value() -> None:
    doc = {"a": {"b": ["fine", "ghp_" + "c" * 36]}, "c": "ok"}
    matches = find_secrets(doc)
    assert [(m.path, m.pattern) for m in matches] == [("a.b[1]", "github_token")]
    assert "ccc" not in repr(matches)
