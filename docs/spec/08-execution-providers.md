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
- `prepare`: clone or fetch the repository into
  `<artifact_root>/workspaces/<attempt>/repo` on the host (bare-cache plus
  worktree for speed), check out `base_ref`, create `work_branch`, write
  shims, write `.git/info/exclude`, take the checkout lease, render the
  identity bundle to `<artifact_root>/workspaces/<attempt>/identity`.
- `launch`: create a container from the allowlisted image with: `--user
  1000:1000`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--read-only` root with tmpfs for `/tmp` and `/home/worker`, memory and
  CPU limits from policy, `--pids-limit`, network per policy (dedicated
  bridge with egress allowlist via the proxy's network, or `none`), mounts:
  repo (rw), identity (ro), one credential dir (ro or narrow rw), report dir
  (rw). Labels: `crucible.attempt`, `crucible.task`, `crucible.owner`.
- `observe`: container inspect; `lost` if the container ID no longer exists.
- `logs`: docker logs with timestamps, since offset.
- `collect`: read report dir, `git diff --stat` and full diff against
  `base_ref`, changed-path list, HEAD SHA, whether `work_branch` was pushed
  (ls-remote), transcript.
- `terminate`: `drain` sends SIGTERM and waits the policy grace; `kill`
  sends SIGKILL.
- `cleanup`: remove container; keep or delete the workspace per policy;
  release the lease.
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
