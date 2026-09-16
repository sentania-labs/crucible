from __future__ import annotations

from datetime import datetime

from crucible.domain.time import utcnow


class SystemClock:
    def now(self) -> datetime:
        return utcnow()
