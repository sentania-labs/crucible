# 13. Local operation: Docker Compose and the Docker security model

## Services (`compose.yaml`)

| Service | Image | Role | Persistent |
|---|---|---|---|
| `postgres` | `postgres:16` pinned by digest | authoritative state | volume `crucible-pg` |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` pinned by digest, or an equivalent | reduced Docker API surface | no |
| `egress-proxy` | a small CONNECT proxy image pinned by digest | worker egress allowlist | no |
| `crucible` | `ghcr.io/sentania-labs/crucible:<tag>` | api + supervisor (`serve --all`) | volume `crucible-artifacts` |

Workers are not Compose services. They are containers Crucible creates
through the proxy, on the `crucible-workers` network, labeled with the
attempt. `docker compose down` does not remove running workers by design;
`crucible-admin drain` does.

Compose owns lifecycle: `restart: unless-stopped` on all three. Closing any
Foundry session touches none of them. Foundry detects Crucible down by
`GET /ready` failing and runs the configured start command (`docker compose
-f <path> up -d`), recorded as an event once the API is reachable.

## Two modes

**Normal**: `docker compose up -d`. Everything in containers.

**Developer**: `docker compose --profile dev up -d` starts `postgres` and the
proxy only; `uv run crucible serve --all --reload` runs on the host with
`CRUCIBLE_DOCKER_HOST=tcp://localhost:2375` (the proxy published on
loopback only). Same API, same providers, same worker containers. The
artifact root is a host directory in this mode; the Docker provider bind-
mounts workspaces from it exactly as in normal mode.

A `Makefile` wraps both: `make up`, `make dev`, `make down`, `make lint`,
`make test`, `make e2e`. CI calls the same targets.

## Worker images

One base image per harness version, built from `images/<harness>/Dockerfile`:
Debian slim, non-root `worker` (uid 1000), git, curl, jq, `gh`, the harness
CLI at a pinned version, and nothing else. Tagged
`crucible-worker:<harness>-<harness-version>-<build>`. Reproducible: pinned
base digest, pinned package versions, `SOURCE_DATE_EPOCH`. Project-specific
toolchains (Python, Node, Go) come from a per-project image the task
contract names, built `FROM` the harness base; the provider's image
allowlist pattern controls what may run.

## Docker socket: the authority Crucible holds, stated honestly

Anything that can talk to the Docker socket is root-equivalent on the host.
Crucible must create containers, so it needs some of that authority. The
model, and its limits:

1. **Crucible never sees the raw socket.** Only `docker-socket-proxy` mounts
   `/var/run/docker.sock`. Crucible talks HTTP to the proxy.
2. **The proxy narrows the API surface, not the request bodies.** It allows
   `containers` (create, start, inspect, logs, stop, kill, remove, list),
   `images` (inspect, list), `networks` (inspect, connect), `volumes`
   (create, remove, inspect), and disables `exec`, `build`, `swarm`,
   `system`, `plugins`, `secrets`, `configs`. It cannot reject a create
   request that asks for `Privileged`, a host namespace, or a bind of `/`,
   and it exposes every container on the host to inspect, kill, and
   archive. So the proxy is a tripwire against accidents and a reduction of
   surface, not a security boundary against a compromised Crucible.
3. **Create-request policy in Crucible** refuses to emit `Privileged`, host
   PID or network namespaces, any bind mount outside the artifact root and
   credential root, any capability add, or an image outside the allowlist.
   Unit-tested. This protects against Crucible bugs, not against Crucible
   being compromised, because compromised code does not run its own checks.
4. **Effective host authority Crucible retains: root-equivalent.** A
   compromise of Crucible in the default local mode is a compromise of the
   host. This is the same authority any developer tool with socket access
   holds, and it is acceptable only because Crucible is trusted control-plane
   software on a single-operator workstation that already runs Docker.
   The operator decides whether that is acceptable (22, Q11). Two ways to
   bound it, in order of preference:
   - **Rootless Docker daemon dedicated to Crucible.** The socket belongs to
     an unprivileged user; a full escape yields that user, not root. Costs
     one daemon to install and some feature loss (no privileged ports,
     slower overlay).
   - **A body-validating authorization layer** in front of the socket (a
     Docker authz plugin or a small purpose-built proxy that parses create
     bodies) so the rejections in item 3 happen server-side. More code to
     own; recommended only if rootless is not possible.
5. **Workers never receive the socket or the proxy endpoint.** Verified by
   the isolation integration test (18) that runs a worker whose only job is
   to try.
6. **Not carried to Kubernetes.** The Kubernetes provider uses the API
   server with a namespaced ServiceAccount. Nothing in the Docker model
   survives that move except the create-request policy, which becomes a Pod
   spec policy enforced by admission on the cluster, not by Crucible.

## Networking

Workers attach only to `crucible-workers`, a Docker network created with
`internal: true`: no default route, no reach to the Compose internal
network, `postgres`, `crucible`, or the socket proxy. Egress is provided by
an `egress-proxy` service (an HTTP CONNECT proxy with a hostname allowlist,
plus a resolver) that sits on both `crucible-workers` and the outside; the
provider sets `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` in the worker
environment, and git and each harness honor them. The allowlist is the
union of the policy's `egress_allowlist` and the adapter's declared model
endpoints (S6). `network: none` gives `--network none` and no proxy. No
host firewall rules are touched by Crucible; it cannot and must not program
host iptables.

## Filesystem

Read-only root, tmpfs `/tmp` and `/home/worker` (size-limited), repo mount
rw, identity ro, one credential mount, report dir rw. Nothing else.

## Resource limits

From the policy (05b): CPU, memory, pids, tmpfs total, concurrency per
provider and per harness.
