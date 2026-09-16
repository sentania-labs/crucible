# 05b. Policy schema (PolicyV1)

A policy is a named, versioned document uploaded by an admin principal and
referenced from every task contract. Versions are immutable once referenced.
Every tunable the specification mentions lives here, so nothing is a magic
default in code.

```yaml
schema_version: "1.0"
name: "default-software"
version: 1
description: "Software repositories consumed by something else: branch, PR, merge."

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
  per_harness: { claude_code: 1, codex: 1, agy: 1 }   # 1 while a harness needs rw-narrow credentials

resources:
  cpus: 2
  memory: "4GiB"
  pids: 512
  tmpfs_total: "20GiB"

network:
  mode: "egress-proxy"                 # egress-proxy | none
  egress_allowlist:                    # hostnames the egress proxy permits
    - "github.com"
    - "api.github.com"
    - "objects.githubusercontent.com"
    - "pypi.org"
    - "files.pythonhosted.org"
    - "registry.npmjs.org"
  harness_endpoints: "from-harness"    # each adapter contributes its model endpoints (S6)

images:
  allowlist: ["crucible-worker:*", "ghcr.io/sentania-labs/crucible-worker:*"]

git:
  author_name: "crucible-worker"
  author_email: "crucible-worker@users.noreply.github.com"
  commit_trailer: "Crucible-Attempt"   # trailer key carrying the attempt ID

gates:
  required:
    - report_present
    - exit_clean
    - scope_contained
    - no_injected_files
    - no_secrets
    - verification_ran
    - run_evidence_present
    - criteria_mapped
    - branch_pushed_at_head
    - review_round_recorded
    - pr_exists_head_matches
    - ci_green_for_head
    - dependencies_unchanged
    - ci_unchanged
    - workspace_clean
  pending_tolerant: ["external_review_round", "ci_green_for_head"]
  skipped: ["release_shape"]
  ci:
    required_checks: []                # empty means every non-skipped run for the SHA must succeed
  external_review:
    bot_login: ""                      # empty disables the gate (it stays skipped)
    accepted_signals: ["review", "reaction:+1"]

cleanup:
  workspace_on_success: "keep_diff_only"
  workspace_on_failure: "keep"
  workspace_max_age_hours: 168
  container_remove: "always"           # only after logs_drained
  credential_volume_remove: "always"

retention:
  logs_days: 90
  wakes_after_ack_days: 30
  bootstrap_archive_days: 180
```

## Validation

- Every field present with the listed types; unknown fields rejected.
- `gates.required`, `pending_tolerant`, and `skipped` partition the gate set
  defined in 11; a gate in none of them is an error.
- `retry.eligible_classes` is a subset of the `ExitClass` enum (07).
- `concurrency.per_harness` must be 1 for any harness whose credential
  `mount_mode` is `rw-narrow` (12); the API rejects the policy otherwise.
- `network.egress_allowlist` entries are hostnames, no wildcards in v0.x.

## Precedence with the task contract

The contract may narrow but never widen: `timeout_seconds` within limits,
`max_attempts` at or under the cap, `retry_on` a subset of
`eligible_classes` (retry happens only when the class is in both), `network`
may select `none` under an `egress-proxy` policy but not the reverse.
