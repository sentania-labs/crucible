# 13. Local operation: Docker Compose, worker images, and the Docker security model

Crucible runs as a persistent local Docker service now and as a Kubernetes
service later. Foundry keeps operating through Claude Code, Codex, and AGY
sessions; nothing here assumes Foundry is a service.

## Services (`compose.yaml`)

| Service | Image | Role | Persistent |
|---|---|---|---|
| `postgres` | `postgres:16` pinned by digest | authoritative state | volume `crucible-pg` |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` pinned by digest, or an equivalent | reduced Docker API surface | no |
| `egress-proxy` | a small CONNECT proxy image pinned by digest | worker egress allowlist | no |
| `publish-proxy` | the same CONNECT proxy image | publisher egress allowlist, narrower than the workers' | no |
| `crucible` | `ghcr.io/sentania-labs/crucible:<tag>` | api + supervisor (`serve --all`) | volume `crucible-artifacts` |

Workers, collectors, verifiers, and publishers are not Compose services.
They are containers Crucible creates through the proxy, labeled with the
attempt or job. Workers, collectors, and verifiers sit on the
`crucible-workers` network; the publisher sits on `crucible-publish`, its
own internal network with its own proxy, because the one container that
holds a GitHub credential must not reach the model endpoints the workers'
allowlist permits (23). `docker
compose down` does not remove running workers by design; `crucible-admin
drain` does.

## The credential root is the operator's to create

Compose binds the four credential directories (`github` and one per harness)
into the `crucible` container from a configurable root. A bind mount whose
source path does not exist is not an error: the Docker daemon creates it, as
root, mode 0755. That is precisely what 12 says a credential directory must
never be, and because the service runs unprivileged and has to write a
refreshed auth file back into that directory after a run, a fresh deployment
that let the daemon create them would look healthy and fail on the first
sync-back.

So the credential root and each per-harness directory under it are created
before the stack starts, by the operator or by the deployment script, owned
by the Crucible service user and mode 0700. `tools/deploy/deploy_local.sh`
does exactly that and creates the layout and nothing else; a credential
itself enters only through `crucible-admin credentials login` (25).

Crucible refuses at startup when a configured credential directory is
missing, is not owned by the service user, or is not mode 0700, and names the
directory and what is wrong with it. That refusal is specified here and is
the work item; it is not implemented yet, so today a wrongly created
directory is caught by the deployment script's own layout step or not at all.

**Login needs the harness CLI on the host.** The credential login flow of 25
drives the harness's own CLI in a pty, and by the image rule above each CLI
exists only in its worker image; the Crucible service image carries none of
them. The API form of login therefore cannot work on a normal deployment and
refuses with that reason, and login is a local-mode operation run where the
harness CLI is. Making the API form work anywhere means running the flow
inside the promoted harness image, the way the probe does, with the pty and
the operator's pasted code relayed through the daemon's attach stream; that
is a follow-up phase, not a fix.

Compose owns lifecycle: `restart: unless-stopped` on all four. Closing any
Foundry session touches none of them. Foundry detects Crucible down by
`GET /ready` failing and runs the configured start command (`docker compose
-f <path> up -d`), recorded as an event once the API is reachable.

## Two modes

**Normal**: `docker compose up -d`. Everything in containers.

**Developer**: `docker compose --profile dev up -d` starts `postgres`, the
socket proxy, and the egress proxy only; `uv run crucible serve --all
--reload` runs on the host with `CRUCIBLE_DOCKER_HOST` pointing at the
proxy published on loopback only. Same API, same providers, same worker
containers. The artifact root is a host directory in this mode.

A `Makefile` wraps both: `make up`, `make dev`, `make down`, `make reset`
(down with volumes; the only sanctioned way to discard a development
database), `make lint`, `make test`, `make e2e`. CI calls the same
targets. `COMPOSE_PROFILES=full` in `.env` is what lets one compose file
serve both modes; `--profile dev` overrides it.

The egress proxy image must be able to log to the container's stdout;
`ubuntu/squid` cannot (S9), so the pinned choice is made in C3 with that
requirement.

## Worker images and harness version management

One base image per harness version, built from `images/<harness>/Dockerfile`:
Debian slim, non-root `worker` (uid 1000), git, curl, jq, the harness CLI
at a pinned version, and nothing else. No `gh`: workers have no GitHub
credential to use it with. Where a CLI needs a companion binary to work at
all, as Codex does for the 5.6 model family, the companion ships in the same
image from the same pinned release, fetched by URL and verified against a
recorded sha256 of the tarball, with its mtime set to the epoch (07, S11). Labels: `org.opencontainers.image.version`,
`crucible.harness`, `crucible.harness_version`, `crucible.build_inputs`
(hash). Reproducible: pinned base digest, pinned package versions,
`SOURCE_DATE_EPOCH`. Project-specific toolchains come from a per-project
image the task contract names, built `FROM` the harness base; the
provider's image allowlist controls what may run.

Rules:

- Harness CLIs never update themselves inside a running worker. The image
  sets each CLI's auto-update opt-out and the root filesystem is
  read-only, so an update cannot land even if attempted. Found in S7:
  Claude Code honors `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1`; AGY
  honors `AGY_CLI_DISABLE_AUTO_UPDATE=1`; Codex has no environment
  variable, only the config key `check_for_update_on_startup=false`, which
  the Codex adapter passes at launch. S11 re-confirms on each promotion.
- Every harness's image is a **declared pin**, never "the newest". Every
  reproducible image carries the same `SOURCE_DATE_EPOCH` creation time, so
  two tags of one harness tie and a choice by creation time is arbitrary;
  a live run picked a stale tag exactly that way. `images/build.sh` writes
  `images/manifest.env` with the tag and OCI manifest digest per harness,
  and the e2e and live tiers read it. Recording is not a build input, so
  writing the manifest never changes a tag. `make lint` and `make e2e`
  execute `images/check-manifest.sh`, which runs the tag calculation in
  `build.sh` without building and refuses a stale declared pin. A launch is
  refused when more than one tag matches a harness and the manifest pins none.
- An image is launched with a harness's credential only when it carries a
  `crucible.harness` label equal to the requested harness (07). No label,
  or a different one, is a refusal before anything is seeded.
- Every attempt records the image digest it ran, resolved at launch.
  Retries and corrections of a task keep that digest unless Foundry
  explicitly authorizes a different image in the correction contract.
- Each adapter declares its tested version range. `GET /harnesses` reports
  installed versions (from image labels of allowlisted images) and the
  supported range; a launch with an unsupported combination is refused
  with a wake, never a warning.
- Images are built locally. Versioned, digest-pinned images
  publish to `ghcr.io/sentania-labs/crucible-worker` once the live harness
  phase and the release workflow exist. The current images on the rootless
  daemon, with `images/manifest.env` as the authoritative record:

  | Harness | Tag |
  |---|---|
  | `claude_code` | `crucible-worker:claude_code-2.1.273-1d43260eec11` |
  | `codex` | `crucible-worker:codex-0.153.4-8cc315ca7aab` |
  | `agy` | `crucible-worker:agy-1.2.4-975aff4033e0` |
  | `hermes` | `crucible-worker:hermes-0.19.0-ced6620edef7` |
  | `script-harness` | `crucible-worker:script-harness-1.0.0-88cd22bf214a` |

  `build.sh` is itself a hashed build input, so editing it retags every
  image; the previous tags stay on the daemon as the rollback. CI builds
  the script image from the same inputs and so gets the same tag, which is
  how its e2e job and the pin agree without a push. The CI lint job calls the
  same `make lint` definition as a local run, so drift is rejected in both.

Image promotion (a Crucible repository process, C8):

1. Renovate detects a new harness version on a weekly schedule and opens a
   dependency-update PR. No automatic promotion.
2. CI builds a candidate image with pinned inputs and records its digest.
3. Adapter contract tests run against the candidate.
4. A bounded live subscription-authenticated canary task runs against it
   (`make e2e-live`), outside CI, results attached to the PR.
5. A person reviews changed flags, output shape, authentication behavior,
   and report parsing.
6. The supported-version declaration is updated in the same PR.
7. Merge and tag publish the image to GHCR with its digest.
8. `POST /images/{digest}/promote` to `default` is an explicit admin act,
   recorded as a decision. The previous default becomes `retained`.
9. At least one known-good prior version stays `retained` for rollback.

## Docker authority, stated honestly

Anything that can talk to the Docker socket is root-equivalent on that
daemon's host user. Crucible must create containers, so it needs some of
that authority. The model, in order of preference:

1. **Dedicated rootless Docker daemon for Crucible (preferred).** A
   service user runs its own `dockerd` in rootless mode; only that daemon's
   socket is proxied to Crucible. A full escape yields that unprivileged
   user, not root. Costs: one daemon to install, some feature loss (no
   privileged ports, slower overlay, cgroup limits depend on the host's
   cgroup v2 delegation). Spike S9 passed on the development workstation
   on 2026-09-16 (`docs/spikes/S9.md`): limits enforced, internal network
   egress only through the proxy, performance on par with rootful. This is
   the default local arrangement from C3; socket `/run/user/<uid>/docker.sock`
   of the `crucible` service user, proxied.
2. **Default socket through the proxy (temporary fallback).** If S9 fails,
   the host daemon's socket is proxied. A compromise of Crucible is then a
   compromise of the host. That risk is recorded in the readiness report
   and in ADR 0004, and revisited before any multi-user deployment.

In both arrangements:

- **Crucible never sees the raw socket.** Only `docker-socket-proxy` mounts
  it. Crucible talks HTTP to the proxy.
- **The proxy narrows the API surface, not the request bodies.** It allows
  `containers` (create, start, inspect, logs, stop, kill, remove, list),
  `images` (inspect, list), `networks` (inspect, connect), `volumes`
  (create, remove, inspect), and disables `exec`, `build`, `swarm`,
  `system`, `plugins`, `secrets`, `configs`. Note from S9: with the
  reference proxy image, `EXEC=0` refuses `exec` start and inspect but
  still lets `POST /containers/{id}/exec` create an exec instance; no
  command runs, but Crucible's client must not rely on the create call
  failing. It cannot reject a create request that asks for `Privileged`,
  a host namespace, or a bind of `/`.
  It is a tripwire against accidents and a reduction of surface, not a
  boundary against a compromised Crucible.
- **Create-request policy in Crucible** refuses to emit `Privileged`, host
  PID or network namespaces, any bind mount outside the artifact root and
  credential root, any capability add, or an image outside the allowlist.
  Unit-tested. Protects against Crucible bugs, not a hostile Crucible.
- **Workers, collectors, verifiers, and publishers never receive the socket
  or the proxy endpoint.** Verified by the isolation integration test (18).
- **Not carried to Kubernetes.** The Kubernetes provider (26) uses the API
  server with a namespaced ServiceAccount. Only the create-request policy
  survives, as a Pod spec policy enforced by Pod Security admission on the
  workers namespace; egress control becomes a per-attempt NetworkPolicy.

## Networking

Workers attach only to `crucible-workers`, a Docker network created with
`internal: true`: no default route, no reach to the Compose internal
network, `postgres`, `crucible`, or the socket proxy. Egress is provided by
`egress-proxy` (an HTTP CONNECT proxy with a hostname allowlist plus a
resolver) that sits on both `crucible-workers` and the outside; the
provider sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` in the container
environment. One egress proxy serves the deployment, so its allowlist is
deployment-wide: the union over every enabled harness of the adapter's
declared endpoints, the hostnames of every enabled local model
`endpoint_url` in the routing policy (05b), and the policy
`egress_allowlist`; Crucible refuses
to launch an attempt whose effective allowlist exceeds what the proxy
was configured with, and a per-attempt proxy is a later hardening. Worker
`/tmp` is mounted without `noexec` in v0.x because no evidence exists yet
that the real harnesses never execute from it. C5a did not test it; the
test against each real harness is carried forward and this section is
updated with the result.

`make proxy-config` accepts routing policy JSON through
`ROUTING_POLICY_FILES`. Generation reads only entries whose `enabled` value is
true and whose `endpoint` is `local`; a configured but disabled local URL is not
authorized. Each enabled plain-HTTP URL becomes an exact destination ACL and an
exact port ACL. The port is added to `Safe_ports`, never `SSL_ports`, and the allow
line follows both unsafe-port denies and precedes the final deny. After an operator
uploads or selects a different routing policy, the proxy configuration is regenerated
from that policy and the proxy is reloaded before an attempt can use the route.

From S6, as each list stood after an authenticated task completed through
the filter (so none of them is provisional any more): Claude Code
`api.anthropic.com` (plus `mcp-proxy.anthropic.com` only if account MCP
connectors are wanted; telemetry to Datadog denied); Codex
`api.openai.com`, `auth.openai.com`, and `chatgpt.com`, which a
ChatGPT-plan login requires rather than merely prefers; AGY
`daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`,
`www.googleapis.com`, and `lh3.googleusercontent.com`, the last two for the
CLI's eligibility check, which fails closed without them. Everything
else each CLI tried (experiment flags, update checks, browser downloads,
`ab.chatgpt.com`, `antigravity-unleash.goog`, `play.googleapis.com`) is
denied, and the runs complete without them. The
publisher's allowlist is `github.com` and `api.github.com` only, and it is
a separate proxy on a separate network rather than an entry on the
workers' list. `network:
none` gives `--network none` and no proxy. Crucible never programs host
firewall rules.

## What the cluster deployment provides instead

Everything above is local operation. On Kubernetes the same service is the api
and supervisor Deployments of 26, and four of this document's arrangements have
a different shape there. `deploy/kubernetes` is the manifests and
`docs/deployment.md` is the runbook (C9).

| Compose here | Kubernetes there |
|---|---|
| the socket proxy, and the create-request policy behind it | no Docker socket at all; Pod Security admission at `restricted` on `crucible-workers` is the enforcement, and the namespaced ServiceAccount is the authority (26) |
| the egress proxy with a hostname allowlist | one NetworkPolicy per attempt per role that needs egress, over a namespace default deny (26) |
| the credential root, a directory per harness, created 0700 before start | one Secret per harness in `crucible-workers`, readable only by the supervisor's account; `credentials.<harness>.path` is not set at all, only `mount_mode` |
| `crucible serve --all` in one container | `--api` and `--supervisor` in two Deployments, the supervisor at one replica with the same database lease |

The artifact root is the one thing that gets harder rather than simpler: it is
one named volume locally and has to be a `ReadWriteMany` claim there, because
the api serves what the supervisor wrote.

## GitHub webhook ingress

Polling is the complete observation path and the local default; no
public route to the workstation is required or created (22, Q13).
`github.webhook_enabled` stays false locally. In Kubernetes the endpoint
sits behind the cluster's ingress with the webhook secret validating every
delivery in memory before anything is stored (23).

## Filesystem

Read-only root, tmpfs `/tmp` and `/home/worker` (size-limited), repo mount
rw, identity ro, the credential mount, report dir rw. Nothing else. The
credential mount is one writable mount plus the Crucible-owned template
files, each bind-mounted read-only at its own path **inside** the credential
directory, on top of the copy (12). A template is Crucible's own file from
the identity bundle, not a second credential: it is written into the bundle
before the bundle is hashed, so the identity hash covers it, and it is what
stops a worker planting a hook, an MCP server definition, or a plugin that a
later worker would inherit.

## Resource limits

From the policy (05b): CPU, memory, pids, tmpfs total, concurrency per
provider (3) and per harness (1).
