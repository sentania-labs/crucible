from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crucible.adapters.execution.collected import read_outputs
from crucible.adapters.execution.scripts import collector_script
from crucible.adapters.ui.pages.tasks import _attempt_diff_link
from crucible.application.evidence import _scanner_findings
from crucible.ports.execution import CollectedArtifact, CollectedOutputs, LaunchSpec


def _run_collector(tmp_path: Path, *, cap: int = 1024) -> Path:
    repo = tmp_path / "repo"
    report = tmp_path / "worker-report"
    output = tmp_path / "output"
    for path in (repo, report, output):
        path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "work.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "checkout", "-qb", "crucible/test"], cwd=repo, check=True)
    (repo / "work.txt").write_text("changed\n" + "x" * 4096, encoding="utf-8")
    subprocess.run(["git", "add", "work.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "change",
        ],
        cwd=repo,
        check=True,
    )
    script = collector_script(base_ref="main", work_branch="crucible/test", size_cap_bytes=cap)
    script = (
        script.replace("/crucible/repo", str(repo))
        .replace("/crucible/report", str(report))
        .replace("/crucible/out", str(output))
    )
    result = subprocess.run(["sh", "-c", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return output


def test_collector_exposes_a_bounded_diff_with_stat_and_truncation_marker(tmp_path: Path) -> None:
    output = _run_collector(tmp_path, cap=512)
    review = (output / "report" / "diff.patch").read_text()
    assert "work.txt |" in review
    assert "diff --git" in review
    assert "[crucible: diff truncated]" in review
    assert len(review.encode()) <= 512
    assert (output / "diff.patch").stat().st_size > len(review.encode())


def test_collected_review_diff_is_a_diff_artifact(tmp_path: Path) -> None:
    output = _run_collector(tmp_path)
    spec = LaunchSpec(
        attempt_id="A1",
        task_id="T1",
        external_id="X1",
        role="implement",
        harness="test",
        model="test",
        image="test",
        timeout_seconds=1,
        contract={"repository": {"base_ref": "main", "work_branch": "crucible/test"}},
    )
    outputs = read_outputs(
        output,
        tmp_path / "verify",
        spec=spec,
        bundle_verified=False,
        collector_exit=0,
        verifications=(),
        tail_bytes=1024,
    )
    artifact = next(a for a in outputs.artifacts if a.name == "report/diff.patch")
    assert artifact.type == "diff"
    assert artifact.content_type == "text/x-diff"


def test_review_diff_is_scanned_like_every_other_artifact() -> None:
    secret = ("gh" + "p_" + "a" * 36).encode()
    outputs = CollectedOutputs(
        report=None,
        report_raw=None,
        blocked_md=None,
        artifacts=(
            CollectedArtifact(
                name="report/diff.patch",
                type="diff",
                content=b"+token=" + secret,
                content_type="text/x-diff",
            ),
        ),
    )
    assert _scanner_findings(outputs, None) == [
        {"where": "artifact:report/diff.patch", "pattern": "github_token"}
    ]


def test_task_view_diff_link_targets_the_authenticated_ui_content_route() -> None:
    artifact = SimpleNamespace(id="ART1", type="diff", filename="report/diff.patch")
    uow: Any = SimpleNamespace(artifacts=SimpleNamespace(list_for_attempt=lambda _: [artifact]))
    assert _attempt_diff_link(uow, "A1") == {
        "kind": "link",
        "href": "/ui/artifacts/ART1/content",
        "label": "diff.patch",
    }
