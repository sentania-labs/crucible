# C8a: the Kubernetes execution provider

## Result

`crucible/adapters/execution/kubernetes.py` is a second `ExecutionProvider`
(08) that realises spec 26 on Jobs and Pods. Everything above the provider is
unchanged: the same identity bundle, the same preparer, collector, bundle
verifier and verifier scripts, the same reading of what they wrote, the same
credential rules, the same gates and evidence. What differs is how a container
is created and how bytes come back out of a workspace.

The provider is proven against an in-memory Kubernetes API
(`k8sfake.py`). No cluster, no kind, no kubectl was used; the cluster half is
C8b's `make e2e-kind` tier.

Five new modules, all inside `crucible/adapters/execution/`:

| Module | What it is |
|---|---|
| `k8sapi.py` | A narrow API client for one namespace: eight resource kinds, `pods/log`, `pods/exec`, no cluster-scoped call and no way to name a second namespace |
| `k8sregistry.py` | A read-only OCI distribution client, because a cluster holds no image on Crucible's side and the version refusal (07) has to happen before a kubelet pulls |
| `k8sspec.py` | Every Kubernetes object an attempt becomes, as pure functions |
| `kubernetes.py` | The provider |
| `k8sfake.py` | The in-memory API and registry the unit and integration tiers run against |

Two sections of the Docker provider moved out rather than being copied:
`logstream.py` (the strict-after log resume of 10) and `collected.py` (reading
what the collector wrote). Both are the Docker provider's own code, unchanged,
with the names made public; `docker.py` imports them and its tests pass
untouched.

## Decisions where 26 was silent or could not be met literally

Each of these is the option closest to the Docker provider's behaviour, and
each is now written into 26 as well.

**No Kubernetes client dependency.** The contract allowed a pinned one. The
Docker provider hand-rolls a narrow client on purpose: it has no `exec`, no
`build` and no `system` call, so the discipline is in what the client cannot
say, not only in what the proxy refuses. `k8sapi.py` is the same shape: it
knows the eight kinds 26 names and nothing cluster-scoped. `pyproject.toml`
and `uv.lock` are untouched, so no Makefile or CI change was forced.

**Mount paths.** 26's draft named one `/crucible/workspace` mount. The identity
bundle every worker reads tells it the checkout is at `/crucible/repo` and the
report directory at `/crucible/report` (06, and the constants in
`ports/execution.py`). The provider mounts the claim's `repo` and `report`
subpaths at those paths; only the preparer gets the whole claim, at
`/crucible/work`, exactly as on Docker. 26 is corrected.

**`rw-narrow` cannot be the Secret.** A Kubernetes Secret volume is read-only
however it is mounted, and the three subscription harnesses refresh their own
token in place (S1). A read-only harness mounts `cred-<attempt>` directly. A
`rw-narrow` harness gets an init container that copies the declared auth files
off the Secret's read-only projection into the `credential` leaf of the
attempt's own claim, mode 0700 on the directory and 0600 on each file. That is
the same shape, the same properties and the same place the Docker provider
puts it (12), and it is what makes the sync-back possible at all.

A Secret key cannot hold a path separator, so an auth file in a subdirectory
(AGY's `antigravity-cli/antigravity-oauth-token`) is keyed with the separator
replaced by an underscore and the volume's `items` project it back to its
declared path. **Lab-admin's harness Secrets must use the flattened key.**

**Bytes leave a Pod on an exec stream, never through a log.** 26 says outputs
are read through a short-lived reader Pod. The only channels out of a Pod are
its log and a stream; the kubelet writes logs to the node's disk, so a rotated
auth file through a log would be a credential on a node (12). The reader Pod
hands the output directory back as `tar cf -` over `pods/exec`, the transport
`kubectl cp` uses, and hands a single auth file back the same way, bounded and
checked for being a regular file before it is read.

**One NetworkPolicy per attempt per role that needs egress.** A NetworkPolicy
has one `podSelector`. A single object per attempt would carry the union of
every role's destinations, which would give the worker GitHub and the collector
the model endpoints. The selector therefore carries the role too, and a role
with no egress gets no object: the namespace's default deny is already its
answer.

**The allowlist is resolved to addresses.** A `networking.k8s.io/v1` CNI has no
FQDN rule, so the names are resolved when the policy is written and recorded in
a `crucible.io/egress-hosts` annotation. A name that does not resolve refuses
the launch rather than being dropped or widened, which is 13's rule for an
attempt the egress path cannot actually permit. `broad_egress` is the opt-out
for a deployment whose CNI enforces names some other way; it is off by default,
because the broad form ("the internet on 443 minus every denied range") would
let a worker reach GitHub. Every denial 26 names is the `except` of every
allow, so a name that resolves into a denied range cannot open one. IPv6 never
appears in a rule and is denied entirely.

**Pending past the launch timeout is exit 70, not an exception.** 26 calls it a
launch failure. Raising from `observe` would make one unschedulable Pod an
exception on every supervisor tick for as long as it stayed unschedulable. Exit
70 is `environment` in 16: the attempt retries if the policy allows it, the
Pod's conditions are the detail, and the tick keeps moving.

**A Pod Crucible deleted is an exit, never a loss.** Deleting a Pod is the only
signal Kubernetes offers, and the exit code goes with the Pod object. The
provider remembers what it did, so a drained Pod reads back as exit 143 and a
killed one as 137, the codes the Docker provider records for the same two acts.
A Pod that is gone with nothing Crucible did, or one the cluster evicted, is
`lost`.

**Requests equal limits.** 26 names both and says nothing about the ratio.
Equal values give Guaranteed QoS, so a worker promised the policy's memory is
not the first thing evicted under node pressure, which would otherwise show up
as a `lost` attempt nobody caused (16).

**26's observability fields go in an artifact.** The Job and Pod names, the
node, the effective limits, the pod PID limit and the NetworkPolicy applied
have no field on the provider port, and the port, the evidence kinds and the
migrations were out of scope. They are stored as one
`report/kubernetes-launch.json` artifact of the attempt, which carries an
`artifact_present` evidence row like any other per-attempt fact Crucible
observed (11). The image digest stays on the attempt row as it does on Docker.

**The recorded digest is `repo@sha256:...`.** The first attempt kept the tag
beside the digest, which is a legal reference and more auditable. The
`attempts.image_digest` column is `varchar(128)` and a GHCR reference with a
harness tag overflows it. Migrations were out of scope, so the recorded form is
the Docker provider's.

## What is not implemented, and why

- **`probe_credential`** raises with its reason. The bounded auth probe (25)
  runs the harness's own CLI interactively; on Kubernetes that is 26's login
  Job, which is not part of C8a's requirements. The NetworkPolicy for the login
  role is implemented and tested.
- **The publisher Job.** `publish-<attempt>` is in 26's table; the publisher is
  a separate port (`crucible/ports/publish.py`) and the Docker publisher is
  wired only to the Docker provider. Its NetworkPolicy is implemented and
  tested.
- **`run_login_container` and `push_quota_checkpoint`** are duck-typed
  extensions the supervisor and the admin surface probe for with `getattr`. The
  Kubernetes provider does not carry them, so the login flow and the local
  quota checkpoint stay Docker-only, as they are today.
- **The projected-volume identity bundle.** 08 and 26 name an object-store form
  above the ConfigMap size cap. A bundle over 1 MiB refuses the launch rather
  than being truncated: a truncated bundle is a worker given the wrong contract.

## Runs

```text
$ make lint
ruff format --check, ruff check, mypy, lint-imports: 3 contracts kept, 0 broken

$ make test
tests/unit          760 passed in 9.86s
tests/integration   349 passed in 328.65s (0:05:28)

$ make scan
gitleaks: no leaks found, tree (4.05 MB) and history (8 commits)

$ make e2e DOCKER='<the rootless daemon wrapper>'
17 passed, 12 deselected in 89.76s (0:01:29)
```

The Docker tier ran on the rootless daemon of the `crucible` service user
(13, ADR 0004, S9) and is unchanged by this branch. One earlier run of it
failed `test_a_verifier_that_never_finishes_fails_the_gate` while the
integration tier was running on the same machine: that test gives the verifier
container a 5 second deadline, and under the contention the container exited on
its own before the deadline could fire. Run alone, the tier is green.

## The review round, and what it changed

A non-author adversarial pass ran against the diff and the stated scope before the
PR opened (the `sdlc` skill's step 4). It returned five blockers, all real, all fixed
here:

1. **The NetworkPolicy's denials were never emitted.** The `except` check asked whether
   a denied range sat *inside* an allowed one. An allowed destination is a `/32`, so
   that is always false and the `except` key never appeared. The check now runs the
   other way and a resolved address that falls inside a denied range refuses the launch,
   naming the host and the address. Without this, an allowlisted name whose record
   pointed at the API server's ClusterIP, at cloud metadata, or into the lab's own
   ranges became an allow rule for exactly that address.
2. **The readiness canary could only report "unreachable".** It tested the API server
   with bash's `/dev/tcp` redirect, which is not a feature of `sh`: under dash or
   busybox the redirect fails to open and the else branch fires. The one gate that is
   supposed to be un-fakeable passed on a namespace with no egress enforcement at all.
   Confirmed on this workstation: `dash` reports "unreachable" for a host that answers.
   The test is now curl, whose exit code says which happened, and anything that is not a
   definite refusal to connect is `inconclusive`, which does not pass the probe and
   therefore refuses every launch.
3. **A worker reached GitHub under the shipped default policy.** 26 is unconditional,
   and the seeded `default-software` policy names `github.com` in its `egress_allowlist`
   for the roles that do need it. The worker and verifier roles now subtract the git
   remote rather than trusting the list, and the annotation records what was granted.
4. **A transient API error reported a running worker as `lost`.** `_pod_of` swallowed
   every `KubernetesApiError`, so one 503 during a rolling restart read as "the Pod is
   gone", which is terminal under 16: the attempt would be failed and retried while the
   original Pod kept running. Nothing is decided from a look that failed; `observe`
   raises and the supervisor asks again next tick.
5. **The per-attempt Secret survived every `prepare` failure.** The Secret is seeded
   before the preparer Job, and the supervisor calls neither `discard` nor `cleanup` for
   an attempt whose `prepare` raised, so a live harness credential sat in the namespace
   until a retention sweep happened to notice. `prepare` now removes it on every path
   out.

Non-blockers fixed in the same pass: retention never removed workspace claims (a failed
prepare left a 20Gi PVC forever); a completed Job whose Pod was garbage collected read
as `lost` after a restart; an adopted attempt could never time out of `Pending` because
the launch clock was in-memory only; a cleanup that could not remove a retained
credential copy reported success silently; a truncated or failed read of the collected
output produced a quietly short report instead of an environment failure; and
`kubernetes.enabled` outside a cluster stopped the service from starting.

Non-blockers carried forward rather than fixed, with the reasoning:

- **`drain` records 143 for a worker that exited cleanly inside the grace window.**
  Deleting a Pod is the only signal Kubernetes offers and the exit code goes with the
  Pod object. Recovering the real code needs a watch rather than a poll, which is a
  design change, not a fix.
- **The pod PID limit is read from the canary's node.** That is 26's design (it is a
  kubelet setting, not a pod field); on a multi-node cluster the canary can land
  somewhere other than the workers. The kind tier is where this becomes measurable.
- **`pod_log` has no `limitBytes`.** Every poll reads the whole pod log into memory.
  Worth a bound before a long-running worker on a real cluster.
- **A read-only harness with declared templates.** Mounting a template inside a Secret
  volume mount may fail at container creation, because a Secret volume is a read-only
  tmpfs. All three subscription harnesses are `rw-narrow` today, so the path is latent;
  the kind tier should cover it before any harness declares read-only with templates.

## The repository's automatic reviewer

The external round on the PR returned six findings, five P1 and one P2. All six were
real and all six are fixed:

1. **An absent optional auth file broke the Pod.** `_seed_credential` omits an optional
   file the harness Secret does not carry, but the volume projected every declared file
   with `optional: false`, and a Secret projection naming a key that is not there is a
   Pod the kubelet refuses to start. Claude Code declares `.claude.json` optional, so a
   harness Secret with only `oauth-token` is an ordinary deployment. The projection is
   now built from the keys the per-attempt Secret actually holds, read back rather than
   assumed so a restart between `prepare` and `launch` still projects the truth.
2. **A Pod whose init container failed reported `running` forever.** Kubernetes reports
   the Pod `Failed` with only the init status terminated, `_terminated_state` found
   nothing, and the fallback said running. The credential-seed init container makes this
   reachable, and the attempt would have hung with nothing to classify, collect or clean
   up. A `Failed` phase is now terminal, with the init container's own exit in the
   detail.
3. **An exec stream that ended early was accepted as success.** `exit_code` is None when
   the API server never sent the error channel. For the output tar that let a partial
   archive through `_extract`, which suppresses tar errors, so a report or a diff
   quietly missing files would have produced a wrong gate result; for a credential read
   it would have been recorded as "absent after the run". Both now require an explicit
   zero, and the credential case records "read failed", which is not the same fact.
4. **A cleaner Job that failed was reported as a successful removal.** `_remove_from_claim`
   discarded the Job's exit, so a retained claim could keep its credential leaf while
   `cleanup` said it had gone. It raises now, and the caller records `removed: False`.
5. **Embedded kubeconfig certificates were not read.** kind writes
   `certificate-authority-data` and friends inline; the parser read only the path forms,
   so the developer and kind path could not connect at all. The inline material is
   decoded to a private temporary file, which is what `ssl` needs. C8b depends on this.
6. **An adopted Pending Job never timed out.** 26's Pending timeout is measured from a
   launch time held in memory, so after a restart an adopted Pod that would never
   schedule was reported running until the much longer Job deadline. The clock now comes
   from the Job's own `creationTimestamp`, so a Pod already past the window is caught on
   the first observation rather than being given it again.

Each has a test. Findings 2 and 6 are worth noting together: both were cases where the
provider's answer to "what is this worker doing" was "running" when the honest answer
was "it is never going to run", which is the failure mode 16 is least able to recover
from on its own.

## What C8b needs from here

- `k8sfake.FakeKubernetesApi` and `FakeRegistry` stay the unit and integration
  doubles. The kind tier replaces them with a real client and a real registry;
  nothing else about the tests should have to change.
- `KubernetesProvider` takes its client, its registry and its resolver as
  constructor arguments for exactly that reason.
- `NamespaceProbe` is the object the kind tier asserts on when it runs once
  with the enforcing CNI removed.
- The canary's contract is three lines on its own log
  (`crucible-canary.api=`, `crucible-canary.pids=`, `crucible-canary.done=1`);
  `_read_probe` is the parser.
- The denials the kind tier proves from inside a worker Pod are the `except`
  list in `k8sspec.DEFAULT_DENIED_CIDRS` plus the cluster DNS rule.
- The RWO workspace claim is mounted by one Pod at a time by construction (the
  roles run one after another), but the reader Pod overlaps a finished worker
  Pod that has not been cleaned up yet. On one node that is fine; whether a
  multi-node kind cluster schedules the reader elsewhere is the first thing the
  kind tier should look at.
- Nothing here creates a namespace, a ResourceQuota, a default-deny policy or a
  ServiceAccount. Those are 26's lab-admin checklist and C9's manifests.
