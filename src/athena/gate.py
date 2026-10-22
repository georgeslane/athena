"""Who uses the model next: your messages, before what Athena does by itself, but not in the middle of a task.

The model server answers one thing at a time, and what's cached is what it read last. So when you send a message
while Athena's working through an agent's DAG, it's answered once the task that's running has finished, before
the next one starts. The next task waits until you've been quiet for a little while, so a conversation isn't
broken up by tasks. While a task waits for your OK to use a tool, it isn't using the model: your messages can
go ahead, and the task carries on once they're answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable

GRACE_SECONDS = 60.0  # how long you've been quiet before the next task starts


class Gate:
    def __init__(self, grace: float = GRACE_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.grace = grace
        self.clock = clock
        self.working: str | None = None  # what Athena's doing by itself, while it is, such as "apply in Jobs"
        self.users = 0  # your messages being answered, or waiting to be
        self._yielding = 0  # Athena's waiting for your OK, so your messages can go ahead
        self._last_yours = float("-inf")
        self._changed = asyncio.Condition()

    @property
    def held(self) -> bool:
        """Whether a message of yours would wait now."""
        return self.working is not None and not self._yielding

    @contextlib.asynccontextmanager
    async def yours(self) -> AsyncIterator[None]:
        """For a message of yours: waits for the task that's running, if one is, to finish."""
        async with self._changed:
            self.users += 1
            try:
                await self._changed.wait_for(lambda: not self.held)
            except BaseException:
                self.users -= 1
                self._changed.notify_all()
                raise
        try:
            yield
        finally:
            async with self._changed:
                self.users -= 1
                self._last_yours = self.clock()
                self._changed.notify_all()

    @contextlib.asynccontextmanager
    async def task(self, what: str, grace: bool = True) -> AsyncIterator[None]:
        """For something Athena does by itself: it starts once no message of yours is waiting, and you've been quiet
        for the grace period, unless not ``grace``."""
        async with self._changed:
            while True:
                if self.users or self.working is not None:
                    await self._changed.wait()
                    continue
                left = self.grace - (self.clock() - self._last_yours) if grace else 0.0
                if left <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), left)
            self.working = what
        try:
            yield
        finally:
            async with self._changed:
                self.working = None
                self._changed.notify_all()

    @contextlib.asynccontextmanager
    async def aside(self) -> AsyncIterator[None]:
        """While the task that's running waits for something other than the model, such as your OK: your messages
        go ahead, and it carries on once they've been answered."""
        async with self._changed:
            self._yielding += 1
            self._changed.notify_all()
        try:
            yield
        finally:
            try:
                async with self._changed:
                    await self._changed.wait_for(lambda: self.users == 0)
            finally:
                self._yielding -= 1
