# 05. Task contract schema (TaskContractV1)

The contract is the only way work enters Crucible. It is authored by the
orchestrator, validated on submit, stored verbatim with its SHA-256, and never
edited in place. Sanitized example: `examples/task-contracts/`.

## Fields

```yaml
schema_version: "1.0"
external_id: "FDY-0042"            # orchestrator's stable ID, unique per orchestrator
title: "Add retry policy to ledger import"
project: "example-service"         # free label for grouping and policy lookup
parent_external_id: null

repository:
  url: "https://github.com/example-org/example-service.git"
  default_branch: "main"
  base_ref: "main"                 # where the worker branches from
  work_branch: "crucible/FDY-0042" # created by Crucible; worker must not rename
  auth: { source: "credential:github-app" }   # a named mount, never a value

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

required_verification:             # commands Crucible will look for in the report and logs
  - { id: "V1", command: "make lint", expect_exit: 0 }
  - { id: "V2", command: "make test", expect_exit: 0 }
  - { id: "V3", kind: "artifact", path: "report/run-evidence.md" }

constraints:
  prohibited_actions:
    - "git push --force"
    - "modify files outside allowed_paths"
    - "create or modify GitHub Actions workflows"
    - "delegate to other agents"
  network: "policy"                # policy | none | allowlist (names a policy entry)

deliverables:
  - { kind: "branch", ref: "crucible/FDY-0042" }
  - { kind: "pull_request", target: "main", draft: false }

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
  harness: "codex"
  model: "gpt-5-codex"             # passed through to the harness
  effort: "high"
  provider: "docker"
  image: "ghcr.io/sentania-labs/crucible-worker:codex-0.153"
  timeout_seconds: 5400
  rationale: "Codex-first to preserve Claude quota; task is mechanical."

lifecycle:
  max_attempts: 2                  # must not exceed the policy's cap
  retry_on: ["environment", "lost"]      # subset of the ExitClass enum (07); never on gate failure
  cleanup: "policy"
```

The submitting principal and receipt time are not contract fields: Crucible
records them on the task row and the `task_submitted` event from the
authenticated token, so a client cannot claim another principal.

## Validation rules (deterministic, on submit)

- Every field above present; unknown fields rejected.
- `schema_version` major supported.
- `external_id` unique within the submitting principal's namespace.
- `allowed_paths` and `prohibited_paths` are valid globs; the two must not
  fully overlap.
- Every `acceptance_criteria.id` and `required_verification.id` unique.
- `policy` exists and is not retired; `lifecycle.max_attempts` within it.
- `execution_request.harness` is registered; `provider` is registered and
  supports the harness; `image` matches the provider's allowlist pattern.
- `repository.auth.source` and every `credential:` reference name a
  configured mount, and none contains a value.
- `timeout_seconds` within policy bounds; `retry_on` a subset of both the
  `ExitClass` enum and the policy's `retry.eligible_classes`.
- The contract is authoritative for harness, model, provider, image, and
  policy. `POST /tasks/{id}/start` may carry an `overrides` object; applying
  it creates a new contract version through the amendment path and records
  an `amend` event, so the stored contract always says what actually ran.
- No string field contains something that matches the secret-pattern
  scanner (bearer-like tokens, private key headers). Rejected with 422 and
  the offending path, not the value.

## What the contract is not

It is not a prompt. Crucible renders the worker identity (06) from it. It is
not editable: an amendment is a new version linked by `amend` events, and an
in-flight attempt keeps the version it started with.
