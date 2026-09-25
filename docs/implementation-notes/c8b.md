# C8b: the Kubernetes end-to-end tier on kind

## Result

`make e2e-kind` creates a uniquely named, single-node kind cluster, installs
Calico, applies the restricted workers namespace, runs the Kubernetes provider
cases, dumps cluster evidence on failure, and deletes both the cluster and its
disposable registry on every exit path. Cleanup also verifies that the cluster,
registry, temporary image tag, kubeconfig, and scratch cache are absent; a
cleanup failure makes the tier fail. When the run created the shared `kind`
Docker network itself, cleanup removes it too; a network a concurrent kind
cluster still holds is reported and left in place, not counted as a failure,
because its name is shared by every kind cluster on the host. A second
interrupt during cleanup is ignored so cleanup always runs to the end, and a
failure to pull the busybox image that loosens bind-mount permissions does not
fail an otherwise clean run, since the scratch path's removal is checked on its
own.

The tier uses a real Kubernetes API server, kubelet, Calico data plane,
PersistentVolumeClaim, OCI registry, and the kubeconfig that kind writes with
inline certificate data. The script-harness image is both loaded with `kind
load docker-image` and pushed to the disposable registry. The provider resolves
the registry tag and runs the digest form through kind's local registry mirror.

Calico is pinned to v3.32.2. Its downloaded manifest must match SHA-256
`a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02`.
The registry image and the test peer image are pinned by OCI digest.

## Cluster contract

`deploy/kind/workers.yaml` applies:

- `crucible-workers` with Pod Security admission at `restricted`
- a namespace-wide default deny for ingress and egress
- separate supervisor and no-permission worker ServiceAccounts
- the supervisor Role and RoleBinding for the namespaced provider surface
- a ResourceQuota that supplies the provider's concurrency bound
- the `standard` storage class for attempt claims
- a test-only host-backed claim for the reference cache
- a responding service in another namespace for the cross-namespace denial
- a host-network responder on each denied private and link-local address
- a test-only listener on the cluster DNS service's non-DNS port

The kind kubelet has `podPidsLimit: 512`. No user kubeconfig and no real
cluster is read.

## Cases and readiness rows

| Kind case | What it proves | Readiness row |
|---|---|---|
| `test_row_5_7_11_full_lifecycle_on_a_real_pod_and_pvc` | prepare, launch, observe, logs, collection, independent verification, bundle, digest resolution, cleanup | 5, 7, 23 |
| `test_rows_5_7_11_23_supervisor_restart_and_full_gate_lifecycle` | the real API and Supervisor drive full gates, restart, plain worker failure, timeout, cancellation with the partial report collected, stall, detached completion, hardening verification IDs, and orphan cleanup through Kubernetes | 5, 7, 11, 23 |
| `test_restart_adopts_the_job_and_resumes_logs` | a new provider adopts the live Job and resumes strictly after the stored log offset | 5 |
| `test_deleted_pod_is_lost_and_sigterm_ignoring_pod_dies_at_grace` | an out-of-band deletion and an eviction through the Eviction API (the `kubectl drain` path) are both `lost`; kubelet kills a worker that ignores SIGTERM after the configured grace | 7, 11 |
| `test_row_12_concurrent_attempts_use_distinct_claims` | concurrent attempts on one repository have distinct PVCs and working trees | 12 |
| `test_network_policy_denies_every_kubernetes_destination_from_the_worker` | each API server, DNS off port 53, other namespace, link-local, RFC 1918, and carrier-grade NAT endpoint is first reached without a selecting policy, then denied inside the policy-selected worker | 16 |
| `test_per_attempt_secret_is_removed_under_every_cleanup_policy` | the attempt Secret is gone for `keep`, `delete`, and `keep_diff_only` | 16 |
| `test_probe_refuses_launches_without_default_deny` | removing the enforcing default deny makes the real canary refuse readiness | 16 |
| `test_row_23_a_harness_the_image_does_not_declare_is_refused` | an attempt asking the tier's image for a harness its label does not declare is refused before any claim exists | 23 |
| `test_isolation_probes_are_refused_on_kubernetes` | the Docker tier's isolation probes (`test_isolation.py`) all read `refused` from a real Pod driven by the API and Supervisor | 16 |
| `test_scripted_quota_reroutes_on_kubernetes` | the Docker tier's class-routing reroute (`test_class_routing.py`): a real Pod exhausts its quota and the attempt reroutes to a successor that resumes from the remote branch; checkpoint continuity stays Docker-only, because only the Docker provider pushes a checkpoint to a local file origin | Docker parity (72) |

## Provider defects exposed by the real cluster

1. `pods/exec` carried the normal API client's `Accept: application/json`
   into its WebSocket upgrade. The real API server returned 406, so no reader
   Pod could return prepared or collected bytes. The upgrade now requests
   `*/*`, with a unit regression test.
2. The readiness probe requested timestamped pod logs while `_read_probe`
   consumes `crucible-canary.*` keys at column one. Every real canary was
   therefore reported as producing no result. The probe now requests raw,
   un-timestamped lines, with a unit regression test. Worker log pulls remain
   timestamped because resume depends on them.
3. Concurrent launches could both observe an empty readiness cache and start
   separate 4 GiB canaries. Under the test namespace's 8 GiB quota, the second
   canary could prevent a worker from launching. Readiness now serializes the
   probe and checks the cache again inside the lock, with a unit regression
   test.
4. The kind script left its temporary registry tag attached to the shared
   worker image. A later Docker e2e run then selected that repository digest
   instead of the bare local image ID. The cleanup trap now removes the tag;
   the kind-then-Docker sequence verifies that no digest, cluster, or registry
   state is left behind.
5. A restarted provider adopted a live worker Job with an empty image field.
   Collection then submitted Jobs with no image. Reconciliation now recovers
   the exact digest reference and termination grace from the real Pod, with a
   unit regression test.
6. Reader and role Pod deletion is asynchronous on a real API server. The
   provider could sample workspace cleanliness while a deleted reader still
   reported `Running`, failing a clean workspace. It now waits for confirmed
   Pod removal, with delayed-deletion unit regressions.

## Documentation decisions carried from C8a

- The reference cache is mounted read-write into the preparer. The preparer
  refreshes and may replace its bare mirror, so read-only did not describe the
  implemented contract.
- Credential sync-back runs whenever an attempt reaches collection, not only
  after exit zero. This matches Docker and preserves a valid newer token even
  when the task fails.

## Carried-forward items still open

- `drain` still records 143 when a worker exits cleanly inside the grace
  period. Recovering the real code needs a watch rather than the current poll.
- The PID limit is read from the canary's node. This tier has one node and
  cannot prove a heterogeneous multi-node cluster.
- `pod_log` still has no `limitBytes`; a long-running worker can make a poll
  read its complete log.
- No current harness combines a read-only credential with templates, so that
  latent projected-volume interaction remains unobserved.

## CI placement and required-check decision

The job runs on `ubuntu-24.04`, the same GitHub-hosted runner class as the
Docker tier. It needs a local Docker daemon and no lab access. Its timeout is
30 minutes.

The job is not proposed as a sixth required check in this PR. The five current
required checks stay unchanged while this new tier establishes runtime and
flake history. It still runs on every pull request and blocks the workflow's
green result when it fails.

## Runs

```text
$ make e2e-kind
10 passed, 2 warnings in 384.15s (0:06:24)
```

The initial pull request workflow passed all six jobs, including `e2e-kind`:
[CI run 35692723709](https://github.com/sentania-labs/crucible/actions/runs/35692723709).

## Limitations and risks

- The tier is single-node, so it proves ReadWriteOnce behavior on kind but not
  cross-node attachment behavior.
- The disposable registry is plain HTTP on host loopback and the private kind
  network. It exists only for this tier and is removed by the cleanup trap.
- The denied-range reachability controls add test-only loopback addresses to
  the throwaway kind node. They never touch a host or lab interface.
- Calico and its images are fetched during cluster creation. The manifest hash
  prevents changed content from running under the pinned version.
