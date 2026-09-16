# 17. Notification and Foundry-wake contract

## When Crucible wakes Foundry

Only when judgment is required or work has stopped needing it:

- task `gates_passed` or `gates_failed` (awaiting acceptance)
- task `blocked` (escalation opened)
- attempt `failed`, `timed_out`, `lost` with no retry remaining
- `quota_exhausted` or `auth_failure` on any harness
- an escalation older than `escalation_stale_hours` (repeat)
- supervisor takeover by a new instance (informational, once)
- bootstrap import verified (awaiting commit)

Progress is not a wake. Foundry polls or tails logs when it wants progress.

## WakeV1

```json
{
  "id": "01J...", "schema_version": "1.0",
  "principal": "foundry",
  "reason": "gates_failed",
  "task": { "id": "01J...", "external_id": "FDY-0042", "state": "awaiting_acceptance" },
  "attempt_id": "01J...",
  "summary": "2 of 9 required gates failed: scope_contained, verification_ran",
  "links": { "task": "/v1/tasks/01J...", "gates": "/v1/attempts/01J.../gates" },
  "created_at": "2026-09-16T06:10:00-05:00"
}
```

## Delivery

1. Row first: the wake is committed in the same transaction as the state
   change that caused it.
2. Webhook: POST to the principal's configured URL with an HMAC signature
   header; retries with exponential backoff for `wake.retry_hours`
   (default 24), then stops retrying but keeps the row.
3. Poll: `GET /v1/wakes` returns unacked wakes for the caller. Foundry's
   start-of-session procedure always polls, so webhook failure only delays.
4. Ack: `POST /v1/wakes/{id}/ack` with what Foundry did. Unacked wakes are
   listed on `GET /supervisor` as a count.

For a Foundry running inside an interactive harness on a workstation, the
webhook target is a small local receiver (part of Foundry's tooling, not
Crucible) that writes to Foundry's inbox file, or nothing at all: poll is
sufficient and is the default configuration.
