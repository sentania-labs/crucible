"""POST /tasks/{id}/start: Foundry's dispatch decision. Moves the task to scheduled and
records the request; the supervisor materializes the execution and first attempt on
its next tick, because execution and attempt rows are fenced to the supervisor (14)."""

from __future__ import annotations

from crucible.application.errors import ContractValidationError, NotFoundError
from crucible.application.transitions import move_task
from crucible.contracts.api import StartRequest
from crucible.contracts.task_contract import TaskContractV1
from crucible.domain.entities import Principal, Task
from crucible.domain.events import EventKind
from crucible.domain.lifecycle import TaskState
from crucible.ports.clock import Clock
from crucible.ports.repository import UnitOfWork


def start_task(
    uow: UnitOfWork, clock: Clock, *, principal: Principal, task_id: str, request: StartRequest
) -> Task:
    task = uow.tasks.get(task_id, for_update=True)
    if task is None:
        raise NotFoundError(f"task {task_id} not found")
    stored = uow.contracts.get(task.id, task.contract_version)
    assert stored is not None
    contract = TaskContractV1.model_validate(stored.document)
    problems = []
    req = contract.execution_request
    if request.overrides is not None and any(
        v is not None for v in request.overrides.model_dump().values()
    ):
        problems.append(
            {"path": "overrides", "message": "overrides create an amendment, which is C2"}
        )
    for field, expected, given in (
        ("harness", req.harness.value, request.harness.value),
        ("model", req.model, request.model),
        ("provider", req.provider.value, request.provider.value),
        ("image", req.image, request.image),
    ):
        if expected != given:
            problems.append(
                {
                    "path": field,
                    "message": f"contract says {expected!r}; the contract is authoritative",
                }
            )
    if request.policy_version != contract.policy.version:
        problems.append(
            {
                "path": "policy_version",
                "message": f"contract names version {contract.policy.version}",
            }
        )
    if problems:
        raise ContractValidationError("start request disagrees with the contract", errors=problems)
    move_task(
        uow,
        clock,
        task,
        TaskState.SCHEDULED,
        EventKind.TASK_SCHEDULED,
        principal=principal.name,
        payload={
            "harness": req.harness.value,
            "model": req.model,
            "effort": request.effort or req.effort,
            "provider": req.provider.value,
            "image": req.image,
            "policy": {"name": contract.policy.name, "version": contract.policy.version},
            "contract_version": task.contract_version,
        },
    )
    uow.commit()
    return task
