# 09. Lifecycle state machines

Every state is a column value guarded by a transition table in
`crucible/domain/lifecycle.py`. An illegal transition raises and is recorded
as an event; nothing bypasses the table. Each transition writes one event in
the same database transaction as the state change.

## Task

```
submitted --start--> scheduled --launch--> running
running --report(0)--> reported
running --blocked(75)--> blocked
running --fail--> failed        (attempts exhausted or non-retryable)
running --cancel--> cancelling --terminated--> cancelled
reported --gates pass--> gates_passed --wake--> awaiting_acceptance
reported --gates fail--> gates_failed --wake--> awaiting_acceptance
awaiting_acceptance --accept--> accepted
awaiting_acceptance --reject--> rejected
awaiting_acceptance --needs_more_work--> submitted   (new execution required)
blocked --decision--> scheduled | cancelled
failed --retry(policy)--> scheduled
{submitted, scheduled, blocked} --cancel--> cancelled
accepted --close--> closed
```

Terminal: `cancelled`, `rejected`, `closed`. Foundry alone moves
`awaiting_acceptance` forward. Crucible alone moves everything else.

## Execution

`created` -> `active` -> `succeeded` | `failed` | `cancelled`. An execution
is `succeeded` when its final attempt is `succeeded`; `failed` when attempts
are exhausted or a non-retryable class occurred.

## Attempt

```
pending --prepare--> preparing --ready--> launching --started--> running
running --exit 0 with report--> succeeded
running --exit 75--> blocked
running --exit other--> failed
running --timeout--> timed_out
running --terminate--> terminating --exited--> cancelled
running --lease expired--> lost
{preparing, launching} --error--> failed
any non-terminal --supervisor restart--> reconciling --resolved--> (prior state or lost)
```

`reconciling` is transient and exists so a restart is visible in the event
log; it resolves within one tick.

## Worker

`injected` -> `alive` (first heartbeat) -> `quiet` (no activity for
`stall_warn_seconds`) -> `stalled` (past `stall_fail_seconds`, attempt
becomes `timed_out` with reason `stall`) | `exited`.

## Gate

Per attempt per gate: `pending` -> `pass` | `fail` | `skipped`
(policy did not require it) | `error` (evaluator could not run; treated as
fail for completion). Gates re-evaluate only on a new attempt.

## Escalation

`open` -> `answered` (a Decision references it) -> `closed`. An escalation
older than `escalation_stale_hours` produces a repeat wake, not a state
change.

## Transition side effects (always in the same transaction)

| Transition | Side effects |
|---|---|
| task `start` | create execution, first attempt `pending`, enqueue for supervisor |
| attempt `launching` | checkout lease taken; identity bundle hash recorded |
| attempt `running` | attempt lease created; worker `injected` |
| attempt exit | collect outputs; artifacts and evidence rows; parse report; release checkout lease per cleanup policy |
| task `reported` | gate evaluation scheduled |
| task `gates_*` | wake created for the submitting principal |
| task `blocked` | escalation opened; wake created |
| attempt `lost` | provider cleanup attempted; retry decision per policy |
| task `cancelled` | any running attempt terminated; leases released |
