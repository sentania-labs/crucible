# 08. Execution-provider contracts

A provider owns the environment an attempt runs in. Task, worker, execution,
event, and API contracts are identical across providers; only isolation
strength and mechanics differ. `GET /providers` reports each provider's
capabilities so Foundry can choose knowingly.

## Interface (`crucible/ports/execution.py`)

```python
class ExecutionProvider(Protocol):
    name: ProviderName                   # "fake" | "docker" | "kubernetes" | "hostprocess"
    def capabilities(self) -> ProviderCapabilities: ...
    async def prepare(self, attempt: Attempt, spec: LaunchSpec) -> Workspace: ...
    async def launch(self, ws: Workspace, spec: LaunchSpec) -> Handle: ...
    async def observe(self, h: Handle) -> Observation: ...   # running, exited(code), lost
    async def logs(self, h: Handle, since: Offset) -> AsyncIterator[LogChunk]: ...
    async def collect(self, h: Handle, ws: Workspace) -> CollectedOutputs: ...
    async def terminate(self, h: Handle, mode: Literal["drain", "kill"]) -> None: ...
    async def cleanup(self, ws: Workspace, policy: CleanupPolicy) -> None: ...
    async def reconcile(self) -> list[Handle]: ...          # what is actually running
```

`ProviderCapabilities`: `isolation` (none, process, container, pod),
`network_control` (bool), `resource_limits` (bool), `shared_disk` (bool),
`supports_harnesses` (set), `max_concurrency`.

`Workspace`: the checkout location, the identity mount, the report dir, the
credential mounts, and the checkout lease ID. Two shapes, after Sandcastle:
`shared_disk` (host directory bind-mounted, cheap, Docker) and `isolated`
(provider copies in and out, Kubernetes with a volume, later microVM).

## Fake provider

Deterministic, no model, no containers. Scripted behaviors selected by a
field on the launch spec in tests: succeed with a given report, exit 75,
crash, hang until timeout, vanish (simulate loss), write outside allowed
paths, emit N log lines. Every lifecycle and gate test runs against it.

## Docker provider (local, v0.x)

- Talks to Docker through the socket proxy endpoint, never the raw socket
  (13). API version pinned.
- `prepare`: take the checkout lease first; then `git clone --reference
  <cache> --dissociate` into `<artifact_root>/workspaces/<attempt>/repo`
  (the cache is a bare repository only Crucible reads; nothing shared is
  mounted into a worker); if remote `work_branch` already exists (a retry or
  `needs_more_work`), fetch and check it out, else create it from
  `base_ref`; record which happened as an event; write shims and
  `.git/info/exclude`; install the git credential helper and author
  identity from policy; render the identity bundle to
  `<artifact_root>/workspaces/<attempt>/identity`; create the empty report
  directory at `<artifact_root>/workspaces/<attempt>/report`.
- `launch`: create a container from the allowlisted image with: `--user
  1000:1000`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--read-only` root with tmpfs for `/tmp` and `/home/worker`, memory and
  CPU limits from policy, `--pids-limit`, network per policy (dedicated
  bridge with egress allowlist via the proxy's network, or `none`), mounts:
  repo (rw), identity (ro), one credential dir (ro or narrow rw), report dir
  (rw). Labels: `crucible.attempt`, `crucible.task`, `crucible.owner`.
- `observe`: container inspect; `lost` if the container ID no longer exists.
- `logs`: docker logs with timestamps, since offset.
- `collect`: never runs git or reads worker-written files as the Crucible
  process. It launches a throwaway collector container (the same hardened
  shape as a worker, `--network none`, uid 1000, the repo mounted ro, the
  report dir mounted ro, an output dir mounted rw) that produces: `git diff
  --stat` and the full diff against `base_ref`, the changed-path list, HEAD
  SHA, `git log --format` of `work_branch`, and a copy of the report
  directory. Git in the collector runs with `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`, `-c core.fsmonitor= -c diff.external= -c
  core.pager=cat -c core.hooksPath=/dev/null`. The copy step opens every
  file with `O_NOFOLLOW`, rejects symlinks, hard links, devices, and files
  above the policy size cap, and records each rejection as an event.
  Whether `work_branch` was pushed is checked by `ls-remote` from Crucible
  against the remote, not from the worker's tree. Everything read from the
  tree is data.
- `terminate`: `drain` sends SIGTERM and waits the policy grace; `kill`
  sends SIGKILL.
- `cleanup`: only for attempts whose exit path recorded `logs_drained`;
  remove container; keep or delete the workspace per policy; release the
  lease; remove the per-attempt credential volume.
- `reconcile`: list containers by label; anything with a label but no live
  attempt row is orphaned and removed; any live attempt with no container is
  marked `lost`.

## Kubernetes provider (designed for v1.x)

Same contract. `launch` creates a Job in the worker namespace with a
per-attempt PVC or emptyDir plus init container that performs the checkout,
identity as a ConfigMap (or projected volume from an object store when
bundles exceed ConfigMap limits), credentials as a Secret projected read-only,
`securityContext` mirroring the Docker flags, NetworkPolicy per policy.
`observe` watches Job status. No Docker socket anywhere. Crucible's
ServiceAccount is limited to Jobs, Pods, ConfigMaps, and Secrets in that one
namespace. Local Kubernetes testing uses kind, later than Compose.

## Host-process provider (escape hatch)

Runs the harness as a subprocess of Crucible's own host, in a worktree,
with credential directories referenced from the host home. Isolation level
`process`. Enabled only by explicit config flag and per-task policy
allowance, documented as insecure, intended only for a harness that cannot
run in a container at all. Not used in the default loop; exists so a proven
gap does not stall delivery.
