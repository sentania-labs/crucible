# 05b. Policy schema (PolicyV1)

A policy is a named, versioned document uploaded by an admin principal and
referenced from every task contract. Versions are immutable once referenced.
Every tunable the specification mentions lives here, so nothing is a magic
default in code. The values below are the operator's initial defaults
(decisions of 2026-09-16).

```yaml
schema_version: "1.0"
name: "default-software"
version: 2                             # version 1 is the same document naming routing version 1
description: "Software repositories consumed by something else: branch, PR, merge, tag."   # the seeded row's own description names the version and what changed in it

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
  per_harness: { claude_code: 1, codex: 1, agy: 1 }   # all three mount rw-narrow, so all three are 1

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
  policy: { name: "default-routing", version: 2 }   # default-software v2 names routing v2; v1 named routing v1

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
  accepted_signals: ["reaction:+1", "review"]  # from an allowlisted login only; +1 is the durable "no findings" signal (S12 rerun); "comment" is opt-in
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
  `mount_mode` is `rw-narrow`; the mount mode comes from Crucible's
  credential configuration (12, 25), not from the policy, so the check
  runs against the configured credential sources at upload and at launch.
  All three real harnesses mount `rw-narrow`, so all three are capped at 1.
- The per-harness cap is checked **after** the checkout lease (10), not
  before. The lease is the older and more specific rule: an attempt whose
  checkout another attempt holds should be told that, not held back by a
  cap it never reached. Only a launch that could take the checkout is
  measured against the cap. A launch over the cap waits and records
  `harness_launch_deferred`; it is not a failure.
- An attempt counts against the cap until its credential copy has been
  synced back and removed, which is after `exited` (12), so `terminating`
  and `exited` attempts are still busy.
- `network.egress_allowlist` entries are hostnames, no wildcards in v0.x.
- `external_review.required_rounds: 0` makes the external review gates
  `skipped`; `reviewer_logins` must be non-empty when rounds are above 0.
- `external_review.accepted_signals` does not carry `comment` by default.
  The provider posts its summary comment within about ten seconds of the
  PR opening, minutes before any verdict, and edits it in place when the
  review lands (S12), so a comment accepted by default completes the cycle
  before there is anything to complete. A repository may add `comment`
  deliberately; even then a comment carrying the provider's summary marker
  is never a round (23).
- `external_review.components` lists the components one cycle expects and
  defaults to `["code"]`. The cycle logic depends on it being present, so
  a repository running code and security review sets both.
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

Uploaded and versioned like a policy. Foundry names a tier; Crucible
selects the model within it by the rule below, at every attempt launch,
inside the fenced transaction that moves the attempt to `launching`. The
rule is mechanical and total over database state: two supervisors given
the same rows select the same entry. Crucible still refuses an operator pin
whose model is absent or disabled, whose harness does not match the entry,
or whose quota pool is exhausted, and reports per-pool usage and
exhaustion marks on `GET /routing/usage`. (Decided 2026-09-20 on the
operator's direction; before C6b the contract pinned a model and Crucible
only refused.)

**Selection rule.** Candidates are the policy's entries that are enabled,
whose harness is enabled and holds a credential (25), whose capability is
in the tier's `allowed_capability`, whose pool is under its soft limit,
and whose pool carries no live exhaustion mark. Order them by: position of
their capability in the tier's `prefer` list (unlisted last); then quality
demotion as `rotation.quality_feedback` states; then weighted least-recent:
a candidate never launched on this project ranks first, otherwise rank by
`(now - last_launched_at) * weight`, largest first; then id, as the final
tie break. Recency is read directly from the latest AttemptMetrics per
model on the project, never through a paged task listing. The first candidate is selected. The launch event records the
selected entry, the derived image, and the ordered candidate list with
each exclusion's reason.

**Exhaustion marks.** A worker exit classified `quota_exhausted` (07, 16)
marks the attempt's pool exhausted until `reset_at`: the reset the harness
reported when the adapter can parse one, otherwise now plus the pool's
`default_cooldown_seconds`. A parsed reset is used as given, even when it
lies beyond any task's wait cap; the cap ends the task, it does not shorten
the pool's fact. Because a mark is shared by every task, it is written only
when the harness's own provider-error event (07) says the provider refused
for quota, never from quota-shaped text elsewhere in a transcript; text
alone may still classify that one attempt `quota_exhausted` and reroute it,
without a mark. Marks are rows, survive a restart, expire on
their own, and can be cleared by the administrator with a reason (25). A
launch-time reservation that finds the pool over its soft limit does not
create a mark; the soft limit is Crucible's own count, the mark is the
provider's word. A harness that is
disabled, by configuration or by the administrator's flag (25), is likewise
a contract problem at submit on `execution_request.harness`, answered 422
with the reason, not a refusal the task discovers later. The operator's
direction (2026-09-16): rotate work across providers by capability, cost,
and speed; never spend frontier models on simple work; local models carry
routine work once they exist.

`default-routing` version 3, seeded by C6b, is the document below: version 2's
roster plus `default_cooldown_seconds` on every pool and the `reroute` block.
Versions 1 and 2 stay beside it because policy versions reference them and a
version is immutable; version 1's ids were placeholders, version 2's are the
ones the CLIs themselves list.

```yaml
schema_version: "1.0"
name: "default-routing"
version: 3
tiers:                                 # task tiers Foundry assigns in the contract's execution_request.tier
  trivial:   { allowed_capability: ["small", "mid"],  prefer: ["small"] }     # frontier is refused, not merely dispreferred
  standard:  { allowed_capability: ["mid", "small"],  prefer: ["mid"] }       # a task that truly needs frontier is marked complex
  complex:   { allowed_capability: ["frontier", "mid"], prefer: ["frontier"] }
models:                                # every entry weight 1: rotation is least-recent until the outcomes say otherwise
  - { id: "claude-haiku-4-5",      harness: claude_code, endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: anthropic-sub, weight: 1, enabled: true }
  - { id: "claude-sonnet-5",       harness: claude_code, endpoint: subscription, capability: mid,      cost: medium, speed: fast,   pool: anthropic-sub, weight: 1, enabled: true }
  - { id: "claude-fable-5-1",      harness: claude_code, endpoint: subscription, capability: frontier, cost: high,   speed: medium, pool: anthropic-sub, weight: 1, enabled: true }
  - { id: "gpt-5.6-luna",          harness: codex,       endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: openai-sub,    weight: 1, enabled: true }
  - { id: "gpt-5.6-terra",         harness: codex,       endpoint: subscription, capability: mid,      cost: medium, speed: medium, pool: openai-sub,    weight: 1, enabled: true }
  - { id: "gpt-5.6-sol",           harness: codex,       endpoint: subscription, capability: frontier, cost: high,   speed: medium, pool: openai-sub,    weight: 1, enabled: true }
  - { id: "gpt-6-astra",           harness: codex,       endpoint: subscription, capability: frontier, cost: high,   speed: slow,   pool: openai-sub,    weight: 1, enabled: true }
  - { id: "gemini-3.8-flash-low",  harness: agy,         endpoint: subscription, capability: small,    cost: low,    speed: fast,   pool: google-sub,    weight: 1, enabled: true }
  - { id: "gemini-3.8-flash-high", harness: agy,         endpoint: subscription, capability: mid,      cost: medium, speed: medium, pool: google-sub,    weight: 1, enabled: true }
  - { id: "gemini-3.1-pro-high",   harness: agy,         endpoint: subscription, capability: frontier, cost: high,   speed: slow,   pool: google-sub,    weight: 1, enabled: true }
pools:                                 # budget_units is one of attempts | tokens_out | cost_units, all recorded in AttemptMetrics
  anthropic-sub: { window: "5h", budget_units: "tokens_out", soft_limit: 0, default_cooldown_seconds: 18000 }   # 0 means observe only until measured; cooldown is the mark length when the harness reports no reset
  openai-sub:    { window: "5h", budget_units: "tokens_out", soft_limit: 0, default_cooldown_seconds: 18000 }
  google-sub:    { window: "5h", budget_units: "attempts",   soft_limit: 0, default_cooldown_seconds: 3600 }
rotation:
  strategy: "weighted-least-recent"    # among models allowed for the tier, prefer the preferred capability, then the least recently used, weighted
  quality_feedback: true               # a model whose last N attempts on this project ended in gate failures or corrections drops one preference step
  quality_window: 20
reroute:                               # C6b: what happens when a worker dies of quota (16)
  reroute_max: 3                       # reroutes per task per contract version, counted apart from lifecycle.max_attempts
  resume_max_wait_seconds: 86400       # longest a task waits in awaiting_quota before it ends reported with a wake
```

Version 3 carries subscription entries only. Version 4 is immutable version 3
plus a `spark-local` pool with `max_concurrency: 4` and one disabled model entry:
`gpt-oss:120b`, harness `hermes`, endpoint `local`, capability `mid`, cost
`none`, and the configured Spark `/v1` URL. Without that configuration the URL
is null and `disabled_reason` records that it is not configured. With the URL,
the entry remains disabled with the reason that the enablement gate has not
passed. Version 5 differs only by enabling that entry and removing the reason,
after the single-task and four-way live gates pass. No Qwen model is in either
version.

Model ids are what the harness accepts. Which routing version a deployment
uses is the policy's own choice. `default-software` version 2, seeded by
migration 0009 (C5b), is version 1's document naming `default-routing`
version 2, and it is the version the shipped example policy, the example
contract, the compose smoke, the fixtures, and the tiers all reference, so
the verified roster is what a fresh deployment routes with. Version 1 stays
beside it naming `default-routing` version 1, because a policy version is
immutable once referenced. Where a caller names no version, the newest
version of the policy is the one that applies. The Codex pool in version 2 is exactly the
operator's roster decision: `gpt-5.6-luna` small, `gpt-5.6-terra` mid, `gpt-5.6-sol` and
`gpt-6-astra` frontier. Nothing older and no mini; `gpt-5.5` is a recorded
fallback outside the pool. The ids come from the CLI's own listing, the cost
and speed classes are Foundry's tiering. AGY carries its effort inside the
model id (07), which is why the Flash entries differ only by suffix. A `local`
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
records the tier with its rationale in the contract (05); Crucible records
the selected model and the outcome in AttemptMetrics (03, 14) and exposes
`GET /routing/history?model=&project=`, which returns both the model
Crucible selected and the model the transcript named (14), which is what
the least-recent and quality terms of the selection rule read.
Foundry's judgment is the tier; the policy and the rule do the rest. The
operator alone may pin (05).

`max_concurrency` is a pool-wide count of launching and running attempts. It is
enforced independently of `concurrency.per_harness`; the local Hermes pool may use
all four Spark slots, while subscription routes retain their credential-derived
per-harness caps. A fifth Spark task stays scheduled and records
`harness_launch_deferred` until capacity is released.
