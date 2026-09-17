"""Provider health (25): name, capabilities, `ok`, `degraded` or `unavailable`."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import AdminContext


async def providers_status(ctx: AdminContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, provider in ctx.providers.items():
        try:
            health = await provider.health()
            state, checks = health.state, health.checks
        except Exception as exc:  # the status document never raises for one provider
            state, checks = "unavailable", {"error": type(exc).__name__}
        out.append(
            {
                "name": name,
                "capabilities": provider.capabilities().as_dict(),
                "health": state,
                "checks": checks,
            }
        )
    return out
