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
reported --mechanical gates pass, review required--> awaiting_internal_review --wake-->
reported --mechanical gates pass, review not required--> gates_passed
awaiting_internal_review --ReviewReport recorded for this head--> gates_passed
gates_passed --wake--> awaiting_acceptance

awaiting_acceptance --accept, deliverable is not pull_request--> accepted
awaiting_acceptance --accept, deliverable is pull_request--> publishing
awaiting_acceptance --reject--> rejected
awaiting_acceptance --needs_more_work (correction attached)--> scheduled
pre_pr_gates_failed --correction attached--> scheduled
pre_pr_gates_failed --reject--> rejected

publishing --branch pushed, PR opened or head updated--> awaiting_external_review   (rounds outstanding)
publishing --branch pushed, PR opened or head updated--> awaiting_ci_certification  (rounds satisfied or 0)
publishing --push or PR call failed--> publish_failed --wake-->
publish_failed --retry publish (decision)--> publishing
publish_failed --cancel--> cancelled

awaiting_external_review --review received from allowlisted login--> external_feedback_received --wake-->
awaiting_external_review --wait_timeout_hours elapsed--> (repeat wake, reason external_review_overdue; no state change)
external_feedback_received --every comment has a disposition, none is fix--> awaiting_ci_certification
external_feedback_received --correction attached--> scheduled

awaiting_ci_certification --required checks green on current head--> ready_for_merge --wake-->
awaiting_ci_certification --a required check failed on current head--> ci_certification_failed --wake-->
awaiting_ci_certification --head changed out of band--> awaiting_ci_certification (new certification row; informational wake)
ci_certification_failed --ci-decision rerun--> awaiting_ci_certification
ci_certification_failed --ci-decision correct, correction attached--> scheduled
ci_certification_failed --ci-decision reject--> rejected

ready_for_merge --PR merged (observed)--> merged --wake-->
ready_for_merge --head changed--> awaiting_ci_certification
ready_for_merge --PR closed unmerged--> rejected
merged --included in a release contract--> release_candidate
release_candidate --release succeeded--> released
release_candidate --release failed or cancelled--> merged
{accepted, merged, released} --close (orchestrator POST)--> closed

{submitted, scheduled, blocked, awaiting_internal_review, awaiting_acceptance,
 pre_pr_gates_failed, publish_failed, awaiting_external_review,
 external_feedback_received, awaiting_ci_certification, ci_certification_failed,
 ready_for_merge} --cancel--> cancelled
running --cancel--> cancelling --all attempts terminal--> cancelled
```

Terminal: `cancelled`, `rejected`, `closed`. There is no task-level
`failed`: a failed attempt with no retry remaining still produces a
`reported` task whose gates then fail (`exit_clean`, `report_present`), so
Foundry always sees the outcome through the same path. Foundry alone moves
`awaiting_acceptance`, `pre_pr_gates_failed`, `external_feedback_received`,
`ci_certification_failed`, and `publish_failed` forward and issues `close`.
Crucible alone moves everything else, and only Crucible touches GitHub.

A correction re-enters at `scheduled` with a `correct` execution whose
workspace starts from the remote `work_branch` head (08). It then passes
through `reported`, the pre-PR gates, internal review if the policy
requires it for corrections, acceptance, and `publishing` again; the push
updates the PR head. Because the default policy has
`retrigger_after_correction: false` and `required_rounds: 1`, the second
pass through `publishing` lands in `awaiting_ci_certification`, never back
in `awaiting_external_review`.

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
running --timeout--> exited            (after drain then kill; exit_class timeout)
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
push is recorded with `pushed_by: other` and wakes Foundry; Crucible never
force-pushes over it.

## Release (24)

`submitted` -> `verifying` -> `gates_failed` | `tagging` -> `tagged` ->
`workflow_running` -> `succeeded` | `workflow_failed`; `cancelled` from any
state before `tagging`.

## Gate

Per attempt (pre-PR) or per PR head (publication, post-PR) per gate:
`pending` -> `pass` | `fail` | `skipped` | `error` (evaluator could not
run; treated as fail). Pre-PR gates evaluate once per collected head.
Post-PR gates re-evaluate each reconcile tick and on every processed GitHub
delivery for the head until they resolve or the task is terminal.

## Escalation

`open` -> `answered` (a Decision references it) -> `closed`. An escalation
older than `escalation_stale_hours` produces a repeat wake, not a state
change.

## Transition side effects (always in the same transaction)

| Transition | Side effects |
|---|---|
| task `start` | execution created; first attempt `pending`; enqueued |
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
| task `ready_for_merge` | wake (reason `ready_for_merge`) |
| task `merged` | merge SHA and merger recorded; wake (informational) |
| task `blocked` | escalation opened; wake created |
| task `cancelling` | running attempt terminated (`drain`); leases released when terminal |
| task `closed` | cleanup pass eligible for all workspaces of the task |
