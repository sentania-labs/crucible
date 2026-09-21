"""Turning a collected attempt into artifacts and evidence rows (11).

Everything a gate later reads is written here, once, at `collected`. Crucible's own
observations are `verified`; the worker's own claim is recorded too, as `source: worker`
and `verified: false`, so Foundry can read it and no gate can ever consume it."""

from __future__ import annotations

import json
import logging
from typing import Any

from crucible.application.transitions import record_event
from crucible.contracts.evidence import (
    ROLE_COMPLETION_CLAIM,
    ROLE_RUN_EVIDENCE,
    ROLE_WORKER_CLAIM,
    EvidenceKind,
    EvidenceSource,
)
from crucible.domain.entities import Artifact, Attempt, EvidenceRecord, Task
from crucible.domain.events import PRINCIPAL_CRUCIBLE, EventKind
from crucible.domain.ids import new_id
from crucible.domain.secrets import find_secrets, scan_text
from crucible.ports.artifacts import ArtifactStore, SecretInArtifactError
from crucible.ports.clock import Clock
from crucible.ports.execution import CollectedOutputs
from crucible.ports.harness import ParsedReport
from crucible.ports.repository import UnitOfWork

log = logging.getLogger("crucible.evidence")


def _add(
    uow: UnitOfWork,
    clock: Clock,
    *,
    attempt: Attempt,
    kind: EvidenceKind,
    source: EvidenceSource,
    payload: dict[str, Any],
    artifact_id: str | None = None,
) -> EvidenceRecord:
    record = EvidenceRecord(
        id=None,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        kind=kind.value,
        observed_at=clock.now(),
        source=source.value,
        # 11: only Crucible's own and verified GitHub observations are verified.
        verified=source is not EvidenceSource.WORKER,
        payload=payload,
        artifact_id=artifact_id,
    )
    return uow.evidence.add(record)


def store_artifact(
    uow: UnitOfWork,
    clock: Clock,
    store: ArtifactStore,
    *,
    attempt: Attempt,
    name: str,
    artifact_type: str,
    content: bytes,
    content_type: str,
    created_by: str = PRINCIPAL_CRUCIBLE,
) -> Artifact:
    """Scan, store content-addressed, and record the row. Raises on a secret match."""
    blob = store.put(content)
    existing = uow.artifacts.find_by_sha256(blob.sha256, attempt.id)
    if existing is not None and existing.type == artifact_type and existing.filename == name:
        return existing
    artifact = Artifact(
        id=new_id(),
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        type=artifact_type,
        filename=name,
        path=blob.path,
        size=blob.size,
        sha256=blob.sha256,
        content_type=content_type,
        created_at=clock.now(),
        created_by=created_by,
    )
    uow.artifacts.add(artifact)
    record_event(
        uow,
        clock,
        EventKind.ARTIFACT_STORED,
        principal=created_by,
        task_id=attempt.task_id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload={
            "artifact_id": artifact.id,
            "type": artifact_type,
            "name": name,
            "sha256": blob.sha256,
            "size": blob.size,
        },
    )
    return artifact


def _scanner_findings(
    outputs: CollectedOutputs, claim: dict[str, Any] | None
) -> list[dict[str, str]]:
    """The secret scanner over the diff, every commit message, and the report (11).

    A finding names where and which pattern, never the value."""
    findings: list[dict[str, str]] = []
    if claim is not None:
        findings.extend(
            {"where": f"report.{m.path}" if m.path else "report", "pattern": m.pattern}
            for m in find_secrets(claim)
        )
    if outputs.blocked_md:
        hit = scan_text(outputs.blocked_md)
        if hit:
            findings.append({"where": "report/blocked.md", "pattern": hit})
    if outputs.diff_text is not None:
        # The content, not the path list: a credential committed into a file is what
        # this gate exists to catch (11).
        hit = scan_text(outputs.diff_text)
        if hit:
            findings.append({"where": "diff", "pattern": hit})
    for path in outputs.diff_paths:
        hit = scan_text(path)
        if hit:
            findings.append({"where": f"diff-path:{path}", "pattern": hit})
    if outputs.bundle is not None:
        for index, message in enumerate(outputs.bundle.commit_messages):
            hit = scan_text(message)
            if hit:
                findings.append({"where": f"commit[{index}].message", "pattern": hit})
    for artifact in outputs.artifacts:
        hit = scan_text(artifact.content.decode("utf-8", "replace"))
        if hit:
            findings.append({"where": f"artifact:{artifact.name}", "pattern": hit})
    return findings


def _scanned_inputs(outputs: CollectedOutputs, claim: dict[str, Any] | None) -> list[str]:
    scanned = ["report" if claim is not None else "report:absent"]
    if outputs.diff_text is not None:
        scanned.append("diff")
    scanned.extend(f"diff-path:{p}" for p in outputs.diff_paths)
    if outputs.bundle is not None:
        scanned.extend(f"commit[{i}].message" for i in range(len(outputs.bundle.commit_messages)))
    scanned.extend(f"artifact:{a.name}" for a in outputs.artifacts)
    return scanned


def record_collection_evidence(
    uow: UnitOfWork,
    clock: Clock,
    store: ArtifactStore,
    *,
    attempt: Attempt,
    task: Task,
    outputs: CollectedOutputs,
    claim: dict[str, Any] | None,
    claim_parsed_ok: bool,
    parse_errors: list[dict[str, Any]],
    parsed_report: ParsedReport | None = None,
) -> str | None:
    """Write the artifacts and evidence a pre-PR gate consumes. Returns the collected head."""
    findings = _scanner_findings(outputs, claim)
    _add(
        uow,
        clock,
        attempt=attempt,
        kind=EvidenceKind.EXIT_INFO,
        source=EvidenceSource.CRUCIBLE,
        payload={
            "exit_code": attempt.exit_code,
            "exit_class": attempt.exit_class.value if attempt.exit_class else None,
            "termination_reason": attempt.termination_reason,
        },
    )
    claim_artifact_id: str | None = None
    if claim is not None and not findings:
        try:
            artifact = store_artifact(
                uow,
                clock,
                store,
                attempt=attempt,
                name="report/completion-claim.json",
                artifact_type="completion_claim",
                content=json.dumps(claim, sort_keys=True, indent=2).encode("utf-8"),
                content_type="application/json",
            )
            claim_artifact_id = artifact.id
        except SecretInArtifactError as exc:
            findings.append({"where": "report/completion-claim.json", "pattern": exc.pattern})
    if claim is not None:
        refs = claim.get("refs", {}) if isinstance(claim.get("refs"), dict) else {}
        # A report the scanner matched is never copied into a row: 14 says no table ever
        # holds a secret, and the gate that reads this one has already failed.
        redacted = bool(findings)
        payload: dict[str, Any] = {
            "role": ROLE_COMPLETION_CLAIM,
            "parsed_ok": claim_parsed_ok,
            "parse_errors": parse_errors,
            "redacted": redacted,
        }
        if not redacted:
            payload.update(
                {
                    "claimed_head_sha": refs.get("head_sha"),
                    "claimed_commits": refs.get("commits"),
                    "mapped_criteria": [
                        {"id": m.get("id"), "status": m.get("status")}
                        for m in claim.get("acceptance_mapping", [])
                        if isinstance(m, dict)
                    ],
                    "run_evidence": claim.get("run_evidence", []),
                    "changed_files": claim.get("changed_files", []),
                }
            )
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload=payload,
            artifact_id=claim_artifact_id,
        )
        # The worker's own claim, recorded unverified. A gate never reads this row (11).
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.WORKER,
            payload={"role": ROLE_WORKER_CLAIM, "asserted": {} if findings else claim},
        )
    if parsed_report is not None and parsed_report.run_evidence_error is not None:
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "role": "harness_run_evidence",
                "parsed_ok": False,
                "error": parsed_report.run_evidence_error,
            },
        )
    head_sha: str | None = None
    if outputs.bundle is not None:
        bundle = outputs.bundle
        head_sha = bundle.head_sha
        claimed = (claim or {}).get("refs", {}).get("head_sha") if claim else None
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.BUNDLE_HEAD,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "head_sha": bundle.head_sha,
                "base_ref": bundle.base_ref,
                "work_branch": bundle.work_branch,
                "commits": bundle.commits,
                "bundle_verified": bundle.verified,
                "bundle_sha256": bundle.sha256,
                "commit_paths": list(bundle.commit_paths),
                "commit_messages": list(bundle.commit_messages),
                "claimed_head_sha": claimed,
            },
        )
    if outputs.diff_paths or outputs.bundle is not None:
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.DIFF_PATHS,
            source=EvidenceSource.CRUCIBLE,
            payload={"paths": list(outputs.diff_paths)},
        )
    for collected in outputs.artifacts:
        if collected.type != "run_evidence":
            continue
        artifact_id: str | None = None
        try:
            stored = store_artifact(
                uow,
                clock,
                store,
                attempt=attempt,
                name=collected.name,
                artifact_type=collected.type,
                content=collected.content,
                content_type=collected.content_type,
            )
            artifact_id = stored.id
        except SecretInArtifactError as exc:
            findings.append({"where": f"artifact:{collected.name}", "pattern": exc.pattern})
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.ARTIFACT_PRESENT,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "role": ROLE_RUN_EVIDENCE,
                "path": collected.name,
                "size": len(collected.content),
            },
            artifact_id=artifact_id,
        )
    # 11: Crucible's own re-run of every required command, in a verifier container from
    # the collected tree. The worker's logs are a claim; these are the evidence.
    verification_logs = {a.name: a for a in outputs.artifacts if a.type == "verification_log"}
    for run in outputs.verifications:
        verify_artifact_id: str | None = None
        collected_log = verification_logs.get(f"verify/{run.id}.log")
        if collected_log is not None and collected_log.content:
            try:
                verify_artifact_id = store_artifact(
                    uow,
                    clock,
                    store,
                    attempt=attempt,
                    name=collected_log.name,
                    artifact_type=collected_log.type,
                    content=collected_log.content,
                    content_type=collected_log.content_type,
                ).id
            except SecretInArtifactError as exc:
                findings.append({"where": f"artifact:{collected_log.name}", "pattern": exc.pattern})
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.VERIFICATION_RUN,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "id": run.id,
                "command": run.command,
                "expect_exit": run.expect_exit,
                "exit_code": run.exit_code,
                "ran": run.ran,
                "detail": run.detail,
            },
            artifact_id=verify_artifact_id,
        )
        record_event(
            uow,
            clock,
            EventKind.VERIFICATION_COMPLETED,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={
                "id": run.id,
                "command": run.command,
                "exit_code": run.exit_code,
                "expect_exit": run.expect_exit,
                "ran": run.ran,
            },
        )
    if outputs.workspace_state is not None:
        _add(
            uow,
            clock,
            attempt=attempt,
            kind=EvidenceKind.WORKSPACE_STATE,
            source=EvidenceSource.CRUCIBLE,
            payload={
                "checked": outputs.workspace_state.checked,
                "leftover": list(outputs.workspace_state.leftover),
                "detail": outputs.workspace_state.detail,
            },
        )
    # 08: the collector records each file it refused to copy out.
    for rejection in outputs.copy_rejections:
        record_event(
            uow,
            clock,
            EventKind.COLLECTOR_REJECTED_FILE,
            principal=PRINCIPAL_CRUCIBLE,
            task_id=task.id,
            execution_id=attempt.execution_id,
            attempt_id=attempt.id,
            payload={"reason": rejection.get("reason"), "path": rejection.get("path")},
        )
    _add(
        uow,
        clock,
        attempt=attempt,
        kind=EvidenceKind.SCANNER_RESULT,
        source=EvidenceSource.CRUCIBLE,
        payload={
            "findings": findings,
            "scanned": _scanned_inputs(outputs, claim),
            # The gate reports `pass` only when the diff itself was read (11). A
            # collector that produced no diff leaves this false and the gate waits.
            "diff_scanned": outputs.diff_text is not None,
        },
    )
    record_event(
        uow,
        clock,
        EventKind.EVIDENCE_RECORDED,
        principal=PRINCIPAL_CRUCIBLE,
        task_id=task.id,
        execution_id=attempt.execution_id,
        attempt_id=attempt.id,
        payload={
            "head_sha": head_sha,
            "diff_paths": len(outputs.diff_paths),
            "artifacts": len(outputs.artifacts),
            "scanner_findings": len(findings),
        },
    )
    return head_sha
