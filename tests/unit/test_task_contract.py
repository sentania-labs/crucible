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


def _pinned_document() -> dict[str, Any]:
    doc = contract_document()
    doc["execution_request"].update(
        {
            "harness": "codex",
            "model": "gpt-5.6-luna",
            "pin_reason": "unit test operator pin",
        }
    )
    return doc


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
    # Assembled at runtime: .gitleaks.toml now carries the scanner's own header pattern,
    # and a committed literal would be a finding (12).
    doc["constraints"]["prohibited_actions"].append("-----BEGIN " + "PRIVATE KEY-----")
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


def test_class_request_omits_model_harness_and_image() -> None:
    doc = contract_document()
    for field in ("model", "harness", "pin_reason", "image"):
        doc["execution_request"].pop(field, None)
    contract = TaskContractV1.model_validate(doc)
    assert contract.execution_request.pinned_model is None
    assert contract.execution_request.pinned_harness is None


def test_harness_without_model_is_refused() -> None:
    doc = _pinned_document()
    doc["execution_request"].pop("model")
    doc["execution_request"].pop("pin_reason")
    assert any("harness without model" in error for error in _errors(doc))


def test_model_without_harness_is_refused() -> None:
    doc = _pinned_document()
    doc["execution_request"].pop("harness")
    assert any("pinned model must name its harness" in error for error in _errors(doc))


def test_model_without_pin_reason_is_refused() -> None:
    doc = _pinned_document()
    doc["execution_request"].pop("pin_reason")
    assert any("pinned model requires pin_reason" in error for error in _errors(doc))


def test_real_provider_refuses_supplied_image() -> None:
    doc = contract_document()
    doc["execution_request"]["provider"] = "docker"
    assert any("image is derived" in error for error in _errors(doc))


def test_nested_operator_pin_is_supported_but_may_not_mix_with_flat_fields() -> None:
    doc = _pinned_document()
    request = doc["execution_request"]
    request["pin"] = {
        "harness": request.pop("harness"),
        "model": request.pop("model"),
        "pin_reason": request.pop("pin_reason"),
    }
    contract = TaskContractV1.model_validate(doc)
    assert contract.execution_request.pinned_model == "gpt-5.6-luna"
    request["model"] = "gpt-5.6-luna"
    assert any("may not be combined" in error for error in _errors(doc))


def test_sha256_is_canonical() -> None:
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert contract_sha256(a) == contract_sha256(b)
    assert len(contract_sha256(a)) == 64
