# 19. Crucible readiness gate

Crucible may be used against a real project only when every row below has
passing evidence, presented by Foundry and approved by the operator.

| Requirement | Proof |
|---|---|
| Accept and validate a versioned task contract | unit: contract validation suite; integration: submit valid and invalid |
| Persist task, worker, execution, event, artifact, evidence, and decision state | integration: full-run event sequence test; migration tests |
| Launch at least one supported harness in an isolated environment | live: `e2e-live HARNESS=codex` (or claude_code) completes a trivial task in the Docker provider |
| Inject identity and instructions without committing them | e2e: `no_injected_files` gate passes on the resulting branch; shim absent from diff |
| Capture or stream worker events and logs | e2e: log chunks stored, live tail delivers during run |
| Continue authorized work while Foundry is disconnected | e2e: client-exit test; wake waiting on poll |
| Detect completion, failure, timeout, cancellation, stall, and loss | integration: one test per class with the fake provider; e2e: timeout, loss, cancel on Docker |
| Preserve worker reports and verification evidence | integration: claim parsed and stored; evidence rows verified-only for gates |
| Restart and reconcile active or interrupted executions | e2e: Crucible restart with running worker; integration: reconcile idempotence |
| Enforce the required deterministic gates | unit: each gate; integration: a run that fails `scope_contained` reaches `gates_failed` |
| Stop or terminate a worker safely | e2e: drain then kill; partial report captured |
| Prevent concurrent workers from corrupting the same working tree | integration: checkout lease refusal; e2e: two attempts, one repo |
| Expose sufficient API state for Foundry to inspect and reconcile | integration: a scripted Foundry start-of-session (list tasks, wakes, events) reconstructs state from the API alone |
| Demonstrate through automated tests | CI green on the release tag with all tiers except live; live results attached |

Additional, from the operator's direction:

| Requirement | Proof |
|---|---|
| Each harness runs non-interactively in the worker container with subscription auth | spikes S1 to S3 recorded with transcripts |
| Workers cannot access the Docker socket, proxy, database, or other credentials | e2e isolation test |
| Codex's disabled sandbox cannot escape the container | spike S4: container hardening verified with the sandbox disabled |
| Bootstrap ledger imported and authority handed off | integration: import tests; live: the real Foundry ledger imported, verified, committed |

The readiness report is a document in Crucible's repo listing each row, the
test names, the CI run URL, and the live-run artifact IDs.
