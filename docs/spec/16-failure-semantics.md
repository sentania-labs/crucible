# 16. Failure, restart, retry, cancellation, cleanup, and retention

## Failure classes and default handling

| Class | Meaning | Default |
|---|---|---|
| `completed` | exit 0 and report present | gates |
| `blocked` | exit 75, `blocked.md` present | escalation, wake, task `blocked` |
| `environment` | exit 70, or provider failed before the harness ran | retry if attempts remain; else `failed` |
| `auth_failure` | harness reported auth problem (adapter classified) | retry per policy (`retry.auth_failure_max`, after `auth_retry_delay_seconds`); wake regardless |
| `quota_exhausted` | harness reported rate or quota limit | not retry-eligible by default; task `reported` with the class visible; wake (Foundry may choose another harness) |
| `timeout` | contract timeout or stall | no retry; gates run on what exists; wake |
| `killed` | terminated by request | `cancelled` |
| `crashed` | non-zero exit not otherwise classified | no retry by default; wake |
| `lost` | provider cannot find the worker | retry if attempts remain and policy allows `lost`; else `failed` |
| `completed_without_report` | exit 0, no report | report gate fails; no retry; wake |

Retry is never a way to re-roll the worker's judgment. A class retries only
when it is in both the policy's `retry.eligible_classes` and the contract's
`retry_on`. Each retry is a new attempt with the same contract version and a
fresh workspace that resumes from the remote `work_branch` if the previous
attempt pushed (08), so pushed work is never abandoned or force-pushed over.

## Restart of Crucible

Supervisor restart triggers reconciliation (10). Workers keep running in
their containers during the restart; the provider's `reconcile` re-attaches
by label. Log capture resumes from the last stored timestamp-and-hash position (10). Attempt leases that expired during the outage are renewed
if the container is alive; a worker is never marked lost merely because
Crucible was down, only because the provider cannot see it.

API restart is stateless. In-flight requests fail with 503 and the client
retries with the same idempotency key.

## Cancellation

`POST /tasks/{id}/cancel` records the verbatim reason, moves the task to
`cancelling`, and terminates running attempts with `drain`. Whatever the
worker wrote to the report directory before termination is collected and
stored (it may include a partial report, kept as an artifact but never
parsed as a claim). The checkout is kept per cleanup policy so partial work
is recoverable by a person. Cancellation of a task with no running attempt
is immediate.

## Cleanup policy (per policy document)

| Setting | Options | Default |
|---|---|---|
| `workspace.on_success` | delete, keep, keep_diff_only | keep_diff_only |
| `workspace.on_failure` | delete, keep | keep |
| `workspace.max_age_hours` | number | 168 |
| `container.remove` | always, on_success, never | always |
| `credential_volume.remove` | always | always |

A cleanup pass runs each reconcile tick. Deletions are events. Nothing that
a gate consumed is deleted before the task is terminal.

## Retention

| Data | Retention |
|---|---|
| events, gate results, decisions, acceptance results | indefinite |
| completion claims, evidence | indefinite |
| artifacts (report, diff, review, check logs) | indefinite in v0.x; size-capped per attempt by policy |
| transcripts and log chunks | `logs.retention_days`, default 90; then deleted, event recorded |
| workspaces | per cleanup policy above |
| wakes | 30 days after ack |
| bootstrap SQLite archive | `bootstrap.retention_days`, default 180 |
