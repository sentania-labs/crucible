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
credential to use it with. Labels: `org.opencontainers.image.version`,
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
- Every attempt records the image digest it ran, resolved at launch.
  Retries and corrections of a task keep that digest unless Foundry
  explicitly authorizes a different image in the correction contract.
- Each adapter declares its tested version range. `GET /harnesses` reports
  installed versions (from image labels of allowlisted images) and the
  supported range; a launch with an unsupported combination is refused
  with a wake, never a warning.
- Images are built locally in C3 and C5. Versioned, digest-pinned images
  publish to `ghcr.io/sentania-labs/crucible-worker` once the live harness
  phase and the release workflow exist.

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
- **Not carried to Kubernetes.** The Kubernetes provider uses the API
  server with a namespaced ServiceAccount. Only the create-request policy
  survives, as a Pod spec policy enforced by admission on the cluster.

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
that the real harnesses never execute from it; C5 tests each harness with
`noexec` and 13 is updated with the result. From S6
(Claude Code and the others provisional until an authenticated run
completes through the filter in C3): Claude Code `api.anthropic.com`
(plus `mcp-proxy.anthropic.com` only if account MCP connectors are
wanted; telemetry to Datadog denied); Codex `api.openai.com`,
`auth.openai.com`, possibly `chatgpt.com`; AGY
`daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`. Everything
else each CLI tried (experiment flags, update checks, browser downloads)
is denied. The
publisher's allowlist is `github.com` and `api.github.com` only, and it is
a separate proxy on a separate network rather than an entry on the
workers' list. `network:
none` gives `--network none` and no proxy. Crucible never programs host
firewall rules.

## GitHub webhook ingress

Polling is the complete observation path and the local default; no
public route to the workstation is required or created (22, Q13).
`github.webhook_enabled` stays false locally. In Kubernetes the endpoint
sits behind the cluster's ingress with the webhook secret validating every
delivery in memory before anything is stored (23).

## Filesystem

Read-only root, tmpfs `/tmp` and `/home/worker` (size-limited), repo mount
rw, identity ro, one credential mount, report dir rw. Nothing else.

## Resource limits

From the policy (05b): CPU, memory, pids, tmpfs total, concurrency per
provider (3) and per harness (1).
