"""The policy the e2e tier uploads (05b).

It is the default policy with two changes the script harness needs: the required
checks are shell scripts the throwaway repository carries (the worker image has no
make), and the image allowlist admits the script-harness image.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = _ROOT / "examples" / "policies" / "default-software.yaml"


E2E_ROUTING = "e2e-routing"


def e2e_routing_document() -> dict[str, Any]:
    """A routing policy with one entry: the script harness, no model, no cost (05b)."""
    return {
        "schema_version": "1.0",
        "name": E2E_ROUTING,
        "version": 1,
        "tiers": {
            "trivial": {"allowed_capability": ["small"], "prefer": ["small"]},
            "standard": {"allowed_capability": ["small"], "prefer": ["small"]},
            "complex": {"allowed_capability": ["small"], "prefer": ["small"]},
        },
        "models": [
            {
                "id": "none",
                "harness": "script-harness",
                "endpoint": "subscription",
                "capability": "small",
                "cost": "none",
                "speed": "fast",
                "pool": "e2e",
                "weight": 1,
                "enabled": True,
            }
        ],
        "pools": {"e2e": {"window": "1h", "budget_units": "attempts", "soft_limit": 0}},
        "rotation": {
            "strategy": "weighted-least-recent",
            "quality_feedback": False,
            "quality_window": 10,
        },
    }


def e2e_policy_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    document["name"] = "e2e-script"
    document["version"] = 1
    document["description"] = "The e2e tier's script harness on the Docker provider (18)."
    document["repository"]["required_checks"] = [
        "sh checks/lint.sh",
        "sh checks/test.sh",
        "sh checks/scan.sh",
    ]
    document["images"]["allowlist"] = ["crucible-worker:*"]
    document["limits"]["grace_seconds"] = 5
    document["limits"]["timeout_seconds"] = {"min": 5, "max": 3600, "default": 600}
    document["resources"] = {"cpus": 2, "memory": "1GiB", "pids": 256, "tmpfs_total": "1GiB"}
    document["network"]["egress_allowlist"] = ["github.com"]
    document["routing"] = {"policy": {"name": E2E_ROUTING, "version": 1}}
    document["concurrency"]["per_harness"] = {
        **document["concurrency"]["per_harness"],
        "script-harness": 1,
    }
    document["cleanup"]["workspace_on_success"] = "keep_diff_only"
    document.update(overrides)
    return document
