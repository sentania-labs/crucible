from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from crucible.contracts.task_contract import TaskContractV1, contract_sha256
from tests.fixtures import contract_document


def _errors(doc: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        TaskContractV1.model_validate(doc)
    return [".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.value.errors()]


def test_example_validates() -> None:
    contract = TaskContractV1.model_validate(contract_document())
    assert contract.external_id == "EX-0001"
    assert contract.verification_commands == ["make lint", "make test", "make scan"]


def test_unknown_field_rejected() -> None:
    errs = _errors(contract_document(surprise=1))
    assert any("surprise" in e for e in errs)


def test_nested_unknown_field_rejected() -> None:
    doc = contract_document()
    doc["repository"]["token"] = "x"
    assert any("repository.token" in e for e in _errors(doc))


def test_missing_field_rejected() -> None:
    doc = contract_document()
    del doc["escalation"]
    assert any(e.startswith("escalation") for e in _errors(doc))


@pytest.mark.parametrize("version", ["2.0", "0.9", "1", "abc"])
def test_unsupported_major_version(version: str) -> None:
    assert any("schema_version" in e for e in _errors(contract_document(schema_version=version)))


def test_minor_version_accepted() -> None:
    TaskContractV1.model_validate(contract_document(schema_version="1.3"))


def test_duplicate_acceptance_ids() -> None:
    doc = contract_document()
    doc["acceptance_criteria"].append({"id": "AC1", "text": "dup"})
    assert any("acceptance_criteria ids" in e for e in _errors(doc))


def test_duplicate_verification_ids() -> None:
    doc = contract_document()
    doc["required_verification"].append({"id": "V1", "command": "make x"})
    assert any("required_verification ids" in e for e in _errors(doc))


@pytest.mark.parametrize("pattern", ["/abs/**", "../escape", "src/[abc", " padded"])
def test_invalid_globs(pattern: str) -> None:
    doc = contract_document()
    doc["scope"]["allowed_paths"] = [pattern]
    assert any("scope.allowed_paths" in e for e in _errors(doc))


def test_overlapping_globs() -> None:
    doc = contract_document()
    doc["scope"]["prohibited_paths"] = ["src/ledger/**"]
    assert any("overlap" in e for e in _errors(doc))


def test_secret_in_string_field_rejected_by_path_only() -> None:
    doc = contract_document(objective="use ghp_" + "z" * 36 + " to push")
    errs = _errors(doc)
    assert any("github_token" in e and "objective" in e for e in errs)
    assert not any("zzzz" in e for e in errs)


def test_secret_in_nested_list_rejected() -> None:
    doc = contract_document()
    doc["constraints"]["prohibited_actions"].append("-----BEGIN PRIVATE KEY-----")
    assert any(
        "constraints.prohibited_actions[0]" in e or "private_key_header" in e for e in _errors(doc)
    )


def test_report_dir_fixed() -> None:
    doc = contract_document()
    doc["reporting"]["report_dir"] = "/repo/report"
    assert any("reporting.report_dir" in e for e in _errors(doc))


def test_retry_on_must_be_exit_classes() -> None:
    doc = contract_document()
    doc["lifecycle"]["retry_on"] = ["gate_failure"]
    assert any("lifecycle.retry_on" in e for e in _errors(doc))


def test_deliverable_needs_target() -> None:
    doc = contract_document(deliverables=[{"kind": "branch"}])
    assert any("deliverables" in e and "target" in e for e in _errors(doc))


def test_unknown_harness_and_provider() -> None:
    doc = contract_document()
    doc["execution_request"]["harness"] = "cursor"
    assert any("execution_request.harness" in e for e in _errors(doc))
    doc = contract_document()
    doc["execution_request"]["provider"] = "lambda"
    assert any("execution_request.provider" in e for e in _errors(doc))


def test_sha256_is_canonical() -> None:
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert contract_sha256(a) == contract_sha256(b)
    assert len(contract_sha256(a)) == 64
