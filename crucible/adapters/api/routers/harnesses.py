"""/harnesses and /images (07, 13, 25). Read-only in C5a; the admin mutations are C5b."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from crucible.adapters.api.deps import Ctx, Reader, UoW
from crucible.application.harness_views import harness_list, image_list
from crucible.contracts.api import HarnessList, ImageList
from crucible.ports.execution import ImageInfo, ProviderError

router = APIRouter()


async def _images(ctx: Ctx) -> list[tuple[str, ImageInfo]]:
    """What every provider can see. A provider that cannot answer contributes nothing
    rather than failing the whole listing; its health is `GET /supervisor`'s subject."""
    out: list[tuple[str, ImageInfo]] = []
    for provider in ctx.providers:
        try:
            found = await asyncio.wait_for(provider.list_images(), timeout=15)
        except (ProviderError, TimeoutError, OSError):
            continue
        out.extend((provider.name, image) for image in found)
    return out


@router.get("/harnesses", response_model=HarnessList)
async def list_harnesses(ctx: Ctx, uow: UoW, _principal: Reader) -> HarnessList:
    images = await _images(ctx)
    return harness_list(
        uow,
        ctx.harnesses,
        gates=ctx.harness_gates,
        sources=ctx.credential_sources,
        images=[image for _, image in images],
    )


@router.get("/images", response_model=ImageList)
async def list_images(ctx: Ctx, uow: UoW, _principal: Reader) -> ImageList:
    return image_list(uow, ctx.harnesses, await _images(ctx))
