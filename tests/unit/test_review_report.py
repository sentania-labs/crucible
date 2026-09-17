"""ReviewReportV1 parsing (11)."""

from __future__ import annotations

from typing import Any

from crucible.adapters.execution.fake import default_review_report, synthetic_head_sha
from crucible.contracts.review_report import ReviewReportV1, parse_review_report
from crucible.ports.execution import LaunchSpec
from tests.fixtures import contract_document

HEAD = "c" * 40


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "task_external_id": "EX-0001",
        "reviewed_head_sha": HEAD,
        "reviewer": {"kind": "orchestrator", "principal": "foundry"},
        "verdict": "approve",
        "findings": [],
        "summary": "Nothing to change.",
    }
    document.update(overrides)
    return document


def test_valid_report_parses() -> None:
    report, errors = parse_review_report(_document())
    assert errors == [] and report is not None
    assert report.reviewed_head_sha == HEAD and report.verdict == "approve"


def test_findings_carry_severity_and_location() -> None:
    report, errors = parse_review_report(
        _document(
            verdict="request_changes",
            findings=[{"severity": "major", "path": "src/a.py", "line": 42, "text": "narrow it"}],
        )
    )
    assert errors == [] and report is not None
    assert report.findings[0].severity == "major" and report.findings[0].line == 42


def test_non_mapping_is_an_error_not_an_exception() -> None:
    report, errors = parse_review_report("not a report")
    assert report is None and errors[0]["msg"] == "review report is not a mapping"


def test_unknown_field_rejected() -> None:
    report, errors = parse_review_report(_document(surprise=1))
    assert report is None and any("surprise" in e["loc"] for e in errors)


def test_unknown_verdict_rejected() -> None:
    report, errors = parse_review_report(_document(verdict="lgtm"))
    assert report is None and any("verdict" in e["loc"] for e in errors)


def test_orchestrator_reviewer_must_name_a_principal() -> None:
    report, errors = parse_review_report(_document(reviewer={"kind": "orchestrator"}))
    assert report is None and any("principal" in e["msg"] for e in errors)


def test_execution_reviewer_must_name_an_attempt() -> None:
    report, errors = parse_review_report(_document(reviewer={"kind": "crucible_review_execution"}))
    assert report is None and any("attempt_id" in e["msg"] for e in errors)


def test_major_schema_version_is_checked() -> None:
    report, errors = parse_review_report(_document(schema_version="2.0"))
    assert report is None and any("schema_version" in e["loc"] for e in errors)


def test_the_fake_reviewer_produces_a_valid_report() -> None:
    spec = LaunchSpec(
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        task_id="t",
        external_id="EX-0001",
        role="review",
        harness="codex",
        model="gpt-5.6-luna",
        image="crucible-worker:fake-review",
        timeout_seconds=60,
        contract=contract_document(),
    )
    head = synthetic_head_sha("some-implement-attempt")
    for verdict in ("approve", "request_changes"):
        document = default_review_report(spec, head, verdict)
        report = ReviewReportV1.model_validate(document)
        assert report.reviewed_head_sha == head
        assert report.reviewer.attempt_id == spec.attempt_id
        assert report.verdict == verdict
