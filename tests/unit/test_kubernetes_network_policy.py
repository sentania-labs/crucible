"""The per-attempt NetworkPolicy (26, requirement 3 of C8a).

The allowlist source is the one `make proxy-config` uses: the policy's
`egress_allowlist`, the contract's `egress_extra`, the adapter's declared endpoints
(S6), and a local route's exact `endpoint_url` host and port (05b, S16).

A `networking.k8s.io/v1` policy has no deny verb, so 26's explicit denials are the
`except` of the one broad allow, and a role with no egress gets no policy at all: the
namespace's default deny is already the answer for it.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sspec import SpecError
from crucible.ports.execution import CleanupPolicy, ProviderError
from tests.unit.kubernetes_fixtures import build, spec

DENIED_BY_26 = (
    # the cluster's API server and every other service address
    "10.96.0.1",
    # the node network and other namespaces' pod network
    "10.244.3.7",
    "172.17.0.5",
    # link-local, which is where a cloud metadata service lives
    "169.254.169.254",
    # the lab's own private ranges
    "192.168.40.10",
    "10.10.0.1",
)


def rules(policy: dict[str, Any]) -> list[dict[str, Any]]:
    egress: list[dict[str, Any]] = policy["spec"]["egress"]
    return egress


def allows(policy: dict[str, Any], address: str, port: int, protocol: str = "TCP") -> bool:
    """Whether this policy permits one packet. `except` is what makes a denial."""
    wanted = ipaddress.ip_address(address)
    for rule in rules(policy):
        ports = rule.get("ports") or []
        if ports and not any(
            int(p["port"]) == port and str(p.get("protocol", "TCP")) == protocol for p in ports
        ):
            continue
        for destination in rule.get("to") or []:
            block = destination.get("ipBlock")
            if not block:
                continue
            if wanted not in ipaddress.ip_network(block["cidr"]):
                continue
            if any(wanted in ipaddress.ip_network(e) for e in block.get("except") or []):
                continue
            return True
    return False


async def policies(**kwargs: Any) -> dict[str, dict[str, Any]]:
    """Every NetworkPolicy one whole attempt creates, by the role it selects."""
    api, _registry, provider = build()
    launch = spec(**kwargs)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    await provider.collect(handle, workspace, launch)
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    return {
        row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]: row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
    }


@pytest.fixture
async def rendered() -> dict[str, dict[str, Any]]:
    return await policies()


async def test_the_selector_is_this_attempts_pods_of_this_role(
    rendered: dict[str, dict[str, Any]],
) -> None:
    for role, policy in rendered.items():
        assert policy["spec"]["podSelector"]["matchLabels"] == {
            k8sspec.LABEL_ATTEMPT: "01ATTEMPT0000000000000000A",
            k8sspec.LABEL_ROLE: role,
        }
        # No ingress section at all: nothing ever connects to a worker.
        assert policy["spec"]["policyTypes"] == ["Egress"]


async def test_the_collector_and_the_bundle_verifier_get_no_policy_at_all(
    rendered: dict[str, dict[str, Any]],
) -> None:
    """26: no egress for the collector or the bundle verifier. A namespace with a
    default deny needs no object to express that, and an empty policy would be one
    more thing that could be written wrongly."""
    assert k8sspec.ROLE_COLLECTOR not in rendered
    assert k8sspec.ROLE_BUNDLE not in rendered
    assert k8sspec.ROLE_READER not in rendered
    assert k8sspec.ROLE_CLEANER not in rendered


async def test_the_worker_reaches_what_the_allowlist_names_and_nothing_else(
    rendered: dict[str, dict[str, Any]],
) -> None:
    worker = rendered[k8sspec.ROLE_WORKER]
    hosts = worker["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS].split(",")
    # The union of the policy's allowlist and the adapter's declared endpoints (13, S6).
    assert "pypi.org" in hosts
    assert allows(worker, "151.101.0.223", 443)
    # Nothing the allowlist did not name, on any port.
    assert not allows(worker, "203.0.113.9", 443)
    assert not allows(worker, "151.101.0.223", 22)


async def test_the_preparer_reaches_github_only(rendered: dict[str, dict[str, Any]]) -> None:
    """26: the preparer and the publisher do the git traffic, and nothing else."""
    preparer = rendered[k8sspec.ROLE_PREPARER]
    assert preparer["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS] == (
        "api.github.com,github.com"
    )
    assert allows(preparer, "140.82.121.4", 443)
    assert not allows(preparer, "151.101.0.223", 443)


async def test_the_provider_never_adds_github_to_a_worker() -> None:
    """26: GitHub is not reachable from a worker; the preparer and the publisher do the
    git traffic. The provider adds nothing of its own to a worker's destinations, so a
    policy document that does not name GitHub produces a worker that cannot reach it.

    The seeded `default-software` policy does name `github.com` in its
    `egress_allowlist` (05b, "read-only in effect: workers hold no GitHub credential"),
    which is an operator decision in a policy document rather than something this
    provider can or should override. What the provider owes is that the rendered rule
    is exactly the allowlist and nothing wider."""
    rendered = await policies(
        policy={
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": ["pypi.org"]},
            "resources": {"cpus": 2, "memory": "4GiB"},
            "limits": {"grace_seconds": 30},
        }
    )
    worker = rendered[k8sspec.ROLE_WORKER]
    assert not allows(worker, "140.82.121.4", 443)
    assert not allows(worker, "140.82.121.6", 443)
    assert allows(worker, "151.101.0.223", 443)


async def test_a_host_that_does_not_resolve_refuses_the_launch() -> None:
    """13's rule for the same situation: an attempt whose allowlist the egress path
    cannot actually permit is refused, never run with less network than promised."""
    _api, _registry, provider = build(resolver=lambda host: [])
    launch = spec()
    with pytest.raises(ProviderError, match="do not resolve"):
        await provider.prepare(launch)


@pytest.mark.parametrize("address", DENIED_BY_26)
async def test_every_denial_26_names_is_denied_for_every_role(
    rendered: dict[str, dict[str, Any]], address: str
) -> None:
    for role, policy in rendered.items():
        assert not allows(policy, address, 443), f"{role} reached {address}"
        assert not allows(policy, address, 80), f"{role} reached {address}"


async def test_cluster_dns_is_port_53_on_the_dns_address_and_nothing_else(
    rendered: dict[str, dict[str, Any]],
) -> None:
    for policy in rendered.values():
        assert allows(policy, "10.96.0.10", 53, "UDP")
        assert allows(policy, "10.96.0.10", 53, "TCP")
        # 26: nothing else on that address.
        assert not allows(policy, "10.96.0.10", 443)
        assert not allows(policy, "10.96.0.10", 8080)


async def test_ipv6_is_denied_entirely(rendered: dict[str, dict[str, Any]]) -> None:
    """Every rule is an IPv4 ipBlock, so a v6 destination matches nothing."""
    for policy in rendered.values():
        for rule in rules(policy):
            for destination in rule.get("to") or []:
                assert ipaddress.ip_network(destination["ipBlock"]["cidr"]).version == 4


async def test_a_hostname_local_route_resolves_to_exact_addresses_and_port() -> None:
    """C10: a configured gateway name may resolve into a private range, but the
    generated exception is still only the resolved address and configured port."""

    def resolver(host: str) -> list[str]:
        return ["10.10.0.42/32"] if host == "llm.apps.int.sentania.net" else ["151.101.0.223/32"]

    api, _registry, provider = build(resolver=resolver)
    launch = spec(endpoint="local", endpoint_url="https://llm.apps.int.sentania.net:8443/v1")
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    rendered = {
        row["body"]["spec"]["podSelector"]["matchLabels"][k8sspec.LABEL_ROLE]: row["body"]
        for row in api.created
        if row["kind"] == "networkpolicies"
    }
    worker = rendered[k8sspec.ROLE_WORKER]
    assert allows(worker, "10.10.0.42", 8443)
    assert not allows(worker, "10.10.0.42", 443)
    assert not allows(worker, "10.10.0.43", 8443)
    await provider.cleanup(workspace, CleanupPolicy.DELETE, launch)
    assert handle


async def test_a_direct_private_local_address_is_refused() -> None:
    """C10 keeps names as the trust anchor and refuses a raw cluster or lab address."""
    _api, _registry, provider = build()
    launch = spec(endpoint="local", endpoint_url="http://10.10.0.42:8000/v1")
    workspace = await provider.prepare(launch)
    with pytest.raises((ProviderError, SpecError), match="directly names an address"):
        await provider.launch(workspace, launch)


async def test_network_none_creates_no_policy_and_therefore_no_egress() -> None:
    rendered = await policies(network="none")
    assert k8sspec.ROLE_WORKER not in rendered


async def test_the_login_role_gets_the_harness_login_endpoints() -> None:
    _api, _registry, provider = build()
    plan = provider._egress_plan(spec(harness="codex"), k8sspec.ROLE_LOGIN)
    assert "auth.openai.com" in plan.hosts


async def test_the_verifier_gets_the_policys_registries_and_not_the_model_endpoints() -> None:
    _api, _registry, provider = build()
    plan = provider._egress_plan(spec(), k8sspec.ROLE_VERIFIER)
    assert set(plan.hosts) == {"pypi.org"}


# ----- the denials are real for a resolved allowlist -----------------------


async def test_a_host_that_resolves_into_a_denied_range_refuses_the_launch() -> None:
    """26: the denials are the `except` of every allow, so a name that resolves into a
    denied range cannot open one.

    An allowed destination is a `/32`, so asking whether a denied `/8` sits inside it is
    always false; the check that matters is the other direction. Without it, a vendor
    host whose record points at the API server's ClusterIP, at cloud metadata, or into
    the lab's own ranges becomes an allow rule for exactly that address."""
    for address in ("169.254.169.254/32", "10.43.0.1/32", "192.168.40.10/32"):
        _api, _registry, provider = build(resolver=lambda _host, a=address: [a])
        launch = spec()
        with pytest.raises(ProviderError, match="denies"):
            await provider.prepare(launch)


async def test_the_rendered_rules_never_name_a_denied_address() -> None:
    """The property the parametrised denial test above states, asserted against the
    rendered object rather than against a fixture that could not produce one."""
    rendered = await policies()
    for policy in rendered.values():
        for rule in rules(policy):
            for destination in rule.get("to") or []:
                block = destination["ipBlock"]
                if block["cidr"] == "0.0.0.0/0":
                    continue
                network = ipaddress.ip_network(block["cidr"])
                for denied in k8sspec.DEFAULT_DENIED_CIDRS:
                    if k8sspec.denied_by(block["cidr"], [denied]) is not None:
                        # Cluster DNS is the one address 26 allows inside a denied
                        # range, on port 53 and nothing else.
                        assert network.version == 4
                        assert block["cidr"] == "10.96.0.10/32", block
                        assert rule["ports"] == [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ]


async def test_a_worker_never_reaches_the_git_remote_whatever_the_policy_names() -> None:
    """26 is unconditional: GitHub is not reachable from a worker; the preparer and the
    publisher do the git traffic. The seeded default policy names `github.com` in its
    `egress_allowlist` for the roles that need it, so the worker role subtracts it
    rather than trusting the list."""
    rendered = await policies(
        policy={
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {
                "mode": "egress-proxy",
                "egress_allowlist": ["github.com", "api.github.com", "pypi.org"],
            },
            "resources": {"cpus": 2, "memory": "4GiB"},
            "limits": {"grace_seconds": 30},
        }
    )
    worker = rendered[k8sspec.ROLE_WORKER]
    assert not allows(worker, "140.82.121.4", 443)
    assert not allows(worker, "140.82.121.6", 443)
    assert allows(worker, "151.101.0.223", 443)
    # What was granted is what the annotation records.
    assert "github.com" not in worker["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS]
    # The preparer still does the git traffic.
    assert allows(rendered[k8sspec.ROLE_PREPARER], "140.82.121.4", 443)


async def test_the_verifier_does_not_get_the_git_remote_either() -> None:
    _api, _registry, provider = build()
    plan = provider._egress_plan(
        spec(
            policy={
                "images": {"allowlist": ["crucible-worker:*"]},
                "network": {
                    "mode": "egress-proxy",
                    "egress_allowlist": ["github.com", "pypi.org"],
                },
                "resources": {"cpus": 2, "memory": "4GiB"},
                "limits": {"grace_seconds": 30},
            }
        ),
        k8sspec.ROLE_VERIFIER,
    )
    assert set(plan.hosts) == {"pypi.org"}
