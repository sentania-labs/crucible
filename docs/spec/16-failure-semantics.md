# 16. Failure, restart, retry, cancellation, cleanup, and retention

## Failure classes and default handling

| Class | Meaning | Default |
|---|---|---|
| `completed` | exit 0 and report present | gates |
| `blocked` | exit 75, `blocked.md` present | escalation, wake, task `blocked` |
| `environment` | exit 70, the provider failed before the harness ran, the kernel killed the worker out of memory (exit 137 with the daemon's OOM flag), or the harness was refused | retry if attempts remain; else `failed`. A harness refusal (07, 25) is the exception: it is never retried, because the same refusal would come back |
| `auth_failure` | harness reported auth problem (adapter classified) | retry per policy (`retry.auth_failure_max`, after `auth_retry_delay_seconds`); wake regardless |
| `quota_exhausted` | harness reported rate or quota limit | reroute (below): mark the pool, commit WIP, new attempt on the next candidate in the tier; if none, `awaiting_quota` until the earliest reset; caps exceeded or task pinned to the exhausted pool: task `reported` with the class visible, wake |
| `timeout` | contract timeout or stall | no retry; gates run on what exists; wake |
| `killed` | terminated by request | `cancelled` |
| `crashed` | non-zero exit not otherwise classified | no retry by default; wake |
| `lost` | provider cannot find the worker | retry if attempts remain and policy allows `lost`; else `failed` |
| `completed_without_report` | exit 0, no report | report gate fails; no retry; wake |
| `incomplete` | the harness exited cleanly while its own tooling reported a command still running (07, issue 128) | attempt `failed`, never a completion, whatever the report claims; the running commands are on `attempt_collected` as `work_in_flight`; retried only when policy and contract both name it |

Retry is never a way to re-roll the worker's judgment. A class retries only
when it is in both the policy's `retry.eligible_classes` and the contract's
`retry_on`. Each retry is a new attempt with the same contract version and a
fresh workspace. A correction execution starts from the remote
`work_branch` head Crucible pushed (08), so published work is never
abandoned or force-pushed over; a plain retry of an unpublished attempt
starts from `base_ref`, because nothing of the failed attempt was ever
pushed.

## Quota reroute and resume (C6b)

A quota exit is not a failure of the worker's judgment, so it is handled
apart from retry. It counts against `reroute.reroute_max` in the routing
policy, never against `lifecycle.max_attempts`, and it never needs
`retry_on` to name it. The sequence, all in one supervisor transaction per
step:

1. The collector runs. Uncommitted changes in the worktree are committed to
   the work branch as one commit whose message begins `wip(crucible):` and
   names the attempt, then pushed; the SHA goes in the event. Nothing is
   discarded silently and nothing is left uncommitted. Squash on merge
   removes the WIP commit from `main`.
2. The attempt's pool is marked exhausted until `reset_at` (05b).
3. Selection runs again for the tier with marked pools excluded. A
   candidate: a new attempt on the same contract version, resumed from the
   remote work branch as corrections are, and a `reroute` event naming the
   pool left, the model chosen, and the ordered candidates. No wake.
4. No candidate: the task moves to `awaiting_quota` with `resume_at` the
   earliest `reset_at` among the tier's pools, and one informational wake
   (17). The supervisor tick relaunches at `resume_at` through step 3. Past
   `reroute.resume_max_wait_seconds`, or past `reroute_max`, the task ends
   `reported` with the class visible and a wake, which is the pre-C6b
   behaviour.

A pinned task (05) skips step 3: it waits for its own pool's reset within
the cap or ends `reported`. A quota refusal at launch-time reservation
(the pool over Crucible's own soft limit since selection) is a routing
race, not a provider fact: it creates no exhaustion mark and no checkpoint,
and it goes straight to step 3 with the refused pool excluded, recorded as
a `reroute` event with source `reserve`. It counts toward `reroute_max`
like any other reroute. (Amended 2026-09-20 after the C6b implementation:
the original text sent this case to a wake, which with class-based
selection is a round trip for a decision the rule already makes.)

Timed resumes from `awaiting_quota` count toward `reroute_max` together
with reroutes, per contract version. Attempts created by a reroute or a
resume do not count toward `lifecycle.max_attempts`; retry eligibility
compares the number of non-quota attempts. Once an execution has pushed
any head (a checkpoint or a completed attempt's branch), every later
attempt on that execution resumes from the remote work branch, whatever
created it.

## Delivery-half failures (23)

| Situation | Handling |
|---|---|
| push rejected (remote moved) | `publish_failed`, remote head recorded, wake; never force |
| PR API error | `publish_failed` with response class; wake; Foundry may retry publish |
| required CI check failed | `ci_certification_failed`; evidence captured; wake; no retry, no correction until Foundry decides |
| external review not received in `wait_timeout_hours` | repeat wake; state unchanged |
| head changed by someone else | task `head_diverged`; previous head's acceptance, review, and gates superseded; wake; Foundry chooses recollect or reject; Crucible never overwrites |
| PR closed without merge | task `rejected`, closer recorded |
| release gate failed or tag push rejected | release `gates_failed`; wake; nothing pushed or re-pushed |
| release workflow failed | release `workflow_failed`; wake; no re-tag |

## Restart of Crucible

Supervisor restart triggers reconciliation (10). Workers keep running in
their containers during the restart; the provider's `reconcile` re-attaches
by label. Log capture resumes from the last stored timestamp-and-hash
position (10). Attempt leases that expired during the outage are renewed if
the container is alive; a worker is never marked lost merely because
Crucible was down, only because the provider cannot see it. A publisher job
interrupted mid-way is re-verified on restart: if the remote head already
equals the bundle head the push step is complete; if a PR for the task
exists the open step is complete; otherwise the job re-runs. Stored normalized
webhook deliveries are processed after restart; polling covers anything
delivered while down.

API restart is stateless. In-flight requests fail with 503 and the client
retries with the same idempotency key.

## Cancellation

`POST /tasks/{id}/cancel` records the verbatim reason, moves the task to
`cancelling` if an attempt is running and terminates it with `drain`, or to
`cancelled` at once otherwise. Whatever the worker wrote to the report
directory before termination is collected and stored (it may include a
partial report, kept as an artifact but never parsed as a claim). The
checkout is kept per cleanup policy so partial work is recoverable by a
person. An open PR is never closed by Crucible on cancel.

## Cleanup policy (per policy document, 05b)

| Setting | Options | Default |
|---|---|---|
| `workspace_on_success` | delete, keep, keep_diff_only | keep_diff_only |
| `workspace_on_failure` | delete, keep | keep |
| `container_remove` | always, on_success, never | always, after `logs_drained` |
| `credential_volume_remove` | immediately_after_validated_sync | immediately_after_validated_sync (the field keeps its name; the copy is a directory, 12) |

The credential copy is not subject to `workspace_on_success` or
`workspace_on_failure`: it is removed under every option, `keep` included,
and on every path that never reaches a validated sync at all, including a
start that failed after seeding, a worker the provider lost, and a
transport failure during the read-back (12).

A cleanup pass runs each reconcile tick. Every deletion is an event and a
`RetentionAction` row naming the policy version that authorized it.
Nothing that a gate consumed is deleted before the task is terminal.

## Retention (initial defaults, configurable per policy)

| Data | Retention |
|---|---|
| events, gate results, decisions, acceptance results, dispositions | indefinite |
| completion claims, review reports, evidence, external reviews, CI certifications, release records | indefinite |
| diffs and artifact metadata | indefinite; artifact bytes size-capped per attempt by policy |
| worker logs and transcripts | 90 days, then deleted with an event |
| completed worker workspaces | 14 days |
| per-attempt credential copies | removed immediately after validated synchronization, and on every path that skips it |
| wakes | 30 days after ack |
| bootstrap SQLite archive | 180 days |

Retention cleanup is deterministic (a pure function of policy, clock, and
rows), auditable (each action is an event), and idempotent.
