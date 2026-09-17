"""Harness administration (25): list, enable, disable."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    guard_mutation,
)
from crucible.application.errors import NotFoundError
from crucible.application.harness_views import harness_list
from crucible.application.harnesses import set_harness_enabled
from crucible.domain.lifecycle import AttemptState
from crucible.ports.execution import ImageInfo
from crucible.ports.repository import UnitOfWork


async def list_images(ctx: AdminContext) -> list[tuple[str, ImageInfo]]:
    out: list[tuple[str, ImageInfo]] = []
    for name, provider in ctx.providers.items():
        try:
            out.extend((name, image) for image in await provider.list_images())
        except Exception:  # a provider that cannot answer contributes nothing (25)
            continue
    return out


def _concurrency(uow: UnitOfWork) -> dict[str, int]:
    live = uow.attempts.list_in_states(
        [
            AttemptState.PREPARING,
            AttemptState.LAUNCHING,
            AttemptState.RUNNING,
            AttemptState.TERMINATING,
            AttemptState.EXITED,
        ]
    )
    counts: dict[str, int] = {}
    for attempt in live:
        execution = uow.executions.get(attempt.execution_id)
        if execution is not None:
            counts[execution.harness] = counts.get(execution.harness, 0) + 1
    return counts


def list_harnesses(
    ctx: AdminContext, uow: UnitOfWork, images: list[ImageInfo]
) -> list[dict[str, Any]]:
    """25 status `harnesses[]`: the C5a view plus concurrency in use and the images
    known for each harness."""
    views = harness_list(
        uow,
        ctx.harnesses,
        gates=ctx.harness_gates,
        sources=ctx.credential_sources,
        images=images,
    )
    in_use = _concurrency(uow)
    promotions = {p.digest: p.state for p in uow.image_promotions.list_all()}
    out: list[dict[str, Any]] = []
    for view in views.items:
        entry = view.model_dump(mode="json")
        entry["concurrency_in_use"] = in_use.get(view.name, 0)
        entry["images"] = [
            {
                "reference": i.reference,
                "harness_version": i.harness_version,
                "digest": i.digest,
                "promotion_state": promotions.get(i.digest, "candidate"),
            }
            for i in images
            if i.harness == view.name
        ]
        out.append(entry)
    return out


def set_enabled(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    enabled: bool,
    reason: str | None,
) -> dict[str, Any]:
    """25: configuration retained; running attempts finish (nothing here touches them);
    new launches are refused with a wake by the registry (07)."""
    reason = guard_mutation(
        ctx,
        uow,
        reason,
        principal=principal,
        operation=f"harnesses {'enable' if enabled else 'disable'}",
    )
    if ctx.harnesses.get(harness) is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    # set_harness_enabled records the harness_enabled or harness_disabled event with
    # the principal, the reason and the before-and-after summary (C5a).
    state = set_harness_enabled(
        uow, ctx.clock, principal_name=principal, name=harness, enabled=enabled, reason=reason
    )
    return {
        "harness": harness,
        "enabled": state.enabled,
        "reason": state.reason,
        "session_compatibility": state.session_compatibility,
        "running_attempts": _concurrency(uow).get(harness, 0),
    }
