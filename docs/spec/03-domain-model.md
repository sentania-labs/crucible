# 03. Domain model and ownership of authoritative state

Every entity below is versioned (`schema_version` on contracts, Alembic
revision on tables). Crucible's PostgreSQL database is the single authority
for all of them after the bootstrap handoff (15). Foundry keeps no competing
ledger.

## Entities

| Entity | What it is | Owner of truth |
|---|---|---|
| **Task** | One unit of delegated work: contract, current state, references, results. `external_id` carries the orchestrator's stable ID (for example `FDY-0006`). | Crucible |
| **TaskContract** | Immutable, versioned document the orchestrator submitted. Stored verbatim with its hash. A change is a new version linked by an amendment or correction event. | Foundry authors, Crucible stores |
| **Policy** | Named, versioned set of deterministic rules: timeouts, retry counts, concurrency caps, required gates, allowed paths, network mode, review and CI rules, cleanup and retention. Referenced by name and version from the contract. | Operator authors, Crucible enforces |
| **Repository** | A target repository Crucible may act on: URL, GitHub App installation reference, default branch, policy name. Registered by an admin; a contract may only name a registered repository. | Operator registers, Crucible holds |
| **Harness** | A supported worker CLI: `claude_code`, `codex`, `agy`. Carries the adapter's supported version range, launch spec, credential spec, report parser. | Crucible |
| **WorkerImage** | A built worker image: harness, installed harness version, digest, build inputs, promotion state (`candidate`, `default`, `retained`, `retired`). | Crucible |
| **RoutingPolicy** | Named, versioned document listing every model Foundry may name: harness, endpoint kind (subscription, local), capability tier, cost class, speed class, quota pool, rotation weight, enabled flag. Foundry chooses from it; Crucible validates against it and reports usage per pool. | Operator authors, Crucible enforces |
| **AttemptMetrics** | Per attempt: wall time, harness-reported tokens or cost where available, quota pool, exit class, gate pass count, correction count that followed, acceptance verdict. The history Foundry reads before selecting. | Crucible |
| **ExecutionProvider** | Where a worker runs: `fake`, `docker`, `kubernetes`, `hostprocess`. Carries capabilities (isolation level, network control, resource limits). | Crucible |
| **Execution** | The decision to run a task with a given harness, model, image, and provider under a policy, in a given role: `implement`, `correct` (against the existing branch), or `review` (non-author internal review). One task has one or more executions. | Crucible |
| **Attempt** | One launch inside an execution. Retries per policy create attempts. Holds container or job reference, image digest, start and end, exit reason, resource usage. | Crucible |
| **Worker** | The running process for an attempt: identity injected, lease, heartbeats, last observed activity. Ends when the attempt ends. | Crucible |
| **WorkerIdentity** | The rendered instruction bundle mounted into the worker: role, objective, boundaries, contract, project guidance, reporting requirements. Stored by hash; content stored as an artifact. | Foundry authors the inputs, Crucible renders and records |
| **Lease** | A time-bounded claim: supervisor lease, attempt lease, checkout lease (which attempt may write a working tree). Expiry is the loss signal. | Crucible |
| **Heartbeat** | Timestamped liveness observation for an attempt. Absence over a policy window means stall. | Crucible |
| **Event** | Append-only, ordered record of everything that happened, including every outward-facing GitHub action Crucible took. The audit trail. | Crucible |
| **Artifact** | A file or bundle produced by a run: worker report, diff, branch bundle, test log, screenshots, transcript, rendered identity, review report. Content-addressed, stored outside the database. | Crucible |
| **LogStream** | Captured stdout and stderr of an attempt, chunked. | Crucible |
| **Evidence** | A typed, verified observation with a pointer to an artifact or an observed fact. Gates consume evidence. | Crucible records, gates evaluate |
| **CompletionClaim** | The worker's completion report: summary, changed files, local head SHA, checks it ran, criteria mapping, proposed PR title and body, limitations, blockers, follow-ups. A claim, never an acceptance. | Worker authors, Crucible parses |
| **ReviewReport** | The internal non-author review of a collected head: findings with severity and location, verdict. Produced by a `review` execution or uploaded by Foundry. | Reviewer authors, Crucible stores |
| **GateResult** | Outcome of one deterministic gate for one attempt or PR head: pass, fail, pending, skipped, error, with the evidence it used. | Crucible |
| **AcceptanceResult** | Foundry's semantic verdict on a collected head: accepted, rejected, needs_more_work, with reasoning. Recorded, never computed, by Crucible. | Foundry |
| **PullRequest** | The PR Crucible opened for a task: number, URL, base, head SHA history, state, merge SHA, merged by. Crucible is the only writer of the PR. | Crucible |
| **ExternalReview** | One review round received from an allowlisted external reviewer identity on a PR: reviewer login, reviewed SHA, signal kind, comments, reactions, received time. | GitHub emits, Crucible records |
| **ReviewDisposition** | Foundry's recorded interpretation of one external review comment: `fix`, `decline`, `out_of_scope`, `already_addressed`, `question`, with reasoning. Every received comment needs one before the PR can be ready. | Foundry |
| **CICertification** | The observed state of required checks for a PR head SHA: pending, green, failed, with the check runs, workflow, job, and log pointers captured. | GitHub emits, Crucible records |
| **ReleaseContract** | Foundry's explicit, operator-authorized request to release: repository, target branch and SHA, version and tag, included PRs, notes, required gates, the authorization decision. Immutable once submitted. | Foundry authors, operator authorizes, Crucible stores |
| **Release** | The lifecycle record of one release contract: gate results, the tag Crucible pushed, the release workflow run observed, outcome. | Crucible |
| **Decision** | A recorded choice by Foundry or the user that Crucible needed before proceeding: approval to release, risk accepted, scope clarified, CI failure cause. Carries the verbatim words and who said them. | Foundry or user |
| **Escalation** | An open question Crucible or Foundry raised for the user, linked to the wake that carried it and the decision that closed it. | Crucible raises, user closes |
| **Wake** | A notification to Foundry that judgment is required, with reason and the entities involved. Delivered by webhook and retained for poll. | Crucible |
| **RetentionAction** | A recorded deletion or archival performed by the retention policy: what, why, when, under which policy version. Also an event. | Crucible |

## Relationships

```
Task 1..n Execution 1..n Attempt 1..1 Worker
Task 1..n TaskContract (versions; immutable each)
Task n..1 Policy (by name+version), n..1 Repository
Execution n..1 Harness, n..1 WorkerImage, n..1 ExecutionProvider
Attempt 1..n Heartbeat, 1..n LogStream, 1..n Artifact, 1..n Evidence
Attempt 1..1 CompletionClaim (optional), 1..n GateResult
Task 0..n ReviewReport (each for one collected head SHA)
Task 0..n AcceptanceResult, 0..n Decision, 0..n Escalation, 0..n Wake
Task 0..1 PullRequest 0..n ExternalReview 0..n ReviewDisposition
PullRequest 0..n CICertification (one per head SHA)
ReleaseContract 1..1 Release, n..n PullRequest (included)
Everything 1..n Event
```

## Invariants Crucible enforces

- A task contract is never modified in place. Amendments and corrections
  are events with a new contract version linked to the same task.
- At most one attempt holds a checkout lease for a given repository and
  branch at a time. Two attempts never share a working tree.
- An attempt cannot enter `succeeded` without a parsed CompletionClaim.
- A task cannot enter `gates_passed` unless every pre-PR gate the policy
  requires has a `pass` GateResult for the final attempt's collected head.
- A task cannot enter `accepted` or `publishing` without an
  AcceptanceResult from an authenticated orchestrator principal for that
  collected head. Crucible never creates one.
- Crucible never pushes a branch, opens or edits a PR, or pushes a tag
  unless the state machine (09) is in the state that permits exactly that
  action, and every such action is an event before it is attempted and
  after it completes.
- A PR cannot enter `ready_for_merge` while any received external review
  comment lacks a ReviewDisposition, or while CI certification for the
  current head is not green.
- A release cannot tag without a Decision of kind `release_authorization`
  by the operator principal referenced from the release contract.
- Credentials never appear in any entity. The credential spec names a mount
  source, not a value. Installation tokens exist only in memory and in the
  tmpfs of the publisher container that uses them.
- Events are append-only. Corrections are new events.
