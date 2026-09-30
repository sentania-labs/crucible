from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crucible.adapters.api.deps import Ctx, UoW
from crucible.adapters.ui.actions import register
from crucible.adapters.ui.render import _page
from crucible.adapters.ui.session import _require
from crucible.application.admin import (
    repositories,
)
from crucible.contracts.api import (
    ExternalReviewAttestation,
    RepositoryRegistration,
)
from crucible.domain.entities import Principal, Role

router = APIRouter(prefix="/ui", include_in_schema=False)


@router.get("/repositories", response_class=HTMLResponse)
def repositories_page(request: Request, ctx: Ctx, uow: UoW) -> Response:
    found = _require(request, ctx, uow)
    if isinstance(found, RedirectResponse):
        return found
    principal, csrf = found
    items = list(uow.repositories.list_all())
    sections: list[dict[str, Any]] = [
        {
            "title": "Registered repositories",
            "columns": [
                "Name",
                "URL",
                "Default branch",
                "Policy",
                "Installation",
                "Private",
                "External review",
                "",
            ],
            "rows": [
                [
                    item.name,
                    item.url,
                    item.default_branch,
                    item.policy_name,
                    item.installation_id,
                    item.private,
                    item.external_review_attested,
                    # The row's own removal, never a typed name (crucible#127); refused
                    # while tasks reference it, and it asks for a reason (crucible#117).
                    {
                        "kind": "form",
                        "action": "/ui/actions/repository-remove",
                        "label": "Remove",
                        "danger": True,
                        "reason": True,
                        "hidden": {"name": item.name},
                    }
                    if principal.role is Role.ADMIN
                    else "",
                ]
                for item in items
            ],
        }
    ]
    if principal.role is Role.ADMIN:
        sections.extend(
            [
                {
                    "title": "Register or update",
                    "note": (
                        "For a repository the GitHub page's picker cannot show. The picker "
                        "fills the installation ID, the default branch and whether it is "
                        "private from GitHub. A private repository is cloned with a "
                        "read-only token from the GitHub App, so it needs the App connected "
                        "and the installation ID that covers it."
                    ),
                    "form": {
                        "action": "/ui/actions/repository-register",
                        "label": "Save registration",
                        "fields": [
                            {"name": "name", "label": "Name", "required": True},
                            {"name": "url", "label": "Clone URL", "required": True},
                            {
                                "name": "default_branch",
                                "label": "Default branch",
                                "value": "main",
                                "required": True,
                            },
                            {
                                "name": "policy_name",
                                "label": "Policy",
                                "value": "default-software",
                                "required": True,
                            },
                            {
                                "name": "installation_id",
                                "label": "GitHub installation ID",
                                "kind": "number",
                            },
                            {
                                "name": "private",
                                "label": "Private (clone with the GitHub App's read-only token)",
                                "kind": "checkbox",
                            },
                            {
                                "name": "attested_all_prs",
                                "label": "External reviewer covers all PRs",
                                "kind": "checkbox",
                            },
                            {"name": "attested_by", "label": "Attested by"},
                            {"name": "reason", "label": "Reason", "required": True},
                        ],
                    },
                },
            ]
        )
    return _page(
        request,
        principal,
        csrf,
        active="/ui/repositories",
        heading="Repositories",
        intro="Delivery registrations and GitHub installation binding.",
        sections=sections,
    )


async def _action_repository_register(
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
    repositories.register(
        ctx.admin,
        uow,
        principal=principal.name,
        name=form.get("name", ""),
        registration=RepositoryRegistration(
            url=form.get("url", ""),
            default_branch=form.get("default_branch", "main"),
            policy_name=form.get("policy_name", "default-software"),
            installation_id=int(form["installation_id"]) if form.get("installation_id") else None,
            external_review=ExternalReviewAttestation(
                attested_all_prs=form.get("attested_all_prs") == "true",
                attested_by=form.get("attested_by") or None,
            ),
            private=form.get("private") == "true",
        ),
        reason=reason,
    )
    return None


register("repository-register", _action_repository_register)


async def _action_repository_remove(
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
    repositories.remove(
        ctx.admin, uow, principal=principal.name, name=form.get("name", ""), reason=reason
    )
    return None


register("repository-remove", _action_repository_remove)
