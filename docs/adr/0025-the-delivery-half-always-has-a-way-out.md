# ADR 0025: The delivery half always has a way out

Status: accepted, operator direction of 2026-09-29 ("review is the enforcement;
Crucible does anything mechanical; hard failures only where the damage is real or
a claim is false"), delivered by hades FDY-0139.

## Context

No task had got past `publishing` on the lab, and a read of the delivery code
found several ways the first real pull request would stall with no move left to
anyone: a `fix` disposition that nothing ever cleared, a merge or close that the
task never saw, a reviewer that never reviews, a repository with no CI, and a CI
re-run decision that re-failed on the next poll and then ignored the green that
followed. ADR 0008 bounds external review and ADR 0009 makes a CI failure
escalate; neither meant a task to wait for ever.

## Decision

- **An observed merge or close ends the task from any delivery state.** A merge
  moves the task to `merged`, a close without merge to `rejected`, and both wake
  Foundry (`merged`, `pull_request_closed`). A person may act on the pull request
  at any time; Crucible records it.
- **A correction settles the feedback on the head it replaced.** Once the
  corrected head is accepted, comments made on the heads Crucible pushed before it,
  before the corrected head appeared, no longer need a disposition, `fix` included.
  A later reply is new feedback. Dispositions stay add-only.
- **Two operator waivers, per task, as recorded Decisions.**
  `waive_external_review` waives the external review rounds still outstanding;
  the rounds gate reads `skipped` and names the decision. `accept_no_ci` accepts
  that the repository has no CI for this task: with nothing at all run on the
  accepted head (a skipped run counts as nothing), certification is `skipped`; a
  check that does run is still certified. Both are operator-only (like
  `release_authorization`), may be recorded only while a pull request is under
  observation, need the operator's words, and are made through the API, the CLI
  (`crucible decisions --kind`), or the task page. Nothing but a recorded decision
  waives anything.
- **A CI re-run decision is about the failure it was recorded for.** The runs it
  names are stale and are not counted again; any other result is fresh. A green
  certification on the accepted head moves a task out of `ci_certification_failed`
  whether or not a decision preceded it.

## Consequences

ADR 0009's "a flaky test costs a Foundry decision every time it fires" still
holds for the failure; the green that follows a re-run no longer costs a second
one. ADR 0009's "unless a repository is explicitly policy-marked as having no CI"
gains a per-task form that is an operator decision rather than a policy edit.
Foundry sees a new wake reason, `pull_request_closed`.
