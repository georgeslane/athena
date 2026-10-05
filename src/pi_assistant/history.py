"""Per-chat conversation history and sessions, stored in SQLite.

Only the user's text and the assistant's final reply are kept (not intermediate
tool calls), which keeps prompts short. Messages are only deleted when you tell
Athena to forget everything: /session, /reset and trimming just move the start of
the window the model sees.

A session is everything since you last started one with /session (or from the
dashboard). It applies to every chat, and usage statistics are counted per session.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pi_assistant.memory import connect_db


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


class ConversationStore:
    def __init__(self, db_path: Path, max_messages: int = 40):
        self.max_messages = max(4, max_messages)
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

    def load(self, chat_id: str) -> list[dict[str, str]]:
        """Messages the model should see, oldest first, always starting with a user message."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, role, content FROM messages WHERE chat_id = ? AND id > ? ORDER BY id",
                (chat_id, self._window_start(chat_id)),
            ).fetchall()
            if len(rows) > self.max_messages:
                # Drop the oldest messages in one step, down to half the limit, so the
                # prefix stays stable (and cached) for the next several turns.
                keep = rows[-(self.max_messages // 2) :]
                while keep and keep[0][1] != "user":
                    keep = keep[1:]
                cut_after = rows[len(rows) - len(keep) - 1][0]
                self._set_window_start(chat_id, cut_after)
                rows = keep
        return [{"role": role, "content": content} for _, role, content in rows]

    def append_exchange(self, chat_id: str, user_text: str, assistant_text: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(chat_id, "user", user_text, now), (chat_id, "assistant", assistant_text, now)],
            )

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

    def reset(self, chat_id: str) -> None:
        with self._lock, self._conn:
            self._set_window_start(chat_id, self._last_message_id())

    def clear(self) -> Session:
        """Delete every message, in every chat, and start a new session."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM chat_state")
        return self.new_session()

    def close(self) -> None:
        self._conn.close()
