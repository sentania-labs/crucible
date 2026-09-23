"""Where a Kubernetes worker's DNS and in-cluster model endpoint live (26, crucible#91).

A CNI that translates a service address to its backend pods before it evaluates policy
(Cilium with kube-proxy replacement) never matches an `ipBlock` on a ClusterIP or a
LoadBalancer address. What it does match is a selector on the backends: their namespace
and their pod labels. This module is that selector pair as a document an administrator
edits, and the one place its shape is checked. It is pure, so the admin service, the
provider and the renderer all check the same rules.

The document:

    {"dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
     "local_endpoint": {"namespace": "", "pod_labels": {}, "port": 0}}

An empty `dns.namespace` means the resolver is allowed by its service address alone.
An empty `local_endpoint.namespace` means the local model endpoint is outside the
cluster and keeps its resolved-address rule. A `port` of 0 means the endpoint URL's own
port, which is right when the Service's port and its pods' port are the same.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SETTING_NAME = "kubernetes.egress"

DEFAULT_DNS_NAMESPACE = "kube-system"
DEFAULT_DNS_POD_LABELS: tuple[tuple[str, str], ...] = (("k8s-app", "kube-dns"),)

_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")


def namespace_problem(namespace: str) -> str | None:
    if not _NAMESPACE.match(namespace):
        return f"{namespace!r} is not a namespace name"
    return None


def label_problem(key: str, value: str) -> str | None:
    prefix, _, name = key.rpartition("/")
    if (prefix and not _LABEL_PREFIX.match(prefix)) or not _LABEL_NAME.match(name):
        return f"{key!r} is not a label key"
    if not _LABEL_VALUE.match(value):
        return f"{key}={value!r} is not a label value"
    return None


def parse_labels(text: str) -> dict[str, str]:
    """`k8s-app=kube-dns,app=litellm` into a mapping: how the CLI and the admin UI take
    labels. An empty string is no labels."""
    out: dict[str, str] = {}
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"{item!r} is not a key=value label")
        if key.strip() in out:
            raise ValueError(f"the label {key.strip()!r} is given twice")
        out[key.strip()] = value.strip()
    return out


def format_labels(labels: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


@dataclass(frozen=True, slots=True)
class ClusterEgress:
    dns_namespace: str = DEFAULT_DNS_NAMESPACE
    dns_pod_labels: tuple[tuple[str, str], ...] = DEFAULT_DNS_POD_LABELS
    endpoint_namespace: str = ""
    endpoint_pod_labels: tuple[tuple[str, str], ...] = ()
    endpoint_port: int = 0

    @property
    def endpoint_in_cluster(self) -> bool:
        return bool(self.endpoint_namespace)

    def as_document(self) -> dict[str, Any]:
        return {
            "dns": {"namespace": self.dns_namespace, "pod_labels": dict(self.dns_pod_labels)},
            "local_endpoint": {
                "namespace": self.endpoint_namespace,
                "pod_labels": dict(self.endpoint_pod_labels),
                "port": self.endpoint_port,
            },
        }


def _labels(value: Any, what: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = parse_labels(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} pod_labels must be an object of label keys to values")
    pairs: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{what} pod_labels must map strings to strings")
        problem = label_problem(key, item)
        if problem is not None:
            raise ValueError(f"{what} pod label {problem}")
        pairs.append((key, item))
    return tuple(sorted(pairs))


def _section(
    document: Mapping[str, Any], key: str, what: str
) -> tuple[str, tuple[tuple[str, str], ...]]:
    section = document.get(key) or {}
    if not isinstance(section, Mapping):
        raise ValueError(f"{key} must be an object")
    namespace = section.get("namespace")
    if namespace is None:
        namespace = ""
    if not isinstance(namespace, str):
        raise ValueError(f"{what} namespace must be a string")
    namespace = namespace.strip()
    labels = _labels(section.get("pod_labels"), what)
    if namespace:
        problem = namespace_problem(namespace)
        if problem is not None:
            raise ValueError(f"{what} namespace {problem}")
        if not labels:
            # An empty podSelector is every pod in the namespace, which is not a
            # destination anybody chose.
            raise ValueError(f"{what} needs at least one pod label when a namespace is set")
    elif labels:
        raise ValueError(f"{what} pod labels need a namespace")
    return namespace, labels


def parse_cluster_egress(
    document: Mapping[str, Any], *, protected_namespaces: tuple[str, ...] = ()
) -> ClusterEgress:
    """Check a document and return it normalised. Raises ValueError naming the field.

    `protected_namespaces` are the workers namespace and Crucible's own: a selector into
    the first is a worker reaching another attempt, into the second a worker reaching
    Crucible's database. Neither is ever the resolver or a model gateway."""
    dns_namespace, dns_labels = _section(document, "dns", "cluster DNS")
    endpoint_namespace, endpoint_labels = _section(
        document, "local_endpoint", "the in-cluster local endpoint"
    )
    raw_port = (document.get("local_endpoint") or {}).get("port") or 0
    if isinstance(raw_port, bool) or not isinstance(raw_port, int | str):
        raise ValueError("the in-cluster local endpoint port must be a number")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("the in-cluster local endpoint port must be a number") from exc
    if not 0 <= port <= 65535:
        raise ValueError("the in-cluster local endpoint port must be between 0 and 65535")
    if port and not endpoint_namespace:
        raise ValueError("the in-cluster local endpoint port needs a namespace")
    for namespace in (dns_namespace, endpoint_namespace):
        if namespace and namespace in protected_namespaces:
            raise ValueError(
                f"a selector may not name the {namespace!r} namespace, which a worker may "
                "never reach"
            )
    return ClusterEgress(
        dns_namespace=dns_namespace,
        dns_pod_labels=dns_labels,
        endpoint_namespace=endpoint_namespace,
        endpoint_pod_labels=endpoint_labels,
        endpoint_port=port,
    )


__all__ = [
    "DEFAULT_DNS_NAMESPACE",
    "DEFAULT_DNS_POD_LABELS",
    "SETTING_NAME",
    "ClusterEgress",
    "format_labels",
    "label_problem",
    "namespace_problem",
    "parse_cluster_egress",
    "parse_labels",
]
