from crucible.domain.entities import Task, TaskContract
from tests.fixtures import contract_document
from datetime import datetime, UTC

task = Task(
    id="T1",
    external_id="EX-1",
    principal_id="P1",
    project="p1",
    title="T",
    state="ready_for_merge",
    contract_version=1,
    network_mode="policy",
    created_at=datetime.now(UTC),
    updated_at=datetime.now(UTC),
)
