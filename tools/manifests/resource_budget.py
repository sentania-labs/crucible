"""Render-time resource budget check (issue 93, requirement 5).

Sums the CPU and memory *requests* the rendered `crucible` namespace's own Deployments,
StatefulSets and Jobs carry (the control plane: api, supervisor, postgres, the migration
Job), and adds the `crucible-workers` namespace's ResourceQuota `requests.cpu` /
`requests.memory`, which is deploy/kubernetes's own record of what the configured
concurrency requests at once (26, the ResourceQuota comment). That sum is what the
cluster must have unreserved for Crucible plus one attempt at every running slot before
anything is Pending. Printed always; refused only when a budget is given and exceeded,
so a target with no meaningful budget (kind, or a lab overlay still carrying its
placeholders) can still be rendered without failing this check.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

_MEMORY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_cpu(value: Any) -> float:
    text = str(value).strip()
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


def parse_memory(value: Any) -> float:
    text = str(value).strip()
    for suffix, factor in _MEMORY_UNITS.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * factor
    return float(text)


def _pod_containers(obj: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Every container and init container a Deployment, StatefulSet or Job carries."""
    kind = obj.get("kind")
    if kind not in ("Deployment", "StatefulSet", "Job", "DaemonSet"):
        return
    template = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
    yield from template.get("containers") or []
    yield from template.get("initContainers") or []


def control_plane_requests(objects: list[dict[str, Any]], namespace: str) -> tuple[float, float]:
    """The CPU and memory requests of every container in one namespace's workloads."""
    cpu = 0.0
    memory = 0.0
    for obj in objects:
        if (obj.get("metadata") or {}).get("namespace") != namespace:
            continue
        for container in _pod_containers(obj):
            requests = ((container.get("resources") or {}).get("requests")) or {}
            if "cpu" in requests:
                cpu += parse_cpu(requests["cpu"])
            if "memory" in requests:
                memory += parse_memory(requests["memory"])
    return cpu, memory


def workers_quota_requests(objects: list[dict[str, Any]]) -> tuple[float, float]:
    """`requests.cpu` / `requests.memory` of the workers ResourceQuota: the configured
    concurrency's worth of attempts, already sized for the running policy (26)."""
    for obj in objects:
        if obj.get("kind") != "ResourceQuota":
            continue
        hard = (obj.get("spec") or {}).get("hard") or {}
        if "requests.cpu" not in hard and "requests.memory" not in hard:
            continue
        cpu = parse_cpu(hard["requests.cpu"]) if "requests.cpu" in hard else 0.0
        memory = parse_memory(hard["requests.memory"]) if "requests.memory" in hard else 0.0
        return cpu, memory
    return 0.0, 0.0


def _format_memory(value: float) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f}Gi"
    if value >= 1024**2:
        return f"{value / 1024**2:.2f}Mi"
    return f"{value:.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="a rendered, fully-kustomized YAML file")
    parser.add_argument("--label", default="", help="a name for this target in the printed line")
    parser.add_argument(
        "--cpu-budget", type=float, default=None, help="cores; refuse the target above this"
    )
    parser.add_argument(
        "--memory-budget-gi", type=float, default=None, help="Gi; refuse the target above this"
    )
    args = parser.parse_args(argv)

    objects = [doc for doc in yaml.safe_load_all(args.manifest.read_text()) if doc]
    control_cpu, control_memory = control_plane_requests(objects, "crucible")
    quota_cpu, quota_memory = workers_quota_requests(objects)
    total_cpu = control_cpu + quota_cpu
    total_memory = control_memory + quota_memory

    label = args.label or args.manifest.name
    print(
        f"manifests: {label}: requested {total_cpu:.2f} CPU "
        f"({control_cpu:.2f} control plane + {quota_cpu:.2f} workers at configured "
        f"concurrency), {_format_memory(total_memory)} memory "
        f"({_format_memory(control_memory)} control plane + "
        f"{_format_memory(quota_memory)} workers)"
    )

    over_budget = []
    if args.cpu_budget is not None and total_cpu > args.cpu_budget:
        over_budget.append(
            f"{total_cpu:.2f} CPU requested exceeds the {args.cpu_budget:.2f} budget"
        )
    if args.memory_budget_gi is not None:
        budget_bytes = args.memory_budget_gi * 1024**3
        if total_memory > budget_bytes:
            over_budget.append(
                f"{_format_memory(total_memory)} memory requested exceeds the "
                f"{args.memory_budget_gi:.2f}Gi budget"
            )
    if over_budget:
        for line in over_budget:
            print(f"manifests: {label}: {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
