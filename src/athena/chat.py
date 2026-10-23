"""Chatting with Athena's agents on the dashboard.

Each agent is a session (history.py), its conversation, with a model, guidance and DAG of its own (dags/). The
master agent is the one Telegram, Siri and the terminal talk to; the others you talk to here. You send a message
to an agent, and Athena answers in the background while the page follows how it's going. One message at a time:
while Athena's answering you, here or anywhere else, nothing more can be sent, so messages can't pile up. If it
couldn't answer, your message is kept, here only, for you to try again. A message can come with photos, which the
model sees, and files, which go into the workspace's inbox/ as Telegram's do (files.py). Agents can be named,
muted so they don't notify your devices, and deleted, with their DAGs, but the master agent.

Tools that ask first ask here, and Telegram's requests can be answered here too (approvals.py). You can allow
tools without asking for one message, or for a whole session, which Telegram's messages in it get too.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from athena import commands, files, workspace
from athena.files import SENT
from athena.history import Busy, Session
from athena.photos import MAX_BYTES, MAX_PER_MESSAGE, TYPES, Photo, mime_of
from athena.tools import NEVER_UNASKED, ToolError

if TYPE_CHECKING:
    from athena.agent import AgentResult
    from athena.app import Services

log = logging.getLogger(__name__)

MAX_CHARS = 4000  # the longest message you can send, as in Telegram
MAX_FILES = 5  # with a message, besides its photos
MAX_FILE_MB = 20  # each, as in Telegram
CHANNEL = "Dashboard"
TAKE_WORDS = {"think", "remember", "recall", "forget"}  # commands that take something after them


@dataclass
class Reply:
    """A message you sent here, while it's answered, and after if it couldn't be."""

    session: int
    text: str
    allowed: tuple[str, ...]  # tools allowed without asking, for this message only
    started: float
    error: str = ""  # why it didn't get an answer, once it hasn't
    photos: list[Photo] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "text": self.text,
            "started": self.started,
            "error": self.error,
            "photos": len(self.photos),
        }


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

    def new_session(self, name: Any = "") -> Session:
        """A new agent."""
        if not isinstance(name, str):
            raise ValueError('Send its name: {"name": "Job hunting"}.')
        session = self.s.history.new_session(name)
        log.info("Dashboard: made agent %d", session.id)
        return session

    def allow_in_session(self, session_id: int, names: Collection[str]) -> Session:
        """The tools that may run without asking in a session. KeyError if there's no such session."""
        return self.s.history.set_allowed_tools(session_id, self._check_tools(names))

    def rename(self, session_id: int, name: Any) -> Session:
        """Call a session something, or "" for after your first message in it. KeyError if there's no such
        session."""
        if not isinstance(name, str):
            raise ValueError('Send its name: {"name": "Job hunting"}, or "" for none.')
        return self.s.history.rename(session_id, name)

    def mute(self, session_id: int, muted: Any) -> Session:
        """Whether a session keeps from notifying your devices. KeyError if there's no such session."""
        if not isinstance(muted, bool):
            raise ValueError('Send {"muted": true} or {"muted": false}.')
        return self.s.history.set_muted(session_id, muted)

    async def delete(self, session_id: int) -> None:
        """Delete an agent, with its DAG, runs and files. Busy if Athena's answering you in it, KeyError if there's
        no such agent, and ValueError if it's the master agent."""
        await self.s.delete_session(session_id)
        self._changed()

    # -- sending ------------------------------------------------------------------------------------

    async def send(self, session_id: int, text: Any, allow: Collection[str] = (), attachments: Any = ()) -> str | None:
        """Send ``text`` in a session, with ``attachments``, as the page sends them: [{"name", "data": base64}]. A
        command runs now and returns what it says, and a message is answered in the background, returning None.
        Busy if Athena's answering you, KeyError if there's no such session, and ValueError if the message won't
        do."""
        if not isinstance(text, str):
            raise ValueError("Write something to send.")
        text = text.strip()
        if len(text) > MAX_CHARS:
            raise ValueError(f"That's over {MAX_CHARS:,} characters, more than I can take in one message.")
        if command := commands.parse(text):
            if attachments:
                raise ValueError("Send a command by itself, without photos or files.")
            return await self._command(session_id, *command)
        allowed = self._check_tools(allow)
        photos, sent = self._attachments(attachments)
        if not text and not photos and not sent:
            raise ValueError("Write something to send.")
        if self.busy:
            raise Busy()
        if self.s.history.get_session(session_id) is None:
            raise KeyError(session_id)
        try:
            kept = [files.receive(self.s.config, name, data) for name, data in sent]  # for read_file
        except ToolError as exc:
            raise ValueError(f"I couldn't keep that file: {exc}") from None
        self._start(session_id, " ".join([*kept, text]).strip(), allowed, photos)
        return None

    def _attachments(self, attachments: Any) -> tuple[list[Photo], list[tuple[str, bytes]]]:
        """A message's photos, and its files as (name, what's in it). ValueError, saying why, if one won't do."""
        if not isinstance(attachments, list | tuple) or not all(
            isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("data"), str)
            for a in attachments
        ):
            raise ValueError('Send photos and files as [{"name", "data": base64}].')
        photos: list[Photo] = []
        sent: list[tuple[str, bytes]] = []
        for attachment in attachments:
            name = workspace.safe_name(attachment["name"])
            try:
                data = base64.b64decode(attachment["data"], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"{name} didn't arrive whole: try again.") from None
            if mime := mime_of(data):
                if len(data) > MAX_BYTES:
                    raise ValueError(f"{name} is over {MAX_BYTES // 1_000_000} MB, more than I can look at.")
                photos.append(Photo(data, mime))
            elif name.rpartition(".")[2].lower() in {*TYPES.values(), "jpeg", "heic", "heif", "avif", "bmp", "tiff"}:
                raise ValueError(f"{name}: I can only look at JPEG, PNG, WebP and GIF images.")
            elif problem := files.why_not(self.s.config, name, len(data), MAX_FILE_MB):
                raise ValueError(f"{name}: {problem}")
            else:
                sent.append((name, data))
        if len(photos) > MAX_PER_MESSAGE:
            raise ValueError(f"Send up to {MAX_PER_MESSAGE} photos with a message.")
        if len(sent) > MAX_FILES:
            raise ValueError(f"Send up to {MAX_FILES} files with a message.")
        return photos, sent

    def _start(self, session_id: int, text: str, allowed: tuple[str, ...], photos: list[Photo] | None = None) -> None:
        if self.busy:
            raise Busy()
        session = self.s.history.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        self.failed.pop(session_id, None)
        self.replying = Reply(session_id, text, allowed, time.time(), photos=list(photos or []))
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
                photos=reply.photos or None,
            )
        except asyncio.CancelledError:
            reply.error = "You stopped it."
        except Exception as exc:
            reply.error = commands.failure(self.s, exc, photos=bool(reply.photos))
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
            self._start(session_id, failed.text, failed.allowed, failed.photos)
            return ""
        exchange = await self.s.take_back_last_exchange(session.chat_id)
        if exchange is None:
            return "There's nothing to try again here."
        photos = [photo for name in exchange.photos if (photo := self.s.history.photos.load(name))]
        self._start(session_id, exchange.question, (), photos)  # its text says [Photo]
        return ""

    async def _command(self, session_id: int, name: str, rest: str) -> str:
        if self.s.history.get_session(session_id) is None:
            raise KeyError(session_id)
        if name == "stop":
            return "Stopped. Send /retry to have me try again." if await self.stop() else "I'm not working on anything."
        if name == "retry":
            return await self.retry(session_id)
        said = await commands.run(
            self.s, name, rest, source="dashboard", chat_id=self.s.history.get_session(session_id).chat_id
        )
        return said if said is not None else f"There's no /{name} command. /help lists them."

    # -- what the page shows -------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The agents, whether Athena's answering you and where, and what's waiting for your OK."""
        history = self.s.history
        snapshot = self.s.status.snapshot()
        elsewhere = None
        if self.replying is None and self.s.agent.answering_you:  # in Telegram, through Siri or in the terminal
            elsewhere = {"session": history.master.id, "channel": snapshot.channel, "text": snapshot.task}
        dags = self.s.dags
        configs = {c.id: c for c in dags.store.all()}
        waiting: dict[int, int] = {}
        for proposal in dags.store.proposals():
            waiting[proposal.agent] = waiting.get(proposal.agent, 0) + 1
        return {
            "master": history.master.id,
            "sessions": [
                {
                    "id": s.id,
                    "master": s.master,
                    "title": s.title,
                    "name": s.name,
                    "started": s.started,
                    "last_used": s.last_used,
                    "messages": s.messages,
                    "last": s.last_message,
                    "muted": s.muted,
                    "dag": self._dag(configs.get(s.id)),
                    "proposals": waiting.get(s.id, 0),
                }
                for s in history.sessions(also={c.id for c in configs.values() if c.dag})
            ],
            "busy": self.busy,
            "replying": self.replying.as_dict() if self.replying else None,
            "elsewhere": elsewhere,  # one of yours from Telegram, Siri or the terminal, to the master agent
            "step": snapshot.step if self.busy else "",
            "failed": {str(k): v.as_dict() for k, v in self.failed.items()},
            "approvals": [a.as_dict() for a in self.s.approvals.pending()],
            "askable": self.askable(),
            "commands": [
                {"name": c, "description": d, "takes": c in TAKE_WORDS}
                for c, d in [*commands.COMMANDS, ("help", "What you can ask, and these commands")]
            ],
            # What can be sent with a message: photos, and files if Athena has a workspace to keep them in.
            "attach": {"photos": MAX_PER_MESSAGE, "files": MAX_FILES if self.s.config.workspace.enabled else 0},
        }

    def _dag(self, config: Any) -> dict[str, Any] | None:
        """An agent's DAG, as the list of agents shows it: when it runs, and whether it is."""
        if config is None or not config.dag:
            return None
        described = self.s.dags.describe(config.id)
        dag = described["dag"]
        return {
            "when": dag["when"] if dag else "its file has a problem",
            "running": described["running"] is not None,
            "paused": described["paused"],
            "next_run": described["next_run"],
        }

    def session(self, session_id: int, before: int | None = None) -> dict[str, Any]:
        """An agent's messages, the last page of them or those before a message, and its settings: what's allowed in
        it, its model, guidance and what its DAG kept."""
        session = self.s.history.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        messages, more = self.s.history.messages(session_id, before)
        return {
            "id": session.id,
            "master": session.master,
            "name": session.name,
            "muted": session.muted,
            "started": session.started,
            "last_used": session.last_used,
            "allowed_tools": list(session.allowed_tools),
            **self.s.dags.store.get(session_id).as_dict(),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "text": m.text,
                    "at": m.created_at,
                    "channel": m.channel,
                    "tools": m.tools,
                    "photos": m.photos,
                }
                for m in messages
            ],
            "more": more,
        }

    def _changed(self) -> None:
        for listener in list(self.listeners):
            listener()
