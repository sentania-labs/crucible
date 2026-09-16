# 20. Proposed implementation phases

Each phase ends with a tagged release, a demo the operator can run with one
command, and a Foundry review. Nothing broad before something works end to
end. Phase sizes are bounded so a worker can deliver each as a reviewed PR
series; Foundry decomposes further at dispatch.

## Phase C0: spikes (no product code)

Deliverable: `docs/spikes/` with results for S1 to S8 (21). Acceptance:
each spike has a recorded outcome, a transcript artifact, and a decision
(proceed, adjust design, escalate). Unblocks the credential and provider
design details.

## Phase C1: walking skeleton

Compose with `postgres` and `crucible`; migrations for tasks, contracts,
executions, attempts, events; `/v1/tasks` submit, get, start; fake
provider; the task, execution, and attempt state machines; supervisor loop
with the supervisor lease; structured logging. Acceptance: a submitted task
runs through the fake provider to `reported` and the event log tells the
whole story; unit and integration tiers green; `make up` works from a
clean clone.

## Phase C2: gates, claims, evidence, wakes

CompletionClaimV1 parsing; evidence model; all diff-based gates
(`report_present`, `exit_clean`, `scope_contained`, `no_injected_files`,
`no_secrets`, `verification_ran`, `run_evidence_present`,
`criteria_mapped`, `dependencies_unchanged`, `ci_unchanged`); policies;
acceptance and decision endpoints; wakes with poll and webhook.
Acceptance: fake-provider runs reach `awaiting_acceptance` with correct
gate results for pass and fail fixtures; a scripted client reconstructs
state from the API.

## Phase C3: Docker provider and worker images

Socket proxy, provider implementation, create-request policy, workspace
prepare with worktrees and checkout leases, log capture, collect, cleanup,
reconcile by label, harness base images, the script-harness e2e image.
Acceptance: full e2e tier green including isolation, loss, orphan, restart,
and disconnect tests.

## Phase C4: harness adapters live

Claude Code, Codex, AGY adapters; credential mounts per S1 outcome; report
parsing from real runs; `branch_pushed_at_head`, `pr_exists_head_matches`,
`ci_green_for_head`, `external_review_round`, `release_shape` gates
against a throwaway public repository. Acceptance: `e2e-live` passes for
each harness on a trivial task that opens a PR.

## Phase C5: bootstrap import and readiness

Import API and commit; `foundry-ledger export --format crucible` and
`mark-migrated` on the Foundry side; `crucible-admin` commands; readiness
report. Acceptance: the real Foundry ledger is imported and authoritative;
readiness document complete; operator approval requested.

## Later (not scheduled)

Kubernetes provider with kind tests; Argo deployment manifests; object
storage for artifacts; observer UI; microVM isolation.
