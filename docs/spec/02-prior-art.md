# 02. Prior-art decision record

Crucible was designed after surveying five systems. Three of them are the
operator's own earlier private orchestrators, unlicensed and unpublished,
referred to below as "earlier private" with a short tag; **no code is reused
from them**, only concepts. FirstMate (MIT, `kunchenguid/firstmate`) and Sandcastle (MIT,
`mattpocock/sandcastle`) are licensed; neither is vendored by default. Any
future code reuse from a licensed project needs its own ADR naming the file,
the license, and the technical reason.

Classification: **Adopt** (use substantially as designed), **Adapt** (keep the
idea, redesign), **Reject** (exclude, with reason), **Defer** (later).

## Adopt

| Concept | Seen in | Reasoning |
|---|---|---|
| A non-model process owns every state write; a worker's "done" is a claim | earlier private (A), earlier private (B), FirstMate learnings | Every surveyed system that trusted self-reports recorded an incident where a service was called done while broken |
| Facts from artifacts: HEAD SHA, PR head, CI run, review file for that exact SHA | earlier private (B) | A reviewer once re-reviewed a stale SHA taken from a report file |
| Pure, unit-tested gate function returning pass, pending, fail | earlier private (B) | Deterministic by construction; testable without infrastructure |
| Supervisor writes a liveness record every tick so staleness proves it is dead | earlier private (A) ledger contract | A prior monitor died silently with the session that started it |
| Marker-based, restart-safe reconciliation: exited but not ingested, died silently, stale | earlier private (B), earlier private (A) | Restart survival is a readiness-gate item |
| Owner's verbatim words recorded before consequential action | FirstMate, earlier private (B) | Matches the operator's standing rule |
| Runtime identity and state outside the project tree, never committed | earlier private (B), FirstMate | Matches the instruction architecture Crucible serves |
| One attempt per working tree; reviewers get a detached tree | earlier private (B) | Readiness gate: no tree corruption |
| Idle timeout plus post-exit grace window | Sandcastle | Cheapest fix for harnesses that hang or hold stdout open |
| Fail-fast lock on a working tree; collision errors rather than corrupts | Sandcastle | Same invariant, enforced early |
| Two-shape execution interface: shared-disk and isolated | Sandcastle | Docker and later microVM or Kubernetes share one worker code path |

## Adapt

| Concept | Seen in | What changes |
|---|---|---|
| Harness launch quirks (`claude -p` with a system-prompt file, `codex exec` reading stdin, `agy -p` argv-only with a 128KB ceiling) | earlier private (A), earlier private (B) | Re-implemented as harness adapters behind one launch contract; contracts always delivered as mounted files, never argv |
| Detached execution that outlives its launcher | earlier private (A) (systemd), earlier private (C), cgroup-reap scar | Crucible is a Compose-owned service; workers are containers it owns. No dependency on a user session or user systemd |
| Task record with a fixed field set | earlier private (B), earlier private (A), FirstMate | Versioned Pydantic contract, PostgreSQL rows; no markdown frontmatter, no marker dotfiles |
| Event log separate from current state | FirstMate insight, earlier private (B) events file | Append-only `events` table plus derived state columns; API serves both |
| Quota and capacity as scheduling inputs with recorded rationale | FirstMate dispatch config and learnings | Foundry chooses model and harness and records why in the contract; Crucible enforces concurrency caps and per-harness limits from policy |
| Mailbox with send-time resolution and correlation ID | earlier private (C) | Reused as the shape of the wake channel: durable row first, delivery best effort, poll as fallback |
| Board and report pages | earlier private (B), FirstMate | Later, reading the API; never a system of record |
| Env resolution that errors on overlapping keys | Sandcastle | Kept, plus one-credential-per-worker rule |

## Reject

| Concept | Seen in | Why |
|---|---|---|
| GitHub issue labels as the lifecycle engine | earlier private (A) | Welds lifecycle to one channel; no UI channel may be the system of record |
| Multi-harness consensus deliberation as default | earlier private (A), earlier private (B) | Too heavy by the operator's own retrospective; consensus is a task shape Foundry can request, not supervisor behavior |
| Session-resident supervision re-armed by harness hooks | FirstMate, earlier private (B) | Ties liveness to an interactive session; contradicts disconnected operation |
| tmux panes and pane scraping | FirstMate, earlier private (B) | Not container-portable, not deterministic |
| Full identity and manual in every repository | FirstMate, earlier private (C) | The architecture Crucible replaces |
| Policy prepended to every prompt regardless of task | earlier private (A) keystone | Contradicts runtime skill discovery; inject only what the task needs |
| Markdown backlog as ledger | FirstMate, earlier private (C) | Not machine-checkable; holds decay |
| Completion detected by sniffing a string in output | Sandcastle | Exit status, report file presence, and parsed report are the facts; a string is a hint at most |
| Container flags alone as containment | Sandcastle | No cap-drop, read-only root, memory cap, or egress control; Crucible sets all of them |
| Codex or any harness sandbox as the primary boundary | this project's own probe | a harness sandbox can be unavailable on a given host (Codex's needs user namespaces); the container is the boundary, the harness sandbox is defense in depth |

## Defer

| Concept | Seen in | When |
|---|---|---|
| Turning a worker's `blocked` into a request to another workspace | earlier private (A) gateway | After Crucible runs; Foundry escalates to the user first |
| Remote worker hosts over SSH | FirstMate | Superseded by Kubernetes provider |
| Deploy verification after sync as a gate | earlier private (A) | Belongs to the deployer per the delivery policy |
| Session transcript write-back to harness home directories | Sandcastle | A second host-write channel; revisit after the trust model is proven |
| microVM isolation | Sandcastle (Firecracker provider) | Kubernetes provider first |

## Observations that shaped the design

- Prior attempts failed by building the substrate first and never reaching
  use. Phases (20) put a usable end-to-end path before breadth.
- Concurrency on a single developer machine saturates quickly; the default
  cap is 3 per provider and is a policy value.
- Everything prior kept state under one user's home directory with absolute
  paths. Crucible's state is a database and a configurable artifact root.
