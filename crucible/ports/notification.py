"""Wake delivery (17). Rows are written first; delivery is best effort with retry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    detail: str


class WakeDeliverer(Protocol):
    """POST the wake body to the configured URL with an HMAC signature header."""

    @property
    def configured(self) -> bool:
        """False when no webhook URL is configured; poll is then the only delivery."""
        ...

    async def deliver(self, body: bytes) -> DeliveryResult: ...
