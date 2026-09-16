# 11. Definition of done, gates, and evidence

## Four levels

1. **Worker completion.** The worker wrote a `CompletionClaimV1` and exited
   0. A claim.
2. **Crucible completion.** Every gate the policy requires has `pass` for
   the final attempt, and the evidence rows the gates used exist. Recorded
   as task state `gates_passed`.
3. **Foundry acceptance.** Foundry read the claim, the diff, and the
   evidence, and recorded an `AcceptanceResult`. Semantic. Crucible never
   infers it.
4. **User approval.** Consequential decisions (merge, release, accepted
   risk, scope change) recorded as `Decision` rows with verbatim words.
   Crucible does not proceed past a policy-marked approval point without
   one.

## CompletionClaimV1 (the worker report)

```yaml
schema_version: "1.0"
task_external_id: "FDY-0042"
summary: "..."
changed_files: ["src/ledger/import.py", "tests/ledger/test_import.py"]
refs:
  branch: "crucible/FDY-0042"
  head_sha: "abc123..."
  pushed: true
  pull_request: "https://github.com/example-org/example-service/pull/18"
checks:
  - { id: "V1", command: "make lint", exit: 0, log: "report/V1.log" }
  - { id: "V2", command: "make test", exit: 0, log: "report/V2.log" }
acceptance_mapping:
  - { id: "AC1", status: "met", evidence: "tests/ledger/test_import.py::test_duplicate_id_409" }
  - { id: "AC2", status: "met", evidence: "report/V2.log" }
run_evidence: ["report/run-evidence.md", "report/screenshot-1.png"]
ci: { run_url: null, status: "not_applicable" }
limitations: ["..."]
risks: ["..."]
blockers: []
follow_ups: ["..."]
```

Every field required; empty lists are explicit. Missing or unparsable
report is a report-gate failure.

## Derivation from the delivery pipeline

The operator's delivery skills define the pipeline: write, lint with the
CI definition, run it and see the use case work, one non-author adversarial
review before the PR, PR opened when done, one external review round,
merge, release by annotated tag. Branch through PR whenever anything else
consumes `main`. Publish is a one-way handoff; CI never deploys. CI placement
by capability. Secrets never in any surface.

What Crucible enforces is the mechanically checkable residue of that. What
it cannot check stays with Foundry or the user.

## Deterministic gates (GateV1, evaluated by pure functions over evidence)

| Gate | Passes when | Evidence consumed |
|---|---|---|
| `report_present` | report parsed, all fields present | CompletionClaim artifact |
| `exit_clean` | exit code 0 | attempt exit info |
| `scope_contained` | every changed path matches `allowed_paths` and none matches `prohibited_paths` | diff path list from `collect` |
| `no_injected_files` | shims, `.crucible/`, identity paths absent from diff and from any commit on `work_branch` | diff, `git log --stat` |
| `no_secrets` | secret scanner over the diff and the report finds nothing | scanner output artifact |
| `verification_ran` | for each `required_verification` command: a log artifact exists, its recorded exit matches `expect_exit`, and the command string appears in the captured worker transcript | check logs, transcript |
| `run_evidence_present` | each `kind: artifact` verification path exists and is non-empty | artifacts |
| `criteria_mapped` | every `acceptance_criteria.id` appears in `acceptance_mapping` with a status | report |
| `branch_pushed_at_head` | remote `work_branch` head equals reported `head_sha` equals collected HEAD | ls-remote, collect |
| `review_round_recorded` | a review artifact for `head_sha` by a principal other than the worker exists (Foundry attaches it via the artifacts API after running the reviewer) | artifact of type `review` |
| `pr_exists_head_matches` | PR exists, targets `deliverables.target`, head equals `head_sha`, not draft when contract says not draft | provider query via repository auth |
| `ci_green_for_head` | the workflow run for `head_sha` concluded success (the run, not the merge) | CI API query |
| `external_review_round` | configured reviewer bot has left a review or reaction on the PR (absence means not yet, so `pending`) | PR reactions and reviews |
| `release_shape` | when the contract deliverable is a tag: annotated, `vMAJOR.MINOR.PATCH`, reachable from default branch | git query |
| `dependencies_unchanged` | when `may_add_dependencies` is false: lockfiles and manifests unchanged | diff |
| `ci_unchanged` | when `may_modify_ci` is false: no change under workflow paths | diff |
| `workspace_clean` | no leftover ephemeral clusters or containers labeled for this attempt | provider reconcile |

A policy names which gates are required, which are `pending`-tolerant (the
task can reach `awaiting_acceptance` with them pending, for example
`external_review_round`), and which are skipped for a project.

## Judgment (never a gate)

Whether the use case is actually seen working, whether review findings were
truly addressed, whether "not exercised" is acceptable, whether a red CI run
was honestly explained, whether the repository is consumed by something
(branching mode) when unclear, whether a merge earns a release, whether a
risk or limitation is acceptable, whether scope grew, architectural
soundness. Foundry evaluates these from the same evidence and records an
`AcceptanceResult`; consequential ones go to the user as escalations.

## Evidence model (EvidenceV1)

`evidence`: `attempt_id`, `kind` (exit_info, diff_paths, remote_head,
pr_state, ci_run, scanner_result, artifact_present, transcript_match),
`observed_at`, `source` (`crucible` or `worker`), `verified` (true only for
`crucible`), `payload`, `artifact_id`. Gates may consume only
`verified: true` evidence. Worker-asserted facts are shown to Foundry but
never satisfy a gate.
