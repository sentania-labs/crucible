"""The pull request title and body (23): what may appear, and what may not."""

from __future__ import annotations

import pytest

from crucible.domain.publication import (
    MAX_TITLE_LENGTH,
    BodyInput,
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
    body = render_body(_body())
    assert len(body_sha256(body)) == 64
    assert body_sha256(body) == body_sha256(body)
