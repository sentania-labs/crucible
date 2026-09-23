"""Image administration (13, 25): list and promote. Promotion is an explicit admin act
recorded as an event; a previous default the promoted image fully covers becomes
`retained`."""

from __future__ import annotations

from typing import Any

from crucible.application.admin.context import (
    AdminContext,
    admin_event,
    guard_mutation,
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
    """Make one image the default for every harness it carries (13, 25).

    The worker image carries all four real harnesses (C11), so promoting it switches all
    four at once, and promoting the previous digest rolls all four back at once. Every
    harness the image carries must be inside its adapter's tested range; an image with
    one harness outside it is refused whole, because promoting it would put that harness
    on an untested version. A previous default is retained only when the new image
    carries every harness it did, so promoting a narrower image never leaves a harness
    with no default."""
    reason = guard_mutation(ctx, uow, reason, principal=principal, operation="images promote")
    image = _find(await provider_images(ctx), digest)
    if not image.harnesses:
        raise NotFoundError(f"image {image.reference} declares no harness")
    for harness, version in sorted(image.harnesses.items()):
        adapter = ctx.harnesses.get(harness)
        if adapter is None or not version:
            raise NotFoundError(
                f"image {image.reference} names an unknown harness {harness!r} or no version"
            )
        if not adapter.supported_versions.supports(version):
            raise NotFoundError(
                f"image {image.reference} carries {harness} {version}, "
                f"outside the adapter's range {adapter.supported_versions.text}"
            )
    now = ctx.clock.now()
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for existing in uow.image_promotions.list_all():
        if existing.digest == image.digest or existing.state != "default":
            continue
        if set(existing.harnesses) <= set(image.harnesses):
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
            harnesses=dict(image.harnesses),
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
        harnesses=dict(image.harnesses),
        digest=image.digest,
        reference=image.reference,
    )
    return {
        "digest": promoted.digest,
        "reference": promoted.reference,
        "harnesses": dict(promoted.harnesses),
        "promotion_state": promoted.state,
        "retained": [d for d, s in after.items() if s == "retained"],
    }
