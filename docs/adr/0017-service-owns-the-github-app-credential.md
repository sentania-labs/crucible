# ADR 0017: The service owns the GitHub App credential, and the UI connects the App

Status: accepted. The operator's feedback of 2026-09-25 (quoted below), made concrete by
FDY-0117 the same day. Extends ADR 0015's ownership model from the harness credentials to
the GitHub App credential.

## Context

Until this change the GitHub App's private key and webhook secret arrived as the
`crucible-github-app` Secret, delivered by GitOps as a SealedSecret, and the App id was a
setting (`CRUCIBLE_GITHUB__APP__APP_ID`). A deployment had to seal a key, set an id,
turn `github.enabled` on and restart, and then type every repository by hand with its
installation id (crucible#120). The mount was `optional: true`, so a name mismatch in the
sealed Secret produced pods that started clean with no key and failed only at the first
delivery (crucible#79).

The operator, 2026-09-25, on "No repository is registered. Open Repositories before
submitting work.": "It'd be nice to give it a GH cred/install it as an app and then select
repos from an org or account." And on the setup as a whole: "some of these things need to
ensure that options and choices reference other values set in the system, so that things
become populated based on available options" (crucible#121). ADR 0015 already records the
earlier decision behind it: "You should not be concerned with deployment. You are
building an app/seevice."

## Decision

1. **The service is the single writer of the GitHub App credential.** On Kubernetes it is
   the Secret `crucible-github-app` in the service's own namespace (`crucible`, named by
   `github.app.secret_name`), keys `app-id`, `app.pem` and `webhook.secret`. The service
   creates it the first time Connect GitHub writes it, labels it
   `app.kubernetes.io/managed-by: crucible` and `crucible.credential: github-app`, and
   replaces its data with one merge patch on each later write; a webhook secret is
   replaced only when one is given. With the Docker provider the same three files sit
   beside `github.app.private_key_path`, written mode 0600 by the same flow. GitOps no
   longer delivers the Secret; the sealed placeholder is gone from `secret-shapes/sealed`.
2. **Connect GitHub checks before it stores.** The operator enters an existing App's id
   and one of its private keys (the UI's GitHub page, `POST /v1/admin/github/app`, or
   `crucible admin github connect`). The service signs an App JWT with that key in memory
   and calls `GET /app`; a refusal, or an answer naming another App, stores nothing. The
   answer carries the App's install link, built from the `html_url` GitHub returned. The
   key is never returned, logged or audited; its public-key fingerprint is.
3. **The key is read through the API server, on each signature.** Both the api and the
   supervisor read the Secret when they sign a JWT, so a connect is in force at once
   rather than after the kubelet's next projection of the mount. The mount stays, still
   `optional: true`, because a fresh deployment has no Secret until the operator connects
   and must start anyway (#79's "make the mount required" would stop it). It carries
   `webhook.secret` to the webhook route, which reads a file.
4. **A credential counts when the service wrote it, or when the settings say one was
   placed on purpose.** It is configured when it has a key and either an `app-id` the
   service wrote beside it, or `github.enabled` with a non-zero `github.app.app_id`. A
   deployment that sealed its own Secret before this change keeps working with its
   settings unchanged. When `github.enabled` is on and the key is missing, the process
   says so at startup and names the store (#79); the GitHub page names it too.
5. **The picker reads what the App can see.** `GET /app/installations`, then per
   installation one token scoped to `metadata: read`, used for
   `GET /installation/repositories` and discarded before the call returns. A pick
   registers the repository with the installation id, the clone URL and the default
   branch GitHub reported at that moment, and the default policy unless another is named.
   The free-text registration stays for anything the picker cannot show.

## Alternatives considered

- **Keep GitOps as the writer and generate a sealed file in the UI.** Rejected for the
  same reason as in ADR 0015: it is hand-sealing with extra steps, and the operator asked
  to connect the App from the UI.
- **The App manifest flow** (GitHub creates a new App from a manifest and redirects back
  with its credentials). It needs a public callback URL, which on this lab is a public
  DNS record, and those are the operator's alone (operator rule 10). Entering an existing
  App's id and key covers the need without one; the manifest flow can follow if a
  deployment has a public route.
- **Put the Secret in `crucible-workers`, where the Role already grants Secret verbs.**
  Rejected: that is the namespace untrusted worker code runs in, and 12 keeps the App key
  on the `crucible` pods only. A narrow Role in `crucible` is the smaller blast radius.
- **Read the key from the mounted file only.** A connect would then not be in force until
  the kubelet re-projected the Secret, up to a minute or more later, and the picker right
  after a connect would fail for no reason the operator could see.

## Consequences

- The control-plane ServiceAccount gains a second grant in its own namespace, beside
  deleting the first-run Secret (ADR 0016): the
  Role `crucible-github-app` (`deploy/kubernetes/base/crucible/github-app-rbac.yaml`),
  `get` and `patch` on the one Secret by name, and `create` on Secrets, which RBAC cannot
  narrow by name. No `list`, `watch`, `update` or `delete`; the database Secret stays out
  of reach. The residual risk is the pair: a compromised control-plane process could,
  before the first connect, create `crucible-github-app` itself as a service-account
  token Secret for another account in `crucible` and then read it. The service refuses to
  use or adopt a Secret of that name whose type is not `Opaque`, which keeps it from
  mistaking one for the App credential; it cannot stop a process that is already
  compromised. `crucible` holds no account with more than this one's permissions: the
  only other bound one, `crucible-migrate` (ADR 0016), may create Secrets and patch the
  first-run Secret, and cannot read any. Both Roles grant `create` on Secrets in
  `crucible`, to different accounts; neither can read the other's Secret.
- A deployment that sealed `crucible-github-app` before this change removes it from its
  GitOps repository without pruning it (Argo's prune would delete the key), or connects
  the App again from the GitHub page afterwards. On its first write the service takes the
  Secret over: it sets its labels and replaces its data.
- Rotation is a new App key in GitHub and a Connect GitHub with it, then revoking the old
  key in GitHub. Connect GitHub replaces the id and the key together, so the two can
  never disagree.
- With the Docker provider the connect flow needs the directory beside
  `github.app.private_key_path` writable by the service; compose mounts it read-only
  today, and the flow refuses there and names the directory.
