# C6c: readiness gaps that were code

C6c closes readiness rows 5, 7, 11, 12, 14, 21, and 23. Row 18 remains
unproven and unchanged because the real ledger handoff is a separate operator
act.

## Implementation

- `GET /v1/attempts/{attempt_id}/logs` returns offset-paged stdout, stderr, or
  combined text. A request with `Accept: text/event-stream` returns the same
  persisted log chunks as SSE records and ends only after the attempt is
  terminal and the stored tail is drained.
- Migration 0012 adds fenced heartbeat rows and extends the event constraint
  with `worker_quiet` and `worker_stalled`. Worker launch and log progress write
  activity signals. The supervisor warns once after `stall_warn_seconds`,
  creates the operator wake, then drains and kills after `stall_fail_seconds`.
  The attempt records exit class `timeout` and reason `stall`.
- A report present after a killed worker is stored as a `partial_report`
  artifact and is deliberately not parsed as a worker claim.
- Checkout lease contention is covered at integration level, including the
  single denial event and launch after the holder releases.
- The real GitHub target now has a required `crucible-readiness` check. The live
  test can force that check red and proves the task stops in
  `ci_certification_failed` without retry or correction.
- Live harness acceptance now promotes the selected image through the same
  image registry used by production routing. It compares each running
  container's `crucible.harness_version` label with the installed version
  returned by `/v1/harnesses`.

## Release evidence decision

The release workflow does not rerun test tiers. Readiness row 14 cites the `ci`
run on the tagged commit. "All tiers except live" means the five `ci` jobs:
`lint`, `scan`, `test`, `e2e`, and `compose-smoke`.

The most recent release tag, v0.2.1, points to commit
`a7a23679b85161982947abf49f5254cb6bf6d8eb`. Its green `ci` run is
https://github.com/sentania-labs/crucible/actions/runs/35169303005. That run
predates the addition of the `e2e` CI job, so it has four jobs. The current
five-job definition is proven green on `main` by
https://github.com/sentania-labs/crucible/actions/runs/35527704824. A future
release tag on a commit with the current workflow will carry all five jobs
without duplicating them in `release.yml`.

## External target evidence

The workflow was added to `sentania-labs/crucible-spike-target` through pull
request https://github.com/sentania-labs/crucible-spike-target/pull/93. Its
installation run is
https://github.com/sentania-labs/crucible-spike-target/actions/runs/35529498371.
The forced-red acceptance run is
https://github.com/sentania-labs/crucible-spike-target/actions/runs/35529796009.
The three version-comparison runs are:

- Claude Code:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530104688
- Codex:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530201700
- AGY:
  https://github.com/sentania-labs/crucible-spike-target/actions/runs/35530268129

Each live test closed its temporary pull request and deleted its temporary
branch after recording the evidence.

## Local verification

Run on the reference workstation on 2026-09-20, America/Chicago.

| Tier | Result |
|---|---|
| `make lint` | clean: Ruff format, Ruff checks, mypy on 225 files, and 3 import contracts |
| `make test` | 577 unit tests and 301 integration tests passed |
| `make scan` | tree and 8-commit history clean, no leaks found |
| `make e2e-image` | script harness built at digest `sha256:9b5b91e74522bf6e65d159d27fec2fd0815cc915f6b760f11ecc974c20fc6b73` |
| `make e2e` | 16 passed, 10 deselected, 96.97 s on the dedicated rootless daemon |
| `make up`, then `make smoke` | isolated host-daemon project healthy; full task, gates, review, and acceptance passed |
| `make e2e-github` | 3 passed, 1 skipped, 92.06 s; target PRs cleaned up |
| `make e2e-live HARNESS=all` | 3 passed, 23 deselected, 282.85 s; all three real harnesses reached `ready_for_merge` |
| `make e2e-admin` | 3 passed, 23 deselected, 50.31 s with the dedicated credential root |

The rootless service user's daemon cannot traverse the operator's home path for
Compose bind mounts. The compose smoke therefore used the normal host daemon,
a unique project name, no host PostgreSQL port, and a fresh volume. The default
Crucible volume was preserved. Temporary containers and networks were stopped
after the successful smoke.

## Review

The required non-author adversarial review is pending. Its findings and
dispositions will be recorded here before the Crucible pull request opens.

## Spec notes and follow-ups

No spec or ADR file was changed.

- Spec 04's log route is implemented beneath the API's existing `/v1` prefix.
- Spec 10's heartbeat record is now persistent and fenced. Progress signals do
  not replace the worker activity signals used for stall decisions.
- Spec 19 can mark the seven C6c rows proven once the branch CI URL is recorded.
- The v0.2.1 tagged commit predates the fifth CI job. The next release is the
  first tag that can carry the current five-job definition.
- Row 18 still requires the real ledger export, import, commit, and
  `mark-migrated` sequence with the operator's explicit authorization.
- The second-daemon Codex image reproducibility check and the rootless Compose
  location decision remain separate follow-ups already recorded by prior work.
