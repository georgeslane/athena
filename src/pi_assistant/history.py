"""Per-chat conversation history, stored in SQLite.

Only the user's text and the assistant's final reply are kept (not intermediate
tool calls), which keeps prompts short. Messages are never deleted: /reset and
trimming just move the start of the window the model sees.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from pi_assistant.memory import connect_db


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
                """
            )

    def _window_start(self, chat_id: str) -> int:
        row = self._conn.execute("SELECT window_start FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else 0

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

    def reset(self, chat_id: str) -> None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
            self._set_window_start(chat_id, row[0])

    def close(self) -> None:
        self._conn.close()
