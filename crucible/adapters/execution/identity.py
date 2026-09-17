"""Rendering the per-attempt identity bundle (06).

The bundle is assembled by Crucible, mounted read-only, and never written into the
repository. It carries no credential, no API token, and nothing the contract did not
name. Its content hash goes on the attempt and `IDENTITY.md` is stored as an artifact,
so the exact instructions a worker saw are reproducible.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from crucible.ports.execution import IDENTITY_MOUNT, REPO_MOUNT, REPORT_MOUNT

__all__ = ["IDENTITY_MOUNT", "REPORT_MOUNT", "REPO_MOUNT", "bundle_sha256", "write_bundle"]


_NO_POINTERS = "read the repository's own README and CONTRIBUTING first"


def _bullets(values: Any, empty: str = "none") -> str:
    items = [str(v) for v in (values or [])]
    return "\n".join(f"- {item}" for item in items) if items else f"- {empty}"


def render_identity_md(
    *,
    contract: dict[str, Any],
    policy: dict[str, Any],
    external_id: str,
    owner: str,
    work_branch: str,
    network_mode: str,
) -> str:
    """The ten sections of 06, rendered from the contract and the policy."""
    scope = contract.get("scope", {})
    repository = contract.get("repository", {})
    verification = contract.get("required_verification", [])
    git = policy.get("git", {})
    verification_lines = (
        "\n".join(
            f"- `{v.get('id')}`: `{v.get('command')}` (expect exit {v.get('expect_exit', 0)})"
            if str(v.get("kind", "command")) == "command"
            else f"- `{v.get('id')}`: produce the artifact `{v.get('path')}`"
            for v in verification
        )
        or "- none"
    )
    return f"""# Worker identity

## 1. Role

You are a worker executing task `{external_id}` for `{owner}`. You implement;
you do not redefine the task.

## 2. Objective

{contract.get("title", "(no title)")}

{contract.get("objective", "")}

## 3. Authority and boundaries

Allowed paths:
{_bullets(scope.get("allowed_paths"))}

Prohibited paths:
{_bullets(scope.get("prohibited_paths"))}

Prohibited actions:
{_bullets(scope.get("prohibited_actions"))}

- Dependencies may be added: {bool(scope.get("may_add_dependencies"))}
- CI definitions may be modified: {bool(scope.get("may_modify_ci"))}
- Network mode: {network_mode}
- Branch: `{work_branch}`
- Nothing outside the checkout at `{REPO_MOUNT}` and the report directory at
  `{REPORT_MOUNT}` is yours.

## 4. Instruction precedence

1. The operator's explicit words.
2. Safety and repository protection.
3. This contract.
4. Project documents and the skills this bundle carries.
5. The delivery policy in `policy.md`.
6. Global skills.
7. Harness defaults.

On a material conflict: stop and escalate (section 7).

## 5. Procedure pointers

{_bullets(contract.get("project_instructions"), empty=_NO_POINTERS)}

## 6. Verification

Run each of these and capture its output to a log file under `{REPORT_MOUNT}`:

{verification_lines}

## 7. Reporting protocol

The report directory is `{REPORT_MOUNT}`, mounted writable and outside the
checkout. Write `report.yaml` there matching `CompletionClaimV1`
(`report-schema.json` in this bundle). Write `progress.jsonl` lines as
milestones pass. Capture each verification command's output to a log file
there. Write `blocked.md` and exit 75 to escalate. Exit 0 only after
`report.yaml` exists. Every path inside the report resolves against this
directory.

## 8. Exit codes

- `0` done, with a report
- `75` blocked, with `blocked.md`
- `70` cannot proceed, bad environment
- anything else is a failure

## 9. Prohibitions

- No pushing at all. The checkout has no remote credential; Crucible pushes
  after verification.
- No force, and no branch other than `{work_branch}`.
- No delegating the task.
- No writing anywhere but the checkout and `{REPORT_MOUNT}`.
- No claiming completion without evidence.
- No closing references to issues the contract did not name.

## 10. Commits

Commit locally on `{work_branch}` as
`{git.get("author_name", "crucible-worker")} <{git.get("author_email", "")}>`
with the trailer `{git.get("commit_trailer", "Crucible-Attempt")}`. Crucible
collects, verifies, and publishes the commits. The claim's
`proposed_pull_request` is a draft Crucible may rewrite.

Repository: {repository.get("url", "(unset)")} at base ref
`{repository.get("base_ref", "main")}`.
"""


def render_policy_md(policy: dict[str, Any], contract: dict[str, Any]) -> str:
    """The deterministic policy in words (06): timeouts, paths, gates."""
    limits = policy.get("limits", {})
    gates = policy.get("gates", {})
    timeout = contract.get("timeout_seconds") or limits.get("timeout_seconds", {}).get("default")
    return f"""# Delivery policy

- Timeout for this attempt: {timeout} seconds.
- Drain grace before kill: {limits.get("grace_seconds")} seconds.
- A run with no activity for {limits.get("stall_fail_seconds")} seconds is stalled
  and terminated.
- Work branch pattern: `{policy.get("git", {}).get("work_branch_pattern")}`.
- Protected branches you may never touch: {policy.get("git", {}).get("protected_branches")}.

## Gates Crucible evaluates before anything is published

{_bullets(gates.get("pre_pr"))}

These are mechanical. Crucible re-runs every required verification command
itself, from the collected tree, in a container you do not control. Your own
logs are shown to the orchestrator as a claim and never satisfy a gate.
"""


def write_bundle(
    directory: Path,
    *,
    contract: dict[str, Any],
    policy: dict[str, Any],
    external_id: str,
    owner: str,
    work_branch: str,
    network_mode: str,
    report_schema: dict[str, Any],
    history: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Write the bundle and return (IDENTITY.md text, bundle sha256)."""
    directory.mkdir(parents=True, exist_ok=True)
    identity_md = render_identity_md(
        contract=contract,
        policy=policy,
        external_id=external_id,
        owner=owner,
        work_branch=work_branch,
        network_mode=network_mode,
    )
    contract_yaml = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
    files: dict[str, str] = {
        "IDENTITY.md": identity_md,
        "contract.yaml": contract_yaml,
        # The same document as JSON. 06 names contract.yaml; the JSON copy is what a
        # worker with jq and no YAML parser reads, and it is byte-for-byte the same
        # object, so contract.sha256 still covers what the worker was given.
        "contract.json": json.dumps(contract, indent=2, sort_keys=True) + "\n",
        "contract.sha256": hashlib.sha256(contract_yaml.encode("utf-8")).hexdigest() + "\n",
        "policy.md": render_policy_md(policy, contract),
        "report-schema.json": json.dumps(report_schema, indent=2, sort_keys=True) + "\n",
    }
    for name, text in files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o444)
    history_dir = directory / "history"
    history_dir.mkdir(exist_ok=True)
    for name, text in history or []:
        entry = history_dir / name
        entry.write_text(text, encoding="utf-8")
        os.chmod(entry, 0o444)
    return identity_md, bundle_sha256(directory)


def bundle_sha256(directory: Path) -> str:
    """A content hash over every file in the bundle, path and bytes, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
