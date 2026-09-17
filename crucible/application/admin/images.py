"""Image administration (13, 25): list and promote. Promotion is an explicit admin act
recorded as an event; the previous default of the same harness becomes `retained`."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    require_live_supervisor,
    require_reason,
)
from crucible.application.admin.harnesses import list_images as provider_images
from crucible.application.errors import NotFoundError
from crucible.application.harness_views import image_list
from crucible.domain.entities import ImagePromotion
from crucible.domain.events import EventKind
from crucible.ports.execution import ImageInfo
from crucible.ports.repository import UnitOfWork


async def list_all(ctx: AdminContext, uow: UnitOfWork) -> list[dict[str, Any]]:
    images = await provider_images(ctx)
    return [i.model_dump(mode="json") for i in image_list(uow, ctx.harnesses, images).items]


def _find(images: list[tuple[str, ImageInfo]], digest: str) -> ImageInfo:
    for _, image in images:
        if digest in (image.digest, image.reference):
            return image
    raise NotFoundError(f"no provider lists an image with digest or reference {digest!r}")


async def promote(
    ctx: AdminContext, uow: UnitOfWork, *, principal: str, digest: str, reason: str | None
) -> dict[str, Any]:
    reason = require_reason(reason)
    require_live_supervisor(ctx, uow)
    image = _find(await provider_images(ctx), digest)
    if not image.harness:
        raise NotFoundError(f"image {image.reference} carries no crucible.harness label")
    adapter = ctx.harnesses.get(image.harness)
    if adapter is None or not image.harness_version:
        raise NotFoundError(f"image {image.reference} names an unknown harness or no version")
    if not adapter.supported_versions.supports(image.harness_version):
        raise NotFoundError(
            f"image {image.reference} carries {image.harness} {image.harness_version}, "
            f"outside the adapter's range {adapter.supported_versions.text}"
        )
    now = ctx.clock.now()
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for existing in uow.image_promotions.list_all():
        if existing.harness == image.harness and existing.state == "default":
            before[existing.digest] = existing.state
            existing.state = "retained"
            existing.updated_at = now
            existing.updated_by = principal
            existing.reason = f"superseded by {image.digest}"
            uow.image_promotions.put(existing)
            after[existing.digest] = "retained"
    current = uow.image_promotions.get(image.digest)
    before[image.digest] = current.state if current else "candidate"
    promoted = uow.image_promotions.put(
        ImagePromotion(
            digest=image.digest,
            reference=image.reference,
            harness=image.harness,
            harness_version=image.harness_version,
            state="default",
            updated_at=now,
            updated_by=principal,
            reason=reason,
        )
    )
    after[image.digest] = "default"
    admin_event(
        uow,
        ctx,
        EventKind.IMAGE_PROMOTED,
        principal=principal,
        reason=reason,
        before=before,
        after=after,
        harness=image.harness,
        digest=image.digest,
        reference=image.reference,
    )
    return {
        "digest": promoted.digest,
        "reference": promoted.reference,
        "harness": promoted.harness,
        "harness_version": promoted.harness_version,
        "promotion_state": promoted.state,
        "retained": [d for d, s in after.items() if s == "retained"],
    }
