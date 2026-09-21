# C7d implementation notes

## Finding map

| Finding | Fix | Regression |
|---|---|---|
| CRU-01 | Commit `3f0fec7` adds `POST /v1/tasks/{id}/republish` and `crucible-admin task republish` as bounded manual retries. Commit `c08f95a` pins the collected bundle SHA-256, rechecks the bytes before retry and publisher staging, preserves a zero retry cap, and resumes token-mint failures before push. Commit `beb40e1` seals retained pre-upgrade bundles and records publication start before installation validation, so either failure remains recoverable. | Commits `3f0fec7`, `c08f95a`, and `beb40e1`: `test_publish_failure_can_be_republished`, `test_republish_refuses_a_changed_head`, `test_republish_refuses_changed_bundle_content`, `test_token_mint_failure_republishes_from_before_push`, `test_pre_start_installation_failure_can_be_republished`, `test_pre_upgrade_bundle_is_sealed_before_publication`, `test_republish_enforces_the_policy_cap`, and `test_the_publisher_refuses_a_bundle_changed_after_collection` |
| CRU-02 | Commit `1360126` processes accepted review signals before CI advancement while a task awaits certification. Commit `c08f95a` also treats a new allowlisted inline comment on an already-recorded review as feedback, so green CI cannot bypass it. Commit `beb40e1` binds dispositions to the exact comment body and makes an edited finding pending again. | Commits `1360126`, `c08f95a`, and `beb40e1`: `test_observation_review_ci_race`, `test_new_inline_comment_on_recorded_review_revokes_ready`, and `test_edited_inline_comment_invalidates_its_old_disposition` |
| CRU-04 | Commit `1360126` moves `ready_for_merge` back to `ci_certification_failed` when a required check turns red, or to `external_feedback_received` when new accepted feedback arrives. Commits `c08f95a` and `beb40e1` close the late-inline-comment and edited-comment cases. | Commits `1360126`, `c08f95a`, and `beb40e1`: `test_ready_for_merge_invalidation`, `test_new_inline_comment_on_recorded_review_revokes_ready`, `test_edited_inline_comment_invalidates_its_old_disposition`, and live timing coverage in `tests/e2e/test_github_live.py` |
| CRU-05 | Commit `2075b1e` adds one application-layer ownership guard to every named task mutation. Operator and admin identities remain exempt from the ownership comparison. | Commit `2075b1e`: `test_principal_isolation_cancel_and_accept`, `test_principal_isolation_remaining_routes`, and `test_operator_and_admin_are_exempt_from_task_ownership` |

## Review corrections

The required non-author adversarial review found three blockers. Commit `c08f95a`
addresses each one: a late inline comment now revokes merge readiness, a retry is pinned
to the collected bundle digest and the publisher checks the source bytes again, and an
installation-token failure resumes before push. The same commit also fixes the review's
non-blocking zero-cap wake finding.

The review's remaining non-blocking observation was that remote `crucible-admin task
republish` rejects an admin token. That was not changed: the contract explicitly limits
the endpoint to orchestrator or operator callers. Local administrative mode remains
available, and remote mode requires an operator token. No behavioral finding was
discarded because the original red-team report cited an invented path.

The automatic post-PR review found three further defects, all corrected in `beb40e1`.
Pre-upgrade bundle evidence without a digest is sealed from the retained bundle before
publication. Publication start is recorded before a missing installation ID can fail the
attempt, preserving the audit record required by manual recovery. Review dispositions
are now append-only versions keyed to the exact comment body SHA-256, so an edited
allowlisted inline comment invalidates the prior disposition and revokes readiness. The
new migration preserves old disposition versions across downgrade and upgrade.

## Verification evidence

- `make lint`: passed.
- `make test`: 631 unit and 336 integration tests passed.
- `make scan`: no leaks found in the working tree or branch history.
- `make e2e` on the dedicated rootless daemon: 17 passed, 12 deselected.
- `make up` followed by `make smoke`: the stack reached healthy, every administrative
  UI page rendered, and the smoke task reached `accepted`.
- `make e2e-github`: 3 passed, 1 skipped, 25 deselected. The real App publication,
  merge observation, required-check failure, and cleanup paths completed on the
  throwaway repository.

The first final `make e2e` invocation used the shared daemon and stopped before any test
ran because its fixed `10.89.0.0/24` subnet was already allocated, the known condition
tracked in GitHub issue 41. The required final run used the dedicated rootless daemon and
passed. An intermediate rootless run exposed the missing-bundle digest edge case in
interrupted attempts; commit `c08f95a` corrected it before the successful final run.
