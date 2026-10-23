"""Requests for your OK before a tool that asks first runs, and questions a DAG's task asks you (ask_user), wherever
you answer them: Telegram, or the dashboard. Each is open until one of them answers it, or it times out: a request
then counts as declined, and a question as unanswered."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ALLOWED, DENIED, NO_ANSWER, STOPPED = "Allowed", "Denied", "No answer, so denied", "Stopped"
ANSWERED, SKIPPED, UNANSWERED = "Answered", "Skipped", "No answer"
MAX_REPLY = 2000  # characters of an answer to a question


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
    question: str | None = None  # a question for you, rather than a tool to allow
    choices: list[str] = field(default_factory=list)  # answers it offers, for a question
    reply: str | None = None  # your answer to a question
    verdict: str = ""  # once answered: ALLOWED, DENIED, NO_ANSWER or STOPPED; ANSWERED, SKIPPED or UNANSWERED
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
            "question": self.question,
            "choices": self.choices,
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
        return self._opened(approval)

    def ask(
        self, question: str, choices: list[str], *, timeout: float, why: str = "", session: int | None = None
    ) -> Approval:
        """Ask you a question: it's open until it's answered, and wait_reply() waits for the answer."""
        now = self.clock()
        approval = Approval(secrets.token_hex(6), "ask_user", {}, None, why, session, now, now + timeout, question)
        approval.choices = list(choices)
        return self._opened(approval)

    def _opened(self, approval: Approval) -> Approval:
        approval._answer = asyncio.get_running_loop().create_future()
        self._open[approval.id] = approval
        self._changed(approval)
        return approval

    async def wait_reply(self, approval: Approval) -> str | None:
        """Your answer to a question: None if you skipped it or didn't answer in time."""
        await self.wait(approval)
        if approval.verdict == NO_ANSWER:
            approval.verdict = UNANSWERED
        return approval.reply

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
        """Allow or decline an open request, or skip a question with allow=False. False if there's no such request
        open, as when it's been answered."""
        approval = self._open.get(approval_id)
        if approval is None or (approval.question is not None and allow):
            return False
        self._close(approval, (SKIPPED if approval.question is not None else DENIED) if not allow else ALLOWED)
        return True

    def reply(self, approval_id: str, text: str) -> bool:
        """Answer an open question. False if there's no such question open, or the answer's empty."""
        approval = self._open.get(approval_id)
        text = text.strip()[:MAX_REPLY]
        if approval is None or approval.question is None or not text:
            return False
        approval.reply = text
        self._close(approval, ANSWERED)
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
