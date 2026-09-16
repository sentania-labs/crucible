# 00. Overview, scope, and non-goals

Status: draft for review. Version 0.3, 2026-09-16, incorporating the operator's decisions on the sixteen open questions (22). Version 0.2 followed one non-author adversarial review round.

## Purpose

Crucible is a deterministic supervisor for AI coding workers. An orchestrator
(Foundry, or a person using the API directly) hands Crucible an explicit,
versioned task contract. Crucible provisions an isolated execution
environment, launches the named harness with an injected identity, watches
the run, records everything durable, evaluates mechanical completion gates,
and exposes the whole record through a versioned HTTP API. Work continues
whether or not the orchestrator is connected.

## The boundary

| Foundry (orchestrator) decides | Crucible (supervisor) does |
|---|---|
| What outcome is required | Validates the task contract against its schema |
| How work is decomposed | Persists tasks and every state transition |
| Which model, harness, and execution environment | Provisions the environment named in the contract |
| Constraints and acceptance criteria | Launches and monitors the worker |
| Whether output semantically satisfies the outcome | Delivers injected identity and instructions |
| What external review feedback means and whether to amend | Pushes branches, opens PRs, pushes tags, and watches them |
| Whether merged changes form a release | Verifies release gates and performs the tag |
| Whether a risk is acceptable | Captures events, logs, evidence, artifacts |
| Whether more work is needed | Enforces deterministic policies and gates |
| What escalates to the user | Detects completion, failure, timeout, loss, stall |
| | Retries only per explicit policy |
| | Interrupts, drains, terminates when authorized |
| | Wakes Foundry when judgment is required |
| | Reconciles after restart; cleans up per policy |

Crucible must not interpret ambiguous requirements, choose architecture,
invent acceptance criteria, broaden scope, pick a model by judgment, create
follow-up work on its own, approve semantic correctness, accept a risk, or
declare subjective success. Every one of those is a Foundry or user act that
Crucible only records.

## In scope for the first release (v0.x)

- Versioned HTTP API (`/v1`) with token authentication.
- Task contract schema v1 and validation.
- PostgreSQL persistence with Alembic migrations.
- Lifecycle state machines for task, execution, attempt, worker, gate.
- Fake execution provider (tests) and Docker execution provider (local).
- Harness adapters for Claude Code, Codex, AGY, non-interactive only.
- Lease and heartbeat based liveness; restart reconciliation.
- Deterministic definition-of-done gates with evidence records.
- Structured logging, durable events, artifact and log storage on disk.
- GitHub delivery: App-authenticated push, PR open with a rendered body,
  webhook and polling observation, external review recording, CI
  certification, merge observation (23).
- Release contract and lifecycle designed; implemented after readiness (24).
- Worker image version pinning, digest recording, and promotion (13).
- Foundry wake channel (webhook and poll).
- Bootstrap-ledger import API and authority handoff.
- Docker Compose for normal and developer modes.
- Unit, integration, and end-to-end tests; reproducible container build.

## Explicit non-goals

- Any user interface. Boards, chat, and Kanban consume the API later.
- Model selection, prompt authoring, or planning.
- Interactive worker sessions. Crucible runs harnesses in print or exec mode.
- Kubernetes execution provider (designed for, not built, in v0.x).
- Multi-tenant authorization. One trust domain, one operator.
- Secret storage. Credentials are mounted from outside; never persisted.
- Deployment of the products workers build. The tag is a one-way handoff
  to the target repository's own release workflow.
- Merging PRs. The operator merges; Crucible observes.
- Moving Foundry into a persistent service. Nothing here depends on it.
- Cross-workspace request routing between agents.
- Compatibility with any earlier system's state formats or files.

## Reading order

Architecture (01) and the domain model (03) first. The contracts (04 to 08)
define what crosses the API. Lifecycle, events, and gates (09 to 11) define
behavior. Operations (12 to 17) define how it runs. Testing, readiness,
phases, spikes, and decisions (18 to 22) define how it gets built and
accepted. GitHub delivery (23) and release (24) define what happens after
a worker is done.
