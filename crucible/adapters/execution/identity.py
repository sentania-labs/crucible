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

from crucible.adapters.execution import scripts
from crucible.domain.gates import ENFORCED_PRE_PR_GATES, PRE_PR_GATES
from crucible.ports.execution import IDENTITY_MOUNT, REPO_MOUNT, REPORT_MOUNT

__all__ = ["IDENTITY_MOUNT", "REPORT_MOUNT", "REPO_MOUNT", "bundle_sha256", "write_bundle"]


_NO_POINTERS = "read the repository's own README and CONTRIBUTING first"


def commit_trailer(policy: dict[str, Any]) -> str:
    """The trailer key the policy names, which the hook adds and the checks look for."""
    return str(policy.get("git", {}).get("commit_trailer") or "Crucible-Attempt")


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
    trailer = commit_trailer(policy)
    verification_lines = (
        "\n".join(
            f"- `{v.get('id')}`: `{v.get('command')}` (expect exit {v.get('expect_exit', 0)})"
            if str(v.get("kind", "command")) == "command"
            else f"- `{v.get('id')}`: produce the artifact `{v.get('path')}`"
            for v in verification
        )
        or "- none"
    )
    criteria = contract.get("acceptance_criteria", [])
    criteria_lines = (
        "\n".join(f"- `{c.get('id')}`: {c.get('text', '')}" for c in criteria) or "- none"
    )
    first_criterion = str(criteria[0].get("id")) if criteria else "AC1"
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
(`report-schema.json` in this bundle), with `schema_version: "1.0"`:
the version of the document format, not the schema's name. Write
`progress.jsonl` lines as milestones pass. Capture each verification
command's output to a log file there. Write `blocked.md` and exit 75 to escalate. Exit 0 only after
`report.yaml` exists. Every path inside the report resolves against this
directory.

The report is your judgement. Write these fields:

- `summary`: what you changed and why, in a few sentences.
- `acceptance_mapping`: one entry per acceptance criterion below (see next).
- `proposed_pull_request`: a `title` and a `body` (`closes` is optional).
- `limitations`, `risks`, `blockers`, `follow_ups`: each a list of text items,
  `[]` when there are none.

Crucible fills the facts itself, from the collected branch, its own re-run of
each check and the files it copies out: `task_external_id`, `changed_files`,
`refs`, `checks` and `run_evidence`. You may leave them out. A value you do
write is compared with Crucible's and a difference is noted, never used.
Write no other field: Crucible sets the pull request's base and head itself.

`acceptance_mapping` has one entry for each acceptance criterion below, keyed
by that criterion's own `id`. The verification ids in section 6 are not
acceptance criteria and never go here. `status` is one of `met`, `not_met`,
`not_exercised` or `partial`. A list of entries, for example
`- {{id: {first_criterion}, status: met, evidence: "v2-make-test.log: the new case passes"}}`,
or a mapping keyed by id, `{first_criterion}: {{status: met, evidence: "..."}}`, are both accepted.

{criteria_lines}

Before you exit 0, run `crucible-report check {REPORT_MOUNT}/report.yaml`. It
prints each problem with the report in plain words; fix every one and run it
again until it reports no problems. If the command is not in this image,
check the report against `report-schema.json` yourself.

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

Commit locally on `{work_branch}`. Every commit must be authored as
`{git.get("author_name", "crucible-worker")} <{git.get("author_email", "")}>`
and carry the trailer `{trailer}: {external_id}`. The checkout is already set
up for both: its git config names that author, and Crucible's own
`commit-msg` hook adds the trailer to every commit you make. Leave
`user.name`, `user.email` and `core.hooksPath` as they are, and do not commit
with `--no-verify`. Crucible checks every commit's author and trailer before
review, and a commit without them fails the `commit_policy` gate.

Crucible collects, verifies, and publishes the commits. The claim's
`proposed_pull_request` is a draft Crucible may rewrite.

Repository: {repository.get("url", "(unset)")} at base ref
`{repository.get("base_ref", "main")}`.
"""


def render_policy_md(policy: dict[str, Any], contract: dict[str, Any]) -> str:
    """The deterministic policy in words (06): timeouts, paths, gates."""
    limits = policy.get("limits", {})
    gates = policy.get("gates", {})
    # With no list the whole pre-PR set runs (11), and the enforced gates run either way.
    listed = gates.get("pre_pr")
    pre_pr = [*(sorted(PRE_PR_GATES) if listed is None else listed), *sorted(ENFORCED_PRE_PR_GATES)]
    timeout = contract.get("timeout_seconds") or limits.get("timeout_seconds", {}).get("default")
    return f"""# Delivery policy

- Timeout for this attempt: {timeout} seconds.
- Drain grace before kill: {limits.get("grace_seconds")} seconds.
- A run with no activity for {limits.get("stall_fail_seconds")} seconds is stalled
  and terminated.
- Work branch pattern: `{policy.get("git", {}).get("work_branch_pattern")}`.
- Protected branches you may never touch: {policy.get("git", {}).get("protected_branches")}.

## Gates Crucible evaluates before anything is published

{_bullets(pre_pr)}

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
    # hades FDY-0135: the checkout's core.hooksPath names this directory, so the hook is
    # Crucible's text, read-only, and covered by the bundle hash like the rest.
    hook_dir = directory / scripts.COMMIT_HOOK_DIR
    hook_dir.mkdir(exist_ok=True)
    # Traversable by the worker uid whatever the service's umask; git skips a hook it
    # cannot reach or execute without a word.
    os.chmod(hook_dir, 0o755)
    hook = hook_dir / "commit-msg"
    hook.write_text(
        scripts.commit_msg_hook(
            trailer=commit_trailer(policy),
            value=external_id,
        ),
        encoding="utf-8",
    )
    os.chmod(hook, 0o555)
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
