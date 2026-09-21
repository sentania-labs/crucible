"""Shared ownership checks for mutations against an existing task."""

from __future__ import annotations

from crucible.application.errors import ForbiddenError
from crucible.domain.entities import Principal, Role, Task


def require_task_principal(principal: Principal, task: Task) -> None:
    """Keep one orchestrator from mutating another orchestrator's task."""
    if principal.role is Role.ORCHESTRATOR and principal.id != task.principal_id:
        raise ForbiddenError(
            f"task {task.id} belongs to another principal; orchestrators may mutate only "
            "their own tasks"
        )
