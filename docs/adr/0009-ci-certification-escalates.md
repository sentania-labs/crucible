# ADR 0009: Pre-PR verification is the proof; PR CI is certification; a required failure escalates

Status: accepted, operator decision 6, 2026-09-16.

## Context

Using the PR as the place to discover whether work builds turns CI into a
retry loop and hides process failures (false evidence, wrong SHA, drift)
behind "try again".

## Decision

- Before Crucible opens a PR, the worker and Crucible must show every
  configured repository check passed on the collected head: Crucible
  re-runs each required command in a verifier container and the gate uses
  only that evidence. The contract must include every check the repository
  policy requires.
- Required PR CI must be green on the final head SHA. Green means a
  non-empty required-check set with every member successful; an empty
  set is pending, never a pass, unless a repository is explicitly
  policy-marked as having no CI.
- A PR head Crucible did not push supersedes the previous head's
  acceptance and gates; the task blocks until Foundry decides.
- A required failure moves the task to `ci_certification_failed` with the
  check, workflow, job, log excerpt, and head SHA captured, wakes Foundry,
  and performs no retry and no worker correction.
- Foundry records the cause from a fixed enum and the action. A rerun or
  correction happens only after that decision.

## Consequences

Distinct states `pre_pr_gates_failed`, `awaiting_external_review`,
`external_feedback_received`, `awaiting_ci_certification`,
`ci_certification_failed`, `ready_for_merge`, `merged`,
`release_candidate` exist in the task lifecycle. A flaky test costs a
Foundry decision every time it fires, which is the signal the operator
wants.
