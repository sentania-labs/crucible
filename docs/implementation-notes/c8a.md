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
ruff format --check / ruff check / mypy / lint-imports: all clean
$ make test
tests/unit: 740 passed
tests/integration: 233 passed
$ make scan
gitleaks: no leaks found (tree and history)
$ make e2e     # the Docker tier, unchanged, on the rootless daemon
```

The exact output is in the task report.

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
