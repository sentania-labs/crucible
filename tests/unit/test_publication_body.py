"""The pull request title and body (23): what may appear, and what may not."""

from __future__ import annotations

import hashlib

import pytest

from crucible.domain import publication
from crucible.domain.publication import (
    MAX_TITLE_LENGTH,
    BodyInput,
    BodyRefusedError,
    CorrectionEntry,
    CriterionMapping,
    TitleRefusedError,
    VerifiedCheck,
    authorized_closes,
    body_sha256,
    defang_closing_keywords,
    render_body,
    validate_title,
)


def _body(**kw: object) -> BodyInput:
    base: dict[str, object] = {
        "external_id": "FDY-0042",
        "objective": "Make the importer refuse a duplicate id.",
        "head_sha": "a" * 40,
        "attempt_id": "01ATTEMPT",
        "harness": "script-harness",
        "harness_version": "1.0.0",
        "image_digest": "sha256:deadbeef",
    }
    base.update(kw)
    return BodyInput(**base)  # type: ignore[arg-type]


def test_a_title_is_taken_from_the_claim_after_validation() -> None:
    assert validate_title("  refuse a   duplicate id  ") == "refuse a duplicate id"


def test_a_title_carrying_a_closing_keyword_is_refused() -> None:
    with pytest.raises(TitleRefusedError, match="closing keyword"):
        validate_title("fixes #12 by refusing duplicates")


def test_a_title_carrying_a_secret_is_refused_by_pattern_name() -> None:
    value = "ghs_" + "a1b2c3d4e5" * 6
    with pytest.raises(TitleRefusedError, match="github_installation_token"):
        validate_title(f"push with {value}")


def test_an_over_long_or_empty_title_is_refused() -> None:
    with pytest.raises(TitleRefusedError, match="limit"):
        validate_title("x" * (MAX_TITLE_LENGTH + 1))
    with pytest.raises(TitleRefusedError, match="empty"):
        validate_title("   ")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fixes #12", "\\Fixes #12"),
        ("this closes owner/repo#3", "this \\closes owner/repo#3"),
        (
            "RESOLVED: https://github.com/o/r/issues/9",
            "\\RESOLVED: https://github.com/o/r/issues/9",
        ),
        ("it fixes the parser", "it fixes the parser"),
        ("see #12", "see #12"),
    ],
)
def test_closing_keywords_are_defanged_only_before_a_reference(text: str, expected: str) -> None:
    assert defang_closing_keywords(text) == expected


def test_only_contract_closing_references_survive() -> None:
    assert authorized_closes(
        ["#12", "owner/repo#3", "https://github.com/o/r/issues/9", "not a ref", "#12"]
    ) == ["#12", "owner/repo#3", "https://github.com/o/r/issues/9"]


def test_the_body_carries_the_verifier_exit_codes_and_names_them_as_crucibles() -> None:
    body = render_body(
        _body(
            checks=(
                VerifiedCheck(id="V1", command="make test", exit_code=0, artifact_id="01ARTIFACT"),
            )
        )
    )
    assert "| V1 | `make test` | 0 | `01ARTIFACT` |" in body
    assert "Crucible's own re-run" in body


def test_worker_asserted_text_is_labelled_and_never_closes_an_issue() -> None:
    body = render_body(
        _body(
            limitations=("this fixes #99 only on Linux",),
            risks=("the importer is slower",),
        )
    )
    assert "Worker-asserted. Crucible did not verify these." in body
    # The keyword survives as readable text but GitHub no longer acts on it.
    assert "this \\fixes #99 only on Linux" in body
    assert "this fixes #99" not in body


def test_at_mentions_in_worker_text_are_defanged() -> None:
    """A mention notifies people, and for a provider whose reviewer answers its own name
    it triggers a review under whatever identity opened the pull request. 23 makes the
    trigger the orchestrator's act, so a mention that arrived through worker text would
    be Crucible performing it instead."""
    body = render_body(_body(limitations=("ask @someone about the parser",), risks=("cc @a-team",)))
    assert "\\@someone" in body and "\\@a-team" in body
    assert " @someone" not in body and " @a-team" not in body


def test_a_mention_quoted_as_code_is_still_defanged() -> None:
    """Backticks are not protection: the provider reads the raw body, not the rendered
    HTML, which is how a quoted phrase triggered it on pull request #22."""
    body = render_body(_body(limitations=("the trigger is `@bot review`",)))
    assert "`\\@bot review`" in body
    assert "`@bot" not in body


def test_an_email_address_is_not_a_mention() -> None:
    body = render_body(_body(limitations=("mail crucible-worker@users.noreply.github.com",)))
    assert "crucible-worker@users.noreply.github.com" in body


def test_a_title_carrying_a_mention_is_refused() -> None:
    with pytest.raises(TitleRefusedError, match="at-mention"):
        validate_title("ask @someone to look at the parser")


def test_the_external_id_is_sanitized_into_the_body() -> None:
    body = render_body(_body(external_id="FDY-1 @someone fixes #3"))
    assert "@someone" not in body.replace("\\@someone", "")
    assert "\\fixes #3" in body


def _private_key_header() -> str:
    # Built at run time so no secret-shaped literal is checked in.
    return "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


def test_every_field_is_sanitized_on_the_way_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-field pass is what normally catches a secret, and it does."""
    body = render_body(_body(objective=_private_key_header()))
    assert "BEGIN" not in body
    assert "[redacted:private_key_header]" in body
    _ = monkeypatch


def test_the_assembled_body_is_scanned_too_and_a_hit_refuses_the_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check on the assembly, not only on its parts: a body is what actually leaves
    (12). A field that stopped being sanitized would be a defect in this module, and the
    backstop turns it into a refusal rather than a disclosure. Sanitization is disabled
    here to reach it, which is the only way a test can."""
    monkeypatch.setattr(publication, "sanitize", lambda text: text)
    with pytest.raises(BodyRefusedError, match="private_key_header"):
        render_body(_body(objective=_private_key_header()))


def test_only_authorized_closing_references_are_rendered() -> None:
    body = render_body(_body(closes=("#12", "nonsense")))
    assert "Closes #12" in body
    assert "nonsense" not in body


def test_a_secret_in_worker_text_is_redacted_before_the_body_leaves() -> None:
    value = "ghs_" + "9z8y7x6w5v" * 8
    body = render_body(_body(limitations=(f"the token is {value}",)))
    assert value not in body
    assert "[redacted:github_installation_token]" in body


def test_criteria_and_corrections_appear_with_their_own_status_words() -> None:
    body = render_body(
        _body(
            criteria=(
                CriterionMapping(
                    id="AC1", text="A duplicate id is refused", status="met", evidence="V1"
                ),
            ),
            corrections=(
                CorrectionEntry(version=2, reason="address the review", addresses=("c1",)),
            ),
            review_reference={"reviewer_kind": "orchestrator", "report_id": "01REPORT"},
        )
    )
    assert "| AC1: A duplicate id is refused | met | V1 |" in body
    assert "contract version 2: address the review (addresses: c1)" in body
    assert "reviewer_kind" in body and "01REPORT" in body


def test_the_body_hash_is_what_the_pull_request_row_records() -> None:
    """The hash is of the exact bytes sent, and a one-character change changes it."""
    body = render_body(_body())
    assert body_sha256(body) == hashlib.sha256(body.encode("utf-8")).hexdigest()
    other = render_body(_body(objective="A different objective."))
    assert body != other
    assert body_sha256(body) != body_sha256(other)
