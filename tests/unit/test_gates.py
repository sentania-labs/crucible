"""Every pre-PR gate against synthetic evidence (18), including the rule that
worker-asserted evidence never satisfies a gate (11)."""

from __future__ import annotations

from typing import Any

import pytest

from crucible.domain.gates import (
    DEFERRED_MARKER,
    DEFERRED_TO_C3,
    PRE_PR_EVALUATORS,
    PRE_PR_GATES,
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    blocking,
    evaluate_gate,
    evaluate_pre_pr,
    waiting_for_review,
)
from tests.fixtures import contract_document

HEAD = "a" * 40


def _ev(
    kind: str,
    payload: dict[str, Any],
    *,
    ident: int = 1,
    source: str = "crucible",
    verified: bool = True,
) -> EvidenceItem:
    return EvidenceItem(id=ident, kind=kind, source=source, verified=verified, payload=payload)


def _claim_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "completion_claim",
        "parsed_ok": True,
        "parse_errors": [],
        "claimed_head_sha": HEAD,
        "mapped_criteria": [{"id": "AC1", "status": "met"}, {"id": "AC2", "status": "met"}],
        "run_evidence": ["report/run-evidence.md"],
        "changed_files": ["src/ledger/fake_change.py"],
    }
    payload.update(overrides)
    return payload


def _passing_evidence() -> list[EvidenceItem]:
    return [
        _ev("exit_info", {"exit_code": 0, "exit_class": "completed"}, ident=1),
        _ev("artifact_present", _claim_payload(), ident=2),
        _ev(
            "bundle_head",
            {
                "head_sha": HEAD,
                "claimed_head_sha": HEAD,
                "commits": 2,
                "bundle_verified": True,
                "commit_paths": ["src/ledger/fake_change.py"],
                "commit_messages": ["fix the import"],
            },
            ident=3,
        ),
        _ev(
            "diff_paths",
            {"paths": ["src/ledger/fake_change.py", "tests/ledger/fake_change.py"]},
            ident=4,
        ),
        _ev(
            "scanner_result",
            {"findings": [], "scanned": ["report", "diff:src/ledger/a.py"]},
            ident=5,
        ),
        _ev(
            "artifact_present",
            {"role": "run_evidence", "path": "report/run-evidence.md", "size": 42},
            ident=6,
        ),
        _ev(
            "review_received",
            {
                "reviewed_head_sha": HEAD,
                "reviewer_kind": "orchestrator",
                "reviewer_is_author": False,
                "verdict": "approve",
            },
            ident=7,
        ),
    ]


def _gi(evidence: list[EvidenceItem], **kw: Any) -> GateInput:
    return GateInput(
        contract=kw.pop("contract", contract_document()),
        policy=kw.pop("policy", {}),
        head_sha=kw.pop("head_sha", HEAD),
        evidence=tuple(evidence),
        internal_review_required=kw.pop("internal_review_required", True),
    )


def test_every_pre_pr_gate_has_an_evaluator() -> None:
    assert set(PRE_PR_EVALUATORS) == set(PRE_PR_GATES)


def test_all_pass_on_a_clean_run() -> None:
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(_passing_evidence()))
    assert blocking(outcomes) == []
    assert not waiting_for_review(outcomes)
    for gate, outcome in outcomes.items():
        if gate in DEFERRED_TO_C3:
            assert outcome.result is GateResult.PENDING
            assert DEFERRED_MARKER in outcome.detail
        else:
            assert outcome.result is GateResult.PASS, (gate, outcome.detail)


def test_deferred_gates_are_never_pass() -> None:
    """verification_ran and workspace_clean need the verifier container (C3, 20)."""
    for gate in DEFERRED_TO_C3:
        for evidence in ([], _passing_evidence()):
            outcome = evaluate_gate(gate, _gi(list(evidence)))
            assert outcome.result is GateResult.PENDING
            assert DEFERRED_MARKER in outcome.detail


def test_worker_asserted_evidence_never_satisfies_a_gate() -> None:
    """11: a worker's own claim is shown to Foundry and consumed by nothing."""
    worker_only = [
        EvidenceItem(id=e.id, kind=e.kind, source="worker", verified=False, payload=e.payload)
        for e in _passing_evidence()
    ]
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES), _gi(worker_only))
    assert not any(o.result is GateResult.PASS for o in outcomes.values())
    assert GateName.EXIT_CLEAN in blocking(outcomes)
    assert GateName.REPORT_PRESENT in blocking(outcomes)


def test_verified_flag_alone_does_not_admit_a_worker_row() -> None:
    forged = EvidenceItem(
        id=9, kind="exit_info", source="worker", verified=True, payload={"exit_code": 0}
    )
    assert forged.admissible is False
    assert evaluate_gate(GateName.EXIT_CLEAN, _gi([forged])).result is GateResult.FAIL


def test_missing_evidence_fails_rather_than_waits() -> None:
    """09: a failed attempt still reaches `reported`, and its gates then fail."""
    outcomes = evaluate_pre_pr(sorted(PRE_PR_GATES - DEFERRED_TO_C3), _gi([]))
    assert GateName.EXIT_CLEAN in blocking(outcomes)
    assert GateName.COMMITS_PRESENT in blocking(outcomes)
    assert outcomes[GateName.INTERNAL_REVIEW_RECORDED].result is GateResult.PENDING


def test_report_present_fails_when_the_claim_did_not_parse() -> None:
    evidence = _passing_evidence()
    evidence[1] = _ev(
        "artifact_present",
        _claim_payload(parsed_ok=False, parse_errors=[{"loc": ["summary"], "msg": "missing"}]),
        ident=2,
    )
    assert evaluate_gate(GateName.REPORT_PRESENT, _gi(evidence)).result is GateResult.FAIL
    assert evaluate_gate(GateName.CRITERIA_MAPPED, _gi(evidence)).result is GateResult.FAIL


def test_exit_clean_fails_on_a_non_zero_code() -> None:
    evidence = _passing_evidence()
    evidence[0] = _ev("exit_info", {"exit_code": 1, "exit_class": "crashed"}, ident=1)
    outcome = evaluate_gate(GateName.EXIT_CLEAN, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "1" in outcome.detail


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"commits": 0}, "no commit"),
        ({"bundle_verified": False}, "bundle verify"),
        ({"head_sha": "b" * 40}, "does not equal"),
    ],
)
def test_commits_present_failures(patch: dict[str, Any], reason: str) -> None:
    evidence = _passing_evidence()
    payload = dict(evidence[2].payload)
    payload.update(patch)
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.COMMITS_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and reason in outcome.detail


def test_scope_contained_fails_outside_allowed_paths() -> None:
    evidence = _passing_evidence()
    evidence[3] = _ev(
        "diff_paths", {"paths": ["src/ledger/a.py", "infrastructure/out.txt"]}, ident=4
    )
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "infrastructure/out.txt" in outcome.detail


def test_scope_contained_fails_on_a_prohibited_path() -> None:
    contract = contract_document()
    contract["scope"]["allowed_paths"] = ["**"]
    contract["scope"]["prohibited_paths"] = [".github/**"]
    evidence = _passing_evidence()
    evidence[3] = _ev("diff_paths", {"paths": [".github/workflows/ci.yml"]}, ident=4)
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, _gi(evidence, contract=contract))
    assert outcome.result is GateResult.FAIL and "prohibited_paths" in outcome.detail


def test_no_injected_files_sees_the_commit_list_as_well_as_the_diff() -> None:
    evidence = _passing_evidence()
    payload = dict(evidence[2].payload)
    payload["commit_paths"] = [".crucible/identity.md"]
    evidence[2] = _ev("bundle_head", payload, ident=3)
    outcome = evaluate_gate(GateName.NO_INJECTED_FILES, _gi(evidence))
    assert outcome.result is GateResult.FAIL and ".crucible/identity.md" in outcome.detail


def test_no_secrets_reports_the_pattern_not_the_value() -> None:
    evidence = _passing_evidence()
    evidence[4] = _ev(
        "scanner_result",
        {"findings": [{"where": "report.summary", "pattern": "github_token"}], "scanned": []},
        ident=5,
    )
    outcome = evaluate_gate(GateName.NO_SECRETS, _gi(evidence))
    assert outcome.result is GateResult.FAIL
    assert "github_token" in outcome.detail and "ghp_" not in outcome.detail


def test_run_evidence_present_fails_when_missing_or_empty() -> None:
    evidence = _passing_evidence()
    del evidence[5]
    assert evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence)).result is GateResult.FAIL
    evidence.append(
        _ev(
            "artifact_present",
            {"role": "run_evidence", "path": "report/run-evidence.md", "size": 0},
            ident=8,
        )
    )
    outcome = evaluate_gate(GateName.RUN_EVIDENCE_PRESENT, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "empty" in outcome.detail


def test_run_evidence_present_is_skipped_without_an_artifact_verification() -> None:
    contract = contract_document()
    contract["required_verification"] = [
        v for v in contract["required_verification"] if "path" not in v
    ]
    outcome = evaluate_gate(
        GateName.RUN_EVIDENCE_PRESENT, _gi(_passing_evidence(), contract=contract)
    )
    assert outcome.result is GateResult.SKIPPED


def test_criteria_mapped_fails_on_an_unmapped_criterion() -> None:
    evidence = _passing_evidence()
    evidence[1] = _ev(
        "artifact_present",
        _claim_payload(mapped_criteria=[{"id": "AC1", "status": "met"}]),
        ident=2,
    )
    outcome = evaluate_gate(GateName.CRITERIA_MAPPED, _gi(evidence))
    assert outcome.result is GateResult.FAIL and "AC2" in outcome.detail


def test_dependencies_and_ci_gates_respect_the_contract_flags() -> None:
    evidence = _passing_evidence()
    evidence[3] = _ev("diff_paths", {"paths": ["uv.lock", ".github/workflows/ci.yml"]}, ident=4)
    assert evaluate_gate(GateName.DEPENDENCIES_UNCHANGED, _gi(evidence)).result is GateResult.FAIL
    assert evaluate_gate(GateName.CI_UNCHANGED, _gi(evidence)).result is GateResult.FAIL
    permissive = contract_document()
    permissive["scope"]["may_add_dependencies"] = True
    permissive["scope"]["may_modify_ci"] = True
    gi = _gi(evidence, contract=permissive)
    assert evaluate_gate(GateName.DEPENDENCIES_UNCHANGED, gi).result is GateResult.SKIPPED
    assert evaluate_gate(GateName.CI_UNCHANGED, gi).result is GateResult.SKIPPED


def test_internal_review_gate_waits_then_passes() -> None:
    evidence = [e for e in _passing_evidence() if e.kind != "review_received"]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(evidence)).result is GateResult.PENDING
    )
    assert (
        evaluate_gate(
            GateName.INTERNAL_REVIEW_RECORDED, _gi(evidence, internal_review_required=False)
        ).result
        is GateResult.SKIPPED
    )
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(_passing_evidence())).result
        is GateResult.PASS
    )


def test_internal_review_gate_ignores_the_author_and_another_head() -> None:
    base = [e for e in _passing_evidence() if e.kind != "review_received"]
    author = [
        *base,
        _ev(
            "review_received",
            {"reviewed_head_sha": HEAD, "reviewer_is_author": True, "verdict": "approve"},
            ident=7,
        ),
    ]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(author)).result is GateResult.PENDING
    )
    other_head = [
        *base,
        _ev(
            "review_received",
            {"reviewed_head_sha": "b" * 40, "reviewer_is_author": False, "verdict": "approve"},
            ident=7,
        ),
    ]
    assert (
        evaluate_gate(GateName.INTERNAL_REVIEW_RECORDED, _gi(other_head)).result
        is GateResult.PENDING
    )


def test_an_evaluator_that_raises_is_error_not_an_exception() -> None:
    broken = _gi([_ev("diff_paths", {"paths": [None]}, ident=4)], contract={"scope": None})
    outcome = evaluate_gate(GateName.SCOPE_CONTAINED, broken)
    assert outcome.result is GateResult.ERROR
    assert blocking({GateName.SCOPE_CONTAINED: outcome}) == [GateName.SCOPE_CONTAINED]


def test_unknown_gate_is_error() -> None:
    assert evaluate_gate("no_such_gate", _gi([])).result is GateResult.ERROR
