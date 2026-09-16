# 20. Proposed implementation phases

Each phase ends with a tagged release, a demo the operator can run with one
command, and a Foundry review. Nothing broad before something works end to
end. Phase sizes are bounded so a worker can deliver each as a reviewed PR
series; Foundry decomposes further at dispatch.

Crucible itself is developed through branches and PRs, tagged releases,
and the tag-triggered SDLC release workflow, with CI on GitHub-hosted
runners because the integration and e2e tiers need Docker. Until Crucible
can supervise its own delivery, Foundry dispatches workers through the
current harnesses and performs the review and PR steps itself, recording
each in the bootstrap ledger.

## Phase C0: spikes (no product code)

Deliverable: `docs/spikes/` with results for S1 to S12 (21). S9 (rootless
Docker) and S10 (GitHub App token minting and publisher push) run first
because their outcomes change the local security arrangement and the
publication design. Acceptance: each spike has a recorded outcome, a
transcript artifact, and a decision (proceed, adjust design, escalate).

## Phase C1: walking skeleton

Compose with `postgres` and `crucible`; migrations for principals,
repositories, policies, tasks, contracts, executions, attempts, events;
`/v1/tasks` submit, get, start; fake provider; the task, execution, and
attempt state machines through `reported`; supervisor loop with the
supervisor lease; structured logging. Acceptance: a submitted task runs
through the fake provider to `reported` and the event log tells the whole
story; unit and integration tiers green; `make up` works from a clean clone.

## Phase C2: gates, claims, review, acceptance, wakes

CompletionClaimV1 and ReviewReportV1 parsing; evidence model; PolicyV1 and
the policy API; every pre-PR gate that needs only the collected tree, the
report, or the artifact store; `awaiting_internal_review` with review
upload and the `review` execution role (fake provider); acceptance,
correction, and decision endpoints; wakes with poll and webhook.
Acceptance: fake-provider runs reach `awaiting_acceptance` with correct
gate results for pass and fail fixtures, including a correction loop; a
scripted client reconstructs state from the API.

## Phase C3: Docker provider and worker images

Socket proxy on the arrangement S9 chose, egress proxy, provider
implementation, create-request policy, workspace prepare with dissociated
clones and checkout leases, collector (with branch bundle), verifier
(`verification_ran`, `workspace_clean`), log capture with
timestamp-and-hash resume, cleanup after `logs_drained`, retention actions,
reconcile by label, harness base images with version labels and digest
recording, the script-harness e2e image. Acceptance: full e2e tier green
including isolation, loss, orphan, restart, and disconnect tests.

## Phase C4: GitHub delivery

Repository registration, GitHub App token minting, the publisher container,
push and PR open with the rendered body, `branch_pushed_at_head` and
`pr_exists_head_matches`, webhook receiver and polling observation,
external review recording with allowlisted logins, dispositions, CI
certification with failure capture, `ready_for_merge` and `merged`
observation, `ci-decision`. Tested against a throwaway public repository
with the script harness (no subscription needed). Acceptance: a
script-harness task goes from submit to `merged` with one simulated
external review round and one correction; a forced CI failure lands in
`ci_certification_failed` with evidence and no retry.

## Phase C5: harness adapters live

Claude Code, Codex, AGY adapters; credential mounts per S1 outcome; report
parsing from real runs; version range declarations and refusal; `GET
/harnesses` and `GET /images`. Acceptance: `e2e-live` passes for each
harness on a trivial task that reaches `ready_for_merge` on the throwaway
repository.

## Phase C6: bootstrap import and readiness

Import API and commit; `foundry-ledger export --format crucible` and
`mark-migrated` on the Foundry side; `crucible-admin` commands; readiness
report (19). Acceptance: the real Foundry ledger is imported and
authoritative; readiness document complete; operator approval requested.
**This is the worker-supervision readiness milestone.**

## Phase C7: release lifecycle (24)

ReleaseContractV1, release gates, tag publisher, workflow observation.
Acceptance: a release of the throwaway repository tagged by Crucible from
an operator-authorized contract; a contract with a stale target SHA is
refused with nothing pushed.

## Phase C8: image promotion and GHCR publication

Renovate configuration, candidate image build in CI, canary procedure,
promotion endpoint, GHCR publication from the release workflow.

## Later (not scheduled)

Kubernetes provider with kind tests; deployment manifests; object storage
for artifacts; credential broker; observer UI; microVM isolation; Foundry
as a persistent service.
