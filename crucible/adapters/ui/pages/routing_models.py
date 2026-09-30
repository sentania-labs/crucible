"""Model and tier controls kept separate from the already large Routing page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.actions import register
from crucible.application.admin import routing_models
from crucible.domain.entities import Principal

router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("/routing/models")
def models_page() -> RedirectResponse:
    return RedirectResponse("/ui/routing#models", status_code=303)


@router.get("/routing/tiers")
def tiers_page() -> RedirectResponse:
    return RedirectResponse("/ui/routing#tiers", status_code=303)


def control_sections(uow: UoW, *, admin: bool) -> list[dict[str, Any]]:
    view = routing_models.routing_controls_view(uow)
    sections: list[dict[str, Any]] = [
        {
            "title": "What a new task will do",
            "columns": ["Tier", "Route now"],
            "rows": [[name, rule["plain_words"]] for name, rule in view["tiers"].items()],
        }
    ]
    if view["pinned"]:
        sections[0]["note"] = (
            "The delivery policy is deliberately pinned. Routing changes publish a new "
            "routing version, but this delivery policy stays on its selected version."
        )
    if not admin:
        return sections
    sections.append(
        {
            "title": "Models",
            "columns": ["Model", "Harness", "Pool", "Capability", "Availability"],
            "rows": [
                [
                    model["id"],
                    model["harness"],
                    model["pool"],
                    model["capability"],
                    {
                        "kind": "form",
                        "action": "/ui/actions/routing-model",
                        "label": "Save model",
                        "hidden": {"model_id": model["id"]},
                        "select": {
                            "name": "enabled",
                            "label": f"{model['id']} availability",
                            "selected": "true" if model["enabled"] else "false",
                            "options": [("true", "Enabled"), ("false", "Disabled")],
                        },
                        "reason": "optional",
                    },
                ]
                for model in view["models"]
            ],
            "note": (
                "Choose Enabled or Disabled for any model. When disabling, type the reason "
                "in the reason box. Enabling clears its disabled reason."
            ),
        }
    )
    for tier, rule in view["tiers"].items():
        ordered = [
            *rule["prefer_pools"],
            *[p for p in view["pools"] if p not in rule["prefer_pools"]],
        ]
        sections.append(
            {
                "title": f"Tier: {tier}",
                "note": rule["plain_words"],
                "form": {
                    "action": "/ui/actions/routing-tier",
                    "label": f"Save {tier} tier",
                    "collapsed": f"Change {tier} pool order and capabilities",
                    "fields": [
                        {"name": "tier", "kind": "hidden", "value": tier},
                        *[
                            {
                                "name": f"pool_{position}",
                                "label": f"Pool {position + 1}",
                                "kind": "select",
                                "value": pool,
                                "options": [(name, name) for name in view["pools"]],
                            }
                            for position, pool in enumerate(ordered)
                        ],
                        *[
                            {
                                "name": f"capability_{capability}",
                                "label": f"Allow {capability}",
                                "kind": "checkbox",
                                "value": capability in rule["allowed_capability"],
                            }
                            for capability in routing_models.CAPABILITIES
                        ],
                        {"name": "reason", "label": "Reason", "required": True},
                    ],
                },
            }
        )
    return sections


async def _actions(
    request: Request,
    action: str,
    ctx: Ctx,
    uow: UoW,
    principal: Principal,
    csrf: str,
    form: dict[str, str],
    reason: str | None,
) -> Response | None:
    assert ctx.admin is not None
    if action == "routing-model":
        routing_models.save_model(
            ctx.admin,
            uow,
            principal=principal,
            model_id=form.get("model_id", ""),
            enabled=form.get("enabled") == "true",
            disabled_reason=reason or "",
            reason=reason,
        )
    elif action == "routing-tier":
        pools = [value for key, value in sorted(form.items()) if key.startswith("pool_")]
        routing_models.save_tier(
            ctx.admin,
            uow,
            principal=principal,
            tier=form.get("tier", ""),
            prefer_pools=pools,
            allowed_capability=[
                capability
                for capability in routing_models.CAPABILITIES
                if form.get(f"capability_{capability}") == "true"
            ],
            reason=reason,
        )
    return None


register("routing-model", _actions)
register("routing-tier", _actions)
