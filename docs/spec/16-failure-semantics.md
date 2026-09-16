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
fresh workspace. A correction execution starts from the remote
`work_branch` head Crucible pushed (08), so published work is never
abandoned or force-pushed over; a plain retry of an unpublished attempt
starts from `base_ref`, because nothing of the failed attempt was ever
pushed.

## Delivery-half failures (23)

| Situation | Handling |
|---|---|
| push rejected (remote moved) | `publish_failed`, remote head recorded, wake; never force |
| PR API error | `publish_failed` with response class; wake; Foundry may retry publish |
| required CI check failed | `ci_certification_failed`; evidence captured; wake; no retry, no correction until Foundry decides |
| external review not received in `wait_timeout_hours` | repeat wake; state unchanged |
| head changed by someone else | new certification row; informational wake; Crucible never overwrites |
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
exists the open step is complete; otherwise the job re-runs. Stored raw
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
| `credential_volume_remove` | immediately_after_validated_sync | immediately_after_validated_sync |

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
| temporary credential volumes | removed immediately after validated synchronization and attempt cleanup |
| wakes | 30 days after ack |
| bootstrap SQLite archive | 180 days |

Retention cleanup is deterministic (a pure function of policy, clock, and
rows), auditable (each action is an event), and idempotent.
