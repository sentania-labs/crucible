# 18. Testing strategy

Same definitions locally and in CI: `make lint` (ruff, mypy --strict,
import-linter), `make test` (unit + integration), `make e2e`. CI runs on
GitHub-hosted runners because the integration and end-to-end tiers need a
Docker daemon.

## Unit (no I/O, milliseconds)

- Every state machine transition table: allowed, disallowed, side effects.
- Every gate function against synthetic evidence, including the
  "worker-asserted evidence must not satisfy a gate" rule.
- Contract validation: each rule in 05 has a failing fixture.
- Harness adapters: launch spec construction, exit classification, report
  parsing with malformed inputs.
- Docker create-request policy: every forbidden option is refused.
- Secret scanner patterns.

## Integration (PostgreSQL in a container, fake provider)

- Migrations up and down from empty and from previous head.
- Submit, start, run to completion through the fake provider; assert the
  full event sequence and gate results.
- Every failure class in 16 produced by the fake provider, with the expected
  retry and wake behavior.
- Lease fencing: a stale supervisor token's write is rejected.
- Reconciliation idempotence: run twice, second run changes nothing.
- Supervisor restart mid-attempt with a running fake worker: state resolves
  correctly and no duplicate attempt is created.
- Concurrent checkout lease: second attempt on the same repository and
  branch is refused.
- Cancellation drains and collects a partial report as an artifact.
- Bootstrap import: verify, reject on tamper, commit, state mapping.
- Wake creation and poll; webhook retry schedule with a failing receiver.
- API auth: roles, idempotency keys, problem details.

## End-to-end (Docker provider, real containers, no model)

A worker image whose "harness" is a script implementing the adapter's
launch contract (reads the identity bundle, writes a report, exits with a
requested code). Proves the real provider path without a subscription:

- Prepare, launch, observe, logs, collect, cleanup on a real container.
- Isolation: the script attempts to reach the Docker socket, the proxy, the
  database, and another credential mount; each attempt must fail and be
  recorded as a test assertion.
- Timeout drain then kill.
- Loss: the container is removed out of band; reconcile marks `lost`.
- Orphan: a labeled container with no attempt row is removed.
- Crucible container restart with a worker still running: re-attach, logs
  resume from offset.
- Foundry disconnect: the API client exits after `start`; the run completes
  and a wake is waiting on poll.
- No injected files in the resulting branch; shims excluded.

## Live harness tests (subscription required, not in CI by default)

`make e2e-live HARNESS=<name>` runs a trivial task through the real harness
image with the operator's mounted credentials. Used for the spikes (21) and
the readiness demonstration; results recorded as artifacts in the
readiness evidence, not as CI status.

## Evidence for done

A test run's junit and coverage output are artifacts of the release; the
readiness gate (19) cites specific test names for each item.
