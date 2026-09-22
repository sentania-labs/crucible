"""An in-memory Kubernetes API for the unit and integration tiers (18, 26).

C8a proves the Kubernetes provider without a cluster: every object it creates, every
Pod state it reads, every log it pulls and every byte it reads back off a workspace claim
is answered here. What a real cluster adds is scheduling, admission, a CNI and a kubelet,
and those are C8b's `make e2e-kind` tier; what this fake exists to prove is that the
provider asks for the right objects and reads the answers correctly.

The shape is the fake execution provider's (`fake.py`): the image tag selects a scripted
behaviour, and the roles of an attempt act it out. A Job's Pod terminates at once for
every role but the worker, which stays Running for a scripted number of observations, so
the lifecycle tests can tick a supervisor through a run the way they do against the other
two providers.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from crucible.adapters.execution.fake import (
    BEHAVIORS,
    REPORTING_BEHAVIORS,
    REVIEW_BEHAVIORS,
    changed_paths,
    default_report,
    default_review_report,
    synthetic_diff,
    synthetic_head_sha,
)
from crucible.adapters.execution.k8sapi import ExecResult, KubernetesApiError, LogFrame
from crucible.adapters.execution.k8sregistry import RegistryError
from crucible.adapters.execution.k8sspec import (
    CONTAINER_NAME,
    LABEL_ATTEMPT,
    LABEL_ROLE,
    ROLE_BUNDLE,
    ROLE_CANARY,
    ROLE_CLEANER,
    ROLE_COLLECTOR,
    ROLE_PREPARER,
    ROLE_READER,
    ROLE_VERIFIER,
    ROLE_WORKER,
)
from crucible.ports.execution import ImageInfo

_TAG = re.compile(r"^.*:.*fake-(?P<behavior>[a-z]+(?:-[a-z]+)*?)(?:-(?P<n>\d+))?$")


class FakeRegistry:
    """The registry half: a reference resolves to a digest and the two labels a launch
    is refused on (07, 13)."""

    def __init__(self, api: FakeKubernetesApi | None = None) -> None:
        # A real registry answers a tag with a digest and the tag is gone from the
        # reference the Pod runs. The fake records the mapping back so a role can still
        # read the behaviour a test spelled in the tag.
        self._api = api
        self.auths: dict[str, Any] = {}
        self._images: dict[str, ImageInfo] = {}
        self._tags: dict[str, list[str]] = {}
        self.unavailable: set[str] = set()

    def register(
        self,
        reference: str,
        *,
        harness: str = "script-harness",
        version: str = "1.0.0",
        labels: Mapping[str, str] | None = None,
    ) -> ImageInfo:
        digest = "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
        resolved = {"crucible.harness": harness, "crucible.harness_version": version}
        if labels is not None:
            resolved = dict(labels)
        info = ImageInfo(
            reference=f"{reference.split(':', maxsplit=1)[0]}@{digest}",
            digest=digest,
            harness=resolved.get("crucible.harness"),
            harness_version=resolved.get("crucible.harness_version"),
            labels=resolved,
        )
        self._images[reference] = info
        if self._api is not None:
            self._api.image_tags[info.reference] = reference
        repository = reference.rsplit(":", 1)[0]
        self._tags.setdefault(repository, []).append(reference.rsplit(":", 1)[-1])
        return info

    def resolve(self, reference: str) -> ImageInfo:
        if reference in self.unavailable:
            raise RegistryError(f"{reference} is not in the registry")
        info = self._images.get(reference)
        if info is None:
            raise RegistryError(f"{reference} is not in the registry")
        return info

    def list_tags(self, repository: str) -> list[str]:
        return list(self._tags.get(repository, []))


@dataclass
class _Worker:
    """The scripted worker of one attempt, exactly as `fake.py` scripts it."""

    behavior: str
    remaining: int
    observations: int = 0
    exit_code: int | None = None
    terminated: bool = False
    drained: bool = False
    kills: int = 0


@dataclass(frozen=True)
class _SpecStub:
    """What `fake.py`'s report builders read off a launch spec."""

    attempt_id: str
    external_id: str
    contract: dict[str, Any]


@dataclass
class _Object:
    kind: str
    name: str
    body: dict[str, Any]
    deleted: bool = False


@dataclass
class FakeKubernetesApi:
    """Everything the provider calls on `KubernetesClient`, in memory.

    The workspace claim is a dict of path to bytes, which is what makes the reader Pod's
    `tar` and its single-file read answerable without a filesystem."""

    namespace: str = "crucible-workers"
    # A canary that cannot reach the API server and a node with a pod PID limit: the
    # namespace 26 asks lab-admin for. A test flips either to prove the refusal.
    egress_enforced: bool = True
    pod_pid_limit: int | None = 4096
    node_name: str = "lab-node-1"
    quota_jobs: int | None = None

    objects: dict[tuple[str, str], _Object] = field(default_factory=dict)
    claims: dict[str, dict[str, bytes]] = field(default_factory=dict)
    logs: dict[str, list[str]] = field(default_factory=dict)
    workers: dict[str, _Worker] = field(default_factory=dict)
    scripts: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Which tag each resolved digest came from, so a role can read the behaviour a test
    # spelled in the tag (a registry answers with a digest and the tag is gone).
    image_tags: dict[str, str] = field(default_factory=dict)
    # What an attempt's roles act out when its image tag names no behaviour. The
    # integration tier sets it per case, because the image a routed task runs is the
    # promoted one and carries no behaviour in its tag.
    default_behavior: tuple[str, int] | None = None
    # A test flips this to make the next create fail, or to make a Pod unschedulable.
    refuse_create: set[str] = field(default_factory=set)
    pending_forever: set[str] = field(default_factory=set)
    created: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)

    # ----- test controls ------------------------------------------------

    def script(self, attempt_id: str, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self.scripts[attempt_id] = (behavior, after)

    def script_all(self, behavior: str, *, after: int = 1) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown fake behavior {behavior!r}")
        self.default_behavior = (behavior, after)

    def remove_pod_out_of_band(self, attempt_id: str) -> None:
        """An operator, an eviction, or a node that went away (26)."""
        for key, obj in list(self.objects.items()):
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if key[0] == "pods" and labels.get(LABEL_ATTEMPT) == attempt_id:
                del self.objects[key]

    def evict(self, attempt_id: str) -> None:
        for key, obj in self.objects.items():
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if key[0] == "pods" and labels.get(LABEL_ATTEMPT) == attempt_id:
                obj.body["status"] = {"phase": "Failed", "reason": "Evicted"}

    def object_names(self, kind: str) -> list[str]:
        return sorted(name for (k, name) in self.objects if k == kind)

    def secret_exists(self, name: str) -> bool:
        return ("secrets", name) in self.objects

    def put_harness_secret(self, name: str, data: Mapping[str, bytes]) -> None:
        self.objects[("secrets", name)] = _Object(
            "secrets",
            name,
            {
                "metadata": {"name": name, "namespace": self.namespace},
                "data": {k: base64.b64encode(v).decode("ascii") for k, v in data.items()},
            },
        )

    def harness_secret(self, name: str) -> dict[str, bytes]:
        obj = self.objects.get(("secrets", name))
        if obj is None:
            return {}
        return {k: base64.b64decode(v) for k, v in (obj.body.get("data") or {}).items()}

    # ----- the client surface -------------------------------------------

    def version(self) -> str:
        return "v1.31.0"

    def create(self, kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
        metadata = dict(body.get("metadata") or {})
        name = str(metadata.get("name", ""))
        if kind in self.refuse_create:
            raise KubernetesApiError(500, f"the fake refuses to create a {kind}")
        if (kind, name) in self.objects:
            raise KubernetesApiError(409, f"{kind}/{name} already exists")
        stored: dict[str, Any] = json.loads(json.dumps(dict(body)))
        self.objects[(kind, name)] = _Object(kind, name, stored)
        self.created.append({"kind": kind, "name": name, "body": stored})
        if kind == "persistentvolumeclaims":
            self.claims.setdefault(name, {})
        if kind == "jobs":
            self._start_job(stored)
        if kind == "pods":
            self._start_pod(stored, owner=None)
        return stored

    def get(self, kind: str, name: str) -> dict[str, Any]:
        obj = self.objects.get((kind, name))
        if obj is None:
            raise KubernetesApiError(404, f"{kind}/{name} not found")
        if kind == "pods":
            self._advance(obj)
        return obj.body

    def list_objects(
        self, kind: str, *, label_selector: str | None = None, field_selector: str | None = None
    ) -> list[dict[str, Any]]:
        wanted = _parse_selector(label_selector)
        out: list[dict[str, Any]] = []
        for (stored_kind, _), obj in list(self.objects.items()):
            if stored_kind != kind:
                continue
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            if any(labels.get(k) != v for k, v in wanted.items() if v is not None):
                continue
            if any(k not in labels for k, v in wanted.items() if v is None):
                continue
            if kind == "pods":
                self._advance(obj)
            out.append(obj.body)
        return out

    def delete(
        self,
        kind: str,
        name: str,
        *,
        grace_period_seconds: int | None = None,
        propagation: str = "Background",
    ) -> None:
        obj = self.objects.get((kind, name))
        if obj is None:
            return
        if kind == "pods":
            labels = (obj.body.get("metadata") or {}).get("labels") or {}
            worker = self.workers.get(str(labels.get(LABEL_ATTEMPT, "")))
            if (
                labels.get(LABEL_ROLE) == ROLE_WORKER
                and worker is not None
                and grace_period_seconds
                and worker.behavior in ("hang", "immortal")
            ):
                # A worker that ignores SIGTERM: the kubelet waits the grace period and
                # the Pod is still there until the kill deletes it with grace zero.
                worker.drained = True
                obj.body.setdefault("metadata", {})["deletionTimestamp"] = "2026-09-21T00:00:00Z"
                return
        self.deleted.append((kind, name))
        del self.objects[(kind, name)]
        if kind == "jobs" and propagation != "Orphan":
            for key, other in list(self.objects.items()):
                labels = (other.body.get("metadata") or {}).get("labels") or {}
                if key[0] == "pods" and labels.get("job-name") == name:
                    del self.objects[key]
        if kind == "persistentvolumeclaims":
            self.claims.pop(name, None)

    def patch(self, kind: str, name: str, body: Mapping[str, Any]) -> dict[str, Any]:
        obj = self.objects.get((kind, name))
        if obj is None:
            raise KubernetesApiError(404, f"{kind}/{name} not found")
        _merge(obj.body, dict(body))
        return obj.body

    def pod_log(
        self,
        name: str,
        *,
        container: str | None = None,
        since_time: str | None = None,
        timestamps: bool = True,
        timeout: float | None = None,
    ) -> list[LogFrame]:
        lines = self.logs.get(name, [])
        if not timestamps:
            lines = [line.partition(" ")[2] for line in lines]
        if since_time:
            lines = [line for line in lines if line[: len(since_time)] >= since_time]
        payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        return [LogFrame("stdout", payload)] if payload else []

    def pod_exec(
        self,
        name: str,
        command: Sequence[str],
        *,
        container: str | None = None,
        timeout: float | None = None,
        limit: int = 0,
    ) -> ExecResult:
        obj = self.objects.get(("pods", name))
        if obj is None:
            raise KubernetesApiError(404, f"pods/{name} not found")
        claim = self._claim_of(obj)
        script = command[-1]
        if "tar cf -" in script:
            return ExecResult(_tar(claim), b"", 0)
        match = re.match(r"^p='(?P<path>[^']*)'", script)
        if match is None:
            return ExecResult(b"", b"the fake has no answer for this command\n", 1)
        wanted = match.group("path").split("/crucible/work/", 1)[-1]
        data = claim.get(wanted)
        if data is None:
            return ExecResult(b"absent\n", b"", 0)
        return ExecResult(b"ok\n" + base64.b64encode(data), b"", 0)

    # ----- acting out a role ---------------------------------------------

    def _claim_of(self, obj: _Object) -> dict[str, bytes]:
        for volume in (obj.body.get("spec") or {}).get("volumes") or []:
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                return self.claims.setdefault(str(claim), {})
        return {}

    def _start_job(self, job: Mapping[str, Any]) -> None:
        name = str((job.get("metadata") or {}).get("name", ""))
        template = (job.get("spec") or {}).get("template") or {}
        labels = dict((template.get("metadata") or {}).get("labels") or {})
        labels["job-name"] = name
        pod_name = f"{name}-abc12"
        pod: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.namespace, "labels": labels},
            "spec": {**dict(template.get("spec") or {}), "nodeName": self.node_name},
        }
        self.objects[("pods", pod_name)] = _Object("pods", pod_name, pod)
        self._start_pod(pod, owner=name)

    def _start_pod(self, pod: Mapping[str, Any], owner: str | None) -> None:
        metadata = pod.get("metadata") or {}
        name = str(metadata.get("name", ""))
        labels = metadata.get("labels") or {}
        role = str(labels.get(LABEL_ROLE, ""))
        attempt_id = str(labels.get(LABEL_ATTEMPT, ""))
        obj = self.objects[("pods", name)]
        if name in self.pending_forever or attempt_id in self.pending_forever:
            obj.body["status"] = {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/1 nodes are available",
                    }
                ],
            }
            return
        if role in (ROLE_READER, ROLE_WORKER):
            obj.body["status"] = _running(self.node_name)
        else:
            obj.body["status"] = _running(self.node_name)
        handler = {
            ROLE_PREPARER: self._act_preparer,
            ROLE_COLLECTOR: self._act_collector,
            ROLE_BUNDLE: self._act_bundle,
            ROLE_VERIFIER: self._act_verifier,
            ROLE_CLEANER: self._act_cleaner,
            ROLE_CANARY: self._act_canary,
            ROLE_WORKER: self._act_worker,
        }.get(role)
        if handler is not None:
            handler(obj, attempt_id)

    def _behavior(self, attempt_id: str, obj: _Object) -> tuple[str, int]:
        scripted = self.scripts.get(attempt_id)
        if scripted is not None:
            return scripted
        image = ""
        for container in (obj.body.get("spec") or {}).get("containers") or []:
            image = str(container.get("image", ""))
        match = _TAG.match(self.image_tags.get(image, image).split("@", 1)[0])
        if match is None or match.group("behavior") not in BEHAVIORS:
            return self.default_behavior or ("succeed", 1)
        return match.group("behavior"), int(match.group("n") or 1)

    def _finish(self, obj: _Object, code: int, *, reason: str = "Completed") -> None:
        obj.body["status"] = {
            "phase": "Succeeded" if code == 0 else "Failed",
            "containerStatuses": [
                {
                    "name": CONTAINER_NAME,
                    "state": {"terminated": {"exitCode": code, "reason": reason}},
                }
            ],
        }

    def _act_canary(self, obj: _Object, attempt_id: str) -> None:
        name = obj.name
        pid = "none" if self.pod_pid_limit is None else str(self.pod_pid_limit)
        self.logs[name] = [
            f"crucible-canary.api={'unreachable' if self.egress_enforced else 'reachable'}",
            f"crucible-canary.pids={pid}",
            "crucible-canary.done=1",
        ]
        self._finish(obj, 0)

    def _act_preparer(self, obj: _Object, attempt_id: str) -> None:
        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        if behavior == "prepare-fails":
            self.logs[obj.name] = ["the fake preparer could not clone"]
            self._finish(obj, 3, reason="Error")
            return
        claim["output/prepared-head.txt"] = (synthetic_head_sha(attempt_id) + "\n").encode()
        claim["output/started-from.txt"] = b"main\n"
        claim["repo/.git/HEAD"] = b"ref: refs/heads/crucible\n"
        self._finish(obj, 0)

    def _spec_of(self, attempt_id: str) -> _SpecStub | None:
        """What the attempt's roles know about the contract: the identity ConfigMap the
        provider created, which is exactly what the worker itself was given (06)."""
        obj = self.objects.get(("configmaps", f"identity-{attempt_id.lower()}"))
        if obj is None:
            return None
        raw = (obj.body.get("data") or {}).get("contract.json")
        if not raw:
            return None
        contract = json.loads(str(raw))
        return _SpecStub(
            attempt_id=attempt_id,
            external_id=str(contract.get("external_id", "")),
            contract=contract,
        )

    def _act_collector(self, obj: _Object, attempt_id: str) -> None:
        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        spec = self._spec_of(attempt_id)
        head = synthetic_head_sha(attempt_id)
        contract = dict(getattr(spec, "contract", {}) or {})
        repository = contract.get("repository", {})
        paths = changed_paths(contract, behavior)
        claim["output/head.txt"] = (head + "\n").encode()
        claim["output/branch.txt"] = (str(repository.get("work_branch", "")) + "\n").encode()
        claim["output/commits.txt"] = b"0\n" if behavior == "no-commits" else b"1\n"
        claim["output/changed.txt"] = ("\n".join(paths) + "\n").encode()
        claim["output/diff.patch"] = synthetic_diff(paths, behavior).encode()
        claim["output/commit-paths.txt"] = ("\n".join(paths) + "\n").encode()
        claim["output/log.txt"] = (
            b"" if behavior == "no-commits" else f"{head}\x1fa fake commit\x1ffake\x1e".encode()
        )
        claim["output/work_branch.bundle"] = b"fake bundle bytes"
        claim["output/copy-rejections.tsv"] = b""
        claim["output/collector.ok"] = b"done\n"
        claim["output/bundle.log"] = b""
        report = self._report_for(spec, behavior, head)
        if report is not None:
            claim["output/report/report.yaml"] = report
        if behavior == "blocked":
            claim["output/report/blocked.md"] = (
                f"# Blocked\n\nThe fake worker for {attempt_id} needs a decision.\n"
            ).encode()
        self._finish(obj, 0)

    def _report_for(self, spec: Any, behavior: str, head: str) -> bytes | None:
        import yaml  # noqa: PLC0415

        if spec is None:
            return None
        if behavior == "bad-report":
            return b"title: c5: live run\nsummary: x\n"
        if behavior in REVIEW_BEHAVIORS:
            verdict = "approve" if behavior == "review" else "request_changes"
            return yaml.safe_dump(default_review_report(spec, head, verdict)).encode()
        if behavior in REPORTING_BEHAVIORS:
            return yaml.safe_dump(default_report(spec, head, behavior)).encode()
        return None

    def _act_bundle(self, obj: _Object, attempt_id: str) -> None:
        self._finish(obj, 0)

    def _act_verifier(self, obj: _Object, attempt_id: str) -> None:
        from crucible.adapters.execution.scripts import encode_check_id  # noqa: PLC0415

        behavior, _ = self._behavior(attempt_id, obj)
        claim = self._claim_of(obj)
        spec = self._spec_of(attempt_id)
        checks = [
            check
            for check in (getattr(spec, "contract", {}) or {}).get("required_verification", [])
            if str(check.get("kind", "command")) == "command"
        ]
        for index, check in enumerate(checks):
            expect = int(check.get("expect_exit", 0))
            failed = behavior == "verification-fails" and index == 0
            safe = encode_check_id(str(check.get("id")))
            claim[f"verify/{safe}.exit"] = f"{expect + 1 if failed else expect}\n".encode()
            claim[f"verify/{safe}.log"] = f"fake verifier re-ran {check.get('command')!r}".encode()
        self._finish(obj, 0)

    def _act_cleaner(self, obj: _Object, attempt_id: str) -> None:
        claim = self._claim_of(obj)
        script = ""
        for container in (obj.body.get("spec") or {}).get("containers") or []:
            script = str((container.get("command") or ["", "", ""])[-1])
        for leaf in re.findall(r'"/crucible/work/([^"]+)"', script):
            for path in [p for p in claim if p == leaf or p.startswith(f"{leaf}/")]:
                del claim[path]
        self._finish(obj, 0)

    def _act_worker(self, obj: _Object, attempt_id: str) -> None:
        behavior, after = self._behavior(attempt_id, obj)
        self.workers[attempt_id] = _Worker(behavior=behavior, remaining=after)
        self.logs[obj.name] = [
            f"{_stamp(0)} fake worker {behavior} start",
        ]

    def _advance(self, obj: _Object) -> None:
        """One observation of a worker Pod. Every other role is already terminal."""
        labels = (obj.body.get("metadata") or {}).get("labels") or {}
        if labels.get(LABEL_ROLE) != ROLE_WORKER:
            return
        attempt_id = str(labels.get(LABEL_ATTEMPT, ""))
        worker = self.workers.get(attempt_id)
        if worker is None or worker.terminated:
            return
        if worker.behavior in ("hang", "immortal"):
            return
        worker.observations += 1
        if worker.observations < worker.remaining:
            return
        if worker.behavior == "vanish":
            worker.terminated = True
            self.objects.pop(("pods", obj.name), None)
            return
        code = {
            "blocked": 75,
            "blocked-nofile": 75,
            "crash": 1,
            "oom": 137,
            "environment": 70,
            "quota": 1,
        }.get(worker.behavior, 0)
        worker.terminated = True
        worker.exit_code = code
        reason = "OOMKilled" if worker.behavior == "oom" else "Completed"
        self.logs.setdefault(obj.name, []).append(f"{_stamp(1)} fake worker exit {code}")
        if worker.behavior == "quota":
            self.logs[obj.name].append(
                f'{_stamp(2)} {{"type":"turn.failed","error":{{"code":"usage_limit_reached"}}}}'
            )
        self._finish(obj, code, reason=reason)


# ----- helpers -----------------------------------------------------------


def _running(node: str) -> dict[str, Any]:
    return {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [{"name": CONTAINER_NAME, "state": {"running": {}}}],
        "hostIP": "10.10.0.1",
        "nodeName": node,
    }


def _stamp(offset: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_760_000_000 + offset)) + ".000000000Z"


def _parse_selector(selector: str | None) -> dict[str, str | None]:
    if not selector:
        return {}
    out: dict[str, str | None] = {}
    for part in selector.split(","):
        key, sep, value = part.partition("=")
        out[key.strip()] = value.strip() if sep else None
    return out


def _merge(target: dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _tar(claim: Mapping[str, bytes]) -> bytes:
    """What the reader Pod's `tar cf -` produces off the claim, minus `output/tree`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in sorted(claim.items()):
            if not path.startswith(("output/", "verify/")) or path.startswith("output/tree/"):
                continue
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


__all__ = ["FakeKubernetesApi", "FakeRegistry"]
