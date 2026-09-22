# 11. Definition of done, gates, and evidence

## Four levels

1. **Worker completion.** The worker wrote a `CompletionClaimV1`, made local
   commits on `work_branch`, ran every required check, and exited 0. A
   claim.
2. **Crucible completion.** Every pre-PR gate the policy requires has `pass`
   for the collected head, the internal review is recorded, and the evidence
   rows the gates used exist. Recorded as task state `gates_passed`. After
   publication, Crucible completion extends to the post-PR gates: external
   review rounds received and dispositioned, CI certification green on the
   final head. Recorded as `ready_for_merge`.
3. **Foundry acceptance.** Foundry read the claim, the diff, the verified
   evidence, and the review, and recorded an `AcceptanceResult` for that
   head. Semantic. Crucible never infers it. It precedes any push.
4. **User approval.** Consequential decisions (merge, release, accepted
   risk, scope change) recorded as `Decision` rows with verbatim words.
   Merge is the operator's act on GitHub, observed by Crucible. Release
   tagging requires a recorded operator authorization (24).

## CompletionClaimV1 (the worker report)

```yaml
schema_version: "1.0"
task_external_id: "FDY-0042"
summary: "..."
changed_files: ["src/ledger/import.py", "tests/ledger/test_import.py"]
refs:
  branch: "crucible/FDY-0042"
  head_sha: "abc123..."            # the worker's local HEAD; Crucible collects, never trusts
  commits: 3
checks:
  - { id: "V1", command: "make lint", exit: 0, log: "report/V1.log" }
  - { id: "V2", command: "make test", exit: 0, log: "report/V2.log" }
  - { id: "V3", command: "make scan", exit: 0, log: "report/V3.log" }
acceptance_mapping:
  - { id: "AC1", status: "met", evidence: "tests/ledger/test_import.py::test_duplicate_id_409" }
  - { id: "AC2", status: "met", evidence: "report/V2.log" }
run_evidence: ["report/run-evidence.md", "report/screenshot-1.png"]
proposed_pull_request:
  title: "Return 409 on duplicate import ID"
  body: "..."                      # the worker's draft; Crucible renders the real body (23)
  closes: ["https://github.com/example-org/example-service/issues/17"]  # must match the contract
limitations: ["..."]
risks: ["..."]
blockers: []
follow_ups: ["..."]
```

Every field required; empty lists are explicit. All paths are relative to
`/crucible/report`. Missing or unparsable report is a report-gate failure.
The claim has no `pushed`, `pull_request`, or `ci` fields: workers cannot
push and never see CI. Those facts are Crucible's to observe.

## ReviewReportV1 (the internal review)

```yaml
schema_version: "1.0"
task_external_id: "FDY-0042"
reviewed_head_sha: "abc123..."
reviewer: { kind: "crucible_review_execution", attempt_id: "01J..." }   # or { kind: "orchestrator", principal: "foundry" }
verdict: "approve"                 # approve | request_changes
findings:
  - { severity: "major", path: "src/ledger/import.py", line: 42, text: "..." }
summary: "..."
```

`reviewer_must_not_be_author` is enforced mechanically: the reviewer is a
different attempt (a `review` execution never shares an attempt with an
`implement` or `correct` one) or a different principal. A verdict of
`request_changes` does not move the task; Foundry reads it and decides.

## Derivation from the delivery pipeline

The operator's delivery skills define the pipeline: write, run every check
the repository defines, see the use case work, one non-author review before
the PR, PR opened only when the work is already proven, one external review
round, dispositions recorded, CI green on the final head, operator merge,
release by annotated tag through the repository's own release workflow.
Pre-PR verification is the proof; PR CI is the independent certification
that the submitted commit matches that proof. The PR is never the place to
find out whether the work builds.

What Crucible enforces is the mechanically checkable residue of that. What
it cannot check stays with Foundry or the user.

## Pre-PR gates (evaluated on the collected head, before any push)

| Gate | Passes when | Evidence consumed |
|---|---|---|
| `report_present` | report parsed, all fields present | CompletionClaim artifact |
| `exit_clean` | exit code 0 | attempt exit info |
| `commits_present` | the collected `work_branch` has at least one commit beyond `base_ref`, the bundle verifies, and the bundle head equals the reported `head_sha` | branch bundle from `collect` |
| `scope_contained` | every changed path matches `allowed_paths` and none matches `prohibited_paths` | diff path list from `collect` |
| `no_injected_files` | `AGENTS.md`, `CLAUDE.md`, other shims, `.crucible/`, and identity paths absent from diff and from any commit on `work_branch` | diff, `git log --stat` |
| `no_secrets` | secret scanner over the diff, every commit message, and the report finds nothing | scanner output artifact |
| `verification_ran` | for each `required_verification` command: Crucible itself re-ran the command after exit, in a fresh verifier container from the collected tree (same image, `network` per policy), and its exit matches `expect_exit`. The worker's own check logs are stored as a claim and shown to Foundry, never consumed by the gate | verifier exit and log (verified) |
| `run_evidence_present` | each `kind: artifact` verification path exists and is non-empty | artifacts |
| `criteria_mapped` | every `acceptance_criteria.id` appears in `acceptance_mapping` with a status | report |
| `dependencies_unchanged` | when `may_add_dependencies` is false: lockfiles and manifests unchanged | diff |
| `ci_unchanged` | when `may_modify_ci` is false: no change under workflow paths | diff |
| `workspace_clean` | no leftover ephemeral clusters or containers labeled for this attempt | provider reconcile |
| `internal_review_recorded` | a `ReviewReportV1` for this exact head SHA exists from a reviewer that is not the implementing attempt; `pending` until then (the task waits in `awaiting_internal_review`) | review report with reviewer identity |

## Publication and post-PR gates (23)

| Gate | Passes when | Evidence consumed |
|---|---|---|
| `branch_pushed_at_head` | remote `work_branch` head equals the collected head Crucible pushed, which equals the head the AcceptanceResult names | ls-remote after push |
| `pr_exists_head_matches` | PR exists, targets `deliverables.target`, head equals the pushed head, draft flag as the contract says, body is the one Crucible rendered | GitHub API |
| `external_review_rounds` | the count of accepted signals (review, comment, or `+1` reaction) from allowlisted logins on this PR is at least `required_rounds`; `pending` until then; `skipped` at 0 rounds. Advancement out of `external_feedback_received` requires this gate to pass, so a policy with more than one round waits for each | ExternalReview rows |
| `feedback_dispositions_complete` | every received review comment has a ReviewDisposition; `skipped` when `require_feedback_disposition` is false | dispositions |
| `ci_green_for_head` | the required-check set (23) is non-empty and every member concluded success on the accepted head; `pending` while any is queued or running **or while the set is empty**, so a head with no observed runs never passes; `fail` on any failure. Only `allow_no_ci: true` turns the empty set into `skipped` | CICertification |

Three evaluation rules. A pre-PR gate whose evidence is produced by
collection (`report_present`, `exit_clean`, `commits_present`,
`scope_contained`, `no_injected_files`, `no_secrets`,
`run_evidence_present`, `criteria_mapped`, `dependencies_unchanged`,
`ci_unchanged`) and is absent after collection is `fail`, not `pending`,
so a failed attempt reaches `pre_pr_gates_failed` unambiguously.
`internal_review_recorded` is the one pre-PR gate whose evidence arrives
after collection; it stays `pending` and the task waits in
`awaiting_internal_review`. A gate whose evaluator belongs to a later
phase (`verification_ran` and `workspace_clean` until C3's verifier
container exists) reports `deferred` (09): it is non-blocking for
`gates_passed`, is shown to Foundry with the phase that will implement
it, and can never report `pass`; once the evaluator ships, the gate
evaluates normally and `deferred` is no longer a possible result. The reviewer identity used
by `internal_review_recorded` and `reviewer_must_not_be_author` is the
authenticated principal that uploaded the report or the review attempt
that produced it, never the identity the document claims.

## Judgment (never a gate)

Whether the use case is actually seen working, whether review findings were
truly addressed, whether external feedback is correct or in scope, whether
"not exercised" is acceptable, why CI failed, whether the repository is
consumed by something when unclear, whether merged changes form a release,
whether a risk or limitation is acceptable, whether scope grew,
architectural soundness. Foundry evaluates these from the same evidence and
records an `AcceptanceResult`, a `ReviewDisposition`, or a `Decision`;
consequential ones go to the user as escalations.

## Evidence model (EvidenceV1)

`evidence`: `attempt_id` or `pull_request_id`, `kind` (exit_info,
diff_paths, diff_content, bundle_head, remote_head, pr_state,
review_received, check_run, scanner_result, artifact_present,
claim_parsed, review_report, verification_run, workspace_state,
transcript_match),
`observed_at`, `source` (`crucible`, `github`, or `worker`), `verified`
(true for `crucible` and for `github` deliveries that passed signature
verification or came from a poll), `payload`, `artifact_id`. Gates may
consume only `verified: true` evidence. Worker-asserted facts are shown to
Foundry but never satisfy a gate.
