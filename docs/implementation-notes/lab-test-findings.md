# Lab test findings on v0.6.3 (hades #187, #189, #190, #191, FDY-0129)

## Decision

The operator, 2026-09-28, on FDY-0129 ("go."): deliver the four findings of the two lab
tests of v0.6.3 on one branch. The contract fixed three choices and left one open:
readiness means the API can serve, with supervisor health on `/v1/supervisor` and a UI
banner; a launch step must not stall the loop that renews the lease; a cancel is
honoured before each launch step. How `refs.head_sha` is treated was left to the worker
with the issue's recommendation.

## What changed, and the choices made (2026-09-28)

- **`refs.head_sha` (#187).** The collected bundle's head is the head. `commits_present`
  passes on the bundle's own facts (a commit beyond `base_ref`, a bundle that verifies, a
  head it names) and notes a reported head that differs or is missing, echoing it only
  when it is a hash. Crucible already records the bundle head on the task, publishes it
  and gates on it, so a hash the worker copied added no check, only a way for a correct
  run to fail. IDENTITY.md still asks for `git rev-parse HEAD` after the final commit,
  and lists the acceptance criteria with the rule for `acceptance_mapping`.
- **The launch beside the tick (#190).** The quick half of a launch (route, gate, checkout
  lease, `preparing`) stays in the tick, one attempt after another, so the per-harness
  cap and the checkout lease see every launch already begun. The slow half is an asyncio
  task; the tick waits on the launches it started for at most a third of the lease TTL,
  capped at 5 seconds, which is derived and not a new setting. The collect step still
  runs in the tick; see the report's follow-ups.
- **Cancel during launch (#189).** `prepare` takes a `cancelled` check. The Kubernetes
  provider asks it before the cache refresh, before the preparer and on every poll of
  either Job, and the supervisor asks once more in the transaction that would move the
  attempt to `launching`.
- **Cancel during launch, after Codex's review of PR 202 (2026-09-28).** The Docker
  preparer's wait asks the check every 2 seconds (a class constant, not a setting) and
  force-removes the container on a cancel. `launch` takes the same check and asks it
  just before it creates the worker. The transaction that would record the worker
  `running` asks it too: on a cancel it settles the attempt `killed` at stage `launch`
  instead, and the launch kills the worker it started. The cancel sweep never acts on
  a `preparing` or `launching` attempt, so only the launch settles it.
- **Readiness (#190).** `/v1/ready` is decided by the database and the migrations; its
  `supervisor` check stays in the response as information. Every signed-in admin page
  carries a red banner while the supervisor is not healthy.
- **The refresher and github.com (#191).** Found on kind with Calico enforcing: the
  refresher's Pod is selected by its policy, and the policy permits what the provider
  resolved. The Pod resolves the name again for itself, and github.com hands out one
  address with a 60 second TTL, different between resolvers, against the provider's 300
  second cache. Every Pod whose policy names resolved addresses now carries them as
  `hostAliases`. The refresh probes the remote for 20 seconds (a constant, not a
  setting) before it fetches.

No new tunable was added, so no new UI control was needed.
