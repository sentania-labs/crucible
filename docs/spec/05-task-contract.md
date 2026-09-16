# 05. Task contract schema (TaskContractV1)

The contract is the only way work enters Crucible. It is authored by the
orchestrator, validated on submit, stored verbatim with its SHA-256, and never
edited in place. Sanitized example: `examples/task-contracts/`.

## Fields

```yaml
schema_version: "1.0"
external_id: "FDY-0042"            # orchestrator's stable ID, unique per orchestrator
title: "Add retry policy to ledger import"
project: "example-service"         # free label for grouping
parent_external_id: null

repository:
  name: "example-service"          # a repository registered with Crucible (04); carries URL and auth
  base_ref: "main"                 # where the worker branches from
  work_branch: "crucible/FDY-0042" # created by Crucible; worker must not rename

scope:
  allowed_paths: ["src/ledger/**", "tests/ledger/**", "docs/ledger.md"]
  prohibited_paths: [".github/**", "**/secrets*"]
  may_add_dependencies: false
  may_modify_ci: false

objective: >
  One paragraph stating what must be true when the task is done.

context:                           # pointers, not prose dumps
  - { kind: "issue", ref: "https://github.com/example-org/example-service/issues/17" }
  - { kind: "doc", ref: "docs/ledger.md" }

project_instructions:              # what the worker must read first
  - { kind: "file", ref: "CONTRIBUTING.md" }
  - { kind: "skill", ref: "sdlc" }

acceptance_criteria:               # each becomes a row the worker must map to
  - id: "AC1"
    text: "Import of a bundle with a duplicate ID fails with a 409 and no partial write."
  - id: "AC2"
    text: "Existing import tests still pass."

required_verification:             # must include every check the repository policy requires
  - { id: "V1", command: "make lint", expect_exit: 0 }
  - { id: "V2", command: "make test", expect_exit: 0 }
  - { id: "V3", command: "make scan", expect_exit: 0 }
  - { id: "V4", kind: "artifact", path: "report/run-evidence.md" }

constraints:
  prohibited_actions:
    - "modify files outside allowed_paths"
    - "create or modify GitHub Actions workflows"
    - "delegate to other agents"
  network: "policy"                # policy | none

deliverables:
  - kind: "pull_request"           # Crucible pushes the branch and opens the PR (23)
    target: "main"
    draft: false
    closes: ["https://github.com/example-org/example-service/issues/17"]   # authorized closing refs; nothing else may be closed

reporting:
  report_schema: "CompletionClaimV1"
  report_dir: "/crucible/report"   # a separate rw mount, never inside the checkout
  progress_events: true

escalation:
  conditions:
    - "acceptance criteria conflict with existing behavior"
    - "a required verification command does not exist in the repository"
    - "any change outside allowed_paths appears necessary"
  action: "write report/blocked.md with the question and exit 75"

policy: { name: "default-software", version: 3 }

execution_request:                 # Foundry's selection; Crucible does not choose
  tier: "standard"                 # trivial | standard | complex, from the routing policy (05b)
  harness: "codex"
  model: "gpt-5-codex"             # passed through to the harness
  effort: "high"
  provider: "docker"
  image: "ghcr.io/sentania-labs/crucible-worker:codex-0.153.4"   # resolved to a digest at launch
  timeout_seconds: 5400
  rationale: "Codex-first to preserve Claude quota; task is mechanical."

lifecycle:
  max_attempts: 2                  # must not exceed the policy's cap
  retry_on: ["environment", "lost"]      # subset of the ExitClass enum (07); never on gate failure
  cleanup: "policy"

correction: null                   # present only on a correction version (below)
```

The submitting principal and receipt time are not contract fields: Crucible
records them on the task row and the `task_submitted` event from the
authenticated token, so a client cannot claim another principal.

Deliverable kinds: `pull_request` (the normal case), `branch` (push only,
no PR; requires `deliverables.allow_branch_only: true` in policy), and
`artifacts` (no repository output; used for spikes and reports). `branch`
and `pull_request` both pass through `publishing` (09); only
`pull_request` continues into review and CI certification (23).

## Correction versions

A correction is a new contract version attached through
`POST /tasks/{id}/corrections`. It carries the full contract (so the stored
contract always says what ran) plus:

```yaml
correction:
  of_version: 1
  reason: "external_review"        # external_review | ci_certification | needs_more_work | pre_pr_gates
  addresses:                       # what this version responds to
    - { kind: "review_comment", id: "2101", disposition_id: "01J..." }
  instructions: >
    Concrete, bounded instructions for the correcting worker.
  resume_from: "remote_branch"     # the worker starts from the current remote work_branch head
  request_internal_review: false   # true when Foundry judges the correction substantial (09)
```

A correction version may narrow `scope` and `objective` and may not widen
them; validation rejects a correction whose `allowed_paths` is not a subset
of the previous version's. `required_verification` may not shrink.

## Validation rules (deterministic, on submit)

- Every field above present; unknown fields rejected.
- `schema_version` major supported.
- `external_id` unique within the submitting principal's namespace.
- `repository.name` is registered (04); `base_ref` exists on the remote at
  validation time; `work_branch` matches the repository policy's branch
  pattern and is not a protected branch.
- `allowed_paths` and `prohibited_paths` are valid globs; the two must not
  fully overlap.
- Every `acceptance_criteria.id` and `required_verification.id` unique.
- `required_verification` includes every command the repository policy's
  `repository.required_checks` lists (05b); missing ones are a 422 naming
  the check.
- `policy` exists and is not retired; `lifecycle.max_attempts` within it.
- `execution_request.model` is an enabled entry of the routing policy
  whose `harness` matches, whose capability is allowed for `tier`, and
  whose quota pool is not over its soft limit; otherwise 422 naming the
  rule, so Foundry picks again.
- `execution_request.harness` is registered; `provider` is registered
  (only `fake` until C3) and supports the harness; `image` matches the provider's allowlist pattern,
  resolves to a known `WorkerImage` whose harness version is inside the
  adapter's supported range, and is not `retired`.
- `deliverables[].closes` entries are issues in the same repository.
- No credential reference or value anywhere: the contract has no auth
  fields by design; repository auth is Crucible configuration.
- `timeout_seconds` within policy bounds; `retry_on` a subset of both the
  `ExitClass` enum and the policy's `retry.eligible_classes`.
- The contract is authoritative for harness, model, provider, image, and
  policy. `POST /tasks/{id}/start` may carry an `overrides` object; applying
  it creates a new contract version through the amendment path and records
  an `amend` event.
- No string field contains something that matches the secret-pattern
  scanner (bearer-like tokens, private key headers). Rejected with 422 and
  the offending path, not the value.

## What the contract is not

It is not a prompt. Crucible renders the worker identity (06) from it. It is
not editable: an amendment or correction is a new version linked by events,
and an in-flight attempt keeps the version it started with. It never carries
a credential or a reference that resolves to one inside a worker.
