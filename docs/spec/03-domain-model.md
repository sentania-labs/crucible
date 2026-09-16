# 03. Domain model and ownership of authoritative state

Every entity below is versioned (`schema_version` on contracts, Alembic
revision on tables). Crucible's PostgreSQL database is the single authority
for all of them after the bootstrap handoff (15). Foundry keeps no competing
ledger.

## Entities

| Entity | What it is | Owner of truth |
|---|---|---|
| **Task** | One unit of delegated work: contract, current state, references, results. `external_id` carries the orchestrator's stable ID (for example `FDY-0006`). | Crucible |
| **TaskContract** | Immutable, versioned document the orchestrator submitted. Stored verbatim with its hash. A change is a new task or an explicit amendment event. | Foundry authors, Crucible stores |
| **Policy** | Named, versioned set of deterministic rules: timeouts, retry counts, concurrency caps, required gates, allowed paths, network mode, cleanup and retention. Referenced by name and version from the contract. | Operator authors, Crucible enforces |
| **Harness** | A supported worker CLI: `claude_code`, `codex`, `agy`. Carries version pin, launch spec, credential spec, report parser. | Crucible |
| **ExecutionProvider** | Where a worker runs: `fake`, `docker`, `kubernetes`, `hostprocess`. Carries capabilities (isolation level, network control, resource limits). | Crucible |
| **Execution** | The decision to run a task with a given harness, model, and provider under a policy. One task has one or more executions (a retry after a policy change is a new execution). | Crucible |
| **Attempt** | One launch inside an execution. Retries per policy create attempts. Holds container or job reference, start and end, exit reason, resource usage. | Crucible |
| **Worker** | The running process for an attempt: identity injected, lease, heartbeats, last observed activity. Ends when the attempt ends. | Crucible |
| **WorkerIdentity** | The rendered instruction bundle mounted into the worker: role, objective, boundaries, contract, project guidance, reporting requirements. Stored by hash; content stored as an artifact. | Foundry authors the inputs, Crucible renders and records |
| **Lease** | A time-bounded claim: supervisor lease (which instance supervises), attempt lease (which worker owns which checkout), checkout lease (which attempt may write a working tree). Expiry is the loss signal. | Crucible |
| **Heartbeat** | Timestamped liveness observation for an attempt: container running, log bytes advanced, filesystem changed. Absence over a policy window means stall. | Crucible |
| **Event** | Append-only, ordered record of everything that happened: state transitions, launches, observations, gate results, wakes, decisions. The audit trail. | Crucible |
| **Artifact** | A file or bundle produced by a run: worker report, diff, test log, screenshots, transcript, rendered identity. Content-addressed, stored outside the database, referenced by row. | Crucible |
| **LogStream** | Captured stdout and stderr of an attempt, chunked, with byte offsets. | Crucible |
| **Evidence** | A typed claim with a pointer to an artifact or an observed fact: "tests ran, exit 0, log at X", "PR head equals reviewed SHA". Gates consume evidence. | Crucible records, gates evaluate |
| **CompletionClaim** | The worker's completion report as parsed from the report directory: summary, changed files, refs, checks, criteria mapping, limitations, blockers, follow-ups. A claim, never an acceptance. | Worker authors, Crucible parses |
| **GateResult** | Outcome of one deterministic gate for one attempt: pass, fail, pending, skipped, with the evidence it used. | Crucible |
| **AcceptanceResult** | Foundry's semantic verdict on a task: accepted, rejected, needs_more_work, with reasoning text. Recorded, never computed, by Crucible. | Foundry |
| **Decision** | A recorded choice by Foundry or the user that Crucible needed before proceeding: approval to merge, risk accepted, scope clarified. Carries the verbatim words and who said them. | Foundry or user |
| **Escalation** | An open question Crucible or Foundry raised for the user, linked to the wake that carried it and the decision that closed it. | Crucible raises, user closes |
| **Wake** | A notification to Foundry that judgment is required, with reason and the entities involved. Delivered by webhook and retained for poll. | Crucible |

## Relationships

```
Task 1..n Execution 1..n Attempt 1..1 Worker
Task 1..1 TaskContract (immutable)
Task n..1 Policy (by name+version)
Execution n..1 Harness, n..1 ExecutionProvider
Attempt 1..n Heartbeat, 1..n LogStream, 1..n Artifact, 1..n Evidence
Attempt 1..1 CompletionClaim (optional), 1..n GateResult
Task 0..n AcceptanceResult, 0..n Decision, 0..n Escalation, 0..n Wake
Everything 1..n Event
```

## Invariants Crucible enforces

- A task contract is never modified in place. Amendments are events with a
  new contract version linked to the same task.
- At most one attempt holds a checkout lease for a given repository and
  branch at a time. Two attempts never share a working tree.
- An attempt cannot enter `succeeded` without a parsed CompletionClaim.
- A task cannot enter `gates_passed` unless every gate the policy requires
  has a `pass` GateResult for the final attempt.
- A task cannot enter `accepted` without an AcceptanceResult from an
  authenticated orchestrator principal. Crucible never creates one.
- Credentials never appear in any entity. The credential spec names a mount
  source, not a value.
- Events are append-only. Corrections are new events.
