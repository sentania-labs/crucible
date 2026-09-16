# ADR 0010: Releases happen only through an operator-authorized release contract; Crucible tags, the repository's workflow releases

Status: accepted, operator decision 7, 2026-09-16. Implementation after the readiness milestone.

## Context

Foundry judges whether merged changes form a release; the mechanics must
be deterministic and the consequential act (a public tag) must trace to
the operator's words.

## Decision

- Foundry proposes; the operator authorizes as a recorded Decision with
  verbatim words; Foundry submits a `ReleaseContractV1` referencing it.
- Crucible verifies fixed gates (authorization, included PRs merged,
  target SHA current, CI green, version increases, version files agree,
  changelog, tag absent, evidence present), then creates and pushes an
  annotated tag through a publisher container, then observes the
  tag-triggered release workflow and records the outcome.
- Foundry never pushes tags. Crucible never decides a release should
  occur. No recursive or multi-stage approval system.
- A standing release policy may later set `require_operator_approval`
  false for a repository; that is itself a recorded decision.

## Consequences

Release is a separate contract and lifecycle, not a task state alone;
tasks only move `merged` to `release_candidate` to `released`. A failed
release workflow is an escalation, never a re-tag.
