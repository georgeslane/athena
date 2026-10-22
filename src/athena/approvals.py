"""Requests for your OK before a tool that asks first runs, wherever you answer them: Telegram's buttons, or the
dashboard. Each is open until one of them answers it, or it times out and counts as declined."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ALLOWED, DENIED, NO_ANSWER, STOPPED = "Allowed", "Denied", "No answer, so denied", "Stopped"


@dataclass
class Approval:
    id: str
    tool: str
    args: dict[str, Any]
    summary: str | None  # what the tool says it will do, and what the privacy check found
    why: str  # what it's for, if it isn't your message, such as "For your task “Briefing”"
    session: int | None  # the session of the message it's for, if it's one of yours
    asked: float  # Unix time
    deadline: float
    verdict: str = ""  # once answered: ALLOWED, DENIED, NO_ANSWER or STOPPED
    _answer: asyncio.Future[bool] = field(default=None, repr=False)  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "summary": self.summary,
            "why": self.why,
            "session": self.session,
            "asked": self.asked,
            "deadline": self.deadline,
        }


class Approvals:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.listeners: list[Callable[[Approval], None]] = []  # told when one opens, and when it's answered
        self._open: dict[str, Approval] = {}

    def open(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str | None,
        *,
        timeout: float,
        why: str = "",
        session: int | None = None,
    ) -> Approval:
        """Ask: it's open until it's answered, and wait() waits for that."""
        now = self.clock()
        approval = Approval(secrets.token_hex(6), tool, args, summary, why, session, now, now + timeout)
        approval._answer = asyncio.get_running_loop().create_future()
        self._open[approval.id] = approval
        self._changed(approval)
        return approval

    async def wait(self, approval: Approval) -> bool:
        """Whether you allowed it: False if you declined it or didn't answer in time. If whatever's waiting is
        stopped, so is the request."""
        try:
            allowed = await asyncio.wait_for(
                asyncio.shield(approval._answer), max(0.0, approval.deadline - self.clock())
            )
        except TimeoutError:
            self._close(approval, NO_ANSWER)
            return False
        except asyncio.CancelledError:
            self._close(approval, STOPPED)
            raise
        return allowed

    def answer(self, approval_id: str, allow: bool) -> bool:
        """Allow or decline an open request. False if there's no such request open, as when it's been answered."""
        approval = self._open.get(approval_id)
        if approval is None:
            return False
        self._close(approval, ALLOWED if allow else DENIED)
        return True

    def pending(self) -> list[Approval]:
        """Open requests, oldest first."""
        return sorted(self._open.values(), key=lambda a: a.asked)

    def _close(self, approval: Approval, verdict: str) -> None:
        if self._open.pop(approval.id, None) is None:
            return
        approval.verdict = verdict
        if not approval._answer.done():
            approval._answer.set_result(verdict == ALLOWED)
        self._changed(approval)

    def _changed(self, approval: Approval) -> None:
        for listener in list(self.listeners):
            listener(approval)
