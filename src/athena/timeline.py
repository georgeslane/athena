"""What Athena did, in order, for the dashboard's Activity tab: your messages and its answers, the tasks it ran,
its checks on what it's watching, alerts, and when it started. Each keeps the steps it took: every tool it used,
what that was given and gave back, whether it ran, and who allowed it.

It holds what was said, cut short, as the conversation history does, so it's kept for KEEP_DAYS and forgetting
everything deletes it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.memory import connect_db

KEEP_DAYS = 30
MAX_STEPS, STEP_CHARS = 40, 400  # tool calls kept of each thing it did, and how much of each's in and out
TITLE_CHARS, SAID_CHARS = 300, 4000
PAGE = 50  # things shown at once

# What it did.
MESSAGE, TASK, CHECK, ALERT, ATHENA = "message", "task", "check", "alert", "athena"
KINDS = (MESSAGE, TASK, CHECK, ALERT, ATHENA)


@dataclass
class ToolEvent:
    """A tool call, as the agent made it (Agent.respond's on_tool)."""

    tool: str
    args: dict[str, Any]
    output: str  # what the model was told
    outcome: str  # RAN, FAILED or DECLINED (stats.py)
    approval: str = ""  # if it asks first: "you" if you were asked, "allowed" if it may run without asking here
    flagged: str = ""  # what the privacy check found in what it would send (privacy.py)


def cut(text: str, limit: int = STEP_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def steps() -> tuple[list[dict[str, str]], Callable[[ToolEvent], Any]]:
    """What a run does, as it does it: a list it fills, and the on_tool for Agent.respond that fills it with each
    tool call, what it was given and gave back (cut short), what happened, and, if it asks first, whether you were
    asked or it was allowed without asking."""
    taken: list[dict[str, str]] = []

    async def on_tool(event: ToolEvent) -> None:
        if len(taken) >= MAX_STEPS:
            return
        given = json.dumps(event.args, ensure_ascii=False) if event.args else ""
        step = {"tool": event.tool, "given": cut(given), "outcome": event.outcome, "said": cut(event.output)}
        if event.approval:
            step["approval"] = event.approval
        if event.flagged:
            step["flagged"] = event.flagged
        taken.append(step)

    return taken, on_tool


@dataclass
class Entry:
    id: int
    at: float  # when it finished, in Unix time
    kind: str  # one of KINDS
    title: str  # your message, the task's title, or what happened
    outcome: str  # how it went, such as "answered", "told you" or "failed: ..."
    said: str = ""  # Athena's answer, what a task found, or why something isn't working
    channel: str = ""  # where a message came from, such as "Telegram"
    seconds: float = 0.0  # how long it took
    steps: list[dict[str, str]] = field(default_factory=list)  # from steps()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "title": self.title,
            "outcome": self.outcome,
            "said": self.said,
            "channel": self.channel,
            "seconds": round(self.seconds, 1),
            "steps": self.steps,
        }


class Timeline:
    def __init__(self, db_path: Path, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    said TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    seconds REAL NOT NULL DEFAULT 0,
                    steps TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS timeline_at ON timeline(at);
                """
            )

    def add(
        self,
        kind: str,
        title: str,
        outcome: str,
        *,
        said: str = "",
        channel: str = "",
        seconds: float = 0.0,
        steps: list[dict[str, str]] | None = None,
    ) -> int:
        """Note something Athena's just done, and forget what's older than KEEP_DAYS."""
        at = self.clock()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO timeline (at, kind, title, outcome, said, channel, seconds, steps) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    at,
                    kind,
                    cut(" ".join(title.split()), TITLE_CHARS),
                    outcome,
                    cut(said.strip(), SAID_CHARS),
                    channel,
                    seconds,
                    json.dumps((steps or [])[:MAX_STEPS], ensure_ascii=False),
                ),
            )
            self._conn.execute("DELETE FROM timeline WHERE at < ?", (at - KEEP_DAYS * 86400,))
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def page(self, before: int | None = None, kinds: Collection[str] = (), limit: int = PAGE) -> list[Entry]:
        """The newest things done, before the one with id ``before`` if given, of ``kinds`` if any are given."""
        where, params = [], []
        if before is not None:
            where.append("id < ?")
            params.append(before)
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            params.extend(kinds)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, at, kind, title, outcome, said, channel, seconds, steps FROM timeline {clause} "
                "ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [Entry(*row[:8], steps=json.loads(row[8])) for row in rows]

    def clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM timeline")

    def close(self) -> None:
        self._conn.close()
