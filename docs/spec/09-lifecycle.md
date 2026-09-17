# 09. Lifecycle state machines

Every state is a column value guarded by a transition table in
`crucible/domain/lifecycle.py`. An illegal transition raises and is recorded
as an event; nothing bypasses the table. Each transition writes one event in
the same database transaction as the state change.

## Task

Two halves. The supervision half runs a worker to a collected head. The
delivery half (only for `pull_request` deliverables) takes that head to a
merged PR. Corrections loop back through the supervision half against the
existing branch.

```
submitted --start--> scheduled --launch--> running
running --attempt collected--> reported          (every exit class, see below)
running --attempt blocked--> blocked
running --attempt retry--> scheduled              (policy permitted a new attempt)
blocked --decision--> scheduled

reported --mechanical pre-PR gates fail--> pre_pr_gates_failed --wake-->
reported --mechanical gates pass, internal review required for this head--> awaiting_internal_review --wake-->
reported --mechanical gates pass, internal review not required for this head--> gates_passed
awaiting_internal_review --ReviewReport recorded for this head--> gates_passed
gates_passed --wake--> awaiting_acceptance

awaiting_acceptance --accept, deliverable is artifacts--> accepted
awaiting_acceptance --accept, deliverable is branch or pull_request--> publishing
awaiting_acceptance --reject--> rejected
awaiting_acceptance --needs_more_work (correction attached)--> scheduled
pre_pr_gates_failed --correction attached--> scheduled
pre_pr_gates_failed --reject--> rejected

publishing --branch pushed and verified, deliverable is branch--> accepted
publishing --branch pushed and verified, PR opened or head updated, rounds outstanding--> awaiting_external_review
publishing --branch pushed and verified, PR opened or head updated, rounds satisfied--> awaiting_ci_certification
publishing --push, verification, or PR call failed--> publish_failed --wake-->
publish_failed --retry publish (decision)--> publishing
publish_failed --cancel--> cancelled

awaiting_external_review --review signal from allowlisted login--> external_feedback_received --wake-->
awaiting_external_review --wait_timeout_hours elapsed--> (repeat wake, reason external_review_overdue; no state change)
external_feedback_received --every comment dispositioned, none is fix, rounds satisfied--> awaiting_ci_certification
external_feedback_received --every comment dispositioned, none is fix, rounds outstanding--> awaiting_external_review
external_feedback_received --correction attached--> scheduled

awaiting_ci_certification --required checks green on the accepted head--> ready_for_merge --wake-->
awaiting_ci_certification --a required check failed on the accepted head--> ci_certification_failed --wake-->
ci_certification_failed --ci-decision rerun--> awaiting_ci_certification
ci_certification_failed --ci-decision correct, correction attached--> scheduled
ci_certification_failed --ci-decision reject--> rejected

{awaiting_external_review, external_feedback_received, awaiting_ci_certification, ready_for_merge}
    --PR head changed out of band--> head_diverged --wake-->
head_diverged --head-decision recollect--> reported     (new collected head from the remote; all gates, review, acceptance start over)
head_diverged --head-decision reject--> rejected
head_diverged --cancel--> cancelled

ready_for_merge --PR merged (observed)--> merged --wake-->
ready_for_merge --PR closed unmerged--> rejected
merged --included in a release contract--> release_candidate
release_candidate --release succeeded--> released
release_candidate --release failed or cancelled--> merged
{accepted, merged, released} --close (orchestrator POST)--> closed

{submitted, scheduled, blocked, awaiting_internal_review, awaiting_acceptance,
 pre_pr_gates_failed, publish_failed, awaiting_external_review,
 external_feedback_received, awaiting_ci_certification, ci_certification_failed,
 head_diverged, ready_for_merge} --cancel--> cancelled
running --cancel--> cancelling --all attempts terminal--> cancelled
```

Terminal: `cancelled`, `rejected`, `closed`. There is no task-level
`failed`: a failed attempt with no retry remaining still produces a
`reported` task whose gates then fail (`exit_clean`, `report_present`), so
Foundry always sees the outcome through the same path. Foundry alone moves
`awaiting_acceptance`, `pre_pr_gates_failed`, `external_feedback_received`,
`ci_certification_failed`, `head_diverged`, and `publish_failed` forward
and issues `close`.
Crucible alone moves everything else, and only Crucible touches GitHub.

A correction re-enters at `scheduled` with a `correct` execution whose
workspace starts from the remote `work_branch` head (08). It then passes
through `reported`, every mechanical pre-PR gate including the full
verification re-run, acceptance, and `publishing` again; the push updates
the PR head. A correction does not automatically require another internal
review: `awaiting_internal_review` is entered for a corrected head only
when the correction contract sets `request_internal_review: true` (Foundry
asks for one when the correction is substantial, expands scope, or creates
architectural risk) or the policy sets
`internal_review.required_for_corrections: true`. Because the default
policy has `retrigger_after_correction: false` and `required_rounds: 1`,
the second pass through `publishing` lands in `awaiting_ci_certification`,
never back in `awaiting_external_review`.

**Every state after `publishing` is bound to the accepted head.** Gate
results, the review report, and the AcceptanceResult all name the SHA
they were made for. A PR head that Crucible did not push invalidates all
of them: the task moves to `head_diverged` and nothing about the new SHA
is trusted, however green its CI. Foundry decides whether to `recollect`
(Crucible fetches the new head into a fresh collector, and the task
re-runs pre-PR gates, internal review when applicable, and acceptance
before `publishing` re-verifies it) or to reject.

**Until the publisher exists (C2, C3)**, an accepted `branch` or
`pull_request` deliverable stays in `awaiting_acceptance` with a
`publish_pending` flag and a wake of that reason; C4 replaces the flag with
the `publishing` edge.

**Branch-only deliverables** (`branch`, allowed only under a policy with
`deliverables.allow_branch_only: true`) pass through `publishing` like a
PR deliverable: the bundle head is pushed and `branch_pushed_at_head`
verified, then the task moves to `accepted`. Nothing is accepted
unpublished.

Cancelling a task after `publishing` never closes the PR; Crucible records
the cancellation on the PR record and leaves the PR to the operator.
Whether an open PR is closed is a person's act.

`POST /attempts/{id}/terminate` on the running attempt moves the attempt to
`cancelled`; the task then follows the retry rule (a terminate is class
`killed`, never retry-eligible), so it goes to `reported`. Terminating an
attempt is not cancelling the task; `POST /tasks/{id}/cancel` is.

## Execution

`created` -> `active` -> `succeeded` | `failed` | `cancelled`. Role
`implement`, `correct`, or `review`. `succeeded` when the final attempt is
`succeeded`; `failed` when attempts are exhausted or a non-retryable class
occurred; `cancelled` on task cancel. A `review` execution's success means
a parsed `ReviewReportV1` exists; its verdict does not change task state
by itself (Foundry's acceptance does).

## Attempt

```
pending --prepare--> preparing --ready--> launching --started--> running
running --provider reports exit--> exited
running --timeout--> exited            (after drain then kill; exit_class timeout; the attempt row holds drain_deadline, killed_at, termination_reason)
running --terminate--> terminating --provider reports exit--> exited   (exit_class killed)
running --provider cannot see it--> exited   (exit_class lost)
exited --collect done, logs drained--> collected
collected --classify--> succeeded | blocked | failed
{preparing, launching} --error--> collected (exit_class environment)
```

`collected` is the single point where outputs, logs, artifacts, evidence,
the branch bundle, and the parsed report exist. Classification:
`succeeded` requires exit 0 and a parsed report; `blocked` requires exit 75
and `blocked.md`; everything else is `failed` with the recorded exit class.
The task transition happens on classification: `succeeded` and `failed`
both lead to task `reported` (unless a retry is permitted), `blocked` leads
to task `blocked`.

Lease expiry is not a transition. Loss is decided only by provider
observation (10).

## Worker

`injected` -> `alive` (first heartbeat) -> `quiet` (no activity for
`stall_warn_seconds`) -> `stalled` (past `stall_fail_seconds`; attempt is
terminated with exit_class `timeout`, reason `stall`) | `exited`.

## PullRequest

`opening` -> `open` -> `merged` | `closed`. Head history is a list of
(SHA, pushed_by: crucible | other, observed_at). A head Crucible did not
push is recorded with `pushed_by: other`, moves the task to
`head_diverged`, and wakes Foundry; Crucible never force-pushes over it.

## Release (24)

`submitted` -> `verifying` -> `gates_failed` | `tagging` -> `tagged` ->
`workflow_running` -> `succeeded` | `workflow_failed`; `cancelled` from any
state before `tagging`.

## Gate

Per attempt (pre-PR) or per PR head (publication, post-PR) per gate:
`pending` -> `pass` | `fail` | `skipped` | `deferred` (no evaluator in
this phase yet; non-blocking, never pass; 11) | `error` (evaluator could
not run; treated as fail). Pre-PR gates evaluate once per collected head.
Post-PR gates re-evaluate each reconcile tick and on every processed GitHub
delivery for the head until they resolve or the task is terminal.

## Escalation

`open` -> `answered` (a Decision references it) -> `closed`. An escalation
older than `escalation_stale_hours` produces a repeat wake, not a state
change.

## Transition side effects (always in the same transaction)

| Transition | Side effects |
|---|---|
| task `start` | the API records the start request and moves the task to `scheduled`; the supervisor materializes the execution and first attempt (`pending`) on its next tick, because those tables are fenced to the supervisor (14) |
| attempt `launching` | checkout lease taken; identity bundle hash recorded; image digest resolved and recorded; container name `crucible-<attempt_id>` reserved |
| attempt `running` | attempt lease created; worker `injected` |
| attempt `exited` | final log drain scheduled; provider handle retained until `collected` |
| attempt `collected` | artifacts, evidence, claim rows, branch bundle; verification re-run scheduled (11); checkout lease released per cleanup policy |
| attempt classified | retry decision per policy; task transition |
| task `reported` | pre-PR gate evaluation scheduled |
| task `awaiting_internal_review` | wake (reason `internal_review_needed`); if policy `executor` is `crucible` and the contract names a reviewer execution request, a `review` execution is created |
| task `gates_passed` / `pre_pr_gates_failed` | wake created for the submitting principal |
| task `publishing` | publisher job enqueued: mint installation token, push bundle head, open or update PR, render body; events before and after each GitHub call |
| task `awaiting_external_review` | PR observation registered (polling and webhook routing) |
| task `external_feedback_received` | ExternalReview and comment rows written; wake |
| task `ci_certification_failed` | CICertification row with captured check, workflow, job, log pointers, head SHA; wake; no retry, no correction |
| task `head_diverged` | previous head's acceptance, review, and gate results marked `superseded` (rows kept); PR observation continues; wake |
| task `ready_for_merge` | wake (reason `ready_for_merge`) |
| task `merged` | merge SHA and merger recorded; wake (informational) |
| task `blocked` | escalation opened; wake created |
| task `cancelling` | running attempt terminated (`drain`); leases released when terminal |
| task `closed` | cleanup pass eligible for all workspaces of the task |
