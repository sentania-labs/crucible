# 19. Crucible readiness gate

Crucible may be used against a real project only when every row below has
passing evidence, presented by Foundry and approved by the operator.

| Requirement | Proof |
|---|---|
| Accept and validate a versioned task contract | unit: contract validation suite; integration: submit valid and invalid |
| Persist task, worker, execution, event, artifact, evidence, and decision state | integration: full-run event sequence test; migration tests |
| Launch at least one supported harness in an isolated environment | live: `e2e-live HARNESS=codex` (or claude_code) completes a trivial task in the Docker provider |
| Inject identity and instructions without committing them | e2e: `no_injected_files` gate passes on the resulting branch; shim absent from diff |
| Capture or stream worker events and logs | e2e: log chunks stored, live tail delivers during run; kind: `test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle`, `test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc`, and `test_restart_adopts_the_job_and_resumes_logs`; [kind CI run](https://github.com/sentania-labs/crucible/actions/runs/35692723709) |
| Continue authorized work while Foundry is disconnected | e2e: client-exit test; wake waiting on poll |
| Detect completion, failure, timeout, cancellation, stall, and loss | integration: one test per class with the fake provider; e2e: timeout, loss, cancel on Docker; kind: `test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle`, `test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc`, and `test_deleted_pod_is_lost_and_sigterm_ignoring_pod_dies_at_grace`; [kind CI run](https://github.com/sentania-labs/crucible/actions/runs/35692723709) |
| Preserve worker reports and verification evidence | integration: claim parsed and stored; evidence rows verified-only for gates |
| Restart and reconcile active or interrupted executions | e2e: Crucible restart with running worker; integration: reconcile idempotence |
| Enforce the required deterministic gates | unit: each gate; integration: a run that fails `scope_contained` reaches `pre_pr_gates_failed` |
| Stop or terminate a worker safely | e2e: drain then kill; partial report captured; kind: `test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle` and `test_deleted_pod_is_lost_and_sigterm_ignoring_pod_dies_at_grace`; [kind CI run](https://github.com/sentania-labs/crucible/actions/runs/35692723709) |
| Prevent concurrent workers from corrupting the same working tree | integration: checkout lease refusal; e2e: two attempts, one repo; kind: `test_row_12_concurrent_attempts_use_distinct_claims`; [kind CI run](https://github.com/sentania-labs/crucible/actions/runs/35692723709) |
| Expose sufficient API state for Foundry to inspect and reconcile | integration: a scripted Foundry start-of-session (list tasks, wakes, events) reconstructs state from the API alone |
| Demonstrate through automated tests | CI green on the release tag with all tiers except live; live results attached |

Additional, from the operator's direction:

| Requirement | Proof |
|---|---|
| Each harness runs non-interactively in the worker container with subscription auth | spikes S1 to S3 recorded with transcripts |
| Workers cannot access the Docker socket, proxy, database, or other credentials | e2e isolation test; kind: `test_isolation_probes_are_refused_on_kubernetes`, `test_network_policy_denies_every_kubernetes_destination_from_the_worker`, `test_per_attempt_secret_is_removed_under_every_cleanup_policy`, and `test_probe_refuses_launches_without_default_deny` |
| Codex's disabled sandbox cannot escape the container | spike S4: container hardening verified with the sandbox disabled |
| Bootstrap ledger imported and authority handed off | integration: import tests; live: the real Foundry ledger imported, verified, committed |
| Workers hold no GitHub credential; Crucible pushes and opens the PR only after pre-PR gates and acceptance | e2e: `git push` from a worker fails; a script-harness task reaches `ready_for_merge` on the throwaway repository |
| External review recorded only from allowlisted logins; feedback reaches Foundry, never a worker | integration: non-allowlisted activity satisfies nothing; disposition required before ready |
| A required CI failure escalates with evidence and triggers no retry or correction | integration and e2e: forced failure lands in `ci_certification_failed` |
| Docker authority arrangement recorded | S9 result and the arrangement in use named in the report |
| Harness versions pinned, digests recorded, unsupported combinations refused | unit: refusal; live: `GET /harnesses` matches image labels; kind: `test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle` and `test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc` resolve the disposable registry tag to a digest and verify its harness label, and `test_row_23_a_harness_the_image_does_not_declare_is_refused` refuses one; [kind CI run](https://github.com/sentania-labs/crucible/actions/runs/35692723709) |

The readiness report is a document in Crucible's repo listing each row, the
test names, the CI run URL, and the live-run artifact IDs.
