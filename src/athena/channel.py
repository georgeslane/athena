"""How any part of Athena reaches you when you haven't just asked it something: to tell you what a
task found, or that a server's tools changed, and to ask before a task uses a tool that asks first.

The Telegram bot is a channel, and sets itself as Services.channel when it starts. Reach is how Athena reaches
you through everything it has: Telegram, if it's set up, and notifications on your devices (push.py). What it
tells you is kept in your conversation too, where the dashboard shows it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from athena.app import Services

log = logging.getLogger(__name__)


class Channel(Protocol):
    async def tell(self, text: str, *, session: int | None = None) -> None:
        """Send you ``text``, in Markdown: what was said in ``session``, if it's given, such as what a task found."""
        ...

    async def send_file(self, noted: str) -> None:
        """Send you a file from the workspace, as send_file noted it (files.py)."""
        ...

    async def ask(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str | None,
        *,
        why: str,
        timeout: float,
        session: int | None = None,
    ) -> bool:
        """Ask you to allow a tool call, saying ``why`` it's wanted, and for ``session`` if it's for something in one.
        False if you deny it or don't answer in time."""
        ...


class Reach:
    """A Channel that's everything Athena has: Telegram if it's set up, and your devices' notifications. Without
    Telegram, you answer its requests for your OK on the dashboard."""

    def __init__(self, services: Services):
        self.s = services

    async def tell(self, text: str, *, session: int | None = None) -> None:
        await self.s.notify(text, session=session)

    async def send_file(self, noted: str) -> None:
        if self.s.channel is None:  # the dashboard has nowhere to keep it
            log.warning("Nowhere to send a file")
            return
        await self.s.channel.send_file(noted)

    async def ask(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str | None,
        *,
        why: str,
        timeout: float,
        session: int | None = None,
    ) -> bool:
        if self.s.channel is not None:  # in Telegram, and on the dashboard
            return await self.s.channel.ask(tool, args, summary, why=why, timeout=timeout, session=session)
        approvals = self.s.approvals
        return await approvals.wait(approvals.open(tool, args, summary, timeout=timeout, why=why, session=session))
