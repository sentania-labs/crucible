"""Administrative routing state, including the editable local model endpoint."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crucible.application.admin.context import AdminContext, admin_event, guard_mutation
from crucible.application.errors import NotFoundError
from crucible.application.policies import put_policy, put_routing_policy
from crucible.application.proxy_config import worker_proxy_config
from crucible.domain.endpoints import validate_endpoint
from crucible.domain.entities import Principal
from crucible.domain.events import EventKind
from crucible.ports.repository import UnitOfWork


def list_exhaustions(ctx: AdminContext, uow: UnitOfWork) -> dict[str, Any]:
    now = ctx.clock.now()
    return {
        "items": [
            {
                "pool": mark.pool,
                "exhausted_at": mark.exhausted_at.isoformat(),
                "reset_at": mark.reset_at.isoformat(),
                "active": mark.cleared_at is None and mark.reset_at > now,
                "task_id": mark.task_id,
                "attempt_id": mark.attempt_id,
                "reason": mark.reason,
                "cleared_at": mark.cleared_at.isoformat() if mark.cleared_at else None,
                "cleared_by": mark.cleared_by,
                "clear_reason": mark.clear_reason,
            }
            for mark in uow.pool_exhaustions.list_all()
        ]
    }


def _active_documents(uow: UnitOfWork) -> tuple[Any, Any]:
    policies = [p for p in uow.policies.list_versions("default-software") if p.retired_at is None]
    if not policies:
        raise NotFoundError("no default-software policy is in force")
    policy = max(policies, key=lambda item: item.version)
    ref = (policy.document.get("routing") or {}).get("policy") or {}
    routing = uow.routing_policies.get(str(ref.get("name", "")), int(ref.get("version", 0)))
    if routing is None or routing.retired_at is not None:
        raise NotFoundError("the routing policy named by default-software is not available")
    return policy, routing


def local_endpoint_view(uow: UnitOfWork) -> dict[str, Any]:
    policy, routing = _active_documents(uow)
    models = [
        copy.deepcopy(model)
        for model in routing.document.get("models", [])
        if model.get("endpoint") == "local"
    ]
    pool_name = str(models[0].get("pool", "")) if models else ""
    pool = copy.deepcopy((routing.document.get("pools") or {}).get(pool_name) or {})
    endpoints = {model.get("endpoint_url") for model in models if model.get("endpoint_url")}
    return {
        "policy": {"name": policy.name, "version": policy.version},
        "routing_policy": {"name": routing.name, "version": routing.version},
        "endpoint_url": next(iter(endpoints)) if len(endpoints) == 1 else None,
        "models": models,
        "pool": {"name": pool_name, **pool},
    }


def save_local_endpoint(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: Principal,
    endpoint_url: str,
    models: list[dict[str, Any]],
    max_concurrency: int,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal.name, operation="routing local-endpoint update"
    )
    validate_endpoint("local", endpoint_url)
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    policy, routing = _active_documents(uow)
    before = local_endpoint_view(uow)
    routing_document = copy.deepcopy(routing.document)
    updates = {str(item.get("id", "")): item for item in models}
    local_models = [
        model for model in routing_document.get("models", []) if model.get("endpoint") == "local"
    ]
    if not local_models:
        raise NotFoundError("the active routing policy has no local model entries")
    unknown = sorted(set(updates) - {str(model.get("id")) for model in local_models})
    if unknown:
        raise ValueError(f"models are not local entries in the active routing policy: {unknown}")
    for model in local_models:
        model["endpoint_url"] = endpoint_url
        update = updates.get(str(model.get("id")))
        if update is None:
            continue
        enabled = bool(update.get("enabled", False))
        model["enabled"] = enabled
        model["chat_template_kwargs"] = {
            "enable_thinking": bool(update.get("enable_thinking", False))
        }
        if enabled:
            model["disabled_reason"] = None
        else:
            model["disabled_reason"] = "operator disabled from the local endpoint panel"
    pool_name = str(local_models[0]["pool"])
    routing_document["pools"][pool_name]["max_concurrency"] = max_concurrency
    routing_versions = uow.routing_policies.list_versions(routing.name)
    next_routing_version = max(item.version for item in routing_versions) + 1
    routing_document["version"] = next_routing_version
    put_routing_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=routing.name,
        version=next_routing_version,
        document=routing_document,
        reason=reason,
    )
    policy_document = copy.deepcopy(policy.document)
    policy_versions = uow.policies.list_versions(policy.name)
    next_policy_version = max(item.version for item in policy_versions) + 1
    policy_document["version"] = next_policy_version
    policy_document["description"] = (
        f"{policy.document.get('description', 'Software delivery policy')} "
        f"Local endpoint update in version {next_policy_version}."
    )
    policy_document["routing"] = {"policy": {"name": routing.name, "version": next_routing_version}}
    put_policy(
        uow,
        ctx.clock,
        principal=principal,
        name=policy.name,
        version=next_policy_version,
        document=policy_document,
        reason=reason,
    )
    if ctx.proxy_config_path:
        rendered = worker_proxy_config(ctx.proxy_subnet, list(ctx.proxy_hosts), [routing_document])
        path = Path(ctx.proxy_config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    parsed = urlsplit(endpoint_url)
    local_destination = (
        f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    )
    docker = ctx.providers.get("docker")
    if docker is not None and hasattr(docker, "config"):
        base_hosts = tuple(
            host for host in getattr(docker.config, "proxy_allowlist", ()) if ":" not in host
        )
        docker.config = replace(
            docker.config,
            proxy_allowlist=tuple(dict.fromkeys([*base_hosts, local_destination])),
        )
    after = {
        "policy": {"name": policy.name, "version": next_policy_version},
        "routing_policy": {"name": routing.name, "version": next_routing_version},
        "endpoint_url": endpoint_url,
        "models": [copy.deepcopy(model) for model in local_models],
        "pool": {"name": pool_name, **copy.deepcopy(routing_document["pools"][pool_name])},
    }
    admin_event(
        uow,
        ctx,
        EventKind.LOCAL_ENDPOINT_UPDATED,
        principal=principal.name,
        reason=reason,
        before={
            "routing_version": before["routing_policy"]["version"],
            "endpoint_url": before["endpoint_url"],
        },
        after={
            "routing_version": next_routing_version,
            "endpoint_url": endpoint_url,
            "max_concurrency": max_concurrency,
        },
    )
    return after


def clear_exhaustion(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    pool: str,
    reason: str | None,
) -> dict[str, Any]:
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation="routing clear-exhaustion"
    )
    before = uow.pool_exhaustions.get(pool, for_update=True)
    if before is None:
        raise NotFoundError(f"quota pool {pool!r} has no exhaustion mark")
    cleared = uow.pool_exhaustions.clear(
        pool, at=ctx.clock.now(), principal=principal, reason=reason
    )
    assert cleared is not None
    admin_event(
        uow,
        ctx,
        EventKind.POOL_EXHAUSTION_CLEARED,
        principal=principal,
        reason=reason,
        before={"pool": pool, "reset_at": before.reset_at.isoformat(), "active": True},
        after={"pool": pool, "active": False},
    )
    return {"pool": pool, "active": False, "cleared_at": cleared.cleared_at}
