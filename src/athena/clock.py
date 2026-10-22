"""The time, for what Athena does by itself on a schedule (tasks/scheduler.py, watching.py): the real time, or a
test's, which moves on by itself whenever the scheduler waits, so a week of schedules runs in a moment."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, tzinfo


class Clock:
    def time(self) -> float:
        """Now, in Unix time."""
        return time.time()

    def now(self, tz: tzinfo) -> datetime:
        return datetime.fromtimestamp(self.time(), tz)

    async def sleep(self, seconds: float, wake: asyncio.Event) -> None:
        """Wait ``seconds``, or until ``wake`` is set."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), seconds)
