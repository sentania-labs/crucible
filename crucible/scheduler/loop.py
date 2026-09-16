"""Run the supervisor tick on an interval until asked to stop."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from crucible.application.supervisor import Supervisor

log = logging.getLogger("crucible.scheduler")


class SupervisorLoop:
    def __init__(self, supervisor: Supervisor, *, tick_seconds: float) -> None:
        self.supervisor = supervisor
        self.tick_seconds = tick_seconds
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("supervisor loop starting", extra={"holder": self.supervisor.holder})
        try:
            while not self._stop.is_set():
                try:
                    result = await self.supervisor.tick()
                    log.debug(
                        "tick",
                        extra={
                            "held": result.held,
                            "launched": result.launched,
                            "observed": result.observed,
                            "finished": result.finished,
                            "duration_ms": result.duration_ms,
                        },
                    )
                except Exception:
                    log.exception("tick failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
        finally:
            try:
                await self.supervisor.stop()
            except Exception:
                log.exception("lease release failed")
            log.info("supervisor loop stopped", extra={"holder": self.supervisor.holder})
