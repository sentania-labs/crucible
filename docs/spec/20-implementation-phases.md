# 20. Proposed implementation phases

Each phase ends with a merged, verified PR, a demo the operator can run
with one command, and a Foundry review. Releases are cut only when the
operator judges a milestone functional (operator decision, 2026-09-16:
"I'm not sure we need to keep tagging non-functional things"); the
worker-supervision readiness milestone (C6) is the next planned release
after v0.2.1. Nothing broad before something works end to
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
**Delivered 2026-09-16.** Live evidence: `make e2e-github` takes a
script-harness task on a throwaway GitHub repository from submit through
publication, PR open with the rendered body, polled observation, CI
certification, and an observed merge, with the installation token absent
from every location still readable once the publisher container is gone.

## Phase C5: harness adapters live

Claude Code, Codex, AGY adapters; credential mounts per S1 outcome; report
parsing from real runs; version range declarations and refusal; `GET
/harnesses` and `GET /images`. Acceptance: `e2e-live` passes for each
harness on a trivial task that reaches `ready_for_merge` on the throwaway
repository. Delivered in two parts: C5a is the adapter port, the three real
adapters and the script harness behind it, the per-attempt credential copy
(12), report parsing and exit classification from real runs, version
pinning with refusal, `GET /harnesses` and `GET /images`, and the
`e2e-live` tier; C5b is the admin API and credential onboarding (25).

**C5a delivered 2026-09-17.** Live evidence: `make e2e-live` takes a
trivial task through each enabled harness in its hardened image with the
credential seeded into a per-attempt copy owned by the worker's uid, to
`ready_for_merge` on the throwaway repository, with no credential value
found in the create request, the events, any text column, the log chunks,
the artifact store, or the pull request.

**C5b delivered 2026-09-17.** Live evidence: `make e2e-admin` runs the
bounded probe for each of the three harnesses with its dedicated credential
and then every other operation in 25's table through both entry points, the
API and `crucible-admin`, against the rootless daemon, with rotate and remove
acting on scratch copies and every probe conclusive and `completed` well
inside the 120 s bound.

**C5 is complete.**

## Phase C6: bootstrap import and readiness

Import API and commit; `foundry-ledger export --format crucible` and
`mark-migrated` on the Foundry side; `crucible-admin` commands; readiness
report (19). Acceptance: the real Foundry ledger is imported and
authoritative; readiness document complete; operator approval requested.
**This is the worker-supervision readiness milestone.**

## Phase C6b: class-based selection and quota reroute

Contract carries a tier and no model; Crucible selects at launch by the
05b rule; exhaustion marks; reroute to the next candidate with WIP
committed to the branch; `awaiting_quota` with timed resume; caps;
administrator read and clear of marks; `default-routing` version 3.
Acceptance: an e2e run in which a scripted quota failure on one image
reroutes to a second image and finishes, with the WIP commit and the
reroute event in the record; a run with every pool marked waits and
resumes on schedule across a supervisor restart. Replaces ADR 0014's
predictive reads, which the operator rejected on 2026-09-19.

## Phase C6d: Hermes on the DGX Spark local pool

Hermes 0.19.0 worker image; credential-free local endpoint plumbing; mandatory
usage evidence and AttemptMetrics; exact plain-HTTP proxy ACLs derived from enabled
routing entries; `spark-local` pool concurrency of four; routing version 4 disabled
before the gate and version 5 only after it. Acceptance: one real Hermes task
passes report, scope, verification, transcript-integrity, routing-history, and metrics
checks; four concurrent tasks complete without shared state while a fifth waits; all
standard and live tiers pass. Delivered 2026-09-20 after the single-task and four-way
gates passed; immutable routing version 5 enables the verified entry.

## Phase C7: release lifecycle (24)

ReleaseContractV1, release gates, tag publisher, workflow observation.
Acceptance: a release of the throwaway repository tagged by Crucible from
an operator-authorized contract; a contract with a stale target SHA is
refused with nothing pushed.

## Phase C8: Kubernetes execution provider (26)

The provider adapter (prepare, launch, observe, logs, collect, terminate,
cleanup, reconcile as Jobs and Pods), per-attempt NetworkPolicy generated
from the same policy that generates the proxy allowlist, per-attempt
credential Secrets with `rw-narrow` sync-back, the namespace readiness
probe, provider capabilities and status-page fields, and the `make e2e-kind`
tier in CI. Acceptance: the Docker end-to-end cases plus the Kubernetes
cases in 26 pass on kind in CI; readiness rows 5, 7, 11, 12, and 23 are
re-proven on that tier and cited in 19. Standard container runtime;
no runtime class.

## Phase C9: cluster deployment

Deployment manifests for the `crucible` and `crucible-workers` namespaces
(api and supervisor Deployments, PostgreSQL for the lab or an external
connection, RBAC, Pod Security admission labels, default-deny
NetworkPolicy, ResourceQuota, storage class reference, SealedSecret or
ExternalSecret shapes for the App key and harness credentials), pinned to
an exact image tag, consumed by an Argo Application in the deployment
repository. Acceptance: the release image comes up on the lab cluster, the
status page shows the namespace probe green, the operator logs the
harnesses in from the admin UI, and one trivial task runs end to end there.
Lab-side prerequisites are the checklist in 26 and are lab-admin work.

## Phase C10: image promotion and GHCR publication

Renovate configuration, candidate image build in CI, canary procedure,
promotion endpoint, GHCR publication from the release workflow.

## Later (not scheduled)

Kubernetes provider with kind tests; deployment manifests; object storage
for artifacts; credential broker; observer UI; a runtime class for worker
pods (gVisor or Kata, the microVM step the operator deferred on 2026-09-21);
Foundry
as a persistent service.
