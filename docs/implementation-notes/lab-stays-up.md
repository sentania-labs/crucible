# The lab keeps running (2026-09-29)

A read-only review of main at ddcb97f found eight ways the lab stops or slows
down on Kubernetes. The operator's direction the same day: "stop
micromanaging"; review is the enforcement, Crucible does anything mechanical,
and hard failures belong only where the damage is real or a claim is false.
This note records what changed and the decisions taken under that direction.

## What changed

1. **Kept claims are released.** `keep` and `keep_diff_only` kept a
   workspace claim forever; the orphan sweep skips a claim with the retention
   label, and the retention window 16 names did not exist. The lab's quota is
   24 claims. The retention step now releases a kept workspace once its task
   is terminal or its work published, or `completed_workspaces_days` after
   cleanup, never while the task's latest work attempt is unpublished or a
   quota checkpoint is unpushed (16, `workspace_release_reason`). The provider
   port gained `release_workspace`.
2. **Transport errors are provider errors.** The Kubernetes client wraps a
   refused, reset or timed-out connection, and a 429 or 5xx answer, in
   `KubernetesUnavailableError`; every `KubernetesApiError` is a
   `ProviderError`. A credential copy is never removed before it was read
   back (12).
3. **One failed look is not an answer.** `_await_pod` and `_await_running`
   ask again until the deadline. A canary the API server could not run is an
   environment failure, never a refusal.
4. **Collection runs beside the tick**, as launches do since hades #190, with
   the same cancellation and lease-loss handling. A collection the cluster
   could not answer is tried again every 30 seconds for 30 minutes.
5. **A full namespace says so at once.** A `FailedCreate` naming the quota
   ends a worker launch or a role Job immediately; the supervisor Role gained
   `get` and `list` on events. Capacity reads CPU and memory quotas too.
6. **Short-role timeouts count from Running** and are the `kubernetes.timeouts`
   setting, with API, CLI and UI (25).
7. **The collected archive streams to disk** rather than being held twice in
   memory.
8. **Configuration writes do not wait for the supervisor.**

## Decisions

- **Which admin operations still need a live supervisor.** Committing a
  bootstrap import (it hands the ledger to supervision, ADR 0006), rotating a
  credential and removing one (they take away what a running worker may be
  using, and the rotated-out copy's shredding is the supervisor's retention
  step). Everything else is a configuration write that the supervisor reads
  when it next runs, so it proceeds. The refusal record (`admin_refused`) and
  the reason rule are unchanged.
- **"Published" means a completed publication of the task.** Once one exists,
  every kept workspace of the task goes except the latest work attempt when
  that attempt is not the one published (a correction still to be pushed).
  Review attempts are never the work a publication reads; theirs follow the
  terminal and window rules.
- **The collection retry window is fixed, not a setting** (30 seconds between
  tries, 30 minutes in all). It bounds how long a failed read-back can keep a
  credential copy on a claim, and it is not something an operator tunes per
  deployment; the setting the contract asked for is the short-role timeout.
- **A canary the API server could not run fails the launch as environment**
  rather than deferring it: the attempt is already prepared, the lifecycle
  has no edge back to pending, and the retry rule covers it.
- **Migration 0031** only adds the `kubernetes_timeouts_updated` event kind.
  It revises 0027 on this branch; whatever lands first among the parallel
  0028 to 0030 changes, the chain and the event-kind list are re-pointed at
  merge.
- **The publisher's Jobs wait for quota room** instead of failing fast. Everywhere
  else a quota-refused Job ends at once (a collection is tried again, a launch
  is an environment failure the retry rule covers), but a publication that gives
  up needs an operator's republish, and a full namespace is not a failed push
  (hades #226 is that situation). Its timeout names the quota.
- **Quota events are matched to the Job's uid.** Role Job names repeat for an
  attempt and an event outlives its Job by an hour, so without the uid a
  collection retried after the namespace had room would still be refused.
- **The Docker provider's credential read-back is unchanged**: the finding and
  the contract named the Kubernetes path, and on Docker the copy is on the
  host where a retry could find it.
