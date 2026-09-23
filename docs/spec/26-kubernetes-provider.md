# 26. Kubernetes execution provider

Status: draft for review, 2026-09-21. Decided by the operator the same day:
"let's move forward with the weaker container only sandboxing on k8s ...
and we will target the microvm once it's working." So this version of the
provider runs worker pods on the node's standard container runtime with the
full pod security context, and a stronger runtime class (gVisor or Kata) is a
later, separate step that changes one field of the pod spec and nothing
else.

Everything above the provider is unchanged: task, worker, execution, event,
artifact, gate, evidence, review, publication, administration, and the API
(03, 04, 09, 10, 11, 16, 23, 25). Spec 08 defines the provider interface
every provider implements; this document is the Kubernetes provider's
mechanics, in the same order as 08's Docker section, plus what the cluster
must guarantee before a worker runs there.

## Why a second provider, and why now

The operator cannot use Crucible for real work until workers are sandboxed
on the lab Kubernetes cluster; the Docker provider (08, 13) is for
development, testing, and the readiness evidence gathered so far. The
bootstrap contract names deployment on that cluster through Argo as a
design requirement. The readiness rows that prove isolation, termination,
log capture, and failure detection (19) are re-proven on this provider
before the gate counts for real-project use.

## Topology (from 01, made concrete)

One namespace for Crucible itself (`crucible`), one for workers
(`crucible-workers`). In `crucible`: the `api` Deployment, the `supervisor`
Deployment (one replica, lease-guarded exactly as today, fenced tokens in
PostgreSQL), and PostgreSQL (operator-managed or external; the manifests
carry a single-instance StatefulSet for the lab and a connection-string
option for an external server). No Docker socket anywhere. The GitHub App
key and webhook secret are a Secret mounted on the `crucible` pods only
(12). Everything a worker needs lives in `crucible-workers` and is created
per attempt by the supervisor through the Kubernetes API.

The supervisor's ServiceAccount is bound to a Role in `crucible-workers`
that permits create, get, list, watch, and delete on Jobs, Pods,
ConfigMaps, Secrets, and PersistentVolumeClaims, plus `pods/log` and
`pods/exec` for log tail and the login flow, and nothing in any other
namespace. It has no cluster-scoped permissions.

The exact verb set, which is what `deploy/kubernetes/base/workers/role.yaml`
carries and what a unit test holds it to (C9):

| Resource | Verbs |
|---|---|
| `pods`, `configmaps` | create, get, list, watch, delete |
| `secrets`, `persistentvolumeclaims` | create, get, list, watch, **patch**, delete |
| `pods/log` | get |
| `pods/exec` | create, get |
| `resourcequotas` | get, list |
| `batch/jobs` | create, get, list, watch, delete |
| `networking.k8s.io/networkpolicies` | create, get, list, watch, delete |

The three additions to the sentence above are the ones the implemented
provider needs: `patch` on PersistentVolumeClaims writes the retention label
`cleanup` leaves behind, `patch` on Secrets is the `rw-narrow` credential
sync-back (12), and reading the ResourceQuota is where `max_concurrency` on
`GET /providers` comes from. `pods/exec` needs `create` as well as `get`
because the API server authorizes an exec against `create` even when the
client opens it as a GET WebSocket upgrade, which is how the reader Pod's tar
stream is opened; a Role with only `get` is refused at the upgrade with a 403
(found on a real cluster in C9).

The account itself lives in the `crucible` namespace, not in
`crucible-workers`: a Pod can only use a ServiceAccount from its own
namespace, and the supervisor Pod runs beside the api. A RoleBinding may name
a subject in another namespace, so the permission stays namespaced to
`crucible-workers` exactly as above. The api Deployment uses the same account,
because the admin status page (25) reports provider health and the Kubernetes
provider answers that by running the namespace readiness canary: the api
process makes real API calls to `crucible-workers` on every status read.
(Made concrete 2026-09-22 during C9.) Admission (the cluster's
Pod Security admission at the `restricted` level on `crucible-workers`)
enforces the pod shape below independently of Crucible's own code, which
is the surviving half of 13's create-request policy.

## The attempt as Kubernetes objects

Per attempt the provider creates, in `crucible-workers`, all labelled
`crucible.attempt`, `crucible.task`, `crucible.owner`, `crucible.role`:

| Object | Role | Lifetime |
|---|---|---|
| PersistentVolumeClaim `ws-<attempt>` | the workspace: `repo/`, `report/`, `output/` | attempt, then per cleanup policy |
| ConfigMap `identity-<attempt>` (or a projected volume from an object store above the ConfigMap size cap, 08) | the identity bundle, read-only | attempt |
| Secret `cred-<attempt>` | the per-attempt copy of one harness credential directory, seeded from the harness's dedicated Secret in `crucible-workers`, `rw-narrow` where the adapter declares it (12) | attempt, deleted under every cleanup policy |
| Job `prepare-<attempt>` | the preparer: clone into the PVC from the reference cache, branch, shims, author identity, `origin` placeholder (08) | until complete, then deleted |
| Job `worker-<attempt>` | the worker, one Pod, `backoffLimit: 0`, `restartPolicy: Never` | until terminal, then deleted after `logs_drained` |
| Job `collect-<attempt>` | the collector, no network, repo and report read-only, output read-write (08) | until complete |
| Job `verify-bundle-<attempt>` | `git bundle verify`, no network | until complete |
| Job `verifier-<attempt>` | re-runs `required_verification` on an independent clone from the bundle (10, 11) | until complete |
| Job `publish-<attempt>` | pushes the sealed bundle with a token on an in-memory volume (23) | until complete |
| Job `login-<harness>-<n>` (admin flow, 25) | the harness's own login in its worker image, credential Secret writable, no workspace | until finished, cancelled, or timed out |

A Job per role keeps the same separation the Docker provider has (worker,
collector, verifier, publisher are distinct processes with distinct
mounts), and lets Kubernetes own restarts, deadlines, and garbage
collection. `activeDeadlineSeconds` on each Job is the policy's timeout for
that role; Crucible still drains before the deadline and classifies the
exit itself (16).

## Pod shape (every role)

Mirrors 13's Docker flags, enforced twice: by the provider's spec and by
Pod Security admission on the namespace.

```yaml
securityContext:            # pod
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  seccompProfile: {type: RuntimeDefault}
containers:
- securityContext:          # container
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities: {drop: ["ALL"]}
  resources:                # from policy limits
    limits: {cpu: ..., memory: ..., ephemeral-storage: ...}
    requests: {cpu: ..., memory: ...}
  volumeMounts:
  - {name: tmp, mountPath: /tmp}           # emptyDir, medium Memory, sizeLimit
  - {name: home, mountPath: /home/worker}  # emptyDir, medium Memory, sizeLimit
  - {name: ws, mountPath: /crucible/repo, subPath: repo}    # PVC, rw for the worker
  - {name: ws, mountPath: /crucible/report, subPath: report}
  - {name: identity, mountPath: /crucible/identity, readOnly: true}
  - {name: cred, mountPath: <adapter mount target>, readOnly: <not rw-narrow>}
automountServiceAccountToken: false
serviceAccountName: crucible-worker      # a no-permission account
enableServiceLinks: false
hostNetwork: false
hostPID: false
hostIPC: false
terminationGracePeriodSeconds: <policy grace>
```

The mount paths are the ones `crucible/ports/execution.py` defines and the
identity bundle names (06): a worker is told its checkout is at
`/crucible/repo` and its report directory at `/crucible/report`, so those are
where they are mounted. Only the preparer gets the whole claim, at
`/crucible/work`, the way the Docker preparer gets the whole workspace (08).
(Corrected 2026-09-21 during C8a; the draft named one `/crucible/workspace`
mount, which would have contradicted the bundle every worker reads.)

A Secret volume is read-only in Kubernetes whatever the mount asks for, so the
`readOnly: <not rw-narrow>` line above is the read-only case only. The
`rw-narrow` case is below, under credentials.

`--init` has no Kubernetes equivalent; the worker image's entrypoint
already reaps and forwards signals (S5), and `terminationGracePeriodSeconds`
plus a SIGTERM from Crucible's `drain` gives the harness the same window.
`--pids-limit` becomes the node's pod PID limit (a kubelet setting lab-admin
sets; the provider records the effective limit in the launch evidence and
refuses to launch if none is configured). A runtime class is not set in this
version; the field is reserved and documented as the microVM step.

## Networking: NetworkPolicy replaces the egress proxy

There is no Squid on the cluster. The workers namespace carries a default
deny for ingress and egress, and the provider creates one NetworkPolicy per
attempt **per role that needs egress**, selecting that attempt's pods of that
role by label. A NetworkPolicy has one `podSelector`, so a single object per
attempt would have to carry the union of every role's destinations, which
would hand the worker GitHub and the collector the model endpoints; a role
with no egress gets no object at all, because the namespace's default deny is
already the answer for it. (Clarified 2026-09-21 during C8a.) The allowed destinations come from the same policy document that
generates the proxy allowlist locally (05b routing pools, the adapter's
declared endpoints, 13), resolved to CIDRs or FQDN rules where the CNI
supports them:

- worker: the model provider endpoints of the routed harness, the package
  registries the project policy names, and, for a local route, the configured
  `endpoint_url` hostname and port over HTTP or HTTPS. GitHub is not
  reachable from a worker; the preparer and the publisher do the git
  traffic.
- preparer and publisher: `github.com` and `api.github.com` only.
- collector, bundle verifier, verifier: no egress at all (the verifier
  gets the registries only when `required_verification` needs them and the
  policy says so).
- login Job: the harness's login endpoints only.

A `networking.k8s.io/v1` policy has no deny verb and no FQDN rule, so the
allowlist's names are resolved to addresses when the policy is written and the
names themselves are recorded in the object's `crucible.io/egress-hosts`
annotation. A name that does not resolve refuses the launch rather than being
dropped or widened. For a configured local endpoint, the hostname is the trust
anchor: the provider resolves it and permits only the resulting addresses on the
configured port. A private result must also be inside the operator's explicit
`kubernetes.local_endpoint_cidrs` declaration. A URL that directly names a denied
address, or a name resolving to the Kubernetes API ClusterIP, another namespace, or
any denied range outside that declaration, is refused. General allowlist hostnames that
resolve into a denied range remain refused. IPv6 never
appears in a rule and is therefore denied entirely.

Two destinations are denied explicitly, because a naive policy lets them
through: cluster DNS is allowed on port 53 UDP and TCP to the cluster's DNS
service and nothing else on that address, and the Kubernetes API service,
the node network, the pod network of other namespaces, link-local
`169.254.0.0/16`, and the lab's private ranges are denied. The e2e tier
proves each denial from inside a worker pod (18).

**Selectors for a CNI that translates service addresses first.** Some CNIs
translate a service or LoadBalancer address to its backend pod addresses before
they evaluate policy; Cilium with kube-proxy replacement is the one the lab runs.
There an `ipBlock` on the kube-dns ClusterIP or on a gateway's service address
never matches, and a worker has no DNS and no model (crucible#91). So the policy
also names those destinations by where they actually are, with a standard
`networking.k8s.io/v1` peer of one `namespaceSelector` (on
`kubernetes.io/metadata.name`) and one non-empty `podSelector`:

- cluster DNS: the resolver's pods, `kube-system` and `k8s-app: kube-dns` by
  default, in the same rule as the address and therefore on port 53 UDP and
  TCP and nothing else. An empty DNS namespace leaves the address rule alone.
- an in-cluster local endpoint: when a namespace is set for it, the worker's
  local route is allowed as the pods that take its connections on their own
  port (the Service's `targetPort`, or the URL's port when that is 0), and the
  URL's host is not resolved into an address rule at all. With no namespace set
  the endpoint is outside the cluster and keeps the resolved-address rule above.
  The pods to name are the ones the URL's connection lands on: the gateway's own
  when the URL is its Service, the ingress controller's when it goes through an
  ingress.

A selector never widens a denial: it adds no address, it carries only port 53 or
the endpoint's one port, it may not be empty, and it may not name the workers
namespace or Crucible's own (a worker reaching another attempt or Crucible's
database). Those rules are checked when the setting is saved, when the service
starts, and again when a policy is rendered. No Cilium-specific policy is used;
every CNI that enforces NetworkPolicy matches both forms.

The selectors are the `kubernetes.egress` setting. The settings file seeds it
(`kubernetes.dns_namespace`, `dns_pod_labels`, `local_endpoint_namespace`,
`local_endpoint_pod_labels`, `local_endpoint_port`); an administrator edits it
from the admin API (`GET` and `POST /v1/admin/kubernetes/egress`), the CLI
(`crucible admin kubernetes egress` and `set-egress`) or the Routing page of the
admin UI. A saved value is a `provider_settings` row that wins over the file,
every edit is an audited `kubernetes_egress_updated` event, and each process's
provider reads the row back (within 15 seconds, and at once in the process that
took the edit), so the supervisor follows an edit made through the API without a
restart. (Added 2026-09-23 for crucible#91.)

**Readiness.** If the cluster's CNI does not enforce egress NetworkPolicy, the
provider refuses to launch: readiness of the namespace is probed by a canary pod
and the result is recorded and shown on the admin status page (25). The canary
runs under its own NetworkPolicy, rendered exactly as a worker's is (cluster DNS,
and the enabled local endpoint of the routing policy in force when there is one),
and must:

1. fail to reach the API server (`egress_enforced`);
2. resolve a cluster name, `kubernetes.default.svc`, through that policy
   (`dns_resolves`);
3. connect to the enabled local endpoint's URL through that policy, when one is
   enabled (`local_endpoint_reachable`).

A failure of any of them is `namespace_ready: false` with a detail naming the
check that failed, and every launch is refused until it passes. A missing tool in
the canary image (no curl, no getent or nslookup) is inconclusive and never a
pass. A passed probe is kept until the `kubernetes.egress` setting or the enabled
local endpoint changes; then it runs again before the next launch.

The canary runs the first worker image reference the provider knows of, which
before any attempt has resolved one is the first entry of
`kubernetes.image_repositories`. Those are bare repositories, and a bare
repository means `:latest` to a kubelet, so a registry with no `latest` leaves
the canary in `ImagePullBackOff` until the launch timeout and the status page
reporting the namespace as not ready for a reason that has nothing to do with
the namespace. A deployment therefore names one exact, pullable reference in
`kubernetes.probe_image`, and the wiring puts it first.
(Added 2026-09-22 during C9, after exactly that happened on kind.)

## Provider mechanics (08's interface)

- `prepare`: create the PVC, ConfigMap, and per-attempt Secret; run the
  preparer Job with the reference cache mounted read-write from a
  cluster-side cache volume that Crucible refreshes with a short-lived
  installation token (the token never enters the workspace); the Job
  performs exactly what 08's Docker `prepare` performs. `prepare` returns
  when the Job completes; a failed Job is a prepare failure with the Job's
  log excerpt as detail.
- `launch`: resolve the worker image to a digest through the image registry
  (11, 25) and record it; refuse an unsupported harness version; create the
  NetworkPolicy and the worker Job; return the Job name as the handle.
- `observe`: read the Job and its Pod; `running` while the Pod is Pending
  or Running, `exited(code)` from the terminated container status, `lost`
  when the Job or Pod no longer exists or the Pod was evicted or its node is
  gone. Pending longer than the policy's launch timeout (image pull, no
  schedulable node, PVC unbound) is a launch failure with the Pod's
  conditions as detail, not a stall.
- `logs`: `pods/log` with timestamps, `sinceTime` from the stored offset,
  resumed strict-after by the (timestamp, line hash) pair (10). A restarted
  supervisor re-attaches by Job name.
- `collect`: the collector Job with the workspace mounted read-only and an
  output subpath read-write; then the bundle verifier Job; outputs are read
  by the supervisor from the PVC through a short-lived reader Pod, never by
  mounting the PVC into the Crucible pods.
- `terminate`: `drain` deletes the Pod with the policy grace period
  (SIGTERM, then SIGKILL by the kubelet); `kill` deletes with grace zero.
- `cleanup`: only after `logs_drained`; delete Jobs and NetworkPolicy;
  delete the per-attempt Secret under every policy; keep or delete the PVC
  per policy (retained PVCs carry a retention label the sweep honours);
  release the lease.
- `reconcile`: list Jobs by label; a Job with no live attempt row is
  orphaned and deleted; a live attempt with no Job is `lost`.

Heartbeats and stall detection (10, C6c) are unchanged: log progress and
the filesystem fingerprint come from the PVC through the reader Pod, and
`container_running` is the Pod phase.

## Credentials on the cluster (12, made concrete)

Each harness's dedicated credential directory becomes one Secret in
`crucible-workers` that only the supervisor's ServiceAccount can read,
delivered through the GitOps repository as a SealedSecret or ExternalSecret.
Per attempt, the provider copies it into `cred-<attempt>`, taking only the
auth files the adapter declares. A Secret key cannot hold a path separator, so
a harness whose auth file sits in a subdirectory (AGY's token) is keyed with
the separator replaced and the volume projects it back to its declared path.

A read-only harness mounts `cred-<attempt>` directly, which is a tmpfs nothing
writes and nothing stores. A harness whose adapter declares `rw-narrow` cannot:
a Kubernetes Secret volume is read-only however it is mounted, and the three
subscription harnesses refresh their own token in place (S1). So an init
container copies the named files off the Secret's read-only projection into the
`credential` leaf of the attempt's own claim, mode 0700 on the directory and
0600 on each file, which is the same shape, the same properties and the same
place the Docker provider puts it (12). The provider reads the rotated file
back from there through the reader Pod and writes it into the harness Secret
whenever the attempt reaches collection, whether the worker exited successfully
or not. This matches the Docker provider: a valid newer refresh is durable state
even when the task itself fails.
(Made concrete 2026-09-21 during C8a.)
The per-attempt Secret is deleted under every cleanup policy. The admin
login flow (25) runs the harness's login in a login Job with the harness
Secret writable and the device URL captured from the Pod log. Hermes declares optional
read-only credential file `api-key`. When `crucible-harness-hermes` is configured, the
provider copies that file into the per-attempt credential Secret and never syncs it
back. When the Secret mapping is absent, the launch uses the adapter's explicit
unauthenticated placeholder.

## Observability and administration

Attempt evidence that has no field of its own on the provider port (the Job and
Pod names, the node, the effective limits, the pod PID limit and the
NetworkPolicy applied) is stored as one `report/kubernetes-launch.json`
artifact of the attempt, which carries an `artifact_present` evidence row like
any other per-attempt fact Crucible observed (11). The image digest stays on
the attempt row. (Made concrete 2026-09-21 during C8a.)

`GET /providers` reports the Kubernetes provider with `isolation: pod`,
`network_control: true`, `resource_limits: true`, `shared_disk: false`, the
harnesses whose images are promoted, and `max_concurrency` from the
namespace's ResourceQuota. The admin status page (25) shows the namespace
readiness probe, the CNI egress enforcement result, the pod PID limit, and
the runtime class in use ("standard" in this version). Attempt evidence
records the image digest, the Job and Pod names, the node, the effective
limits, and the NetworkPolicy applied.

## What the cluster must guarantee first (lab-admin)

Recorded here so the prerequisite is a checklist, not folklore; each item
is verified by the namespace readiness probe or the e2e tier and shown on
the status page.

1. The two namespaces exist; `crucible-workers` has Pod Security admission
   at `restricted` and a default-deny NetworkPolicy.
2. The CNI enforces egress NetworkPolicy (the canary must fail to reach the
   API server), and the worker rules match on it: the canary must resolve a
   cluster name and reach the enabled local endpoint. On a CNI that translates
   service addresses first, the `kubernetes.egress` selectors are what make the
   second half true.
3. A storage class for the workspace PVCs with `ReadWriteOnce` and a size
   the policy's workspace cap fits, and one with `ReadWriteMany` for the
   artifact root: the api serves what the supervisor wrote and they are
   separate Deployments on this topology. Object storage for artifacts would
   remove the second requirement and is a later phase.
   (Added 2026-09-22 during C9.)
4. Nodes have a pod PID limit configured.
5. The cluster can pull the Crucible and worker images from the registry
   the release publishes to (24); a pull secret if the packages are private.
6. Egress from `crucible-workers` to the model providers, the package
   registries, GitHub, and the Spark is possible at the network edge (the
   NetworkPolicy narrows it; the lab's edge must not block it).
7. An Argo Application pointing at the deployment manifests; the manifests
   are the deployment repository's record, and pin an exact image tag (24).
8. Public DNS is the operator's alone (operator rule 10): the API's ingress
   route is provisioned and reported; no record is created by Crucible or by
   any manifest.

A runtime class for worker pods (gVisor or Kata) is not a prerequisite for
this version.

The manifests that satisfy the Crucible half of this list are
`deploy/kubernetes`, and the operator-facing runbook, including every
placeholder lab-admin fills in and the public DNS record the operator creates
alone, is `docs/deployment.md` (C9).

## Testing (18, extended)

A fourth tier, `make e2e-kind`: the end-to-end suite run against the
Kubernetes provider on a throwaway kind cluster with a unique name, deleted
on exit even on failure (the `sdlc` skill's kind pattern). It loads the
script-harness image with `kind load docker-image`, installs a CNI that
enforces NetworkPolicy (kind's default does not), applies the namespace
manifests, and runs the same cases as the Docker tier plus:

- every NetworkPolicy denial from inside a worker pod: API server, cluster
  DNS on any port but 53, another namespace, link-local, the lab ranges;
- a Pod evicted or deleted out of band is `lost`;
- a worker that ignores SIGTERM is killed at the grace period;
- the per-attempt Secret is gone after cleanup under every policy;
- supervisor restart re-attaches to a running Job and logs resume;
- the readiness probe refuses launches when egress enforcement is absent
  (run once with the enforcing CNI removed).

`tools/kind/cilium-egress.sh` is the same kind of disposable cluster running
Cilium with kube-proxy replacement instead of Calico. It puts a stand-in model
gateway behind a Service and shows, from a pod under each policy, that the
address-only rules of before crucible#91 leave a worker with no DNS and no
gateway while the selector rules give it both, with the API server, the rest
of the resolver's ports and the internet still unreachable; then it runs the
provider's own readiness canary in both forms. It is a local proof, not a CI
job. (Added 2026-09-23.)

It runs in CI on the same runner class as the Docker tier. The readiness
rows 5, 7, 11, 12, and 23 are re-proven on this tier and cited in 19 with
their kind run before the gate counts for real-project use.

## Out of scope for this version

Runtime class (microVM or gVisor), multi-cluster, object storage for
artifacts (the PVC and the reader Pod are enough for the lab), a Kubernetes
operator, and autoscaling. Each is a later phase in 20.
