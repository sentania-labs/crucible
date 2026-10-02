"""hades #369: `no_injected_files` fails on a shim the branch adds or fills with the
injected text, and passes a branch that edits or deletes the repository's own CLAUDE.md
or AGENTS.md. Evidence collected before #369 carries no status and is judged as before.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from crucible.adapters.execution import scripts
from crucible.adapters.execution.collected import read_outputs, read_path_changes
from crucible.application.evidence import record_collection_evidence
from crucible.domain.entities import Attempt, EvidenceRecord, Task
from crucible.domain.gates import (
    EvidenceItem,
    GateInput,
    GateName,
    GateResult,
    evaluate_gate,
    injected_shim_text,
)
from crucible.domain.lifecycle import AttemptState, TaskState
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    REPORT_MOUNT,
    CollectedOutputs,
    LaunchSpec,
)
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


# ----- through the real collector, read_outputs and record_collection_evidence ------

NOW = datetime(2026, 10, 2, tzinfo=UTC)
SHIM = injected_shim_text() + "\n"


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout


def _write(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, date: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message, date=date)


def _repo(tmp_path: Path, *, agents_md: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "crucible-worker@users.noreply.github.com")
    _git(repo, "config", "user.name", "crucible-worker")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "CLAUDE.md", "# the project's own\n")
    if agents_md:
        _write(repo, "AGENTS.md", "# also the project's own\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "crucible/test")
    return repo


def _evidence(tmp_path: Path, repo: Path) -> tuple[EvidenceItem, ...]:
    """Collect `repo` with the collector script, read it back as the providers do, and
    record it as the supervisor does; the gate reads what was recorded."""
    output, report = tmp_path / "output", tmp_path / "report"
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
    spec = LaunchSpec(
        attempt_id="01ATTEMPT",
        task_id="01TASK",
        external_id="EX-0001",
        role="implement",
        harness="script-harness",
        model="none",
        image="crucible-worker:test",
        timeout_seconds=600,
        contract=contract_document(),
    )
    read = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=True,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    outputs = CollectedOutputs(
        report=read.report,
        report_raw=read.report_raw,
        blocked_md=read.blocked_md,
        stdout_tail=read.stdout_tail,
        stderr_tail=read.stderr_tail,
        diff_paths=read.diff_paths,
        diff_text=read.diff_text,
        diff_changes=read.diff_changes,
        base_paths=read.base_paths,
        bundle=read.bundle,
        artifacts=read.artifacts,
    )
    recorded: list[EvidenceRecord] = []

    def add(record: EvidenceRecord) -> EvidenceRecord:
        recorded.append(record)
        return record

    uow = MagicMock()
    uow.evidence.add.side_effect = add
    clock = MagicMock()
    clock.now.return_value = NOW
    attempt = Attempt(
        id="01ATTEMPT",
        execution_id="01EXEC",
        task_id="01TASK",
        number=1,
        state=AttemptState.RUNNING,
        created_at=NOW,
    )
    task = Task(
        id="01TASK",
        external_id="EX-0001",
        principal_id="01PRINCIPAL",
        project="hades",
        title="t",
        state=TaskState.RUNNING,
        contract_version=1,
        policy_name="p",
        policy_version=1,
        repository_id="01REPO",
        created_at=NOW,
        updated_at=NOW,
    )
    record_collection_evidence(
        uow,
        clock,
        MagicMock(),
        attempt=attempt,
        task=task,
        outputs=outputs,
        claim=None,
        claim_parsed_ok=False,
        parse_errors=[],
    )
    return tuple(
        EvidenceItem(id=n, kind=r.kind, source=r.source, verified=r.verified, payload=r.payload)
        for n, r in enumerate(recorded, start=1)
    )


def _collected(tmp_path: Path, repo: Path) -> tuple[GateResult, dict[str, Any], dict[str, Any]]:
    """The gate's answer on the recorded evidence, and the diff_paths and bundle_head
    payloads it read."""
    evidence = _evidence(tmp_path, repo)
    gi = GateInput(contract=contract_document(), policy={}, head_sha="a" * 40, evidence=evidence)
    payloads = {e.kind: e.payload for e in evidence}
    result = evaluate_gate(GateName.NO_INJECTED_FILES, gi).result
    return result, payloads["diff_paths"], payloads["bundle_head"]


def test_the_collector_lets_a_branch_delete_and_edit_its_own_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", "AGENTS.md")
    _write(repo, "CLAUDE.md", "# edited\n")
    _commit(repo, "edit and delete")
    result, diff, _ = _collected(tmp_path, repo)
    assert {(c["path"], c["status"]) for c in diff["changes"]} == {
        ("AGENTS.md", "D"),
        ("CLAUDE.md", "M"),
    }
    assert result is GateResult.PASS


def test_the_collector_lets_a_branch_delete_and_restore_its_own_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "rm", "-q", "CLAUDE.md")
    _commit(repo, "delete")
    _write(repo, "CLAUDE.md", "# back\n")
    _commit(repo, "restore")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


def test_merging_a_base_that_added_agents_md_passes(tmp_path: Path) -> None:
    """A merge of the base shows the base's new AGENTS.md as an add against the merge's
    first parent; the merge base has it, so it is the repository's own."""
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "src/a.py", "a = 1\n")
    _commit(repo, "work")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "AGENTS.md", "# the project's new own\n")
    _commit(repo, "base adds AGENTS.md")
    _git(repo, "checkout", "-q", "crucible/test")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert _collected(tmp_path, repo)[0] is GateResult.PASS


def test_the_collector_catches_an_added_or_shim_filled_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "AGENTS.md", SHIM)
    _write(repo, "sub/CLAUDE.md", "new\n")
    _commit(repo, "shim and new file")
    result, diff, _ = _collected(tmp_path, repo)
    assert {c["blob"] for c in diff["changes"] if c["path"] == "AGENTS.md"} == {SHIM_BLOB}
    assert result is GateResult.FAIL


def _side_merge(repo: Path) -> None:
    """Start a no-commit merge of a side branch, as a worker resolving a merge would."""
    _git(repo, "checkout", "-q", "-b", "side", "main")
    _write(repo, "side.txt", "side\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "crucible/test")
    _write(repo, "src/a.py", "a = 1\n")
    _commit(repo, "work")
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")


def test_a_shim_a_merge_adds_and_a_later_commit_deletes_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, agents_md=False)
    _side_merge(repo)
    _write(repo, "AGENTS.md", SHIM)
    _commit(repo, "merge side")
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "drop it")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


def test_a_merge_overwriting_the_bases_claude_md_with_the_shim_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _side_merge(repo)
    _write(repo, "CLAUDE.md", SHIM)
    _commit(repo, "merge side")
    _write(repo, "CLAUDE.md", "# the project's own, edited\n")
    _commit(repo, "edit it")
    assert _collected(tmp_path, repo)[0] is GateResult.FAIL


def test_a_backdated_add_edit_delete_of_a_new_agents_md_around_a_merge_fails(
    tmp_path: Path,
) -> None:
    """By commit date the add sorts before the edit, so the edit would look like the
    oldest record and the base would seem to own AGENTS.md; the graph order says not."""
    repo = _repo(tmp_path, agents_md=False)
    _write(repo, "AGENTS.md", "# notes, not a shim\n")
    _commit(repo, "add", date="2030-01-01T00:00:00Z")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, "side.txt", "side\n")
    _commit(repo, "side", date="2030-01-02T00:00:00Z")
    _git(repo, "checkout", "-q", "crucible/test")
    _write(repo, "AGENTS.md", "# notes, edited\n")
    _commit(repo, "edit", date="2000-01-01T00:00:00Z")
    _git(repo, "merge", "-q", "--no-edit", "side", date="2030-01-03T00:00:00Z")
    _git(repo, "rm", "-q", "AGENTS.md")
    _commit(repo, "delete", date="2030-01-04T00:00:00Z")
    result, _, bundle = _collected(tmp_path, repo)
    assert [c["status"] for c in bundle["commit_changes"]][-1] == "A"
    assert result is GateResult.FAIL


def test_a_shim_in_a_non_ascii_directory_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "d\u00ef/CLAUDE.md", SHIM)
    _commit(repo, "non-ascii")
    result, diff, _ = _collected(tmp_path, repo)
    assert {c["path"] for c in diff["changes"]} == {"d\u00ef/CLAUDE.md"}
    assert result is GateResult.FAIL


def test_a_symlink_replacing_the_bases_claude_md_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").unlink()
    (repo / "CLAUDE.md").symlink_to(f"{IDENTITY_MOUNT}/IDENTITY.md")
    _commit(repo, "link it")
    result, diff, _ = _collected(tmp_path, repo)
    assert [(c["path"], c["status"]) for c in diff["changes"]] == [("CLAUDE.md", "T")]
    assert result is GateResult.FAIL


def test_the_raw_reader_never_reads_a_path_as_a_record(tmp_path: Path) -> None:
    """A path shaped like a raw record is still a path, and the next record still reads."""
    meta = f":000000 100644 {ZERO} {OTHER} A"
    raw = tmp_path / "raw.txt"
    raw.write_bytes(f"{meta}\0{meta}\0{meta}\0CLAUDE.md\0".encode())
    changes = read_path_changes(raw)
    assert changes is not None
    assert [c.path for c in changes] == [meta, "CLAUDE.md"]
