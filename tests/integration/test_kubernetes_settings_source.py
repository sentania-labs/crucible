"""What the Kubernetes provider reads back from the database (crucible#91).

The API process and the supervisor are separate deployments on Kubernetes, so an edit
made through one has to reach the other's provider. Each provider reads the saved
`kubernetes.egress` row and the enabled local endpoint through this source.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crucible.adapters.persistence.unit_of_work import SqlUnitOfWorkFactory
from crucible.application.admin.routing import local_endpoint_view
from crucible.cli.wiring import enabled_database_endpoint, kubernetes_settings_source
from crucible.domain.cluster_egress import SETTING_NAME, ClusterEgress
from crucible.domain.entities import ProviderSetting

pytestmark = pytest.mark.integration


def test_the_source_reads_the_saved_setting_and_the_enabled_endpoint(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    read = kubernetes_settings_source(uow_factory)
    document, endpoint = read()
    assert document is None
    with uow_factory() as uow:
        reference = local_endpoint_view(uow)["routing_policy"]
        record = uow.routing_policies.get(reference["name"], reference["version"])
        expected = enabled_database_endpoint(record.document if record else None)
    assert endpoint == expected

    saved = ClusterEgress(
        endpoint_namespace="litellm", endpoint_pod_labels=(("app", "litellm"),), endpoint_port=4000
    ).as_document()
    with uow_factory() as uow:
        uow.provider_settings.put(
            ProviderSetting(
                name=SETTING_NAME,
                document=saved,
                updated_at=datetime.now(UTC),
                updated_by="tests",
                reason="in-cluster gateway",
            )
        )
        uow.commit()
    document, _endpoint = read()
    assert document == saved
