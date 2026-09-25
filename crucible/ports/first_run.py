"""Where the one-time first-run administrator token goes (25, ADR 0016).

The token is a full administrator credential, so it never reaches stdout, stderr or any
log: a log stream is shipped, retained and copied by whatever collects it (crucible#122).
It goes to one private place the deployment already protects, and is removed from there
the first time its principal signs in or when that principal is revoked.
"""

from __future__ import annotations

from typing import Protocol


class FirstRunDelivery(Protocol):
    def where(self) -> str:
        """A sentence naming the place and how to read it. Never the value."""
        ...

    def deliver(self, token: str) -> None:
        """Write the token, replacing one a previous run left behind. Raises when it
        cannot, so the caller mints nothing it could not hand over."""
        ...

    def discard(self) -> None:
        """Remove the token. Removing one that is already gone is not an error."""
        ...
