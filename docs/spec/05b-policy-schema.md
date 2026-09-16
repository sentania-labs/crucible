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

routing:                               # see RoutingPolicyV1 below; this names which one applies
  policy: { name: "default-routing", version: 1 }

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
  components: ["code"]                 # review components in one cycle; ["code", "security"] where the repo runs both
  round_counting: "completed_cycles"   # a round is one completed cycle (all components terminal) on a published head
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

## Routing policy (RoutingPolicyV1)

Uploaded and versioned like a policy. Foundry selects from it; Crucible
never selects, but refuses a contract whose model is absent or disabled,
whose harness does not match the entry, or whose quota pool is exhausted,
and reports per-pool usage on `GET /routing/usage`. The operator's
direction (2026-09-16): rotate work across providers by capability, cost,
and speed; never spend frontier models on simple work; local models carry
routine work once they exist.

```yaml
schema_version: "1.0"
name: "default-routing"
version: 1
tiers:                                 # task tiers Foundry assigns in the contract's execution_request.tier
  trivial:   { allowed_capability: ["small", "mid"],  prefer: ["small"] }     # frontier is refused, not merely dispreferred
  standard:  { allowed_capability: ["mid", "small"],  prefer: ["mid"] }       # a task that truly needs frontier is marked complex
  complex:   { allowed_capability: ["frontier", "mid"], prefer: ["frontier"] }
models:
  - { id: "claude-fable-5-1",      harness: claude_code, endpoint: subscription, capability: frontier, cost: high,  speed: medium, pool: anthropic-sub, weight: 1, enabled: true }
  - { id: "claude-sonnet-5",       harness: claude_code, endpoint: subscription, capability: mid,      cost: medium, speed: fast,  pool: anthropic-sub, weight: 2, enabled: true }
  - { id: "gpt-5-codex",           harness: codex,       endpoint: subscription, capability: frontier, cost: high,  speed: medium, pool: openai-sub,    weight: 1, enabled: true }
  - { id: "gpt-5-codex-mini",      harness: codex,       endpoint: subscription, capability: mid,      cost: medium, speed: fast,  pool: openai-sub,    weight: 2, enabled: true }
  - { id: "gemini-3-pro",          harness: agy,         endpoint: subscription, capability: mid,      cost: medium, speed: fast,  pool: google-sub,    weight: 2, enabled: true }
  - { id: "local-spark-large",     harness: codex,       endpoint: local, endpoint_url: "http://spark.example.internal:8000/v1", capability: mid,   cost: none, speed: medium, pool: local-spark, weight: 3, enabled: false }   # DGX Spark, when present
  - { id: "local-rtx-small",       harness: codex,       endpoint: local, endpoint_url: "http://rtx.example.internal:8000/v1",   capability: small, cost: none, speed: fast,   pool: local-rtx,   weight: 3, enabled: false }   # RTX 9060 16 GB
pools:                                 # budget_units is one of attempts | tokens_out | cost_units, all recorded in AttemptMetrics
  anthropic-sub: { window: "5h", budget_units: "tokens_out", soft_limit: 0 }   # 0 means observe only until measured
  openai-sub:    { window: "5h", budget_units: "tokens_out", soft_limit: 0 }
  google-sub:    { window: "24h", budget_units: "tokens_out", soft_limit: 0 }
  local-spark:   { window: "1h", budget_units: "attempts", soft_limit: 0 }
  local-rtx:     { window: "1h", budget_units: "attempts", soft_limit: 0 }
rotation:
  strategy: "weighted-least-recent"    # among models allowed for the tier, prefer the preferred capability, then the least recently used, weighted
  quality_feedback: true               # a model whose last N attempts on this project ended in gate failures or corrections drops one preference step
  quality_window: 10
```

Model ids are what the harness accepts; they are illustrative here and
the operator's private routing policy holds the real ones. A `local`
entry must carry `endpoint_url`; Crucible passes it to the adapter's
launch context and adds its hostname to that attempt's egress allowlist.
A pool's `budget_units` must be a unit AttemptMetrics records; when a
harness reports no token counts, `tokens_out` is recorded as null and the
pool falls back to counting attempts, which `GET /routing/usage` states.
The quota check runs twice: advisory at submit (422 so Foundry can pick
again) and authoritative at attempt launch, where the supervisor reserves
the pool capacity in the same fenced transaction that moves the attempt
to `launching`; if the pool crossed its soft limit since submit, the
attempt is refused with class `quota_exhausted` and Foundry is woken. A `local`
entry must carry `endpoint_url`; Crucible passes it to the adapter's
launch context and adds its hostname to that attempt's egress allowlist.
A pool's `budget_units` must be a unit AttemptMetrics records; when a
harness reports no token counts, `tokens_out` is recorded as null and the
pool falls back to counting attempts, which `GET /routing/usage` states.
The quota check runs twice: advisory at submit (422 so Foundry can pick
again) and authoritative at attempt launch, where the supervisor reserves
the pool capacity in the same fenced transaction that moves the attempt
to `launching`; if the pool crossed its soft limit since submit, the
attempt is refused with class `quota_exhausted` and Foundry is woken. Foundry
records the tier and the chosen model with its rationale in the contract
(05); Crucible records the outcome in AttemptMetrics (03, 14) and exposes
`GET /routing/history?model=&project=` so the next selection is informed.
Selection itself stays a Foundry judgment; the policy bounds it.
