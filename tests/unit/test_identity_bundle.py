"""The identity bundle (06): what a worker is told, and what it is never told."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from crucible.adapters.execution.identity import bundle_sha256, write_bundle
from crucible.contracts.completion_claim import CompletionClaimV1
from tests.fixtures import contract_document

POLICY = {
    "git": {
        "author_name": "crucible-worker",
        "author_email": "crucible-worker@users.noreply.github.com",
        "commit_trailer": "Crucible-Attempt",
        "work_branch_pattern": "crucible/*",
        "protected_branches": ["main"],
    },
    "limits": {"grace_seconds": 60, "stall_fail_seconds": 1800},
    "gates": {"pre_pr": ["report_present", "verification_ran"]},
}


def build(tmp_path: Path, **kw: object) -> tuple[str, str, Path]:
    directory = tmp_path / "identity"
    contract = contract_document()
    text, digest = write_bundle(
        directory,
        contract=contract,
        policy=POLICY,
        external_id="EX-0001",
        owner="foundry",
        work_branch="crucible/EX-0001",
        network_mode="policy",
        report_schema=CompletionClaimV1.model_json_schema(),
        **kw,  # type: ignore[arg-type]
    )
    return text, digest, directory


def test_the_bundle_has_the_files_06_names(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    names = {p.name for p in directory.iterdir()}
    assert {
        "IDENTITY.md",
        "contract.yaml",
        "contract.sha256",
        "policy.md",
        "report-schema.json",
        "history",
    } <= names


def test_the_contract_travels_verbatim_in_both_forms(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    as_yaml = yaml.safe_load((directory / "contract.yaml").read_text())
    as_json = json.loads((directory / "contract.json").read_text())
    assert as_yaml == as_json == contract_document()


def test_identity_md_states_the_boundaries_and_the_protocol(tmp_path: Path) -> None:
    text, _, _ = build(tmp_path)
    assert "You implement;\nyou do not redefine the task." in text
    assert "src/ledger/**" in text
    assert "/crucible/report" in text
    assert "exit 75" in text
    assert "No pushing at all" in text
    assert "crucible/EX-0001" in text
    # 06: the verification list, verbatim.
    assert "`make lint`" in text and "`make test`" in text


def test_the_bundle_carries_no_credential_and_no_token(tmp_path: Path) -> None:
    """06: credentials, the Crucible API token and GitHub tokens are never in it."""
    _, _, directory = build(tmp_path)
    body = "\n".join(p.read_text() for p in directory.rglob("*") if p.is_file()).lower()
    for forbidden in ("bearer ", "authorization:", "api_key", "password", "oauth"):
        assert forbidden not in body, forbidden


def test_the_hash_covers_every_file(tmp_path: Path) -> None:
    _, digest, directory = build(tmp_path)
    assert digest == bundle_sha256(directory)
    (directory / "history" / "note.md").write_text("changed", encoding="utf-8")
    assert bundle_sha256(directory) != digest


def test_history_entries_are_written_when_there_are_any(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path, history=[("attempt-1.md", "the first attempt said no")])
    assert (directory / "history" / "attempt-1.md").read_text() == "the first attempt said no"


def test_the_bundle_is_read_only(tmp_path: Path) -> None:
    _, _, directory = build(tmp_path)
    for path in directory.rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o222 == 0, path


def test_the_worker_is_told_the_report_format_version(tmp_path: Path) -> None:
    """hades #181: HT-0001's worker, told only "matching CompletionClaimV1", wrote
    schema_version: CompletionClaimV1 and the report did not parse. The prompt and
    the schema it points at both name the value."""
    text, _, directory = build(tmp_path)
    assert 'with `schema_version: "1.0"`' in text
    assert "not the schema's name" in text
    schema = json.loads((directory / "report-schema.json").read_text())
    version = schema["properties"]["schema_version"]
    assert version["examples"] == ["1.0"]
    assert version["pattern"] == r"^1\.[0-9]+$"
    assert "1.0" in version["description"]
