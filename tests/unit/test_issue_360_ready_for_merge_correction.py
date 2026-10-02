from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from crucible.application.supervisor import Supervisor
from crucible.domain.lifecycle import TaskState
from tests.integration.conftest import (
    correction_document,
    submit_and_start,
)

pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.integration


def test_ready_for_merge_correction_accepted(
    client: TestClient, supervisor: Supervisor, uow_factory: Any
) -> None:
    task_id = submit_and_start(client, "crucible-worker:fake-succeed", start=True)

    with uow_factory() as uow:
        task = uow.tasks.get(task_id, for_update=True)
        task.state = TaskState.READY_FOR_MERGE
        uow.commit()

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == TaskState.READY_FOR_MERGE.value
    rounds = view.get("pull_request", {}).get("review_rounds_completed", 0)

    doc = correction_document(client, task_id, image="crucible-worker:fake-succeed")
    r = client.post(f"/v1/tasks/{task_id}/corrections", json=doc)
    assert r.status_code == 202, r.text

    view = client.get(f"/v1/tasks/{task_id}").json()
    assert view["state"] == TaskState.SCHEDULED.value
    assert view.get("pull_request", {}).get("review_rounds_completed", 0) == rounds

    doc_stale = correction_document(
        client, task_id, image="crucible-worker:fake-succeed", of_version=1
    )
    r_stale = client.post(f"/v1/tasks/{task_id}/corrections", json=doc_stale)
    assert r_stale.status_code == 400

    doc2 = correction_document(
        client, task_id, image="crucible-worker:fake-succeed", required_verification=[]
    )
    r2 = client.post(f"/v1/tasks/{task_id}/corrections", json=doc2)
    assert r2.status_code == 400
