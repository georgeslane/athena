"""Per-chat conversation history and sessions, stored in SQLite.

Only the user's text and the assistant's final reply are kept (not intermediate
tool calls), which keeps prompts short. Messages are only deleted when you tell
Athena to forget everything, or when /retry answers the last one again: /session,
/reset and trimming just move the start of the window the model sees.

A session is everything since you last started one with /session (or from the
dashboard). It applies to every chat, and usage statistics are counted per session.

Each reply also notes the tools Athena used for it, in order, so it can look back at how it
did things and write skills for what it does the same way often (skills.py).

A message with photos notes them by name, and they're kept in the data folder (photos.py). The model
sees the photos of the last few such messages, and the rest just as their text, which says "[Photo]".

A chat's older messages can be compacted: summarised by the model (compaction.py), with the summary
kept for the chat and shown at the start of what the model sees, and the window moved past them. A
summary belongs to the session it was made in, so a new session, /reset or forgetting everything ends it.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pi_assistant.memory import connect_db
from pi_assistant.photos import IN_CONTEXT, Photo, PhotoStore, with_photos


@dataclass(frozen=True)
class Session:
    id: int
    started: float  # Unix time
    first_message: int  # the model only sees messages after this one


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
    session: int


class ConversationStore:
    def __init__(self, db_path: Path, max_messages: int = 40):
        self.max_messages = max(4, max_messages)
        self.photos = PhotoStore(db_path.parent / "photos")
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
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started REAL NOT NULL,
                    first_message INTEGER NOT NULL
                );
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
            if "tools" not in columns:  # a database from before replies noted their tools
                self._conn.execute("ALTER TABLE messages ADD COLUMN tools TEXT")
            if "photos" not in columns:  # or before messages could have photos
                self._conn.execute("ALTER TABLE messages ADD COLUMN photos TEXT")
            state = {row[1] for row in self._conn.execute("PRAGMA table_info(chat_state)")}
            if "summary" not in state:  # or before chats could be compacted
                self._conn.execute("ALTER TABLE chat_state ADD COLUMN summary TEXT")
                self._conn.execute("ALTER TABLE chat_state ADD COLUMN summary_session INTEGER")
        self.session = self._current_session() or self.new_session(first_message=0)

    def _current_session(self) -> Session | None:
        row = self._conn.execute("SELECT id, started, first_message FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return Session(*row) if row else None

    def _last_message_id(self) -> int:
        # Ids are never reused (AUTOINCREMENT), even after messages are deleted.
        row = self._conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'messages'").fetchone()
        return row[0] if row else 0

    def new_session(self, first_message: int | None = None) -> Session:
        """Start a new session: from now on, the model doesn't see earlier messages in any chat."""
        started = time.time()
        with self._lock, self._conn:
            first = self._last_message_id() if first_message is None else first_message
            cur = self._conn.execute("INSERT INTO sessions (started, first_message) VALUES (?, ?)", (started, first))
            self.session = Session(int(cur.lastrowid), started, first)
        return self.session

    def sessions_started(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def _window_start(self, chat_id: str) -> int:
        row = self._conn.execute("SELECT window_start FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return max(row[0] if row else 0, self.session.first_message)

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
        row = self._conn.execute(
            "SELECT summary FROM chat_state WHERE chat_id = ? AND summary_session = ?", (chat_id, self.session.id)
        ).fetchone()
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
                self.session.id,
            )

    def compact(self, chat_id: str, older: Older, summary: str) -> bool:
        """Replace ``older`` with ``summary``. False, and nothing's changed, if the chat's moved on in a way that
        means it no longer applies: a new session, a reset, everything forgotten, or another compaction."""
        with self._lock, self._conn:
            if self.session.id != older.session or self._window_start(chat_id) != older.start:
                return False
            self._conn.execute(
                "INSERT INTO chat_state (chat_id, window_start, summary, summary_session) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET window_start = excluded.window_start, summary = excluded.summary, "
                "summary_session = excluded.summary_session",
                (chat_id, older.through, summary, older.session),
            )
        return True

    def append_exchange(
        self,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        tools: list[str] | None = None,
        photos: list[Photo] | None = None,
    ) -> None:
        """Keep a message, with its photos if it has any, and the reply to it, with the tools used for the reply
        if they're known."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        used = json.dumps(tools) if tools is not None else None
        kept = json.dumps([self.photos.keep(p) for p in photos]) if photos else None
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO messages (chat_id, role, content, created_at, tools, photos) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (chat_id, "user", user_text, now, None, kept),
                    (chat_id, "assistant", assistant_text, now, used, None),
                ],
            )

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
        with self._lock, self._conn:
            self._set_window_start(chat_id, self._last_message_id())
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute(
                "UPDATE chat_state SET summary = NULL, summary_session = NULL WHERE chat_id = ?", (chat_id,)
            )

    def clear(self) -> Session:
        """Delete every message, and every photo, in every chat, and start a new session."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM chat_state")
        self.photos.clear()
        return self.new_session()

    def close(self) -> None:
        self._conn.close()


def summary_block(summary: str) -> str:
    """A chat's summary as the model sees it, before the messages since."""
    return f"<summary>\nWhat we said earlier in this conversation, summarised:\n{summary}\n</summary>"


def _from_a_question(rows: list[Any]) -> list[Any]:
    """``rows`` from the first of your messages on, so what the model sees starts with one."""
    while rows and rows[0][1] != "user":
        rows = rows[1:]
    return rows
