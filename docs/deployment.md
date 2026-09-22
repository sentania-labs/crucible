# Deploying Crucible on Kubernetes

The operator-facing runbook for the manifests in `deploy/kubernetes`. It covers what
lab-admin provides, what Crucible provides, how the deployment is applied through Argo,
how the harnesses are logged in once it is up, how to verify that it works, and the one
thing the operator does alone.

Local operation on Docker Compose is a different document: `docs/spec/13-local-operation.md`
and `docs/implementation-notes/deploy-local.md`. The provider's mechanics are
`docs/spec/26-kubernetes-provider.md`; this is only the deployment.

## What is in the repository

```
deploy/kubernetes/
  base/crucible/         the api and supervisor Deployments, PostgreSQL, the settings
                         ConfigMap, the artifact claim, the migration Job, the Service
                         and the Ingress
  base/workers/          the crucible-workers namespace: Pod Security admission at
                         restricted, the default-deny NetworkPolicy, the worker account,
                         the supervisor Role and RoleBinding, the ResourceQuota and the
                         reference-cache claim
  overlays/lab/          every value that is a property of a cluster, as a named
                         placeholder
  overlays/kind/         the disposable proof (`make deploy-kind`), never applied
                         anywhere else
  secret-shapes/         the shape of every Secret, as SealedSecret placeholders and one
                         ExternalSecret example. No value and no ciphertext.
  argocd/application.yaml  the Application template, automated sync off
```

The Crucible image tag is pinned in exactly one place, `base/kustomization.yaml`, and is
never `latest`. Changing the deployed version is that one line and a sync.

> **The pinned tag today is `0.3.3`, and that release has no Kubernetes provider.**
> `v0.3.3` is commit `8d2f32b`, which is before C8a added
> `crucible/adapters/execution/kubernetes.py`. An 0.3.3 deployment starts, serves and
> reports no `kubernetes` provider at all, so the status-page check and the task in
> **Verifying** below cannot pass on it. Pin the first release cut after C9 merges
> before deploying for real. Until then these manifests are complete and proven
> (`make deploy-kind`, against a locally built image) but the version they name is not
> one that can run a worker on a cluster.

## What lab-admin provides

Spec 26's checklist, made concrete. Each row is either a placeholder in
`overlays/lab/` or an object lab-admin creates in their own GitOps repository.

| # | What | Where it lands |
|---|---|---|
| 1 | Nothing: the two namespaces, the `restricted` admission labels and the default-deny NetworkPolicy are in `base/workers` and are applied by this Application | - |
| 2 | A CNI that enforces egress NetworkPolicy | verified by the readiness canary, shown on the status page |
| 3 | A `ReadWriteOnce` storage class for PostgreSQL, the reference cache and the attempt workspaces | `REPLACE_ME_STORAGE_CLASS_RWO` |
| 3b | A `ReadWriteMany` storage class for the artifact root | `REPLACE_ME_STORAGE_CLASS_RWX` |
| 4 | A pod PID limit configured on the nodes (a kubelet setting) | reported on the status page; the provider refuses to launch without one |
| 5 | The cluster can pull `ghcr.io/sentania-labs/crucible` and the worker images; a pull secret if the packages are private | `REPLACE_ME_IMAGE_PULL_SECRET` |
| 6 | Egress from `crucible-workers` to the model providers, the package registries, GitHub and the Spark is possible at the network edge | the per-attempt NetworkPolicy narrows it; the edge must not block it |
| 7 | The Argo Application | `argocd/application.yaml`, with `REPLACE_ME_ARGOCD_PROJECT`, `REPLACE_ME_MANIFEST_REPO_URL`, `REPLACE_ME_MANIFEST_REVISION` |
| 8 | Nothing: public DNS is the operator's alone (below) | - |

### Every placeholder, and what goes in it

| Placeholder | File | Value |
|---|---|---|
| `REPLACE_ME_LAB_DOMAIN` | `overlays/lab/ingress.yaml` | the domain the API host sits under; the host becomes `crucible.<domain>` |
| `REPLACE_ME_INGRESS_CLASS` | `overlays/lab/ingress.yaml` | the cluster's ingress class name |
| `REPLACE_ME_CERT_ISSUER_ANNOTATION` and `REPLACE_ME_CERT_ISSUER_NAME` | `overlays/lab/ingress.yaml` | the annotation the cluster's certificate issuer watches, and the issuer's name. Delete the annotation entirely if certificates are provisioned some other way; the TLS Secret is `crucible-tls` either way |
| `REPLACE_ME_STORAGE_CLASS_RWO` | `overlays/lab/storage.yaml`, `overlays/lab/settings.yaml` | a `ReadWriteOnce` class. It appears twice on purpose: once for the claims the manifests create and once as the class the provider gives each attempt's workspace claim |
| `REPLACE_ME_STORAGE_CLASS_RWX` | `overlays/lab/storage.yaml` | a `ReadWriteMany` class, for the artifact root the api serves and the supervisor writes |
| `REPLACE_ME_CLUSTER_DNS_IP` | `overlays/lab/settings.yaml` | `kubectl -n kube-system get service kube-dns -o jsonpath='{.spec.clusterIP}'` |
| `REPLACE_ME_RENDER_TIMEZONE` | `overlays/lab/settings.yaml` | the operator's IANA zone. Stored time is UTC; this is only how it is rendered (01) |
| `REPLACE_ME_IMAGE_PULL_SECRET` | `overlays/lab/settings.yaml`, `overlays/lab/pull-secret.yaml` | the pull secret's name, in both namespaces. Delete the `pull-secret.yaml` patch and blank the setting when the packages are public |
| `REPLACE_ME_PROBE_IMAGE` | `overlays/lab/settings.yaml` | one exact, pullable worker image reference. 26's readiness canary runs it, and a bare repository would mean `:latest` to a kubelet. **Fill it or blank it**: left as the placeholder it is also a repository `GET /v1/admin/images` tries to list, and the error names a registry rather than the placeholder |
| `REPLACE_ME_ARGOCD_PROJECT`, `REPLACE_ME_MANIFEST_REPO_URL`, `REPLACE_ME_MANIFEST_REVISION` | `argocd/application.yaml` | the Argo project, the repository holding these manifests, and an exact tag or commit. Never a branch: this field plus the pinned image tag are the deployment's record of what is running |
| `REPLACE_ME_SECRET_STORE_NAME`, `REPLACE_ME_REMOTE_PATH` | `secret-shapes/external/database.yaml` | only on a cluster using external-secrets instead of sealed ones |

### The Secrets

`deploy/kubernetes/secret-shapes/README.md` is the authority on the shapes. In short:

| Object | Namespace | Keys |
|---|---|---|
| `crucible-database` | `crucible` | `password`, and `url`, the whole DSN, which must carry the same password |
| `crucible-github-app` | `crucible` | `app.pem`, `webhook.secret` |
| `crucible-harness-claude-code` | `crucible-workers` | `oauth-token`, `.claude.json` |
| `crucible-harness-codex` | `crucible-workers` | `auth.json` |
| `crucible-harness-agy` | `crucible-workers` | `antigravity-cli_antigravity-oauth-token` |

A Secret key cannot hold a path separator, so AGY's auth file is keyed with the
separator replaced by an underscore and the volume projects it back (C8a). **The harness
Secrets must use the flattened key**, or the provider will not find the file.

The harness Secrets may be left absent at first. The login flow below fills them.

The placeholders in `secret-shapes/sealed/` are deliberately not valid ciphertext: the
sealed-secrets controller refuses them, so nothing starts with a wrong value. Seal the
real ones with `kubeseal` against the cluster's own key, in lab-admin's GitOps
repository. Nothing sealed, encrypted or plain belongs in Crucible's repository.

## What Crucible provides

Everything else: both namespaces with their admission labels and the default deny, the
supervisor's Role scoped to `crucible-workers` and nothing else, the worker
ServiceAccount with no permission at all, the ResourceQuota that is also the provider's
concurrency bound, the reference cache claim, PostgreSQL for the lab, the migration Job,
the api and supervisor Deployments, the Service, and the Ingress route without a host.

Everything an attempt becomes (Jobs, Pods, ConfigMaps, per-attempt Secrets,
NetworkPolicies and workspace claims) is created by Crucible at runtime and is not part
of the desired state. Argo tracks only what it applied, so it neither reports those as
drift nor prunes them.

## Applying it

1. Fill every placeholder above in a copy of these manifests in lab-admin's GitOps
   repository, or by overlaying this repository's `overlays/lab` from there.
2. Seal or source the Secrets.
3. Create the Application from `argocd/application.yaml`.
4. Sync it, by hand. **Automated sync is off by default and the first sync is a person
   looking at the diff**: that look is what catches a placeholder nobody filled in.
5. Watch the migration Job complete. `crucible serve` refuses to start against a schema
   that is not at head, so the api and supervisor wait for it rather than corrupting
   anything.
6. Read the one-time first-run administrator token out of the migration Job's log. It is
   printed once, in a framed block, and only its salted hash is stored (25). Store it
   before the log rotates.

## Logging the harnesses in

Once the api is reachable, sign in at `/ui` with that token and, for each harness
(25, step by step in that document):

1. **Credentials**: start the login. The service runs the harness's own CLI inside that
   harness's promoted worker image, with the credential Secret writable and no
   workspace, and captures the device or browser URL from the Pod log. Finish the
   authorization with the provider; the code has a window (Codex fifteen minutes, AGY
   sixty seconds, Claude Code's pasted token).
2. The flow validates the resulting files, runs the bounded probe in the hardened image,
   records the result, and removes everything it created.
3. The harness stays `session_compatibility: unverified` until the daily-session
   compatibility test passes for it (21, S1b). A harness is not enabled for normal
   workers before that.
4. **Harnesses**: flip the administrator's runtime gate. Both gates have to say yes: the
   configuration gate is the `CRUCIBLE_HARNESSES__*` entries in the settings ConfigMap
   and changing one is an edit and a restart; the runtime gate is this page.
5. **Images**: promote one worker image digest per harness to `default`. A launch is
   refused without one.

The operator's own daily-use harness directories are never read, copied or referenced
(12). These are dedicated Crucible logins.

## Verifying

Done means seen working, so all three:

1. **The status page.** `/ui` System status, or `GET /v1/admin/status`. The `kubernetes`
   provider row must read `health: ok` with

   ```json
   {
     "namespace_ready": true,
     "egress_enforced": true,
     "pod_pid_limit": <a number, not null>,
     "runtime_class": "standard"
   }
   ```

   `namespace_ready` is 26's readiness canary: a Pod that must fail to reach the API
   server. `egress_enforced: false` means the CNI is not enforcing egress NetworkPolicy
   and the provider will refuse every launch, which is the correct behaviour and not a
   bug to route around. `pod_pid_limit: null` means checklist item 4 is not done.

2. **The probe, after a change.** The answer is cached while it passes and re-run while
   it fails, so fixing the CNI does not need a Crucible restart.

3. **One task.** Submit a trivial task against a registered repository with
   `execution_request.provider: "kubernetes"` and drive it to `accepted`. What that looks
   like end to end is `tools/smoke/kubernetes_smoke.py`, which is what `make deploy-kind`
   runs against a disposable cluster.

## What the operator does alone

**The public DNS record.** Operator rule 10 and 26 item 8: creating a public DNS record
is the wall between a private lab and the internet, and it is never part of a
provisioning flow, a capability verb or a script. Nothing in this repository creates one,
and nothing in it should ever be changed so that it could.

The deployment provisions the route and reports the record that would be needed:

```
name:   crucible.<the domain filled into REPLACE_ME_LAB_DOMAIN>
type:   A or CNAME
target: the address or name of the cluster's ingress endpoint for
        REPLACE_ME_INGRESS_CLASS
```

Until the operator creates that record, the Ingress is reachable only from inside, and
`kubectl -n crucible port-forward service/crucible-api 8080:8080` is the way in. That is
a perfectly good state to run in: polling is the complete GitHub observation path and the
webhook is only an accelerator (23, Q13). `CRUCIBLE_GITHUB__WEBHOOK_ENABLED` stays
`false` until the record exists and the route terminates TLS.

## Proving a change to these manifests

```sh
make manifests     # kubectl kustomize + kubeconform + the security assertions
make deploy-kind   # the whole thing on a disposable kind cluster, one task through it
```

`make manifests` needs `kubectl` and `kubeconform` on PATH; `make deploy-kind` also needs
`kind`, `openssl` and a Docker daemon, and builds the worker image first
(`make e2e-image`).

CI runs `make manifests`, from this same definition, on every pull request. It does
**not** run `make deploy-kind`: that builds an image and stands up a kind cluster with
Calico, a TLS registry and a full task lifecycle, which is the `e2e-kind` job's cost
again, and `e2e-kind` is still establishing its own runtime and flake history. So
`make deploy-kind` is a local gate and running it is the author's job, not CI's.

What kind proves: the manifests are valid, the RBAC grants what the process asks for, the
volumes are there, the images pull, and a real task completes on the Kubernetes provider.
What kind cannot prove: anything cluster-specific. No ReadWriteMany storage, no ingress
controller, no certificate issuer, no lab network, no DNS. Those are seen for the first
time on the lab cluster, which is why the first sync is manual.
