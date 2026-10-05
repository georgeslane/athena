"""Usage statistics for the dashboard: how many messages Athena has answered, which tools it used,
and how much of the model's context the conversation takes up. Per session, and in total.

Only counts and timings are kept here, never what was said.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pi_assistant.memory import connect_db


@dataclass
class QueryRecord:
    """One message answered (or not), as the agent saw it."""

    session: int
    channel: str = ""  # e.g. "Telegram", "Siri"
    started: float = field(default_factory=time.time)
    seconds: float = 0.0
    model_calls: int = 0
    prompt_tokens: int = 0  # the most the model read in one go while answering
    completion_tokens: int = 0  # everything it wrote
    context_tokens: int = 0  # read and written by the last call: the whole conversation by the end
    error: str = ""  # why it failed, if it did
    tools: list[tuple[str, str]] = field(default_factory=list)  # (tool, outcome) for each call


# What happened to a tool call.
RAN, FAILED, DECLINED = "ran", "failed", "declined"


class UsageStats:
    def __init__(self, db_path: Path):
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stats_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    started REAL NOT NULL,
                    seconds REAL NOT NULL,
                    model_calls INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    context_tokens INTEGER NOT NULL,
                    error TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS stats_queries_session ON stats_queries(session);
                CREATE TABLE IF NOT EXISTS stats_tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query INTEGER NOT NULL,
                    session INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    outcome TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS stats_tool_calls_session ON stats_tool_calls(session);
                -- How much of the model's context the conversation took up, last time it was measured.
                CREATE TABLE IF NOT EXISTS stats_context (
                    session INTEGER PRIMARY KEY,
                    tokens INTEGER NOT NULL,
                    measured REAL NOT NULL
                );
                """
            )

    def record(self, q: QueryRecord) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO stats_queries (session, channel, started, seconds, model_calls, prompt_tokens, "
                "completion_tokens, context_tokens, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    q.session,
                    q.channel,
                    q.started,
                    q.seconds,
                    q.model_calls,
                    q.prompt_tokens,
                    q.completion_tokens,
                    q.context_tokens,
                    q.error,
                ),
            )
            self._conn.executemany(
                "INSERT INTO stats_tool_calls (query, session, tool, outcome) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, q.session, tool, outcome) for tool, outcome in q.tools],
            )
            if q.context_tokens:
                self._note_context(q.session, q.context_tokens, q.started + q.seconds)

    def note_context(self, session: int, tokens: int, measured: float | None = None) -> None:
        """Note how many tokens the conversation takes up now, e.g. from warming up the model's cache."""
        with self._lock, self._conn:
            self._note_context(session, tokens, measured or time.time())

    def _note_context(self, session: int, tokens: int, measured: float) -> None:
        self._conn.execute(
            "INSERT INTO stats_context VALUES (?, ?, ?) ON CONFLICT(session) DO UPDATE "
            "SET tokens = excluded.tokens, measured = excluded.measured WHERE excluded.measured >= measured",
            (session, tokens, measured),
        )

    def summary(self, session: int | None = None) -> dict[str, Any]:
        """Totals for one session, or for all of them."""
        where, params = ("WHERE session = ?", (session,)) if session is not None else ("", ())
        with self._lock:
            queries, failed, seconds, first, largest = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(error != ''), 0), COALESCE(AVG(seconds), 0), MIN(started), "
                f"COALESCE(MAX(prompt_tokens), 0) FROM stats_queries {where}",
                params,
            ).fetchone()
            tools = self._conn.execute(
                "SELECT tool, COUNT(*), COALESCE(SUM(outcome = ?), 0), COALESCE(SUM(outcome = ?), 0) "
                f"FROM stats_tool_calls {where} GROUP BY tool ORDER BY COUNT(*) DESC, tool",
                (FAILED, DECLINED, *params),
            ).fetchall()
            context = None
            if session is not None:
                row = self._conn.execute(
                    "SELECT tokens, measured FROM stats_context WHERE session = ?", (session,)
                ).fetchone()
                context = {"tokens": row[0], "measured": row[1]} if row else None
        return {
            "queries": queries,
            "failed": failed,
            "average_seconds": round(seconds, 2),
            "first": first,
            "largest_prompt": largest,
            "tool_calls": sum(n for _, n, _, _ in tools),
            "tools": [{"name": t, "calls": n, "failed": f, "declined": d} for t, n, f, d in tools],
            "context": context,
        }

    def close(self) -> None:
        self._conn.close()
