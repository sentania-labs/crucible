# Running a release locally: the deployment directory

C3 left one thing unfinished (`c3.md`, "Where the local arrangement is not
finished"): `make up` cannot bring the normal-mode stack up on the dedicated
rootless daemon from the operator's working tree. `make deploy-local` is the
answer, and it is an operator arrangement rather than a code change, which is
why it is a directory and a script and not a patch to the provider.

## The problem, precisely

The rootless daemon belongs to the `crucible` service user (13, ADR 0004, S9).
The compose client that talks to that daemon therefore runs as that user, and it
has to read `compose.yaml`, the generated squid configuration, `.env` and the
build context. On the reference workstation all of those live under
`/home/scott`, which is mode 750. The service user is not in the operator's
group and never should be: widening the operator's home so a service account can
read it is a larger hole than the one being closed.

Nothing here is broken. The daemon is isolated from the operator's files exactly
as intended, and `make up` on CI, which has an ordinary rootful daemon, is
unaffected.

## The answer

A deployment directory the service user owns, holding everything the stack
needs and nothing the operator's home provides:

```
/var/lib/crucible/deploy/          750 crucible:crucible
  compose.yaml                     640  copy of the repository's, unmodified
  compose.deploy.yaml              640  the override that pins the image
  .env                             600  from .env.example, generated password
  var/egress/squid.conf            640  seed copied into the managed proxy volume
/var/lib/crucible/credentials/     700 crucible:crucible
  claude_code/ codex/ agy/ hermes/ github/ 700  empty, for 12 and 25
```

`make deploy-local` creates or refreshes that directory and brings the stack up;
`make deploy-local-down` stops it. Both are safe to run repeatedly.

## What the target does

1. Runs `proxy-config`, so the deployed egress allowlist is the one the Makefile
   defines. The Makefile stays the single definition: the same `EGRESS_ALLOWLIST`
   and `WORKERS_SUBNET` reach both the generated `squid.conf` and the deployed
   `.env`, so the proxy's rules and the application's declared allowlist agree.
2. Refuses to go further without a `crucible` service user, a running rootless
   daemon socket, passwordless sudo, and a Compose plugin of at least 2.24 for
   that user (where `!reset` arrived). The socket is stat'd through sudo:
   `/run/user/<uid>` is mode 700, which is the same wall the target exists for.
   The socket path is derived from the service user's uid and from nothing else.
   `CRUCIBLE_DOCKER_SOCKET`, which the Makefile exports for `make up`, is
   deliberately ignored here: its fallback is the host's rootful socket, and a
   stray environment variable must not be able to put this deployment on the
   daemon whose compromise is a compromise of the host.
3. Refuses an image reference that is `latest` or carries no tag. A deployment
   pins one exact version; that pin is what makes a redeploy reproducible and a
   rollback a one-word change.
4. Creates `/var/lib/crucible/deploy` (750) and `/var/lib/crucible/credentials`
   (700) with one empty directory per harness plus `github`, all owned by the
   service user. The layout is created and nothing more: a credential enters
   only through `crucible-admin credentials login` (25), which points the
   harness's own interactive login at its directory here. The target never reads,
   copies or references the operator's own harness credentials.
5. Copies `compose.yaml` and the generated `squid.conf` in, rather than
   referencing them where they are, so the running stack does not depend on a
   path the service user cannot read.
6. Writes `compose.deploy.yaml`, which pins `image:` on `migrate` and `crucible`
   to the chosen release and drops the development `build:` section with
   `!reset null`. There is no build context in the deployment directory, and a
   deployment runs the published artifact rather than a local rebuild.
7. Writes `.env` from `.env.example`, replacing `POSTGRES_PASSWORD` with 40
   random alphanumerics the first time and keeping what is there on every later
   run. Generation and writing both happen inside one root shell that handles
   the value with builtins only and writes straight to a mode 600 file the
   service user owns: it is never a command-line argument, because
   `/proc/<pid>/cmdline` is world-readable; never in the calling script's
   environment; never in the make output; and never printed. Alphanumeric only,
   because compose interpolates `$` in a `.env` value.
   `CRUCIBLE_DOCKER_SOCKET`, `CRUCIBLE_IMAGE`, `CRUCIBLE_EGRESS_ALLOWLIST` and
   `CRUCIBLE_WORKERS_SUBNET` are appended, so the deployed application's declared
   allowlist and subnet are the same values the deployed `squid.conf` was
   generated from and cannot drift from them inside the deployment directory.
8. Asserts that nothing under the deployment directory names a path in `/home`.
   That is the whole point of the directory, so it is checked rather than
   assumed, and "could not look" is a failure distinct from "found nothing".
9. Pulls the supporting images and the pinned release, then
   `docker compose up -d --wait` as the service user against its own daemon. If
   the egress allowlist changed since the last run the proxy is recreated:
   `squid.conf` is a bind mount, replacing the file gives it a new inode, and a
   running container would otherwise keep the old rules while the target
   reported success.
10. Reads `/v1/health` and refuses to report success unless the version it
    reports is the tag that was deployed. Loopback ports are shared with
    whatever else is running on the workstation, and a `/v1/ready` from
    somebody else's service is not evidence about this one. Then prints
    `/v1/ready`.

`deploy-local-down` is `docker compose down` without `--volumes`:
`crucible_crucible-pg` and `crucible_crucible-artifacts` survive, because a
deployment's database is not a development scratch database. Workers are not
compose services, so a running worker is left alone (13); `crucible-admin drain`
is what removes those.

## Authenticated local gateway

`CRUCIBLE_LOCAL_ENDPOINT_URL` is a first-migration seed, not the continuing source of
truth. After startup, sign in to `/ui`, open **Routing**, and save the gateway `/v1`
URL, model enablement, thinking preference, and pool concurrency. The save writes new
immutable routing and delivery policy versions and regenerates `var/egress/squid.conf`.
The database value wins on later restarts.

Open **Credentials**, paste the Hermes LiteLLM virtual key, and give the mutation a
reason. Crucible writes only `/var/lib/crucible/credentials/hermes/api-key`, mode 0600,
then probes `/health/readiness` without a bearer and `/v1/models` with the bearer. The
key is not returned, logged, or included in audit. The equivalent non-interactive path
is:

```sh
printf '%s\n' "$LITELLM_VIRTUAL_KEY" \
  | crucible-admin --reason "install lab gateway key" credentials set --harness hermes
```

The key remains in the shell variable and stdin. It is never an argument. A routing
save atomically rewrites the live file in the managed `crucible-egress` volume and
signals Squid to validate and reload it. Recovery is to disable `coder` in **Routing**
or select the preceding immutable policy version. No environment edit or manual proxy
restart is needed.

## Choosing the version

```sh
make deploy-local                 # the default pin, DEPLOY_TAG in the Makefile
make deploy-local DEPLOY_TAG=0.2.2
make deploy-local CRUCIBLE_DEPLOY_IMAGE=ghcr.io/sentania-labs/crucible:0.2.2
```

`DEPLOY_TAG` in the Makefile is a deployment pin, not the package version: the
package version still comes from the git tag and no release needs a commit on
`main` (`release.md`). The pin does not follow a new release on its own, which is
the deliberate half of pinning and also the half that goes stale: cutting a
release and wanting to run it here is two steps, not one. Rolling back is the
same edit in the other direction; the database and artifacts are untouched.

`CRUCIBLE_DEPLOY_DIR`, `CRUCIBLE_CREDENTIAL_ROOT`, `CRUCIBLE_SERVICE_USER` and
`CRUCIBLE_DEPLOY_PORT` move the arrangement to a host that is laid out
differently.

## This is not a development target

`make up` is still how you run your working tree, on a daemon that can see it.
`make deploy-local` runs a released image and cannot run uncommitted code at
all: there is no build context in the deployment directory. Using it to test a
change would only test the last release.

The first verification run, on 2026-09-16, brought up
`ghcr.io/sentania-labs/crucible:0.2.1` and reported ready with the schema at
`0004_gates_and_acceptance`, which is that release's head. A release built after
C3 merges will carry `0006`, and the running stack will migrate itself on the
next `make deploy-local` because `migrate` runs before `crucible` starts.

## Limitations

- **A regenerated password against a surviving database locks the stack out.**
  Postgres reads `POSTGRES_PASSWORD` only at initdb. The target keeps the
  password across runs for exactly that reason, but if `.env` is deleted or the
  deployment directory is rebuilt while `crucible_crucible-pg` survives, the new
  password will not match the one in the volume and `migrate` fails to
  authenticate. The cure is the old password or a new database; there is no
  third answer. Keep the deployment directory and the volume together.
- Harness credentials live in the private named credential volume. The GitHub App
  remains a separate read-only host bind. The Hermes directory is created mode 0700 by
  `credential-init`; the admin paste flow owns only its `api-key` file.
- The target assumes one deployment per host: the compose project name comes
  from `compose.yaml` (`crucible`) and the published ports are fixed on
  loopback. A second deployment collides on both, and so does the development
  stack: `make up` and `make deploy-local` both bind 127.0.0.1:8080 and
  127.0.0.1:5432. So does anything else on the workstation that wants those
  ports, which is not hypothetical: the verification run lost 8080 to an
  unrelated container between two invocations and was finished on
  `CRUCIBLE_DEPLOY_PORT=8081`. That variable moves the API port only; postgres is
  published by `compose.yaml` with no variable.
- `make deploy-local-down` exits non-zero when a worker is still running: the
  worker holds the `crucible-workers` network open and compose cannot remove it.
  The container is left alone, which is what 13 asks for, but the exit status
  says something failed. `crucible-admin drain` first is the clean order.
- `tools/deploy/deploy_local.sh` is not linted by `make lint` or by CI, which
  cover Python. It is `shellcheck`-clean as written and nothing keeps it that
  way.
- Nothing supervises the deployment across a reboot. `restart: unless-stopped`
  brings the containers back once the daemon is up, and S9's follow-up 5, a
  reboot test of the lingering service user and the enabled rootless unit, is
  still open.
- Rolling back to an older tag runs an older schema against a database a newer
  release migrated. `/v1/ready` reports that honestly as schema drift; there is
  no down migration.
