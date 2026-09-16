# 05b. Policy schema (PolicyV1)

A policy is a named, versioned document uploaded by an admin principal and
referenced from every task contract. Versions are immutable once referenced.
Every tunable the specification mentions lives here, so nothing is a magic
default in code. The values below are the operator's initial defaults
(decisions of 2026-09-16).

```yaml
schema_version: "1.0"
name: "default-software"
version: 1
description: "Software repositories consumed by something else: branch, PR, merge, tag."

limits:
  timeout_seconds: { min: 300, max: 14400, default: 3600 }
  max_attempts: { max: 3, default: 2 }
  grace_seconds: 60                    # drain before kill
  stall_warn_seconds: 300
  stall_fail_seconds: 1800
  auth_retry_delay_seconds: 600
  escalation_stale_hours: 24
  wake_retry_hours: 24

retry:
  eligible_classes: ["environment", "lost", "auth_failure"]
  auth_failure_max: 1

concurrency:
  per_provider: 3
  per_harness: { claude_code: 1, codex: 1, agy: 1 }   # 1 while writable auth state is shared or synchronized

resources:
  cpus: 2
  memory: "4GiB"
  pids: 512
  tmpfs_total: "20GiB"

network:
  mode: "egress-proxy"                 # egress-proxy | none
  egress_allowlist:                    # hostnames the egress proxy permits for workers
    - "github.com"                     # read-only in effect: workers hold no GitHub credential
    - "objects.githubusercontent.com"
    - "pypi.org"
    - "files.pythonhosted.org"
    - "registry.npmjs.org"
  harness_endpoints: "from-harness"    # each adapter contributes its model endpoints (S6)

images:
  allowlist: ["crucible-worker:*", "ghcr.io/sentania-labs/crucible-worker:*"]
  require_default_or_retained: true    # candidates only with an explicit per-task override

git:
  author_name: "crucible-worker"
  author_email: "crucible-worker@users.noreply.github.com"
  commit_trailer: "Crucible-Attempt"   # trailer key carrying the attempt ID
  work_branch_pattern: "crucible/*"
  protected_branches: ["main", "release/*"]

repository:
  required_checks:                     # commands every contract's required_verification must include
    - "make lint"
    - "make test"
    - "make scan"                      # vulnerability, dependency, and secret scanning as the repo defines it

gates:
  pre_pr:                              # evaluated on the collected head before anything is pushed
    - report_present
    - exit_clean
    - commits_present
    - scope_contained
    - no_injected_files
    - no_secrets
    - verification_ran
    - run_evidence_present
    - criteria_mapped
    - dependencies_unchanged
    - ci_unchanged
    - workspace_clean
    - internal_review_recorded
  publication:                         # evaluated after Crucible pushes and opens the PR
    - branch_pushed_at_head
    - pr_exists_head_matches
  post_pr:                             # evaluated on the PR head as GitHub state arrives
    - external_review_rounds
    - feedback_dispositions_complete
    - ci_green_for_head
  skipped: []                          # release gates live on the release contract (24)

deliverables:
  allow_branch_only: false             # `branch` deliverables refused unless true
  on_out_of_band_head: "block"         # block: task to head_diverged and wake (09); the only option in v0.x

pull_request:
  require_pre_pr_verification: true
  open_only_after_pre_pr_gates_pass: true
  publish_requires_acceptance: true    # Foundry's AcceptanceResult precedes the push (09)
  title_from: "claim"                  # the worker's proposed title, validated by Crucible
  body_template: "default"             # Crucible renders the body from contract and verified evidence
  closing_refs: "contract_only"        # only deliverables[].closes may appear as closing keywords

internal_review:
  required: true
  required_for_corrections: false      # a correction contract may still request one
  reviewer_must_not_be_author: true
  executor: "orchestrator_or_crucible" # orchestrator uploads a ReviewReportV1, or requests a Crucible review execution

external_review:
  provider: "codex"
  reviewer_logins: ["chatgpt-codex-connector[bot]"]   # allowlisted identities (confirmed on sentania-labs/crucible#1)
  required_rounds: 1
  retrigger_after_correction: false
  require_review_on_final_sha: false
  require_feedback_disposition: true
  accepted_signals: ["review", "comment", "reaction:+1"]  # from an allowlisted login only; +1 is the reviewer's "no findings" signal
  round_counting: "per_pull_request"   # a round is one accepted signal on any head; final-SHA rule below
  wait_timeout_hours: 24               # then wake Foundry with reason external_review_overdue

ci_certification:
  require_green_on_final_sha: true
  required_checks: []                  # empty: use branch protection or ruleset required checks; if that is empty too, every observed run on the SHA
  allow_no_ci: false                   # false: zero observed runs is pending forever (wake on timeout); true only for a repository that intentionally has no CI
  on_failure: "escalate"
  automatic_retry: false
  automatic_worker_correction: false
  wait_timeout_hours: 6                # then wake Foundry with reason ci_certification_overdue

release:
  require_operator_approval: true
  authorization_recorder: "orchestrator_relay"   # orchestrator_relay: Foundry records the operator's verbatim approval and identity; operator_token: the operator's own principal must record it
  trigger: "tag"
  tag_pattern: "v{major}.{minor}.{patch}"
  version_files: []                    # paths whose version string must agree with the tag
  changelog_required: true

cleanup:
  workspace_on_success: "keep_diff_only"
  workspace_on_failure: "keep"
  container_remove: "always"           # only after logs_drained
  credential_volume_remove: "immediately_after_validated_sync"

retention:
  logs_and_transcripts_days: 90
  bootstrap_archive_days: 180
  completed_workspaces_days: 14
  wakes_after_ack_days: 30
  indefinite: ["events", "completion_claims", "decisions", "gate_results", "review_reports",
               "external_reviews", "dispositions", "ci_certifications", "release_records",
               "diffs", "artifact_metadata"]
```

## Validation

- Every field present with the listed types; unknown fields rejected.
- `gates.pre_pr`, `publication`, `post_pr`, and `skipped` partition the gate
  set defined in 11 and 23; a gate in none of them is an error.
- `retry.eligible_classes` is a subset of the `ExitClass` enum (07).
- `concurrency.per_harness` must be 1 for any harness whose credential
  `mount_mode` is `rw-narrow` (12); the API rejects the policy otherwise.
- `network.egress_allowlist` entries are hostnames, no wildcards in v0.x.
- `external_review.required_rounds: 0` makes the external review gates
  `skipped`; `reviewer_logins` must be non-empty when rounds are above 0.
- `ci_certification.allow_no_ci: true` and `deliverables.allow_branch_only:
  true` may only be set by an `operator` or `admin` principal and are
  recorded as decisions.
- `release.require_operator_approval` may only be `false` under a policy
  the operator uploaded (principal role `operator` or `admin`), which is
  recorded as a decision.

## Precedence with the task contract

The contract may narrow but never widen: `timeout_seconds` within limits,
`max_attempts` at or under the cap, `retry_on` a subset of
`eligible_classes`, `network` may select `none` under an `egress-proxy`
policy but not the reverse, `required_verification` a superset of
`repository.required_checks`, `deliverables[].closes` the only closing
references. Review round counts and CI rules come from policy only; a
contract cannot change them.
