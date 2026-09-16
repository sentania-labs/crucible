# 13. Local operation: Docker Compose and the Docker security model

## Services (`compose.yaml`)

| Service | Image | Role | Persistent |
|---|---|---|---|
| `postgres` | `postgres:16` pinned by digest | authoritative state | volume `crucible-pg` |
| `docker-socket-proxy` | `tecnativa/docker-socket-proxy` pinned by digest, or an equivalent | restricted Docker API | no |
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

## Docker socket: the authority Crucible holds, and how it is restricted

Anything that can talk to the Docker socket is root-equivalent on the host.
Crucible must create containers, so it needs some of that authority. The
model:

1. **Crucible never sees the raw socket.** Only `docker-socket-proxy` mounts
   `/var/run/docker.sock`. Crucible talks HTTP to the proxy.
2. **The proxy allows only what the provider uses**: `containers` (create,
   start, inspect, logs, stop, kill, remove, list by label), `images`
   (inspect, list), `networks` (inspect, connect), `volumes` (create,
   remove, inspect), `exec` disabled, `build` disabled, `swarm`, `system`,
   `plugins`, `secrets`, `configs` disabled. `POST` is allowed only for the
   listed endpoints.
3. **Create-request policy in Crucible itself**: the provider refuses to
   emit a create request with `Privileged`, host PID or network namespace,
   any bind mount outside the artifact root and credential root, any
   capability add, or an image outside the allowlist. Unit-tested.
4. **Effective host authority Crucible retains**: it can run any allowlisted
   image with bind mounts under two configured roots, as uid 1000, with
   dropped capabilities, on a private network. It cannot escalate to root
   on the host through the proxy's allowed surface. A compromise of Crucible
   is a compromise of every worker workspace and every mounted credential
   directory, which is the accepted blast radius for trusted control-plane
   software on a single-operator workstation.
5. **Workers never receive the socket or the proxy endpoint.** Verified by
   the isolation integration test (18) that runs a worker whose only job is
   to try.
6. **Not carried to Kubernetes.** The Kubernetes provider uses the API
   server with a namespaced ServiceAccount. Nothing in the Docker model
   survives that move except the create-request policy, which becomes a Pod
   spec policy.

## Networking

`crucible-workers` is a bridge network with no access to the Compose
internal network (so a worker cannot reach `postgres` or `crucible`).
Egress: policy `network: none` gives `--network none`; `network: policy`
attaches to the bridge where an egress allowlist (package registries,
GitHub, the model provider endpoints each harness needs) is enforced by an
iptables rule set on the bridge in v0.x. Spike S6 confirms each harness's
required endpoints. Kubernetes uses NetworkPolicy for the same.

## Filesystem

Read-only root, tmpfs `/tmp` and `/home/worker` (size-limited), repo mount
rw, identity ro, one credential mount, report dir rw. Nothing else.

## Resource limits

Defaults from policy: 2 CPU, 4 GiB, 512 pids, 20 GiB tmpfs total. Concurrency
cap default 3 workers per provider, 1 per harness while spike S1 is open.
