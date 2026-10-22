"""Conversations, and sessions, stored in SQLite.

A session is something Athena does for you, such as looking for jobs, and the conversation about it: wherever
you talk to Athena, in Telegram, on the dashboard, through Siri or in the terminal, it goes into the current
session, so you can carry on in one what you started in another. The current session is the one used last, which
the model server has read and cached. Starting a new one with /session leaves the others as they were, and
sending a message in an old one, on the dashboard, makes it the current session again. Each session can have a
name, can be kept from notifying you, and can be deleted. Usage statistics are counted per session.

Each task belongs to a session, and runs in its conversation, as a message from you would (tasks/). The checks on
what's watched have a chat of their own ("heartbeat"), so what they did last time doesn't fill your
conversation. A session's chat is "session:<id>".

Only your text and the assistant's final reply are kept (not intermediate tool calls), which keeps prompts
short. Messages are only deleted when you tell Athena to forget everything, or when /retry answers the last
one again: compacting and trimming just move the start of the window the model sees.

Each reply also notes the tools Athena used for it, in order, so it can look back at how it did things and
write skills for what it does the same way often (skills.py). Each message notes where it came from, such as
Telegram, so the dashboard can show it.

A message with photos notes them by name, and they're kept in the data folder (photos.py). The model
sees the photos of the last few such messages, and the rest just as their text, which says "[Photo]".

A chat's older messages can be compacted: summarised by the model (compaction.py), with the summary
kept for the chat and shown at the start of what the model sees, and the window moved past them.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from athena.memory import connect_db
from athena.photos import IN_CONTEXT, Photo, PhotoStore, with_photos

SESSION = "session:"  # a session's chat id starts with this
BACKGROUND = ("task:", "heartbeat", "warm-up")  # chats that were never yours, before sessions had chats of their own
TITLE_CHARS = 80
NAME_CHARS = 80
_SESSION = "id, started, last_used, allowed_tools, name, muted"  # a Session's columns, in its order
_TASK_RAN = re.compile(r"^\(Your task “(.+?)”\)")  # the start of a task's run, on your side (task_said)


@dataclass(frozen=True)
class Session:
    id: int
    started: float  # Unix time
    last_used: float  # the most recent is the current session
    allowed_tools: tuple[str, ...] = ()  # tools that may run without asking you in this session
    name: str = ""  # what you've called it, if you have
    muted: bool = False  # it doesn't notify your devices

    @property
    def chat_id(self) -> str:
        return f"{SESSION}{self.id}"


@dataclass(frozen=True)
class SessionSummary:
    """A session, as a list of them shows it."""

    id: int
    started: float
    last_used: float
    title: str  # its name, or your first message in it
    messages: int
    name: str = ""
    muted: bool = False
    last_message: int = 0  # the newest message's id, so the page can tell what's new


@dataclass(frozen=True)
class Message:
    """A message, as the dashboard shows it."""

    id: int
    role: str  # user or assistant
    text: str
    created_at: str  # ISO, in UTC
    channel: str = ""  # where it came from, such as "Telegram"; "Athena" for what it told you by itself
    tools: list[str] = field(default_factory=list)  # the tools used for a reply
    photos: list[str] = field(default_factory=list)  # a message's photos, by name in the PhotoStore


@dataclass(frozen=True)
class Exchange:
    """A message and the reply to it."""

    id: int  # the reply's message id
    chat_id: str
    question: str
    answer: str
    created_at: str
    tools: list[str] | None = None  # the tools used for the reply, in order; None if it wasn't noted
    photos: tuple[str, ...] = ()  # the message's photos, by name in the PhotoStore


@dataclass(frozen=True)
class Older:
    """The part of a chat to compact: the messages before the last few, and the summary of those before them."""

    summary: str | None
    messages: list[tuple[str, str]]  # (role, content), oldest first
    through: int  # the id of the last of them
    start: int  # the window's start when they were read, which compacting checks is unchanged
    cleared: int  # how many times everything had been forgotten, which compacting checks too


class Busy(Exception):
    """Athena's answering you, so you can't send another message, or start or change sessions, now."""

    def __init__(self, message: str = "I'm still answering. Wait until I've finished, or stop me, first.") -> None:
        super().__init__(message)


def session_of(chat_id: str) -> int | None:
    """The session a chat is, or None if it's a task's or another chat of Athena's own."""
    rest = chat_id.removeprefix(SESSION)
    return int(rest) if rest != chat_id and rest.isdigit() else None


class ConversationStore:
    def __init__(self, db_path: Path, max_messages: int = 40):
        self.max_messages = max(4, max_messages)
        self.photos = PhotoStore(db_path.parent / "photos")
        self.listeners: list[Callable[[], None]] = []  # told of every change, such as a new message
        self._cleared = 0  # times everything's been forgotten since starting, so a compaction from before can't land
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tools TEXT,
                    photos TEXT,
                    channel TEXT
                );
                CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL DEFAULT 0,
                    summary TEXT,
                    summary_session INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started REAL NOT NULL,
                    first_message INTEGER NOT NULL,
                    last_used REAL,
                    allowed_tools TEXT NOT NULL DEFAULT '[]',
                    name TEXT NOT NULL DEFAULT '',
                    muted INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            if "channel" not in {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}:
                self._conn.execute("ALTER TABLE messages ADD COLUMN channel TEXT")
            if "last_used" not in {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}:
                self._give_sessions_chats()
            if "name" not in {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                self._conn.execute("ALTER TABLE sessions ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        self.session = self._current_session() or self.new_session()

    def _give_sessions_chats(self) -> None:
        """From when a session was a time, not a conversation: everything said between starting one and the next
        becomes its chat, and tasks' chats keep what the model saw of them."""
        self._conn.execute("ALTER TABLE sessions ADD COLUMN last_used REAL")
        self._conn.execute("ALTER TABLE sessions ADD COLUMN allowed_tools TEXT NOT NULL DEFAULT '[]'")
        self._conn.execute("UPDATE sessions SET last_used = started")
        current = self._conn.execute("SELECT id, first_message FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if current is None:
            return
        background = " OR ".join(f"chat_id LIKE '{prefix}%'" for prefix in BACKGROUND)
        query = f"SELECT DISTINCT chat_id FROM messages WHERE NOT ({background})"
        yours = [row[0] for row in self._conn.execute(query)]
        # What the model saw of your chat in the current session: its window and summary, from the chat you used last.
        last = self._conn.execute(
            f"SELECT chat_id FROM messages WHERE NOT ({background}) ORDER BY id DESC LIMIT 1"
        ).fetchone()
        state = (
            last
            and self._conn.execute(
                "SELECT window_start, summary, summary_session FROM chat_state WHERE chat_id = ?", (last[0],)
            ).fetchone()
        )
        for chat in yours:
            self._conn.execute(
                "UPDATE messages SET channel = CASE WHEN role = 'user' AND content LIKE '(%)' THEN 'Athena' "
                "WHEN chat_id GLOB '[0-9]*' THEN 'Telegram' WHEN chat_id = 'cli' THEN 'Terminal' END, "
                "chat_id = 'session:' || COALESCE((SELECT MAX(s.id) FROM sessions AS s WHERE s.first_message < "
                "messages.id), (SELECT MIN(id) FROM sessions)) WHERE chat_id = ?",
                (chat,),
            )
            self._conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat,))
        if state:
            window, summary, made_in = state
            self._conn.execute(
                "INSERT INTO chat_state (chat_id, window_start, summary) VALUES (?, ?, ?)",
                (f"{SESSION}{current[0]}", max(window, current[1]), summary if made_in == current[0] else None),
            )
        # Tasks' chats: what's from before the current session, the model didn't see, and still doesn't.
        for (chat,) in self._conn.execute(f"SELECT DISTINCT chat_id FROM messages WHERE {background}").fetchall():
            self._conn.execute(
                "INSERT INTO chat_state (chat_id, window_start) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "window_start = MAX(window_start, excluded.window_start), summary = CASE WHEN summary_session = ? "
                "THEN summary END",
                (chat, current[1], current[0]),
            )

    # -- sessions ------------------------------------------------------------------------------

    def _session(self, row: tuple[Any, ...]) -> Session:
        id_, started, used, allowed, name, muted = row
        return Session(
            id_, started, used if used is not None else started, tuple(json.loads(allowed)), name, bool(muted)
        )

    def _current_session(self) -> Session | None:
        row = self._conn.execute(f"SELECT {_SESSION} FROM sessions ORDER BY last_used DESC, id DESC LIMIT 1").fetchone()
        return self._session(row) if row else None

    @property
    def current_chat(self) -> str:
        """The current session's chat, where what you say goes."""
        return self.session.chat_id

    def _last_message_id(self) -> int:
        # Ids are never reused (AUTOINCREMENT), even after messages are deleted.
        row = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        return row[0] if row else 0

    def new_session(self, name: str = "", *, current: bool = True) -> Session:
        """Start a new conversation, and make it the current session, unless not ``current``: then it's listed
        just after the current one, as for a task's."""
        now = time.time()
        with self._lock, self._conn:
            used = now if current else min(now, self.session.last_used) - 0.001
            cur = self._conn.execute(
                "INSERT INTO sessions (started, first_message, last_used, name) VALUES (?, ?, ?, ?)",
                (now, self._last_message_id(), used, _name(name)),
            )
            made = Session(int(cur.lastrowid), now, used, name=_name(name))  # type: ignore[arg-type]
            if current:
                self.session = made
        self._changed()
        return made

    def use_session(self, session_id: int) -> Session:
        """Make a session the current one again. KeyError if there's no such session."""
        with self._lock, self._conn:
            if not self._conn.execute(
                "UPDATE sessions SET last_used = ? WHERE id = ?", (time.time(), session_id)
            ).rowcount:
                raise KeyError(session_id)
            self.session = self._current_session()  # type: ignore[assignment]
        self._changed()
        return self.session

    def get_session(self, session_id: int) -> Session | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_SESSION} FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session(row) if row else None

    def _set(self, session_id: int, column: str, value: Any) -> Session:
        with self._lock, self._conn:
            if not self._conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, session_id)).rowcount:
                raise KeyError(session_id)
            if session_id == self.session.id:
                self.session = self._current_session()  # type: ignore[assignment]
        self._changed()
        return self.get_session(session_id)  # type: ignore[return-value]

    def set_allowed_tools(self, session_id: int, tools: Collection[str]) -> Session:
        """The tools that may run without asking you in a session. KeyError if there's no such session."""
        return self._set(session_id, "allowed_tools", json.dumps(sorted(set(tools))))

    def rename(self, session_id: int, name: str) -> Session:
        """Call a session something, or nothing (""), when it's called after your first message in it. KeyError if
        there's no such session."""
        return self._set(session_id, "name", _name(name))

    def set_muted(self, session_id: int, muted: bool) -> Session:
        """Whether a session keeps from notifying your devices. KeyError if there's no such session."""
        return self._set(session_id, "muted", int(muted))

    def sessions(self, limit: int = 100, also: Collection[int] = ()) -> list[SessionSummary]:
        """Sessions, the one used last first: those with something said in them or a name, the current one, and
        those in ``also``, such as those with tasks."""
        also = sorted({int(i) for i in also})
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.started, COALESCE(s.last_used, s.started), s.name, s.muted, "
                "(SELECT content FROM messages WHERE chat_id = 'session:' || s.id AND role = 'user' ORDER BY id "
                "LIMIT 1), (SELECT COUNT(*) FROM messages WHERE chat_id = 'session:' || s.id) AS said, "
                "(SELECT MAX(id) FROM messages WHERE chat_id = 'session:' || s.id) "
                "FROM sessions AS s WHERE s.id = ? OR said > 0 OR s.name != '' "
                f"OR s.id IN ({','.join('?' * len(also))}) "
                "ORDER BY COALESCE(s.last_used, s.started) DESC, s.id DESC LIMIT ?",
                (self.session.id, *also, limit),
            ).fetchall()
        return [
            SessionSummary(i, started, used, name or _title(first or ""), n, name, bool(muted), last or 0)
            for i, started, used, name, muted, first, n, last in rows
        ]

    def title(self, session_id: int) -> str:
        """What a session's called: its name, or your first message in it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT s.name, (SELECT content FROM messages WHERE chat_id = 'session:' || s.id AND role = 'user' "
                "ORDER BY id LIMIT 1) FROM sessions AS s WHERE s.id = ?",
                (session_id,),
            ).fetchone()
        return (row[0] or _title(row[1] or "")) if row else ""

    def last_message(self) -> tuple[int, str, int] | None:
        """The newest message in any session: (its id, its role, its session)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, role, chat_id FROM messages WHERE chat_id LIKE 'session:%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (row[0], row[1], session_of(row[2]) or 0) if row else None

    def delete_session(self, session_id: int) -> list[Exchange]:
        """Delete a session, and everything said in it, with its photos unless another message has them. Returns
        its exchanges, to take out of memory too. If it was the current session, the one used before it is now,
        or a new one. KeyError if there's no such session."""
        chat_id = f"{SESSION}{session_id}"
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
                raise KeyError(session_id)
            rows = self._conn.execute(
                "SELECT id, role, content, created_at, photos FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            photos = {name for row in rows for name in json.loads(row[4] or "[]")}
            still = {
                name
                for (names,) in self._conn.execute("SELECT photos FROM messages WHERE photos IS NOT NULL")
                for name in json.loads(names)
            }
            current = self._current_session() if session_id == self.session.id else self.session
        for name in photos - still:
            self.photos.delete(name)
        exchanges = [
            Exchange(answer[0], chat_id, question[2], answer[2], answer[3])
            for question, answer in zip(rows, rows[1:], strict=False)
            if question[1] == "user" and answer[1] == "assistant"
        ]
        if current is None:
            self.new_session()
        else:
            self.session = current
            self._changed()
        return exchanges

    def adopt(self, chat_id: str, name: str, *, prefix: str = "", drop: str = "") -> Session:
        """Make a chat of Athena's own, such as a task's from before tasks ran in sessions, a session: listed just
        after the current one, with what was said in it and what the model sees of it. ``prefix`` goes before each
        of the messages on your side, and exchanges whose answer has ``drop`` in it are left out."""
        session = self.new_session(name, current=False)
        with self._lock, self._conn:
            if drop:
                answers = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' AND instr(upper(content), ?)",
                        (chat_id, drop.upper()),
                    )
                ]
                for answer in answers:
                    self._conn.execute(
                        "DELETE FROM messages WHERE id = ? OR id = (SELECT MAX(id) FROM messages WHERE chat_id = ? "
                        "AND id < ? AND role = 'user')",
                        (answer, chat_id, answer),
                    )
            self._conn.execute(
                "UPDATE messages SET content = CASE WHEN role = 'user' THEN ? || content ELSE content END, "
                "chat_id = ?, channel = CASE WHEN role = 'user' THEN 'Task' END WHERE chat_id = ?",
                (prefix, session.chat_id, chat_id),
            )
            self._conn.execute("UPDATE chat_state SET chat_id = ? WHERE chat_id = ?", (session.chat_id, chat_id))
        self._changed()
        return session

    def sessions_started(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def messages(self, session_id: int, before: int | None = None, limit: int = 50) -> tuple[list[Message], bool]:
        """A session's messages, oldest first: the last ``limit``, or those before the message ``before``, and
        whether there are earlier ones. Everything said in it, not just what the model sees now."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, role, content, created_at, channel, tools, photos FROM messages "
                "WHERE chat_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (f"{SESSION}{session_id}", before if before is not None else 2**62, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        return [
            Message(i, role, text, at, channel or "", json.loads(tools) if tools else [], json.loads(photos or "[]"))
            for i, role, text, at, channel, tools, photos in reversed(rows[:limit])
        ], more

    # -- what the model sees -------------------------------------------------------------------

    def _window_start(self, chat_id: str) -> int:
        row = self._conn.execute("SELECT window_start FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else 0

    def _set_window_start(self, chat_id: str, message_id: int) -> None:
        self._conn.execute(
            "INSERT INTO chat_state (chat_id, window_start) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET window_start = excluded.window_start",
            (chat_id, message_id),
        )

    def load(self, chat_id: str) -> list[dict[str, Any]]:
        """Messages the model should see, oldest first, always starting with a user message, and with the chat's
        summary at the start if it's been compacted. The last IN_CONTEXT messages with photos have them, as long
        as they're kept."""
        with self._lock, self._conn:
            rows = self._rows(chat_id)
            if len(rows) > 2 * self.max_messages:
                # It should have been compacted long before (the model may be down): drop the oldest messages
                # in one step, down to half the limit, so the prefix stays stable (and cached) for a while.
                keep = _from_a_question(rows[-(self.max_messages // 2) :])
                self._set_window_start(chat_id, rows[len(rows) - len(keep) - 1][0])
                rows = keep
            summary = self._summary(chat_id)
        shown = {row[0] for row in [r for r in rows if r[3]][-IN_CONTEXT:]}
        messages: list[dict[str, Any]] = []
        for message_id, role, content, names in rows:
            if summary and not messages:
                content = f"{summary_block(summary)}\n\n{content}"
            photos = [p for name in json.loads(names) if (p := self.photos.load(name))] if message_id in shown else []
            messages.append({"role": role, "content": with_photos(content, photos)})
        return messages

    def _rows(self, chat_id: str) -> list[tuple[int, str, str, str | None]]:
        return self._conn.execute(
            "SELECT id, role, content, photos FROM messages WHERE chat_id = ? AND id > ? ORDER BY id",
            (chat_id, self._window_start(chat_id)),
        ).fetchall()

    def _summary(self, chat_id: str) -> str | None:
        row = self._conn.execute("SELECT summary FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def length(self, chat_id: str) -> int:
        """How many messages the model sees in a chat."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND id > ?", (chat_id, self._window_start(chat_id))
            ).fetchone()[0]

    def older(self, chat_id: str, keep: int) -> Older | None:
        """The messages to summarise when compacting a chat: all it sees but the last ``keep`` or so, which start
        with a message of yours. None if there aren't any."""
        with self._lock:
            start = self._window_start(chat_id)
            rows = self._rows(chat_id)
            kept = _from_a_question(rows[-keep:]) if keep else []
            older = rows[: len(rows) - len(kept)]
            if not older:
                return None
            return Older(
                self._summary(chat_id),
                [(role, content) for _, role, content, _ in older],
                older[-1][0],
                start,
                self._cleared,
            )

    def compact(self, chat_id: str, older: Older, summary: str) -> bool:
        """Replace ``older`` with ``summary``. False, and nothing's changed, if the chat's moved on in a way that
        means it no longer applies: a reset, everything forgotten, or another compaction."""
        with self._lock, self._conn:
            if self._cleared != older.cleared or self._window_start(chat_id) != older.start:
                return False
            self._conn.execute(
                "INSERT INTO chat_state (chat_id, window_start, summary) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET window_start = excluded.window_start, summary = excluded.summary",
                (chat_id, older.through, summary),
            )
        return True

    # -- changing it ---------------------------------------------------------------------------

    def append_exchange(
        self,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        tools: list[str] | None = None,
        photos: list[Photo] | None = None,
        channel: str = "",
    ) -> None:
        """Keep a message, with its photos if it has any, and the reply to it, with the tools used for the reply
        if they're known, and where the message came from."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        used = json.dumps(tools) if tools is not None else None
        kept = json.dumps([self.photos.keep(p) for p in photos]) if photos else None
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO messages (chat_id, role, content, created_at, tools, photos, channel) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (chat_id, "user", user_text, now, None, kept, channel or None),
                    (chat_id, "assistant", assistant_text, now, used, None, None),
                ],
            )
        self._changed()

    def take_last(self, chat_id: str) -> Exchange | None:
        """Delete the last message in a chat and the reply to it, if the model sees them, and return them:
        for /retry, which answers it again."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, role, content, created_at, photos FROM messages WHERE chat_id = ? AND id > ? "
                "ORDER BY id DESC LIMIT 2",
                (chat_id, self._window_start(chat_id)),
            ).fetchall()
            if len(rows) < 2 or rows[0][1] != "assistant" or rows[1][1] != "user":
                return None
            (answer_id, _, answer, created_at, _), (question_id, _, question, _, names) = rows
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM messages WHERE id IN (?, ?)", (question_id, answer_id))
        self._changed()
        # Its photos are kept: /retry sends them again.
        return Exchange(answer_id, chat_id, question, answer, created_at, photos=tuple(json.loads(names or "[]")))

    def exchanges_after(self, message_id: int, limit: int = 32) -> list[Exchange]:
        """Up to ``limit`` exchanges whose reply came after ``message_id``, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.id, a.chat_id, q.content, a.content, a.created_at FROM messages AS a "
                "JOIN messages AS q ON q.id = (SELECT MAX(id) FROM messages "
                "WHERE chat_id = a.chat_id AND id < a.id AND role = 'user') "
                "WHERE a.role = 'assistant' AND a.id > ? ORDER BY a.id LIMIT ?",
                (message_id, limit),
            ).fetchall()
        return [Exchange(*row) for row in rows]

    def exchanges_since(self, when: str, limit: int = 200) -> list[Exchange]:
        """Exchanges since ``when`` (an ISO time in UTC) whose tools were noted, oldest first, in every chat."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.id, a.chat_id, q.content, a.content, a.created_at, a.tools FROM messages AS a "
                "JOIN messages AS q ON q.id = (SELECT MAX(id) FROM messages "
                "WHERE chat_id = a.chat_id AND id < a.id AND role = 'user') "
                "WHERE a.role = 'assistant' AND a.created_at >= ? AND a.tools IS NOT NULL "
                "ORDER BY a.id DESC LIMIT ?",
                (when, limit),
            ).fetchall()
        return [Exchange(*row[:5], tools=json.loads(row[5])) for row in reversed(rows)]

    def reset(self, chat_id: str) -> None:
        """Start a chat afresh: the model sees nothing said in it so far."""
        with self._lock, self._conn:
            self._set_window_start(chat_id, self._last_message_id())
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("UPDATE chat_state SET summary = NULL WHERE chat_id = ?", (chat_id,))
        self._changed()

    def clear(self) -> Session:
        """Delete every message, and every photo, in every chat, and start a new session."""
        with self._lock, self._conn:
            self._cleared += 1
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM chat_state")
            self._conn.execute("UPDATE sessions SET allowed_tools = '[]'")
        self.photos.clear()
        return self.new_session()

    def _changed(self) -> None:
        for listener in list(self.listeners):
            listener()

    def close(self) -> None:
        self._conn.close()


def summary_block(summary: str) -> str:
    """A chat's summary as the model sees it, before the messages since."""
    return f"<summary>\nWhat we said earlier in this conversation, summarised:\n{summary}\n</summary>"


def task_said(title: str) -> str:
    """How a task's run starts, on your side of its session's conversation."""
    return f"(Your task “{title}”)"


def _name(name: str) -> str:
    return " ".join(name.split())[:NAME_CHARS]


def _title(text: str) -> str:
    if ran := _TASK_RAN.match(text):  # a session that started with a task's run is called after the task
        return ran.group(1)
    text = " ".join(text.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


def _from_a_question(rows: list[Any]) -> list[Any]:
    """``rows`` from the first of your messages on, so what the model sees starts with one."""
    while rows and rows[0][1] != "user":
        rows = rows[1:]
    return rows
