"""The `kubernetes.egress` document (crucible#91): one place its shape is checked."""

from __future__ import annotations

from typing import Any

import pytest

from crucible.domain.cluster_egress import (
    ClusterEgress,
    format_labels,
    parse_cluster_egress,
    parse_labels,
)


def test_the_default_is_coredns_in_kube_system_and_no_in_cluster_endpoint() -> None:
    assert ClusterEgress().as_document() == {
        "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
        "local_endpoint": {"namespace": "", "pod_labels": {}, "port": 0},
    }
    assert parse_cluster_egress(ClusterEgress().as_document()) == ClusterEgress()


def test_an_empty_document_turns_the_dns_selector_off() -> None:
    """`{}` means no selectors at all; the defaults are ClusterEgress(), not the parse."""
    assert parse_cluster_egress({}).dns_namespace == ""
    assert ClusterEgress().dns_namespace == "kube-system"


def test_a_full_document_round_trips() -> None:
    document = {
        "dns": {"namespace": "kube-system", "pod_labels": {"k8s-app": "kube-dns"}},
        "local_endpoint": {
            "namespace": "litellm",
            "pod_labels": {"app.kubernetes.io/name": "litellm"},
            "port": 4000,
        },
    }
    egress = parse_cluster_egress(document)
    assert egress.endpoint_in_cluster
    assert egress.as_document() == document


def test_labels_may_be_given_as_text() -> None:
    egress = parse_cluster_egress(
        {"dns": {"namespace": "kube-system", "pod_labels": "k8s-app=kube-dns"}}
    )
    assert egress.dns_pod_labels == (("k8s-app", "kube-dns"),)
    assert parse_labels(" a=b , c=d ,") == {"a": "b", "c": "d"}
    assert format_labels({"c": "d", "a": "b"}) == "a=b,c=d"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"dns": {"namespace": "kube-system", "pod_labels": {}}}, "at least one pod label"),
        ({"dns": {"namespace": "", "pod_labels": {"a": "b"}}}, "need a namespace"),
        ({"dns": {"namespace": "Kube_System", "pod_labels": {"a": "b"}}}, "not a namespace"),
        ({"dns": {"namespace": "kube-system", "pod_labels": {"a": ""}}}, "not a label value"),
        ({"dns": {"namespace": "kube-system", "pod_labels": {"-a": "b"}}}, "not a label key"),
        ({"dns": {"namespace": "kube-system", "pod_labels": "k8s-app"}}, "key=value"),
        ({"dns": {"namespace": "kube-system", "pod_labels": {"a": 1}}}, "strings"),
        ({"local_endpoint": {"namespace": "litellm", "pod_labels": {}}}, "at least one pod"),
        ({"local_endpoint": {"port": 4000}}, "port needs a namespace"),
        (
            {"local_endpoint": {"namespace": "l", "pod_labels": {"a": "b"}, "port": 70000}},
            "between 0 and 65535",
        ),
        (
            {"local_endpoint": {"namespace": "l", "pod_labels": {"a": "b"}, "port": True}},
            "must be a number",
        ),
        (
            {"local_endpoint": {"namespace": "l", "pod_labels": {"a": "b"}, "port": "x"}},
            "must be a number",
        ),
        ({"dns": "kube-system"}, "must be an object"),
    ],
)
def test_a_bad_document_is_refused_naming_the_field(document: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_cluster_egress(document)


@pytest.mark.parametrize("section", ["dns", "local_endpoint"])
def test_a_protected_namespace_is_refused(section: str) -> None:
    with pytest.raises(ValueError, match="may never reach"):
        parse_cluster_egress(
            {section: {"namespace": "crucible-workers", "pod_labels": {"a": "b"}}},
            protected_namespaces=("crucible-workers", "crucible"),
        )
