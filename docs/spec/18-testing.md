# 18. Testing strategy

Same definitions locally and in CI: `make lint` (image-manifest drift, ruff,
mypy --strict, import-linter), `make test` (unit + integration), `make e2e`.
The e2e target checks the declared script-harness pin before contacting the
daemon and requires an explicit `make e2e-image` to repair drift. CI runs on
GitHub-hosted runners because the integration and end-to-end tiers need a
Docker daemon.

## Test fixtures are off in production

The fake provider (08) runs nothing and the script harness reports completion
without doing work, so neither is wired unless the restart-bound setting
`test_fixtures` (`CRUCIBLE_TEST_FIXTURES`) is true. It defaults to false, and a
deployment that leaves it there never lists, offers, or routes to either: they are
absent from the harness registry, every administration page, and the providers
(crucible#124, 2026-09-25). The integration tier builds its own registry with
them; the compose smoke (CI and release) sets the variable for its boot, and the
kind overlay sets it for the kind tiers. A developer running `make dev` sets it in
their own environment.

## Unit (no I/O, milliseconds)

- Every state machine transition table: allowed, disallowed, side effects.
- Every gate function against synthetic evidence, including the
  "worker-asserted evidence must not satisfy a gate" rule.
- Contract validation: each rule in 05 has a failing fixture.
- Harness adapters: launch spec construction, exit classification, report
  parsing with malformed inputs.
- Docker create-request policy: every forbidden option is refused.
- Secret scanner patterns, including GitHub installation token shapes.
- PR body rendering: worker-asserted text never labeled verified; only
  contract closing references survive; secret patterns rejected.
- Release gates against synthetic tag lists and version files.
- Harness version range refusal.

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
- GitHub delivery against a recorded fake GitHub API: publish, PR open,
  head update, external review from an allowlisted login and from a
  non-allowlisted one (must not satisfy), dispositions, CI green, CI
  failure with evidence capture and no automatic retry, merge observed,
  close-without-merge, head changed by other, webhook signature valid and
  invalid, delivery dedupe, poll-only mode equivalence.
- Correction loop: external feedback to correction to re-publish to CI
  certification without a second external review round or a second
  internal review unless requested.
- Round counting: a two-round policy waits for the second allowlisted
  signal; a `+1` reaction from the allowlisted login counts, from another
  login does not.
- Branch-only deliverable: published and verified before `accepted`.
- Out-of-band head: task blocks in `head_diverged`; green CI on the new
  SHA does not advance it; recollect re-runs the pre-PR path.
- Empty required-check set stays pending; `allow_no_ci` makes it skipped.
- Release tag not matching the rendered version is refused.
- Webhook: raw body never reaches storage; a comment containing a
  credential-shaped string is stored redacted with the body hash.
- Retention: deterministic and idempotent; every deletion an event.

## End-to-end (Docker provider, real containers, no model)

A worker image whose "harness" is a script implementing the adapter's
launch contract (reads the identity bundle, writes a report, exits with a
requested code). Proves the real provider path without a subscription:

- Prepare, launch, observe, logs, collect, cleanup on a real container.
- Isolation: the script attempts to reach the Docker socket, the proxy, the
  database, another credential mount, and to `git push`; each attempt must
  fail and be recorded as a test assertion.
- Publisher: push from a bundle to a throwaway repository with a token on
  tmpfs; the token is absent from env, logs, events, and the host after.
- Timeout drain then kill.
- Loss: the container is removed out of band; reconcile marks `lost`.
- Orphan: a labeled container with no attempt row is removed.
- Crucible container restart with a worker still running: re-attach, logs
  resume from offset.
- Foundry disconnect: the API client exits after `start`; the run completes
  and a wake is waiting on poll.
- No injected files in the resulting branch; shims excluded.

## End-to-end on Kubernetes (`make e2e-kind`, C8)

The end-to-end suite against the Kubernetes provider on a throwaway kind
cluster with an egress-enforcing CNI, plus the NetworkPolicy denials, loss by
eviction, SIGTERM-ignoring workers, per-attempt Secret removal, supervisor
re-attach, and the namespace readiness refusal. Cases and evidence rules in
26; runs in CI beside the Docker tier. The tier also runs a full
API-and-Supervisor lifecycle for gates, timeout, cancellation, stall,
disconnected completion, verification hardening, restart, and orphan cleanup;
two concurrent claims; real registry digest and harness-label resolution; and
every cleanup policy. Every denied NetworkPolicy endpoint has a reachable
control before the selected policy proves it blocked.

## The registry adapter (`make registry-check`, 108)

The Kubernetes provider's registry adapter runs the real `crane` binary, at the
version the service image ships (`tools/crane/fetch.sh` reads the Dockerfile's
pin). Unit tests run it against a stub `crane` (argument shape, the private
credential directory and its removal on every path, error mapping, timeout).
`make registry-check` runs the real binary twice: against a stub registry that
demands a password and answers every blob GET with a 307 to another host, as
GHCR does, and against the published worker image on GHCR, anonymously,
asserting a digest and a version label for every harness. CI runs it on every
branch push; the release runs the GHCR half inside the service image it is
about to publish. The kind tier resolves its own registry through the same
real binary.

## Live harness tests (subscription required, not in CI by default)

`make e2e-live HARNESS=<name>` runs a trivial task through the real harness
image with the operator's mounted credentials. Used for the spikes (21) and
the readiness demonstration; results recorded as artifacts in the
readiness evidence, not as CI status.

## Evidence for done

A test run's junit and coverage output are artifacts of the release; the
readiness gate (19) cites specific test names for each item.
