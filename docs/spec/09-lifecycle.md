# 09. Lifecycle state machines

Every state is a column value guarded by a transition table in
`crucible/domain/lifecycle.py`. An illegal transition raises and is recorded
as an event; nothing bypasses the table. Each transition writes one event in
the same database transaction as the state change.

## Task

```
submitted --start--> scheduled --launch--> running
running --attempt collected--> reported          (every exit class, see below)
running --attempt blocked--> blocked
running --attempt retry--> scheduled              (policy permitted a new attempt)
running --cancel--> cancelling --all attempts terminal--> cancelled
reported --gates pass--> gates_passed --wake--> awaiting_acceptance
reported --gates fail--> gates_failed --wake--> awaiting_acceptance
awaiting_acceptance --accept--> accepted
awaiting_acceptance --reject--> rejected
awaiting_acceptance --needs_more_work--> scheduled   (new execution, same task)
blocked --decision--> scheduled
{submitted, scheduled, blocked, awaiting_acceptance, accepted} --cancel--> cancelled
accepted --close (orchestrator POST)--> closed
```

Terminal: `cancelled`, `rejected`, `closed`. There is no task-level
`failed`: a failed attempt with no retry remaining still produces a
`reported` task whose gates then fail (`exit_clean`, `report_present`), so
Foundry always sees the outcome through the same path. Foundry alone moves
`awaiting_acceptance` forward and issues `close`. Crucible alone moves
everything else.

`POST /attempts/{id}/terminate` on the running attempt of a task moves the
attempt to `cancelled`; the task then follows the retry rule (a terminate is
class `killed`, never retry-eligible), so it goes to `reported`. Terminating
an attempt is not cancelling the task; `POST /tasks/{id}/cancel` is.

## Execution

`created` -> `active` -> `succeeded` | `failed` | `cancelled`. `succeeded`
when the final attempt is `succeeded`; `failed` when attempts are exhausted
or a non-retryable class occurred; `cancelled` on task cancel.

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
and the parsed report exist. Classification: `succeeded` requires exit 0
and a parsed report; `blocked` requires exit 75 and `blocked.md`; everything
else, including exit 0 without a report and exit 75 without `blocked.md`, is
`failed` with the recorded exit class. The task transition happens on
classification: `succeeded` and `failed` both lead to task `reported`
(unless a retry is permitted), `blocked` leads to task `blocked`.

Lease expiry is not a transition. Loss is decided only by provider
observation (10).

A supervisor restart records a `reconcile_started` event on each
non-terminal attempt and resolves it within one tick; there is no separate
state.

## Worker

`injected` -> `alive` (first heartbeat) -> `quiet` (no activity for
`stall_warn_seconds`) -> `stalled` (past `stall_fail_seconds`; attempt is
terminated with exit_class `timeout`, reason `stall`) | `exited`.

## Gate

Per attempt per gate: `pending` -> `pass` | `fail` | `skipped`
(policy did not require it) | `error` (evaluator could not run; treated as
fail for completion). Gates re-evaluate only on a new attempt, except
`pending`-tolerant gates, which re-evaluate each reconcile tick until they
resolve or the task is terminal.

## Escalation

`open` -> `answered` (a Decision references it) -> `closed`. An escalation
older than `escalation_stale_hours` produces a repeat wake, not a state
change.

## Transition side effects (always in the same transaction)

| Transition | Side effects |
|---|---|
| task `start` | execution created; first attempt `pending`; enqueued |
| attempt `launching` | checkout lease taken; identity bundle hash recorded; container name `crucible-<attempt_id>` reserved |
| attempt `running` | attempt lease created; worker `injected` |
| attempt `exited` | final log drain scheduled; provider handle retained until `collected` |
| attempt `collected` | artifacts, evidence, claim rows; verification re-run scheduled (11); checkout lease released per cleanup policy |
| attempt classified | retry decision per policy; task transition |
| task `reported` | gate evaluation scheduled |
| task `gates_*` | wake created for the submitting principal |
| task `blocked` | escalation opened; wake created |
| task `cancelling` | running attempt terminated (`drain`); leases released when terminal |
| task `closed` | cleanup pass eligible for all workspaces of the task |
