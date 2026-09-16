from __future__ import annotations

from crucible.adapters.execution.fake import default_report
from crucible.contracts.completion_claim import parse_claim
from crucible.ports.execution import LaunchSpec
from tests.fixtures import contract_document


def _spec() -> LaunchSpec:
    return LaunchSpec(
        attempt_id="a",
        task_id="t",
        external_id="EX-0001",
        harness="codex",
        model="m",
        image="crucible-worker:fake-succeed",
        timeout_seconds=60,
        contract=contract_document(),
    )


def test_default_report_parses() -> None:
    claim, errors = parse_claim(default_report(_spec()))
    assert errors == []
    assert claim is not None
    assert [m.id for m in claim.acceptance_mapping] == ["AC1", "AC2"]
    assert [c.command for c in claim.checks] == ["make lint", "make test", "make scan"]


def test_missing_field_is_an_error_not_an_exception() -> None:
    doc = default_report(_spec())
    del doc["limitations"]
    claim, errors = parse_claim(doc)
    assert claim is None
    assert errors and errors[0]["loc"] == ["limitations"]


def test_unknown_field_rejected() -> None:
    doc = default_report(_spec())
    doc["pushed"] = True
    claim, errors = parse_claim(doc)
    assert claim is None
    assert any(e["loc"] == ["pushed"] for e in errors)


def test_non_mapping() -> None:
    claim, errors = parse_claim("not yaml mapping")
    assert claim is None and errors[0]["msg"] == "report is not a mapping"
