# Secret shapes

Crucible's repository holds no secret value and no sealed ciphertext. What it holds is
the *shape* of every Secret the deployment reads, so lab-admin can seal or source the
real values without guessing a key name.

Four kinds of secret, and where each is mounted:

| Object | Namespace | Keys | Read by |
|---|---|---|---|
| `crucible-database` | `crucible` | `password`, `url` | the PostgreSQL StatefulSet (`password`) and the api, supervisor and migration Job (`url`) |
| `crucible-github-app` | `crucible` | `app.pem`, `webhook.secret` | the api and supervisor pods only, as files (12) |
| `crucible-harness-<harness>` | `crucible-workers` | one per auth file the adapter declares | the supervisor's account, copied per attempt into `cred-<attempt>` (12, 26) |
| an image pull secret | both | `.dockerconfigjson` | the kubelet, when the packages are private |

`password` and `url` must agree: `url` is the whole DSN
(`postgresql+psycopg://crucible:<password>@crucible-postgres:5432/crucible`) because
pydantic-settings reads one environment variable and does no interpolation.

A harness whose auth file sits in a subdirectory is keyed with the separator replaced by
an underscore, and the volume projects it back to the declared path (C8a). AGY's
`antigravity-cli/antigravity-oauth-token` is therefore the key
`antigravity-cli_antigravity-oauth-token`. **Lab-admin's harness Secrets must use the
flattened key.**

## `sealed/`

SealedSecret placeholders, one per object, carrying `REPLACE_WITH_SEALED_*` in place of
every ciphertext. They are real resources of the lab overlay, so a `kubectl kustomize`
of that overlay shows lab-admin exactly which objects have to exist. They are
deliberately not valid ciphertext: the sealed-secrets controller refuses them and no
workload starts with a wrong value. Sealing is `kubeseal` against the cluster's own key,
and the result belongs in lab-admin's GitOps repository, never here.

Automated sync is off in `argocd/application.yaml` for exactly this reason: the first
sync is a person looking at what is about to be created.

## ExternalSecret instead

A cluster running external-secrets replaces `sealed/` with `external/` in the overlay's
resources. The key names above are the same either way; only where the value comes from
changes. `external/` carries one worked example.

## Plain Secrets, for kind only

`make deploy-kind` generates plain Secrets from throwaway values with kustomize's
`secretGenerator`, in `../overlays/kind`. That overlay exists to prove the manifests on a
disposable cluster and is never applied anywhere else. There is no plain-Secret path for
the lab.
