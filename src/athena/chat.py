"""Chatting with Athena on the dashboard.

You send a message in a session, which makes it the current session if it wasn't (history.py), and Athena
answers in the background while the page follows how it's going. One message at a time: while Athena's
answering you, here or anywhere else, nothing more can be sent, so messages can't pile up, and sessions can't be
changed. If it couldn't answer, your message is kept, here only, for you to try again.

Tools that ask first ask here, and Telegram's requests can be answered here too (approvals.py). You can allow
tools without asking for one message, or for a whole session, which Telegram's messages in it get too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from athena import commands
from athena.files import SENT
from athena.history import Busy, Session
from athena.tools import NEVER_UNASKED

if TYPE_CHECKING:
    from athena.agent import AgentResult
    from athena.app import Services

log = logging.getLogger(__name__)

MAX_CHARS = 4000  # the longest message you can send, as in Telegram: it fits in a request (webserver.py)
CHANNEL = "Dashboard"


@dataclass
class Reply:
    """A message you sent here, while it's answered, and after if it couldn't be."""

    session: int
    text: str
    allowed: tuple[str, ...]  # tools allowed without asking, for this message only
    started: float
    error: str = ""  # why it didn't get an answer, once it hasn't

    def as_dict(self) -> dict[str, Any]:
        return {"session": self.session, "text": self.text, "started": self.started, "error": self.error}


class DashboardChat:
    def __init__(self, services: Services):
        self.s = services
        self.replying: Reply | None = None
        self.failed: dict[int, Reply] = {}  # by session: the message that didn't get an answer, to try again
        self.listeners: list[Callable[[], None]] = []  # told when a reply starts or ends
        # Told of each answer to a message sent here, once it's kept: for notifications.
        self.on_answer: Callable[[Reply, AgentResult], None] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        """Whether Athena's answering you, here or anywhere else."""
        return self.replying is not None or self.s.agent.answering_you > 0

    def askable(self) -> list[dict[str, str]]:
        """The tools that ask first and can be allowed without asking, by name."""
        return sorted(
            (
                {"name": t.name, "description": t.description.split("\n", 1)[0][:200], "source": t.source}
                for t in self.s.tools.all()
                if t.needs_confirmation and t.name not in NEVER_UNASKED
            ),
            key=lambda t: t["name"],
        )

    def _check_tools(self, names: Collection[str]) -> tuple[str, ...]:
        askable = {t["name"] for t in self.askable()}
        if not isinstance(names, list | tuple) or not all(isinstance(n, str) for n in names):
            raise ValueError('Send the tools as a list of names: {"allow": ["fetch"]}.')
        if unknown := sorted(set(names) - askable):
            raise ValueError(f"These can't be allowed without asking: {', '.join(unknown)}.")
        return tuple(sorted(set(names)))

    # -- sessions ---------------------------------------------------------------------------------

    def new_session(self) -> Session:
        """A new conversation, as the current session: the current one, if nothing's been said in it yet."""
        if self.busy:
            raise Busy()
        current = self.s.history.session
        if not self.s.history.messages(current.id, limit=1)[0] and current.id not in self.failed:
            return current
        return self.s.new_session()

    def allow_in_session(self, session_id: int, names: Collection[str]) -> Session:
        """The tools that may run without asking in a session. KeyError if there's no such session."""
        return self.s.history.set_allowed_tools(session_id, self._check_tools(names))

    # -- sending ------------------------------------------------------------------------------------

    async def send(self, session_id: int, text: str, allow: Collection[str] = ()) -> str | None:
        """Send ``text`` in a session: a command runs now and returns what it says, and a message is answered in
        the background, returning None. Busy if Athena's answering you, KeyError if there's no such session, and
        ValueError if the message won't do."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Write something to send.")
        text = text.strip()
        if len(text) > MAX_CHARS:
            raise ValueError(f"That's over {MAX_CHARS:,} characters, more than I can take in one message.")
        if command := commands.parse(text):
            return await self._command(session_id, *command)
        self._start(session_id, text, self._check_tools(allow))
        return None

    def _start(self, session_id: int, text: str, allowed: tuple[str, ...]) -> None:
        if self.busy:
            raise Busy()
        session = self.s.use_session(session_id)  # it becomes the current session
        self.failed.pop(session_id, None)
        self.replying = Reply(session_id, text, allowed, time.time())
        self._task = asyncio.create_task(self._answer(self.replying, session), name="dashboard-reply")
        self._changed()

    async def _answer(self, reply: Reply, session: Session) -> None:
        async def confirm(tool: str, args: dict[str, Any], summary: str | None = None) -> bool:
            timeout = self.s.config.telegram.confirm_timeout_seconds
            return await self.s.approvals.wait(
                self.s.approvals.open(tool, args, summary, timeout=timeout, session=reply.session)
            )

        try:
            result = await self.s.agent.respond(
                session.chat_id,
                reply.text,
                confirm=confirm,
                allowed={*session.allowed_tools, *reply.allowed},
                channel=CHANNEL,
            )
        except asyncio.CancelledError:
            reply.error = "You stopped it."
        except Exception as exc:
            reply.error = commands.failure(self.s, exc)
        else:
            if not result.saved:
                reply.error = result.text
            else:
                await self._send_files(result.notes.get(SENT, []))
                if self.on_answer:
                    self.on_answer(reply, result)
        finally:
            if reply.error:
                self.failed[reply.session] = reply
            self.replying = None
            self._changed()

    async def _send_files(self, noted: list[str]) -> None:
        """Files the answer came with go to Telegram, as a task's do: the dashboard has nowhere to keep them."""
        channel = self.s.channel
        for file in noted if channel else []:
            try:
                await channel.send_file(file)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning("Couldn't send a file: %s", exc)

    async def stop(self) -> bool:
        """Stop answering the message sent here. False if Athena wasn't answering one."""
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    async def retry(self, session_id: int) -> str:
        """Answer your last message in a session again: one that didn't get an answer, or the last one that did,
        which is taken out of the conversation first. What to say, if there's nothing to try again."""
        if self.busy:
            raise Busy()
        session = self.s.history.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        if failed := self.failed.get(session_id):
            self._start(session_id, failed.text, failed.allowed)
            return ""
        exchange = await self.s.take_back_last_exchange(session.chat_id)
        if exchange is None:
            return "There's nothing to try again in this session."
        self._start(session_id, exchange.question, ())
        return ""

    async def _command(self, session_id: int, name: str, rest: str) -> str:
        if self.s.history.get_session(session_id) is None:
            raise KeyError(session_id)
        if name == "stop":
            return "Stopped. Send /retry to have me try again." if await self.stop() else "I'm not working on anything."
        if name == "retry":
            return await self.retry(session_id)
        if name in ("session", "reset"):
            try:
                session = self.new_session()
            except Busy as exc:
                return str(exc)
            return f"Started session {session.id}. Memories are kept, and I can still search what we said before."
        said = await commands.run(
            self.s, name, rest, source="dashboard", chat_id=self.s.history.get_session(session_id).chat_id
        )
        return said if said is not None else f"There's no /{name} command. /help lists them."

    # -- what the page shows -------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The sessions, whether Athena's answering you and where, and what's waiting for your OK."""
        history = self.s.history
        snapshot = self.s.status.snapshot()
        elsewhere = None
        if self.replying is None and self.s.agent.answering_you:
            elsewhere = {"session": history.session.id, "channel": snapshot.channel, "text": snapshot.task}
        return {
            "current": history.session.id,
            "sessions": [
                {
                    "id": s.id,
                    "title": s.title,
                    "started": s.started,
                    "last_used": s.last_used,
                    "messages": s.messages,
                }
                for s in history.sessions()
            ],
            "busy": self.busy,
            "replying": self.replying.as_dict() if self.replying else None,
            "elsewhere": elsewhere,  # one of yours from Telegram, Siri or the terminal
            "step": snapshot.step if self.busy else "",
            "failed": {str(k): v.as_dict() for k, v in self.failed.items()},
            "approvals": [a.as_dict() for a in self.s.approvals.pending()],
            "askable": self.askable(),
            "commands": [{"name": c, "description": d} for c, d in commands.COMMANDS]
            + [{"name": "help", "description": "What you can ask, and these commands"}],
        }

    def session(self, session_id: int, before: int | None = None) -> dict[str, Any]:
        """A session's messages, the last page of them or those before a message, and what's allowed in it."""
        session = self.s.history.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        messages, more = self.s.history.messages(session_id, before)
        return {
            "id": session.id,
            "started": session.started,
            "last_used": session.last_used,
            "allowed_tools": list(session.allowed_tools),
            "messages": [
                {"id": m.id, "role": m.role, "text": m.text, "at": m.created_at, "channel": m.channel, "tools": m.tools}
                for m in messages
            ],
            "more": more,
        }

    def _changed(self) -> None:
        for listener in list(self.listeners):
            listener()
