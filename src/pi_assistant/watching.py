"""Watching: things Athena keeps an eye on for you, such as "when Claude finishes the bank project" or "if gold
drops below 4,000", checked together every [tasks] heartbeat_minutes by the scheduler (tasks.py).

Each check is one turn, with everything on the list in front of the model: it uses its tools to see where each
stands, tells you only what needs your attention, and keeps a short note on each for next time, such as the
last price it saw, or that it's already told you. When something's happened, or no longer matters, it takes it
off the list. Nothing on the list means no check, so it costs nothing until you ask Athena to watch something.

It's added to only while Athena's answering you, so something it reads while working by itself, such as a web
page, can't put things on it. You can add and remove things on the dashboard's Tasks tab too.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from pi_assistant.tools import Tool, ToolError, current_turn

WATCH = "watch"
MAX_WATCHES = 20
WHAT_CHARS, NOTE_CHARS = 300, 500
DEFAULT_DAYS, MAX_DAYS = 14, 90  # how long something's watched for, unless it's taken off sooner


@dataclass
class Watch:
    id: int
    what: str  # what to keep an eye on, and when to tell you
    created: float  # Unix time
    until: float  # when it's no longer watched, if it hasn't happened by then
    by: str  # you, or athena
    note: str = ""  # what Athena noted when it last checked, for next time
    checked: float | None = None


class WatchStore:
    """What's watched, in Athena's database."""

    def __init__(self, db_path: Path):
        from pi_assistant.memory import connect_db

        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    what TEXT NOT NULL,
                    created REAL NOT NULL,
                    until REAL NOT NULL,
                    by TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    checked REAL
                )
                """
            )

    def all(self) -> list[Watch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, what, created, until, by, note, checked FROM watches ORDER BY id"
            ).fetchall()
        return [Watch(*row) for row in rows]

    def get(self, watch_id: int) -> Watch | None:
        return next((w for w in self.all() if w.id == watch_id), None)

    def add(self, what: str, days: float, by: str) -> Watch:
        what = " ".join(what.split())
        if not what:
            raise ValueError("Say what to watch for.")
        if len(what) > WHAT_CHARS:
            raise ValueError(f"Say what to watch for in up to {WHAT_CHARS} characters.")
        if not 0 < days <= MAX_DAYS:
            raise ValueError(f"Watch for between 1 and {MAX_DAYS} days.")
        if len(self.all()) >= MAX_WATCHES:
            raise ValueError(f"{MAX_WATCHES} things are being watched already: take some off first.")
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO watches (what, created, until, by) VALUES (?, ?, ?, ?)",
                (what, now, now + days * 86400, by),
            )
        return Watch(int(cur.lastrowid), what, now, now + days * 86400, by)  # type: ignore[arg-type]

    def note(self, watch_id: int, note: str) -> None:
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE watches SET note = ? WHERE id = ?", (note.strip()[:NOTE_CHARS], watch_id)
            ).rowcount
        if not changed:
            raise ValueError(f"Nothing's watched as #{watch_id}.")

    def checked(self, when: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE watches SET checked = ?", (when,))

    def remove(self, watch_id: int) -> Watch:
        watch = self.get(watch_id)
        if watch is None:
            raise ValueError(f"Nothing's watched as #{watch_id}.")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return watch

    def expire(self, now: float) -> list[Watch]:
        """Take off what's been watched for as long as it was going to be. Returns what was."""
        gone = [w for w in self.all() if w.until <= now]
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM watches WHERE id = ?", [(w.id,) for w in gone])
        return gone

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def describe(watch: Watch, tz: tzinfo) -> str:
    """One line for the model: its id, what, since and until when, and the note from last time."""
    since = datetime.fromtimestamp(watch.created, tz)
    until = datetime.fromtimestamp(watch.until, tz)
    note = f" Your note from last time: {watch.note}" if watch.note else ""
    return f"#{watch.id}: {watch.what} (since {since:%a %-d %b}, until {until:%a %-d %b}).{note}"


def watch_tool(store: WatchStore, tz: tzinfo, every_minutes: float, on_change: Any = lambda: None) -> Tool:
    async def watch(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "")
        try:
            if action == "add":
                if current_turn().alone:
                    raise ToolError(
                        "You can only start watching something while you're answering the user, when they ask."
                    )
                try:
                    days = float(args.get("days") or DEFAULT_DAYS)
                except (TypeError, ValueError):
                    raise ToolError("days must be a number.") from None
                added = store.add(str(args.get("what") or ""), days, by="athena")
                on_change()
                return (
                    f"Watching #{added.id}: {added.what}. You'll check on it every {every_minutes:g} minutes, "
                    f"until {datetime.fromtimestamp(added.until, tz):%a %-d %b}, and tell the user when it needs "
                    "their attention."
                )
            if action in ("note", "done"):
                watch_id = int(args.get("id"))  # type: ignore[arg-type]
                if action == "note":
                    store.note(watch_id, str(args.get("note") or ""))
                    return f"Noted on #{watch_id}, for next time."
                gone = store.remove(watch_id)
                on_change()
                return f"Stopped watching #{gone.id}: {gone.what}."
            if action == "list":
                watches = store.all()
                return "\n".join(describe(w, tz) for w in watches) if watches else "Nothing's being watched."
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc):
                raise ToolError(str(exc)) from None
            raise ToolError("Give the id of what's watched, from action list.") from None
        raise ToolError("Say what to do: add, note, done or list.")

    return Tool(
        name=WATCH,
        description=(
            "Keep an eye on something for the user and tell them when it needs their attention, such as "
            "“when Claude finishes the bank project” or “if gold drops below 4,000”. Everything watched is "
            f"checked together every {every_minutes:g} minutes. add: what to watch for and when to tell them "
            f"(only when they ask; for days, default {DEFAULT_DAYS}). note: keep what you'll need next time, such "
            "as the last price, or that you've told them. done: when it's happened or no longer matters. list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "note", "done", "list"]},
                "what": {"type": "string", "description": "add: what to watch for, and when to tell the user"},
                "days": {"type": "number", "description": f"add: for how long, at most {MAX_DAYS}"},
                "id": {"type": "integer", "description": "note and done: which"},
                "note": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=watch,
    )
