# Crucible readiness report (19)

This is the evidence document `docs/spec/19-readiness-gate.md` asks for: one row per
requirement of its two tables, with the tests that prove it, the tier that runs them,
the run whose result is named here, and the recorded artifact identifiers. It is
written for the operator's approval decision, so it is deliberately ungenerous. A row
is **proven** only when a run of the named tests exists and its result can be named
here. A row whose asked-for proof does not exist, or exists only as a test nobody has
run, is **unproven**, and the gap is named exactly in the section below the tables.

Eight of the twenty-four rows are unproven. None of them is unproven for want of
effort in C6: each names a piece of the system that is not built, not exercised at the
tier 19 asks for, or, in the bootstrap row's case, deliberately held back for the
operator's explicit go.

## What was run for this report

All on the reference workstation, 2026-09-18, America/Chicago, on branch
`c6/bootstrap-import` at the head this report is committed on. The Docker tiers ran
against the dedicated rootless daemon of the `crucible` service user (ADR 0004, S9),
the arrangement row 22 records.

| Tier | Command | Result | Finished |
|---|---|---|---|
| unit | `make test-unit` | 550 passed, 5.84 s | 11:41:12 PM |
| integration | `make test-integration` | 273 passed, 308.90 s, real PostgreSQL 16 in a container | 11:35:04 PM |
| e2e (Docker) | `make e2e DOCKER=<rootless wrapper>` | 12 passed, 9 deselected, 75.26 s | 11:36:29 PM |
| e2e_admin, the C6 test only | `pytest tests/e2e/test_admin_live.py::test_the_bootstrap_import_through_api_and_cli_on_the_live_stack -m e2e_admin` | 1 passed, 4.36 s, live stack on the rootless daemon | 11:37:34 PM |
| lint | `make lint` | ruff format, ruff check, mypy (217 files), lint-imports (3 contracts) all clean | before the commits |
| scan | `make scan` | gitleaks, no leaks, tree and history | before the commits |

The live administration tier could **not** be run in full: the dedicated credential
root `/var/lib/crucible/credentials` is owned by the `crucible` service user with mode
750, and the session that produced this report does not belong to that group, so
`make e2e-admin` fails at collection with `PermissionError`. The C6 test of that tier
needs no harness credential, so it was run on its own against a synthetic credential
root holding three empty directories; that run is the one recorded above. The harness
probe tests of the tier were not run here. Their last recorded run is C5b's, named in
row 15.

The live harness tier (`make e2e-live`) and the live GitHub tier (`make e2e-github`)
were not run for this report. Their last recorded runs are C5's and C4's, named in
rows 3 and 19.

CI run for this branch, all five required checks green:
`https://github.com/sentania-labs/crucible/actions/runs/35422008877`, on commit
`79f0ee9`, the head this report was first pushed at, 2026-09-18 11:42 PM local; the
run for the current head is linked from pull request 32. `lint` 21 s, `scan` 7 s,
`test` (unit and integration) 5 m 59 s, `e2e` 2 m 3 s, `compose-smoke` 1 m 1 s. No
workflow runs any live tier.

### C6b delta, 2026-09-20

Class routing and reactive quota reroute were exercised on the reference workstation
against PostgreSQL 16 and the dedicated rootless Docker daemon. The pull request CI
run URL is added below once the branch is pushed.

| Tier | Command | Result | Finished |
|---|---|---|---|
| unit | `make test-unit` as part of `make test` | 564 passed, 6.03 s | 11:03 AM |
| integration | `make test-integration` as part of `make test` | 285 passed, 442.93 s, real PostgreSQL 16 | 11:09 AM |
| e2e (Docker) | `make e2e DOCKER='<rootless wrapper>'` | 13 passed, 9 deselected, 104.39 s | 11:11 AM |
| lint | `make lint` | ruff format, ruff check, mypy (222 files), lint-imports (3 contracts) all clean | 11:01 AM |
| scan | `make scan` | gitleaks, no leaks, tree and 7-commit branch history | 10:58 AM |

C6b-specific evidence is `tests/unit/test_class_routing.py`,
`tests/unit/test_task_contract.py`, `tests/integration/test_class_routing.py`,
`tests/integration/test_github_delivery.py::test_quota_checkpoint_is_pushed_before_the_reroute_is_scheduled`,
`tests/integration/test_admin.py::test_the_cli_remote_mode_builds_the_same_calls`, and
`tests/e2e/test_class_routing.py::test_scripted_quota_reroutes_to_a_second_image_and_remote_branch`.

## Table 1: the readiness gate

| # | Requirement | Status | Tests, tier, and the run whose result is named |
|---|---|---|---|
| 1 | Accept and validate a versioned task contract | proven | unit `tests/unit/test_task_contract.py` and `tests/unit/test_refs.py`; integration `tests/integration/test_api.py::test_submit_validation_problem_details`, `::test_submit_shape_errors_name_the_path`, `::test_duplicate_external_id_is_409`, `::test_start_disagreeing_with_contract_is_422`, `tests/integration/test_policies_api.py::test_a_contract_is_validated_against_the_routing_policy`, `tests/integration/test_harness_registry.py::test_a_disabled_harness_is_a_contract_problem_at_submit`. C6b adds class-only contracts, exact operator-pin validation, harness-without-model refusal, and real-provider supplied-image refusal in `tests/unit/test_task_contract.py`. Run: the unit and integration tiers above and the C6b delta. |
| 2 | Persist task, worker, execution, event, artifact, evidence, and decision state | proven | integration `tests/integration/test_full_run.py::test_submit_start_run_to_reported`; migrations `tests/integration/test_migrations.py::test_up_down_up_from_empty`, `::test_events_and_contracts_are_append_only`, `::test_fresh_schema_has_no_drift`, `::test_a_downgrade_past_c4_keeps_the_c4_events`, `::test_0010_down_and_up`, and C6b's 0011 down-up event restoration cases; `tests/integration/test_artifacts_api.py`, `tests/integration/test_fencing.py`, and `tests/integration/test_class_routing.py`. Run: the integration tier above and the C6b delta. |
| 3 | Launch at least one supported harness in an isolated environment | proven | e2e_live `tests/e2e/test_live_harness.py::test_a_trivial_task_reaches_ready_for_merge_live`, parametrized `[claude_code]`, `[codex]`, `[agy]`. Run: C5's recorded live tier, 2026-09-17, target `sentania-labs/crucible-spike-target`. claude_code 11:37:45 AM, 42.9 s, exit 0, `ready_for_merge`; codex 11:38:52 AM, 58.3 s, exit 0, `ready_for_merge`; agy 11:40:14 AM, 52.4 s, exit 0, `ready_for_merge`. Artifacts: pull requests 85, 86 and 87, each closed and its branch deleted by the tier. Images `crucible-worker:claude_code-2.1.273-8b75176f203e`, `codex-0.153.4-ce72fc1b2e20`, `agy-1.2.4-26838f92302b`. Not re-run on this branch, which changes no launch path. |
| 4 | Inject identity and instructions without committing them | proven | e2e `tests/e2e/test_full_run.py::test_the_branch_carries_no_injected_file` and `::test_a_script_harness_run_reaches_acceptance_with_every_gate_green`; unit `tests/unit/test_gates.py::test_no_injected_files_sees_the_commit_list_as_well_as_the_diff`, `tests/unit/test_identity_bundle.py` (7 tests). Run: the e2e and unit tiers above. |
| 5 | Capture or stream worker events and logs | **unproven** | See gap 1. The stored half is proven: e2e `tests/e2e/test_full_run.py::test_a_script_harness_run_reaches_acceptance_with_every_gate_green` (log chunk rows, drain, cleanup) and `tests/e2e/test_failures.py::test_a_restart_re_attaches_and_resumes_the_log_offset`; unit `tests/unit/test_docker_logs.py` (13 tests); spike S8, 2026-09-16 11:35 AM. The live tail half has no implementation and no test. |
| 6 | Continue authorized work while Foundry is disconnected | proven | e2e `tests/e2e/test_failures.py::test_the_run_completes_with_no_client_attached`; integration `tests/integration/test_wakes.py::test_the_wake_row_exists_before_any_delivery`, `::test_poll_and_ack`, `::test_no_webhook_configured_means_poll_only`, `::test_a_failing_receiver_only_delays`. C6b adds autonomous quota reroute and timed resume in `tests/integration/test_class_routing.py`, with one informational wake and no Foundry round trip. Run: the e2e and integration tiers above and the C6b delta. |
| 7 | Detect completion, failure, timeout, cancellation, stall, and loss | **unproven** | See gap 2. Five of the six classes are proven: integration `tests/integration/test_failure_classes.py` (12 tests, one per class) and `tests/integration/test_cancel.py` (9 tests); e2e `tests/e2e/test_failures.py::test_a_timeout_drains_then_kills` and `::test_a_worker_removed_out_of_band_is_lost`; unit `tests/unit/test_exit_class.py`. Stall has no implementation and no test, and cancellation is not exercised at the e2e Docker tier. |
| 8 | Preserve worker reports and verification evidence | proven | integration `tests/integration/test_artifacts_api.py::test_collection_stores_the_claim_and_the_run_evidence`, `::test_the_parsed_report_is_readable`, `::test_evidence_is_readable_and_the_worker_row_is_unverified`, `tests/integration/test_fencing.py::test_evidence_is_fenced_and_a_worker_row_can_never_be_verified`; unit `tests/unit/test_gates.py::test_worker_asserted_evidence_never_satisfies_a_gate`. Run: the integration and unit tiers above. |
| 9 | Restart and reconcile active or interrupted executions | proven | e2e `tests/e2e/test_failures.py::test_a_restart_re_attaches_and_resumes_the_log_offset`, `::test_a_labelled_container_with_no_attempt_row_is_removed`; integration `tests/integration/test_reconcile.py` (15 tests, including `::test_reconcile_twice_changes_nothing_after_completion`), `tests/integration/test_fencing.py::test_second_supervisor_waits_as_standby`, and C6b `tests/integration/test_class_routing.py::test_all_pools_wait_and_a_restarted_supervisor_resumes_on_schedule`. Run: the e2e and integration tiers above and the C6b delta. Note: the restart is in-process; `docker compose restart` of the deployed service, S8's other completion condition, is still not exercised by any test. |
| 10 | Enforce the required deterministic gates | proven | unit `tests/unit/test_gates.py` (43 tests, one per pre-PR gate) and `tests/unit/test_delivery_gates.py` (10 tests, the publication and post-PR gates); integration `tests/integration/test_gates_and_acceptance.py::test_fail_path_scope_contained`, `::test_each_fail_fixture_fails_its_gate`, `::test_pass_path_reaches_awaiting_acceptance_then_accepted`; e2e `tests/e2e/test_hardening.py` (3 tests). Run: the unit, integration and e2e tiers above. |
| 11 | Stop or terminate a worker safely | **unproven** | See gap 3. Drain then kill is proven at the e2e tier: `tests/e2e/test_failures.py::test_a_timeout_drains_then_kills`, with spike S5's forced cases (2026-09-16 2:08 PM to 2:15 PM) behind it. The partial report half is proven only against the fake provider: integration `tests/integration/test_cancel.py::test_cancel_with_partial_report_is_not_parsed`. |
| 12 | Prevent concurrent workers from corrupting the same working tree | **unproven** | See gap 4. The e2e half is proven: `tests/e2e/test_failures.py::test_a_second_attempt_on_the_same_branch_waits_for_the_checkout_lease`, which asserts `checkout_lease_denied` on the second attempt. There is no integration-tier checkout lease refusal test. |
| 13 | Expose sufficient API state for Foundry to inspect and reconcile | proven | integration `tests/integration/test_client_reconstruction.py::test_a_client_reconstructs_the_whole_run_from_the_api`, `::test_a_client_reconstructs_a_correction_loop`, `::test_a_client_reconstructs_a_blocked_task_and_its_decision`; C6 adds `tests/integration/test_bootstrap_import.py::test_a_scripted_start_of_session_reconstructs_the_live_task_set`. C6b's `tests/integration/test_class_routing.py` proves the task view exposes each attempt's model, harness, pool, reroute chain, remote-resume flag, and pending `resume_at`; the same module proves routing usage and admin clear state. Run: the integration tier above and the C6b delta. Caveat: worker logs are not readable through the API at all (gap 1), so a reconstruction cannot include them. |
| 14 | Demonstrate through automated tests | **unproven** | See gap 5. The tiers themselves run and pass, as the table at the top records, and the `ci` workflow runs `lint`, `scan`, `test`, `e2e` and `compose-smoke` on every pull request and on `main`. What 19 asks for, CI green on a release tag with all tiers except live, is not achievable from `release.yml` as written, and no green CI run URL is recorded anywhere in the repository. |

## Table 2: additional, from the operator's direction

| # | Requirement | Status | Tests, tier, and the run whose result is named |
|---|---|---|---|
| 15 | Each harness runs non-interactively in the worker container with subscription auth | proven | Spikes with transcripts: S1 (2026-09-16 1:59 PM to 2:08 PM, all three harnesses complete a trivial prompt with subscription auth from a mounted copy as uid 1000), S2 (2:10 PM to 2:13 PM, decision: proceed, do not enable bubblewrap), S3 (1:59 PM and 2:05 PM, AGY exit 0 in 22 s, argv ceiling measured), S1b (operator present, 4:20 PM to 5:21 PM). Live probes, C5b, 2026-09-17: claude_code 11:34:18 AM via API, 3.7 s, version 2.1.273, digest `sha256:aae06466e728...f2101`; codex 11:34:22 AM via CLI, 6.9 s, version 0.153.4, digest `sha256:15362eb4cb32...62615`; agy 11:34:29 AM via API, 5.4 s, version 1.2.4, digest `sha256:4d8c756f5b5a...c7b571`. Six consecutive `make e2e-admin` runs, 11:30 AM to 11:35 AM, 24 probes, every one conclusive. Unit `tests/unit/test_harness_adapters.py` (15 tests) and `tests/unit/test_credential_copy.py` (24 tests), run above. Open: Codex's Crucible-side credential refresh (S1b step 5) did not occur, so Codex stays disabled in the shipped defaults. |
| 16 | Workers cannot access the Docker socket, proxy, database, or other credentials | proven | e2e `tests/e2e/test_isolation.py::test_a_worker_reaches_nothing_it_must_not`, twenty probes, every one refused: `docker-socket-unix`, `docker-socket-run`, `socket-proxy`, `socket-proxy-ip`, `database`, `database-gateway`, `crucible-api`, `other-credential-codex`, `other-credential-claude`, `other-credential-agy`, `other-credential-root`, `git-push`, `egress-not-allowlisted`, `egress-direct-by-ip`, `egress-direct-by-name`, `write-root`, `write-identity`, `write-usr`, `chown-report`, `mknod`. Run: the e2e tier above, on the rootless daemon. |
| 17 | Codex's disabled sandbox cannot escape the container | proven | Spike S4 (2026-09-16 2:04 PM, partial, deferred to C3's rootless re-run) and spike S2, which found bubblewrap unavailable and kept ADR 0005: the container is the boundary and the Codex adapter keeps `--dangerously-bypass-approvals-and-sandbox`. S4's completion condition is the twenty-probe run of row 16, re-run on the real arrangement. Named substitution: the probes drive shell tooling in the worker container shape rather than Codex itself, which is the stronger test of the boundary but is not a Codex-specific escape attempt. |
| 18 | Bootstrap ledger imported and authority handed off | **unproven** | See gap 6. The import path itself is proven: unit `tests/unit/test_bootstrap_bundle.py` (39 tests, every validation rule and every state mapping, refusals included); integration `tests/integration/test_bootstrap_import.py` (17 tests: the full import and commit lifecycle against real PostgreSQL, idempotence, the partial-failure rollback that stores nothing, the refusals, the supervisor leaving the unsupervised attempt alone, CLI and API parity, and the reconstruction of row 13), including `::test_the_bundle_foundry_ledger_wrote_imports_and_commits` against the bundle the real `foundry-ledger` producer wrote for its own invented fixture; live `tests/e2e/test_admin_live.py::test_the_bootstrap_import_through_api_and_cli_on_the_live_stack`, run 2026-09-18 11:37:32 PM, import `01M2VZ92ZZKRGAZW9SGR3MGC6T`, 8 tasks, 15 events, 2 synthetic executions, 2 unsupervised attempts, 8 handoff events, state `authoritative`, 0.4 s. What is missing is the real ledger. |
| 19 | Workers hold no GitHub credential; Crucible pushes and opens the PR only after pre-PR gates and acceptance | proven | e2e `tests/e2e/test_isolation.py::test_a_worker_reaches_nothing_it_must_not`, probe `git-push`, refused, run above; integration `tests/integration/test_github_delivery.py::test_publication_pushes_the_head_and_opens_the_pull_request`, `::test_no_installation_token_reaches_any_record`, `::test_green_required_checks_reach_ready_for_merge_then_merged`, run above; unit `tests/unit/test_publisher_shape.py` and `tests/unit/test_publication_body.py`, run above. C6b reuses that isolated publisher for WIP continuity: `::test_quota_checkpoint_is_pushed_before_the_reroute_is_scheduled` proves the remote head is confirmed before reroute, while the Docker e2e proves the resumed checkout uses that branch. Live evidence remains spike S10, C4's `e2e_github`, and the C5 live harness runs named in the prior report. |
| 20 | External review recorded only from allowlisted logins; feedback reaches Foundry, never a worker | proven | integration `tests/integration/test_github_delivery.py::test_a_non_allowlisted_login_satisfies_nothing`, `::test_a_review_with_findings_wakes_for_dispositions`, `::test_a_fix_disposition_holds_the_task_until_a_correction`, `tests/integration/test_amend_and_dispositions.py::test_a_disposition_needs_a_recorded_review_comment`; unit `tests/unit/test_external_review_cycles.py` (16 tests). Run: the integration and unit tiers above. Live: spike S12, result pass with a design correction, a pull request opened by the App reviewed automatically in 101 seconds by `chatgpt-codex-connector[bot]`; the spike also established that Issues read is not optional, because `GET /issues/{n}/reactions` is 403 without it. The e2e_github round test is opt-in behind `CRUCIBLE_GITHUB_WAIT_FOR_REVIEW` and was not run for this report. |
| 21 | A required CI failure escalates with evidence and triggers no retry or correction | **unproven** | See gap 7. The integration half is proven against the fake GitHub server: `tests/integration/test_github_delivery.py::test_a_required_failure_lands_in_ci_certification_failed_with_evidence`, `::test_ci_decision_rerun_records_the_intent_and_wakes_the_operator`, `::test_a_ci_decision_is_refused_outside_ci_certification_failed`, `::test_an_empty_required_set_is_pending_never_green`; unit `tests/unit/test_certification.py` (13 tests). Run above. There is no e2e half. |
| 22 | Docker authority arrangement recorded | proven | Documentary, and no test proves it. Spike S9, result pass, 2026-09-16, Ubuntu 24.04.5, kernel 6.8.0-139, cgroup v2, `docker-ce-rootless-extras 5:28.3.1`. The arrangement in use is the dedicated rootless daemon of the `crucible` service user (uid 999, socket `/run/user/999/docker.sock`), behind the socket proxy, per ADR 0004 and the operator's decision 13; `make preflight` refuses when the socket is absent and warns when the AppArmor profile the daemon needs after a reboot is missing. Every Docker tier run for this report used that daemon. Open items carried forward: the location decision that would let `make up` bring the whole Compose project up on the rootless daemon, and S9 follow-up 5, the reboot test of the lingering service user. |
| 23 | Harness versions pinned, digests recorded, unsupported combinations refused | **unproven** | See gap 8. The refusal half is proven: unit `tests/unit/test_harness_versions.py` (11 tests, including `::test_a_version_outside_the_range_is_refused` and `::test_an_image_with_no_version_label_is_refused`) and `tests/unit/test_docker_provider.py::test_the_version_range_is_checked_on_every_launch`, run above; integration `tests/integration/test_harness_registry.py::test_get_harnesses_reports_flags_ranges_and_a_sanitized_credential_state`. The digests are recorded: `images/manifest.env` pins `claude_code-2.1.273-8b75176f203e` `sha256:b7aa26be061de6c33be91f1d66c8033904bb7b5dbed6e8d2e4fd1998f9115d0d`, `codex-0.153.4-ce72fc1b2e20` `sha256:173f6031db3799331538c17eff7f04e4e802b71e6e9de0f496567feae15eaf56`, `agy-1.2.4-26838f92302b` `sha256:01cb5b793c7e042d1faa39316ffc321460f5ff832c4d4abdcb42aa277f65d91c`, `script-harness-1.0.0-34cf5b56ca5a` `sha256:9b5b91e74522bf6e65d159d27fec2fd0815cc915f6b760f11ecc974c20fc6b73`; spikes S7 (reproducible digests) and S11 (version against label). The live assertion 19 asks for does not exist. |

## The unproven rows and their exact gaps

1. **Row 5, live log tail.** `docs/spec/04-api.md` line 65 specifies
   `GET /attempts/{id}/logs` with `?stream=stdout|stderr&offset=` and
   `Accept: text/event-stream` for a live tail. No such route is registered:
   `crucible/adapters/api/routers/` exposes `/attempts/{id}`, `/artifacts`,
   `/evidence`, `/gates` and `/report` and nothing else, and no test in any tier
   tails a running worker. Missing: the endpoint, an integration test for offset
   paging, and an e2e test that tails during a run. Log capture and storage are not
   in question; only delivery to a reader is.

2. **Row 7, stall detection.** `docs/implementation-notes/c3.md` follow-up 3 states it
   plainly: heartbeat rows (10) are not written, there is no `heartbeats` table, and
   stall detection is unimplemented. The policy schema carries
   `limits.stall_warn_seconds` and `limits.stall_fail_seconds`, and nothing in
   `crucible/application/supervisor.py` reads either. No test in any tier mentions a
   stall. Secondarily,
   cancellation of a real Docker worker is not covered at the e2e tier; it is proven
   only against the fake provider at the integration tier. Missing: the `heartbeats`
   table, the stall detector, a test per tier, and an e2e cancel test.

3. **Row 11, partial report from a killed real worker.** The drain-then-kill sequence
   is proven on real containers. Capturing a partial report from a worker that was
   drained and then killed is proven only with the fake provider
   (`tests/integration/test_cancel.py::test_cancel_with_partial_report_is_not_parsed`).
   Missing: an e2e test that kills a real container mid-run and asserts the partial
   report is stored unparsed.

4. **Row 12, integration checkout lease refusal.** `checkout_lease_denied` appears
   nowhere under `tests/integration` or `tests/unit`. The refusal is proven only at
   the e2e tier. Missing: an integration test asserting that a second attempt on the
   same repository url and work branch stays `pending` with one `checkout_lease_denied`
   event, and launches once the holder releases.

5. **Row 14, CI evidence.** Three separate things are missing. No green CI run URL is
   recorded anywhere in the repository; the only two run identifiers present are the
   failed release runs `35158679884` (tag v0.1.0) and `35167333375` (tag v0.2.0) in
   `docs/implementation-notes/release.md`. `release.yml` runs no pytest tier at all,
   so "CI green on the release tag with all tiers except live" cannot be satisfied by
   the release workflow as written. And `e2e_github` and `e2e_admin` run in no
   workflow, by design, so "all tiers except live" needs a definition. Missing: a
   decision on whether the release workflow runs the tiers or whether this row cites
   the `ci` run on the tagged commit, and then a recorded green run URL for a release.

6. **Row 18, the real ledger.** Everything here is proven on synthetic bundles and on
   the bundle the real `foundry-ledger` producer wrote for its own invented fixture.
   The operator's real ledger at `~/.local/state/foundry/ledger.sqlite` has not been
   read, exported, imported or marked migrated, and this phase was bounded so that it
   would not be: the real handoff (15 steps 1, 5 and 6) is Foundry's act with the
   operator's explicit go, after this change merges. One finding can stop it:
   `foundry-ledger`'s lifecycle has the states `rejected` and `missing`, and 15's
   mapping table has no row for either, so a bundle carrying such a task is refused
   whole rather than guessed at (`docs/implementation-notes/c6.md`, finding 2). Whether
   the real ledger holds such a task is unknown here by design. Missing: the export,
   the submit, the commit, `mark-migrated`, and then this row updated with the import
   id.

7. **Row 21, the e2e CI failure.** The live target repository
   `sentania-labs/crucible-spike-target` has no CI of its own, so its policy sets
   `ci_certification.allow_no_ci: true` and the live certification is recorded as
   `skipped` (`docs/implementation-notes/c4.md`). Green, failure capture and the
   `ci-decision` paths are proven only against the fake server. Missing: a throwaway
   repository carrying a real required check that can be forced red, and an e2e test
   that lands the task in `ci_certification_failed` with the evidence attached.

8. **Row 23, the live version assertion.** 19 asks for `GET /harnesses` to be matched
   against image labels on a live run. The live tiers assert the manifest pin against
   the daemon's tag and the promotion state of `GET /v1/admin/images`; nothing compares
   the `GET /harnesses` response against the running images' `crucible.harness_version`
   labels. That comparison exists only as spike S11's table and as unit tests over
   synthetic labels. Also open: `docs/implementation-notes/c5.md` follow-up 3, a
   reproducibility check of the rebuilt Codex image on a second daemon. Missing: a live
   assertion that `GET /harnesses` reports the version the running image's label
   carries.

## How to re-run this evidence

```sh
make lint
make test                 # unit and integration; integration needs Docker or CRUCIBLE_TEST_DATABASE_URL
make scan                 # needs gitleaks on PATH
make e2e-image && make e2e DOCKER='<the rootless daemon wrapper in the Makefile header>'
make e2e-admin CRUCIBLE_LIVE_CREDENTIAL_ROOT=<the dedicated root> DOCKER='<same wrapper>'
make e2e-live  HARNESS=claude_code CRUCIBLE_LIVE_CREDENTIAL_ROOT=<...> CRUCIBLE_GITHUB_APP_JSON=<...> \
               CRUCIBLE_GITHUB_APP_KEY=<...> CRUCIBLE_GITHUB_TARGET_REPO=<owner/throwaway>
make e2e-github CRUCIBLE_GITHUB_APP_JSON=<...> CRUCIBLE_GITHUB_APP_KEY=<...> \
               CRUCIBLE_GITHUB_TARGET_REPO=<owner/throwaway>
```

The live tiers need the dedicated credential root readable by the invoking user. No
variable above is ever a key, a token or a secret; each names a file, a directory or a
repository.
