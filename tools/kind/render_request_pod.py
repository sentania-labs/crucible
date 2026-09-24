"""Render one bare Pod through k8sspec, for the requests-below-limits kind proof (issue
93). Not part of the provider's own code path: the provider always renders a Job or a
bare Pod through `kubernetes.py`, but this proof only needs the container's `resources`
block the way 26 defines it, applied directly against a namespace whose ResourceQuota
stands in for the lab's tight budget.
"""

from __future__ import annotations

import argparse
import json

from crucible.adapters.execution import k8sspec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--cpus", type=float, default=2)
    parser.add_argument("--memory", default="256Mi")
    parser.add_argument("--cpu-request-fraction", type=float, default=1.0)
    args = parser.parse_args()

    limits = k8sspec.limits_from_policy(
        {
            "resources": {
                "cpus": args.cpus,
                "memory": args.memory,
                "cpu_request_fraction": args.cpu_request_fraction,
            },
            "limits": {"grace_seconds": 1},
        },
        default_ephemeral="64Mi",
        default_tmpfs_mb=8,
    )
    pod = k8sspec.bare_pod(
        name=args.name,
        namespace=args.namespace,
        object_labels={k8sspec.LABEL_OWNER: "crucible-verify"},
        pod=k8sspec.pod_spec(
            k8sspec.PodRequest(
                role="verify",
                image="busybox:1.36",
                command=["sh", "-c", "sleep 3600"],
                limits=limits,
                mounts=k8sspec.base_mounts(),
                volumes=k8sspec.base_volumes(limits),
            )
        ),
    )
    print(json.dumps(pod))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
