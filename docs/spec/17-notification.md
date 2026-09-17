# 17. Notification and Foundry-wake contract

## When Crucible wakes Foundry

Only when judgment is required or work has stopped needing it:

| Reason | State |
|---|---|
| `internal_review_needed` | `awaiting_internal_review` |
| `gates_passed` | `awaiting_acceptance` |
| `pre_pr_gates_failed` | `pre_pr_gates_failed` |
| `needs_more_work` | after a `needs_more_work` verdict, until a correction is attached |
| `blocked` | `blocked` (escalation opened) |
| `publish_failed` | `publish_failed` |
| `external_feedback_received` | `external_feedback_received` |
| `external_review_overdue` | repeat, no state change |
| `external_review_trigger_needed` | `awaiting_external_review` on a head whose cycle needs the orchestrator's trigger under the operator's account (23) |
| `ci_certification_failed` | `ci_certification_failed` |
| `ci_certification_overdue` | repeat, no state change |
| `ci_rerun_needed` | after a `ci-decision rerun`: Crucible records the intent, the operator re-runs it on GitHub (the App holds no Actions write) |
| `head_diverged` | `head_diverged` (decision required) |
| `ready_for_merge` | `ready_for_merge` |
| `merged` | `merged` (informational) |
| `release_gates_failed`, `release_succeeded`, `release_workflow_failed` | release lifecycle (24) |
| `attempt_failed`, `timed_out`, `lost` with no retry remaining | `reported` |
| `quota_exhausted`, `auth_failure` | any |
| `harness_version_unsupported` | launch refused |
| `escalation_stale` | repeat |
| `supervisor_takeover` | informational, once |
| `bootstrap_import_verified` | awaiting commit |

Every wake in the delivery half stands on an observation Crucible
recorded under the `github` event principal (10), never on an orchestrator
session being connected.

Progress is not a wake. Foundry polls or tails logs when it wants progress.
Review feedback is never sent to a worker; it is only ever carried to
Foundry.

## WakeV1

```json
{
  "id": "01J...", "schema_version": "1.0",
  "principal": "foundry",
  "reason": "external_feedback_received",
  "task": { "id": "01J...", "external_id": "FDY-0042", "state": "external_feedback_received" },
  "attempt_id": "01J...",
  "pull_request": { "number": 18, "url": "...", "head_sha": "abc123..." },
  "summary": "1 review from chatgpt-codex-connector[bot] on abc123: 3 comments, 0 dispositions recorded",
  "links": { "task": "/v1/tasks/01J...", "pull_request": "/v1/tasks/01J.../pull-request" },
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

For a Foundry running inside an interactive harness on a workstation, poll
is sufficient and is the default. Moving Foundry into a persistent service
later changes only the delivery target, not the wake contract.
