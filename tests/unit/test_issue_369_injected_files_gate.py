"""hades #369: `no_injected_files` fails on a shim the branch adds or fills with the
injected text, and passes a branch that edits or deletes the repository's own CLAUDE.md
or AGENTS.md. Evidence collected before #369 carries no status and is judged as before.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import read_path_changes
from crucible.domain.gates import (
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    evaluate_gate,
    injected_shim_text,
)
from crucible.ports.execution import IDENTITY_MOUNT, OUTPUT_MOUNT, REPORT_MOUNT, PathChange
from tests.fixtures import contract_document

ZERO = "0" * 40
OTHER = "b" * 40
SHIM_BLOB = hashlib.sha1(
    b"blob %d\0" % len(injected_shim_text() + "\n") + (injected_shim_text() + "\n").encode()
).hexdigest()


def _change(path: str, status: str, blob: str = OTHER) -> dict[str, str]:
    return {"path": path, "status": status, "blob": ZERO if status == "D" else blob}


def _outcome(
    diff: dict[str, Any], commit_paths: list[str], commit_changes: list[dict[str, str]] | None
) -> GateResult:
    bundle: dict[str, Any] = {"head_sha": "a" * 40, "commits": 1, "commit_paths": commit_paths}
    if commit_changes is not None:
        bundle["commit_changes"] = commit_changes
    evidence = (
        EvidenceItem(id=1, kind="diff_paths", source="crucible", verified=True, payload=diff),
        EvidenceItem(id=2, kind="bundle_head", source="crucible", verified=True, payload=bundle),
    )
    gi = GateInput(contract=contract_document(), policy={}, head_sha="a" * 40, evidence=evidence)
    return evaluate_gate(GateName.NO_INJECTED_FILES, gi).result


def _judge(changes: list[dict[str, str]]) -> GateResult:
    """The same changes as the diff and as one commit, as the collector records them."""
    paths = [c["path"] for c in changes]
    return _outcome({"paths": paths, "changes": changes}, sorted(paths), changes)


def test_deleting_the_bases_claude_md_and_agents_md_passes() -> None:
    assert _judge([_change("CLAUDE.md", "D"), _change("AGENTS.md", "D")]) is GateResult.PASS


def test_editing_the_bases_claude_md_and_agents_md_passes() -> None:
    changes = [_change("CLAUDE.md", "M"), _change("docs/AGENTS.md", "M")]
    assert _judge(changes) is GateResult.PASS


def test_adding_a_claude_md_the_base_lacks_fails() -> None:
    assert _judge([_change("CLAUDE.md", "A")]) is GateResult.FAIL
    assert _judge([_change("pkg/AGENTS.md", "A")]) is GateResult.FAIL


def test_a_file_whose_content_equals_the_injected_shim_fails() -> None:
    assert _judge([_change("AGENTS.md", "M", SHIM_BLOB)]) is GateResult.FAIL


def test_a_shim_committed_and_then_removed_still_fails() -> None:
    """Every commit is read, not only the diff: a shim added in one commit and deleted in
    the next leaves the diff empty but is still in the branch's history."""
    commits = [_change("AGENTS.md", "D"), _change("AGENTS.md", "A", SHIM_BLOB)]
    assert _outcome({"paths": [], "changes": []}, ["AGENTS.md"], commits) is GateResult.FAIL


def test_deleting_and_restoring_the_bases_file_passes() -> None:
    commits = [_change("CLAUDE.md", "A"), _change("CLAUDE.md", "D")]
    assert _outcome({"paths": [], "changes": []}, ["CLAUDE.md"], commits) is GateResult.PASS


def test_a_crucible_path_fails_whatever_its_status() -> None:
    for status in ("A", "M", "D"):
        assert _judge([_change(".crucible/identity.md", status)]) is GateResult.FAIL


def test_old_evidence_without_status_behaves_as_today() -> None:
    assert _outcome({"paths": ["CLAUDE.md"]}, [], None) is GateResult.FAIL
    assert _outcome({"paths": []}, ["AGENTS.md"], None) is GateResult.FAIL
    assert _outcome({"paths": ["src/a.py"]}, ["src/a.py"], None) is GateResult.PASS
    # A status for the diff does not excuse a commit list that has none.
    diff = {"paths": ["CLAUDE.md"], "changes": [_change("CLAUDE.md", "M")]}
    assert _outcome(diff, ["CLAUDE.md"], None) is GateResult.FAIL


def test_the_preparer_writes_the_shim_text_the_gate_knows() -> None:
    script = scripts.preparer_script(
        url="https://github.com/o/r.git",
        base_ref="main",
        work_branch="crucible/test",
        from_remote_branch=False,
        cache_name=None,
        author_name="crucible-worker",
        author_email="crucible-worker@users.noreply.github.com",
        origin_placeholder="crucible-no-remote://nowhere",
        claude_md_wins=True,
        shims=("AGENTS.md",),
        exclude_entries=("/AGENTS.md",),
        identity_mount=IDENTITY_MOUNT,
    )
    assert f"SHIM_TEXT='{injected_shim_text(IDENTITY_MOUNT)}'" in script


# ----- the collector records the status ---------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "crucible-worker@users.noreply.github.com")
    _git(repo, "config", "user.name", "crucible-worker")
    (repo / "CLAUDE.md").write_text("# the project's own\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# also the project's own\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    return repo


def _collect(tmp_path: Path, repo: Path) -> tuple[dict[str, Any], list[str], list[Any]]:
    output = tmp_path / "output"
    report = tmp_path / "report"
    output.mkdir()
    report.mkdir()
    generated = scripts.collector_script(
        base_ref="main", work_branch="crucible/test", size_cap_bytes=1024
    )
    generated = generated.replace(scripts.REPO_MOUNT, str(repo))
    generated = generated.replace(OUTPUT_MOUNT, str(output))
    generated = generated.replace(REPORT_MOUNT, str(report))
    result = subprocess.run(["sh", "-c", generated], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    diff_changes = read_path_changes(output / "diff-raw.txt")
    commit_changes = read_path_changes(output / "commit-raw.txt")
    assert diff_changes is not None and commit_changes is not None

    def payload(changes: tuple[PathChange, ...]) -> list[dict[str, str]]:
        return [{"path": c.path, "status": c.status, "blob": c.blob} for c in changes]

    paths = (output / "changed.txt").read_text().split()
    commit_paths = (output / "commit-paths.txt").read_text().split()
    return {"paths": paths, "changes": payload(diff_changes)}, commit_paths, payload(commit_changes)


def test_the_collector_lets_a_branch_delete_and_edit_its_own_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", "AGENTS.md")
    (repo / "CLAUDE.md").write_text("# edited\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "edit and delete")
    diff, commit_paths, commits = _collect(tmp_path, repo)
    assert {(c["path"], c["status"]) for c in diff["changes"]} == {
        ("AGENTS.md", "D"),
        ("CLAUDE.md", "M"),
    }
    assert _outcome(diff, commit_paths, commits) is GateResult.PASS
    # The same collection, judged without the status, fails as before #369.
    assert _outcome({"paths": diff["paths"]}, commit_paths, None) is GateResult.FAIL


def test_the_collector_catches_an_added_or_shim_filled_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(injected_shim_text() + "\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "CLAUDE.md").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "shim and new file")
    diff, commit_paths, commits = _collect(tmp_path, repo)
    assert {c["blob"] for c in diff["changes"] if c["path"] == "AGENTS.md"} == {SHIM_BLOB}
    assert _outcome(diff, commit_paths, commits) is GateResult.FAIL
    assert _outcome({"paths": ["AGENTS.md"], "changes": diff["changes"][:1]}, [], []) is (
        GateResult.FAIL
    )
