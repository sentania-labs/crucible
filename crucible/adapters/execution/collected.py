"""Reading what the collector, the bundle verifier, and the verifier wrote (08, 11).

The collector runs in a throwaway container and leaves its answers as plain files in the
attempt's output directory. Those files are the same on every provider, because the
script that writes them is the same script (`scripts.collector_script`); only how the
directory reaches Crucible differs, a shared volume on Docker and a tar off the
workspace claim through a reader Pod on Kubernetes (26).

Everything here treats what it reads as data: it is a tree a worker influenced.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from crucible.adapters.execution import scripts
from crucible.ports.execution import (
    BranchBundle,
    CollectedArtifact,
    LaunchSpec,
    VerificationRun,
)

__all__ = ["Outputs", "read_outputs", "read_verifications", "tail", "text"]


@dataclass(frozen=True, slots=True)
class Outputs:
    report: dict[str, Any] | None
    report_raw: str | None
    blocked_md: str | None
    stdout_tail: str
    stderr_tail: str
    diff_paths: tuple[str, ...]
    diff_text: str | None
    bundle: BranchBundle | None
    artifacts: tuple[CollectedArtifact, ...]
    verifications: tuple[VerificationRun, ...]
    copy_rejections: tuple[dict[str, str], ...]
    checkpoint_refusal: str | None


def text(path: Path, limit: int = 8 * 1024 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def tail(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def read_outputs(
    output: Path,
    verify: Path,
    *,
    spec: LaunchSpec,
    bundle_verified: bool,
    collector_exit: int,
    verifications: tuple[VerificationRun, ...],
    tail_bytes: int,
) -> Outputs:
    report_dir = output / "report"
    report: dict[str, Any] | None = None
    report_raw: str | None = None
    report_file = report_dir / "report.yaml"
    if report_file.is_file():
        report_raw = text(report_file)
        try:
            parsed = yaml.safe_load(report_raw)
            report = parsed if isinstance(parsed, dict) else None
        except yaml.YAMLError:
            report = None
    blocked = report_dir / "blocked.md"
    blocked_md = text(blocked) if blocked.is_file() else None

    changed = tuple(p for p in text(output / "changed.txt").splitlines() if p.strip())
    diff_text = text(output / "diff.patch") if (output / "diff.patch").is_file() else None
    commit_paths = tuple(p for p in text(output / "commit-paths.txt").splitlines() if p.strip())
    messages: list[str] = []
    for record in text(output / "log.txt").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) >= 2 and parts[0]:
            messages.append(parts[1])
    head = text(output / "head.txt").strip()
    commits_text = text(output / "commits.txt").strip() or "0"
    repository = spec.contract.get("repository", {})
    bundle_path = output / "work_branch.bundle"
    bundle = None
    if head and collector_exit == 0:
        bundle = BranchBundle(
            head_sha=head,
            base_ref=str(repository.get("base_ref", "main")),
            work_branch=text(output / "branch.txt").strip()
            or str(repository.get("work_branch", "")),
            commits=int(commits_text) if commits_text.isdigit() else 0,
            verified=bundle_verified,
            sha256=(
                hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                if bundle_path.is_file()
                else ""
            ),
            commit_paths=commit_paths,
            commit_messages=tuple(messages),
        )

    artifacts: list[CollectedArtifact] = []
    if report_dir.is_dir():
        for path in sorted(p for p in report_dir.rglob("*") if p.is_file()):
            name = f"report/{path.relative_to(report_dir)}"
            if path.name in ("report.yaml", "blocked.md"):
                continue
            artifacts.append(
                CollectedArtifact(
                    name=name,
                    type="run_evidence",
                    content=path.read_bytes()[: 4 * 1024 * 1024],
                    content_type="text/plain",
                )
            )
    for run in verifications:
        artifacts.append(
            CollectedArtifact(
                name=f"verify/{run.id}.log",
                type="verification_log",
                content=run.log_tail.encode("utf-8"),
                content_type="text/plain",
            )
        )
    rejections: list[dict[str, str]] = []
    for line in text(output / "copy-rejections.tsv").splitlines():
        if "\t" in line:
            reason, path_text = line.split("\t", 1)
            rejections.append({"reason": reason, "path": path_text})
    return Outputs(
        report=report,
        report_raw=report_raw,
        blocked_md=blocked_md,
        stdout_tail=tail(output / "collector.ok", tail_bytes),
        stderr_tail=tail(output / "bundle.log", tail_bytes),
        diff_paths=changed,
        diff_text=diff_text,
        bundle=bundle,
        artifacts=tuple(artifacts),
        verifications=verifications,
        copy_rejections=tuple(rejections),
        checkpoint_refusal=text(output / "checkpoint-refusal.txt").strip() or None,
    )


def read_verifications(
    verify: Path, spec: LaunchSpec, checks: list[tuple[str, str]]
) -> tuple[VerificationRun, ...]:
    expected = {
        str(v.get("id")): int(v.get("expect_exit", 0))
        for v in spec.contract.get("required_verification", [])
    }
    runs: list[VerificationRun] = []
    for check_id, command in checks:
        safe = scripts.encode_check_id(check_id)
        exit_file = verify / f"{safe}.exit"
        log_file = verify / f"{safe}.log"
        if not exit_file.is_file():
            runs.append(
                VerificationRun(
                    id=check_id,
                    command=command,
                    expect_exit=expected.get(check_id, 0),
                    exit_code=-1,
                    log_tail=tail(log_file, 32 * 1024),
                    ran=False,
                    detail="the verifier container recorded no exit for this command",
                )
            )
            continue
        raw = text(exit_file).strip()
        runs.append(
            VerificationRun(
                id=check_id,
                command=command,
                expect_exit=expected.get(check_id, 0),
                exit_code=int(raw) if raw.lstrip("-").isdigit() else -1,
                log_tail=tail(log_file, 32 * 1024),
            )
        )
    return tuple(runs)
