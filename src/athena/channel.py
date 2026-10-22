"""How any part of Athena reaches you when you haven't just asked it something: to tell you what a
task found, or that a server's tools changed, and to ask before a task uses a tool that asks
first. The Telegram bot is the channel for now, and sets itself as Services.channel when it starts."""

from __future__ import annotations

from typing import Any, Protocol


class Channel(Protocol):
    async def tell(self, text: str) -> None:
        """Send you ``text``, in Markdown."""
        ...

    async def send_file(self, noted: str) -> None:
        """Send you a file from the workspace, as send_file noted it (files.py)."""
        ...

    async def ask(self, tool: str, args: dict[str, Any], summary: str | None, *, why: str, timeout: float) -> bool:
        """Ask you to allow a tool call, saying ``why`` it's wanted. False if you deny it or don't answer in time."""
        ...
