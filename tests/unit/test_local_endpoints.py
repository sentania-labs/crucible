from __future__ import annotations

import pytest

from crucible.application.proxy_config import enabled_local_endpoints, worker_proxy_config
from crucible.ports.execution import LaunchSpec
from crucible.ports.harness import LaunchContext


def test_launch_contracts_require_a_local_url_and_forbid_a_subscription_url() -> None:
    with pytest.raises(ValueError, match="requires endpoint_url"):
        LaunchContext(
            attempt_id="attempt",
            model="model",
            effort=None,
            timeout_seconds=30,
            identity_mount="/identity",
            report_mount="/report",
            repo_mount="/repo",
            endpoint="local",
        )
    with pytest.raises(ValueError, match="forbids endpoint_url"):
        LaunchSpec(
            attempt_id="attempt",
            task_id="task",
            external_id="EX-1",
            role="work",
            harness="codex",
            model="model",
            image="image",
            timeout_seconds=30,
            contract={},
            endpoint="subscription",
            endpoint_url="http://example.invalid/v1",
        )


def test_proxy_allows_only_the_exact_local_destination_and_plain_http_port() -> None:
    routing = {
        "models": [
            {
                "endpoint": "local",
                "endpoint_url": "http://192.0.2.41:11434/v1",
                "enabled": True,
            },
            {
                "endpoint": "local",
                "endpoint_url": "http://192.0.2.99:11434/v1",
                "enabled": False,
            },
        ]
    }
    assert enabled_local_endpoints([routing]) == ["http://192.0.2.41:11434/v1"]
    config = worker_proxy_config("10.88.0.0/24", ["github.com"], [routing])
    assert "acl local_destination_0 dst 192.0.2.41/32" in config
    assert "acl local_port_0 port 11434" in config
    assert "acl Safe_ports port 11434" in config
    assert "acl SSL_ports port 11434" not in config
    assert "192.0.2.99" not in config
    unsafe = config.index("http_access deny !Safe_ports")
    connect = config.index("http_access deny CONNECT !SSL_ports")
    local = config.index("http_access allow workers local_destination_0 local_port_0")
    final = config.index("http_access deny all")
    assert unsafe < connect < local < final


def test_proxy_rejects_https_local_destinations() -> None:
    with pytest.raises(ValueError, match="plain HTTP"):
        worker_proxy_config(
            "10.88.0.0/24",
            [],
            [
                {
                    "models": [
                        {
                            "endpoint": "local",
                            "endpoint_url": "https://spark.example.invalid/v1",
                            "enabled": True,
                        }
                    ]
                }
            ],
        )
