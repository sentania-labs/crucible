"""Gates (11): names, phases, and the pure evaluators for the pre-PR set.

A gate evaluator is a pure function of a GateInput built from EvidenceV1 rows and the
contract. It never reads a database, a file, or a clock. Only `verified` evidence from a
source other than the worker is admissible: worker-asserted facts are shown to Foundry
and never satisfy a gate (11).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any


class GateName(StrEnum):
    # pre-PR (11)
    REPORT_PRESENT = "report_present"
    EXIT_CLEAN = "exit_clean"
    COMMITS_PRESENT = "commits_present"
    SCOPE_CONTAINED = "scope_contained"
    NO_INJECTED_FILES = "no_injected_files"
    NO_SECRETS = "no_secrets"
    VERIFICATION_RAN = "verification_ran"
    RUN_EVIDENCE_PRESENT = "run_evidence_present"
    CRITERIA_MAPPED = "criteria_mapped"
    DEPENDENCIES_UNCHANGED = "dependencies_unchanged"
    CI_UNCHANGED = "ci_unchanged"
    WORKSPACE_CLEAN = "workspace_clean"
    INTERNAL_REVIEW_RECORDED = "internal_review_recorded"
    # publication (23)
    BRANCH_PUSHED_AT_HEAD = "branch_pushed_at_head"
    PR_EXISTS_HEAD_MATCHES = "pr_exists_head_matches"
    # post-PR (23)
    EXTERNAL_REVIEW_ROUNDS = "external_review_rounds"
    FEEDBACK_DISPOSITIONS_COMPLETE = "feedback_dispositions_complete"
    CI_GREEN_FOR_HEAD = "ci_green_for_head"


class GateResult(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    ERROR = "error"


PRE_PR_GATES: frozenset[str] = frozenset(
    {
        GateName.REPORT_PRESENT,
        GateName.EXIT_CLEAN,
        GateName.COMMITS_PRESENT,
        GateName.SCOPE_CONTAINED,
        GateName.NO_INJECTED_FILES,
        GateName.NO_SECRETS,
        GateName.VERIFICATION_RAN,
        GateName.RUN_EVIDENCE_PRESENT,
        GateName.CRITERIA_MAPPED,
        GateName.DEPENDENCIES_UNCHANGED,
        GateName.CI_UNCHANGED,
        GateName.WORKSPACE_CLEAN,
        GateName.INTERNAL_REVIEW_RECORDED,
    }
)
PUBLICATION_GATES: frozenset[str] = frozenset(
    {GateName.BRANCH_PUSHED_AT_HEAD, GateName.PR_EXISTS_HEAD_MATCHES}
)
POST_PR_GATES: frozenset[str] = frozenset(
    {
        GateName.EXTERNAL_REVIEW_ROUNDS,
        GateName.FEEDBACK_DISPOSITIONS_COMPLETE,
        GateName.CI_GREEN_FOR_HEAD,
    }
)
ALL_GATES: frozenset[str] = PRE_PR_GATES | PUBLICATION_GATES | POST_PR_GATES

# The two pre-PR gates that need the verifier container (C3, 20). They are evaluated
# here so the row exists and carries a clear marker, and they never report `pass`.
DEFERRED_TO_C3: frozenset[str] = frozenset({GateName.VERIFICATION_RAN, GateName.WORKSPACE_CLEAN})
DEFERRED_MARKER = "deferred:c3-verifier"
# A gate whose evidence the C2 collector cannot produce says so rather than claiming
# coverage it does not have. The fake provider does produce a diff, so this marker is
# what a future collector that cannot would trip.
COLLECTOR_MARKER = "incomplete:collector"

WORKER_SOURCE = "worker"

# Shims and identity paths a worker must never leave behind (11).
INJECTED_PREFIXES: tuple[str, ...] = (".crucible/", "crucible/identity/", ".crucible-shims/")
INJECTED_NAMES: frozenset[str] = frozenset(
    {".crucible", "crucible-identity.md", "crucible-shim", ".crucible-identity"}
)
CI_PATH_PREFIXES: tuple[str, ...] = (".github/workflows/", ".github/actions/", ".gitlab-ci")
DEPENDENCY_FILES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
        "go.mod",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "Gemfile.lock",
        "composer.lock",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One evidence row as a gate sees it. `id` is the database row id."""

    id: int
    kind: str
    source: str
    verified: bool
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None

    @property
    def admissible(self) -> bool:
        """Gates may consume only verified evidence, and never a worker assertion (11)."""
        return self.verified and self.source != WORKER_SOURCE


@dataclass(frozen=True, slots=True)
class GateInput:
    """Everything a pre-PR gate may look at. Pure data."""

    contract: dict[str, Any]
    policy: dict[str, Any]
    head_sha: str | None
    evidence: tuple[EvidenceItem, ...]
    internal_review_required: bool = True

    def of_kind(self, kind: str, *, role: str | None = None) -> list[EvidenceItem]:
        out = [e for e in self.evidence if e.admissible and e.kind == kind]
        if role is not None:
            out = [e for e in out if e.payload.get("role") == role]
        return out

    def one(self, kind: str, *, role: str | None = None) -> EvidenceItem | None:
        items = self.of_kind(kind, role=role)
        return items[-1] if items else None


@dataclass(frozen=True, slots=True)
class GateOutcome:
    result: GateResult
    detail: str
    evidence_ids: tuple[int, ...] = ()


def _missing(kind: str, *, role: str | None = None) -> GateOutcome:
    """Gates run once the attempt is collected, so absent evidence is a failure, not a
    wait. 09: a failed attempt still reaches `reported`, and its gates then fail."""
    what = f"{kind} ({role})" if role else kind
    return GateOutcome(GateResult.FAIL, f"no verified {what} evidence was collected")


@lru_cache(maxsize=1024)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """A path glob where `*` stops at a separator and `**` crosses one.

    `fnmatch` alone lets `src/*` match `src/a/b/c.py`, which would widen every
    allowed_paths entry silently."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
                continue
            if pattern.startswith("**", index):
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
            index += 1
            continue
        if char == "?":
            out.append("[^/]")
            index += 1
            continue
        if char == "[":
            close = pattern.find("]", index + 1)
            if close != -1:
                out.append(pattern[index : close + 1])
                index = close + 1
                continue
        out.append(re.escape(char))
        index += 1
    return re.compile("".join(out) + r"\Z")


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    normalized = posixpath.normpath(path)
    for pattern in patterns:
        normalized_pattern = pattern.rstrip("/")
        if _glob_re(normalized_pattern).match(normalized):
            return True
        # `src/**` names the directory's contents, so `src/a/b.py` is inside it.
        if normalized_pattern.endswith("/**") and normalized.startswith(normalized_pattern[:-2]):
            return True
    return False


# ----- evaluators -------------------------------------------------------------


def report_present(gi: GateInput) -> GateOutcome:
    item = gi.one("artifact_present", role="completion_claim")
    if item is None:
        return _missing("artifact_present", role="completion_claim")
    if not item.payload.get("parsed_ok"):
        errors = item.payload.get("parse_errors") or []
        return GateOutcome(
            GateResult.FAIL,
            f"the report did not parse as CompletionClaimV1 ({len(errors)} problems)",
            (item.id,),
        )
    return GateOutcome(GateResult.PASS, "CompletionClaimV1 parsed with every field", (item.id,))


def exit_clean(gi: GateInput) -> GateOutcome:
    item = gi.one("exit_info")
    if item is None:
        return _missing("exit_info")
    code = item.payload.get("exit_code")
    if code == 0:
        return GateOutcome(GateResult.PASS, "the worker exited 0", (item.id,))
    return GateOutcome(
        GateResult.FAIL,
        f"exit code {code!r}, class {item.payload.get('exit_class')!r}",
        (item.id,),
    )


def commits_present(gi: GateInput) -> GateOutcome:
    bundle = gi.one("bundle_head")
    if bundle is None:
        return _missing("bundle_head")
    ids = (bundle.id,)
    commits = int(bundle.payload.get("commits") or 0)
    if commits < 1:
        return GateOutcome(
            GateResult.FAIL, "the collected work_branch has no commit beyond base_ref", ids
        )
    if not bundle.payload.get("bundle_verified"):
        return GateOutcome(GateResult.FAIL, "git bundle verify failed on the collected branch", ids)
    collected = str(bundle.payload.get("head_sha") or "")
    claimed = str(bundle.payload.get("claimed_head_sha") or "")
    if not claimed:
        return GateOutcome(
            GateResult.FAIL, "the report claims no head_sha to compare the bundle head to", ids
        )
    if collected != claimed:
        return GateOutcome(
            GateResult.FAIL,
            "the bundle head does not equal the head_sha the report claims",
            ids,
        )
    return GateOutcome(GateResult.PASS, f"{commits} commit(s), bundle verified at {collected}", ids)


def scope_contained(gi: GateInput) -> GateOutcome:
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    scope = gi.contract.get("scope", {})
    allowed = [str(p) for p in scope.get("allowed_paths", [])]
    prohibited = [str(p) for p in scope.get("prohibited_paths", [])]
    paths = [str(p) for p in item.payload.get("paths", [])]
    outside = [p for p in paths if not _matches_any(p, allowed)]
    forbidden = [p for p in paths if _matches_any(p, prohibited)]
    if outside or forbidden:
        parts = []
        if outside:
            parts.append(f"outside allowed_paths: {sorted(outside)[:10]}")
        if forbidden:
            parts.append(f"matching prohibited_paths: {sorted(forbidden)[:10]}")
        return GateOutcome(GateResult.FAIL, "; ".join(parts), (item.id,))
    return GateOutcome(
        GateResult.PASS, f"all {len(paths)} changed path(s) inside allowed_paths", (item.id,)
    )


def _injected(path: str) -> bool:
    normalized = posixpath.normpath(path)
    if normalized in INJECTED_NAMES or posixpath.basename(normalized) in INJECTED_NAMES:
        return True
    return any(normalized.startswith(prefix) for prefix in INJECTED_PREFIXES)


def no_injected_files(gi: GateInput) -> GateOutcome:
    diff = gi.one("diff_paths")
    bundle = gi.one("bundle_head")
    if diff is None:
        return _missing("diff_paths")
    if bundle is None:
        # 11 wants the diff and every commit on work_branch. Without the commit list the
        # gate has only half its evidence.
        return _missing("bundle_head")
    ids = tuple(e.id for e in (diff, bundle) if e is not None)
    paths = [str(p) for p in diff.payload.get("paths", [])]
    if bundle is not None:
        for commit in bundle.payload.get("commit_paths", []):
            paths.append(str(commit))
    hits = sorted({p for p in paths if _injected(p)})
    if hits:
        return GateOutcome(GateResult.FAIL, f"injected paths in the branch: {hits[:10]}", ids)
    return GateOutcome(GateResult.PASS, "no shim, .crucible, or identity path in the branch", ids)


def no_secrets(gi: GateInput) -> GateOutcome:
    item = gi.one("scanner_result")
    if item is None:
        return _missing("scanner_result")
    findings = item.payload.get("findings") or []
    if findings:
        # Findings carry the location and the pattern name, never the matched value.
        where = [f"{f.get('where')}:{f.get('pattern')}" for f in findings][:10]
        return GateOutcome(GateResult.FAIL, f"secret pattern matched at {where}", (item.id,))
    scanned = item.payload.get("scanned") or []
    if not item.payload.get("diff_scanned"):
        # 11 wants the scanner over the diff itself. A collector that produced no diff
        # content leaves this gate waiting rather than claiming coverage it lacks.
        return GateOutcome(
            GateResult.PENDING,
            f"{COLLECTOR_MARKER}: the collector produced no diff content to scan",
            (item.id,),
        )
    if not scanned:
        return GateOutcome(
            GateResult.PENDING,
            f"{COLLECTOR_MARKER}: the scanner reported no inputs",
            (item.id,),
        )
    return GateOutcome(
        GateResult.PASS, f"scanner found nothing across {len(scanned)} input(s)", (item.id,)
    )


def run_evidence_present(gi: GateInput) -> GateOutcome:
    required = [
        v
        for v in gi.contract.get("required_verification", [])
        if str(v.get("kind", "command")) == "artifact"
    ]
    if not required:
        return GateOutcome(GateResult.SKIPPED, "the contract requires no artifact verification")
    items = gi.of_kind("artifact_present", role="run_evidence")
    by_path = {posixpath.normpath(str(i.payload.get("path"))): i for i in items}
    ids: list[int] = []
    missing: list[str] = []
    empty: list[str] = []
    for verification in required:
        path = str(verification.get("path"))
        # The contract names a path; a file of the same name elsewhere is a different file.
        item = by_path.get(posixpath.normpath(path))
        if item is None:
            missing.append(path)
            continue
        ids.append(item.id)
        if int(item.payload.get("size") or 0) <= 0:
            empty.append(path)
    if missing:
        return GateOutcome(GateResult.FAIL, f"run evidence missing: {missing}", tuple(ids))
    if empty:
        return GateOutcome(GateResult.FAIL, f"run evidence is empty: {empty}", tuple(ids))
    return GateOutcome(
        GateResult.PASS,
        f"{len(required)} run-evidence artifact(s) present and non-empty",
        tuple(ids),
    )


def criteria_mapped(gi: GateInput) -> GateOutcome:
    item = gi.one("artifact_present", role="completion_claim")
    if item is None:
        return _missing("artifact_present", role="completion_claim")
    if not item.payload.get("parsed_ok"):
        return GateOutcome(
            GateResult.FAIL, "the report did not parse, so nothing is mapped", (item.id,)
        )
    mapped = {
        str(m.get("id")): str(m.get("status")) for m in item.payload.get("mapped_criteria", [])
    }
    required = [str(c.get("id")) for c in gi.contract.get("acceptance_criteria", [])]
    missing = [c for c in required if c not in mapped]
    if missing:
        return GateOutcome(
            GateResult.FAIL, f"acceptance criteria with no mapping: {missing}", (item.id,)
        )
    return GateOutcome(
        GateResult.PASS,
        "every acceptance criterion carries a status: "
        + ", ".join(f"{c}={mapped[c]}" for c in required),
        (item.id,),
    )


def dependencies_unchanged(gi: GateInput) -> GateOutcome:
    if gi.contract.get("scope", {}).get("may_add_dependencies"):
        return GateOutcome(GateResult.SKIPPED, "the contract permits dependency changes")
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    hits = sorted(
        {
            p
            for p in (str(x) for x in item.payload.get("paths", []))
            if posixpath.basename(posixpath.normpath(p)) in DEPENDENCY_FILES
        }
    )
    if hits:
        return GateOutcome(GateResult.FAIL, f"manifest or lockfile changed: {hits}", (item.id,))
    return GateOutcome(GateResult.PASS, "no manifest or lockfile in the diff", (item.id,))


def ci_unchanged(gi: GateInput) -> GateOutcome:
    if gi.contract.get("scope", {}).get("may_modify_ci"):
        return GateOutcome(GateResult.SKIPPED, "the contract permits CI changes")
    item = gi.one("diff_paths")
    if item is None:
        return _missing("diff_paths")
    hits = sorted(
        {
            p
            for p in (str(x) for x in item.payload.get("paths", []))
            if posixpath.normpath(p).startswith(CI_PATH_PREFIXES)
        }
    )
    if hits:
        return GateOutcome(GateResult.FAIL, f"CI definition changed: {hits}", (item.id,))
    return GateOutcome(GateResult.PASS, "no change under a workflow path", (item.id,))


def internal_review_recorded(gi: GateInput) -> GateOutcome:
    if not gi.internal_review_required:
        return GateOutcome(
            GateResult.SKIPPED, "the policy does not require an internal review for this head"
        )
    for item in gi.of_kind("review_received"):
        if str(item.payload.get("reviewed_head_sha")) != str(gi.head_sha):
            continue
        if item.payload.get("reviewer_is_author"):
            continue
        return GateOutcome(
            GateResult.PASS,
            f"ReviewReportV1 for {gi.head_sha} from {item.payload.get('reviewer_kind')} "
            f"with verdict {item.payload.get('verdict')}",
            (item.id,),
        )
    return GateOutcome(
        GateResult.PENDING, f"no non-author ReviewReportV1 for head {gi.head_sha} yet"
    )


def _deferred(name: str) -> Callable[[GateInput], GateOutcome]:
    def evaluate(_gi: GateInput) -> GateOutcome:
        return GateOutcome(
            GateResult.PENDING,
            f"{DEFERRED_MARKER}: {name} needs the verifier container, which arrives in C3",
        )

    return evaluate


PRE_PR_EVALUATORS: dict[str, Callable[[GateInput], GateOutcome]] = {
    GateName.REPORT_PRESENT: report_present,
    GateName.EXIT_CLEAN: exit_clean,
    GateName.COMMITS_PRESENT: commits_present,
    GateName.SCOPE_CONTAINED: scope_contained,
    GateName.NO_INJECTED_FILES: no_injected_files,
    GateName.NO_SECRETS: no_secrets,
    GateName.VERIFICATION_RAN: _deferred(GateName.VERIFICATION_RAN),
    GateName.RUN_EVIDENCE_PRESENT: run_evidence_present,
    GateName.CRITERIA_MAPPED: criteria_mapped,
    GateName.DEPENDENCIES_UNCHANGED: dependencies_unchanged,
    GateName.CI_UNCHANGED: ci_unchanged,
    GateName.WORKSPACE_CLEAN: _deferred(GateName.WORKSPACE_CLEAN),
    GateName.INTERNAL_REVIEW_RECORDED: internal_review_recorded,
}


def evaluate_gate(gate: str, gi: GateInput) -> GateOutcome:
    """Run one pre-PR gate. An evaluator that raises is `error`, which counts as fail (09)."""
    evaluator = PRE_PR_EVALUATORS.get(gate)
    if evaluator is None:
        return GateOutcome(GateResult.ERROR, f"no evaluator for gate {gate!r}")
    try:
        return evaluator(gi)
    except Exception as exc:  # an evaluator that cannot run is `error`, treated as fail
        return GateOutcome(GateResult.ERROR, f"{type(exc).__name__}: {exc}")


def evaluate_pre_pr(gates: Sequence[str], gi: GateInput) -> dict[str, GateOutcome]:
    return {gate: evaluate_gate(gate, gi) for gate in gates}


def blocking(outcomes: dict[str, GateOutcome]) -> list[str]:
    """Gates whose result stops the task: fail and error (09 treats error as fail)."""
    return sorted(
        name
        for name, outcome in outcomes.items()
        if outcome.result in (GateResult.FAIL, GateResult.ERROR)
    )


def waiting_for_review(outcomes: dict[str, GateOutcome]) -> bool:
    outcome = outcomes.get(GateName.INTERNAL_REVIEW_RECORDED)
    return outcome is not None and outcome.result is GateResult.PENDING
