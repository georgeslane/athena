"""Watching: things Athena keeps an eye on for you, such as "new emails about jobs" or "if gold drops below
4,000", checked together every hour ([tasks] heartbeat_minutes) by the scheduler (dags/scheduler.py).

Each check is one turn, with everything on the list in front of the model: it uses its tools to see where each
stands, tells you only what needs your attention, and keeps a short note on each for next time, such as the
last price it saw, or that it's already told you. When something's happened, or no longer matters, it takes it
off the list. Nothing on the list means no check, so it costs nothing until you ask Athena to watch something.

Each has a key, such as job-emails, which an agent's DAG can be triggered by ("watch:job-emails"): when it
happens, the check starts that DAG, with what it found, rather than telling you itself.

It's added to only while Athena's answering you, so something it reads while working by itself, such as a web
page, can't put things on it. You can add, change and remove things on the dashboard's Watching tab too.
Something's watched until a day you choose, or with no end, such as a look at your inbox every time.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any

from athena.clock import Clock
from athena.tools import Tool, ToolError, current_turn

WATCH = "watch"
MAX_WATCHES = 20
WHAT_CHARS, NOTE_CHARS = 300, 500
DEFAULT_DAYS = 14  # how long the model watches something for, unless it's asked for longer, or no end
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # as an event's name is (dags/spec.py)
_STOP_WORDS = frozenset("a an and any are at for from i if in into is it me my of on or the to when with".split())


@dataclass
class Watch:
    id: int
    what: str  # what to keep an eye on, and when to tell you
    created: float  # Unix time
    until: float | None  # when it's no longer watched, if it hasn't happened by then; None: no end
    by: str  # you, or athena
    note: str = ""  # what Athena noted when it last checked, for next time
    checked: float | None = None
    key: str = ""  # what a DAG's triggers call it: "watch:<key>"


class WatchStore:
    """What's watched, in Athena's database."""

    def __init__(self, db_path: Path, clock: Clock | None = None):
        from athena.memory import connect_db

        self.clock = clock or Clock()
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    what TEXT NOT NULL,
                    created REAL NOT NULL,
                    until REAL,
                    by TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    checked REAL
                )
                """
            )
            if any(row[1] == "until" and row[3] for row in self._conn.execute("PRAGMA table_info(watches)")):
                self._conn.executescript(  # from before things could be watched with no end: until can be empty now
                    """
                    ALTER TABLE watches RENAME TO watches_old;
                    CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL,
                        created REAL NOT NULL, until REAL, by TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                        checked REAL);
                    INSERT INTO watches SELECT id, what, created, until, by, note, checked FROM watches_old;
                    DROP TABLE watches_old;
                    """
                )
            if "key" not in {row[1] for row in self._conn.execute("PRAGMA table_info(watches)")}:
                self._conn.execute("ALTER TABLE watches ADD COLUMN key TEXT NOT NULL DEFAULT ''")
                taken: set[str] = set()
                for watch_id, what in self._conn.execute("SELECT id, what FROM watches ORDER BY id").fetchall():
                    key = _unique(slug(what), taken)
                    taken.add(key)
                    self._conn.execute("UPDATE watches SET key = ? WHERE id = ?", (key, watch_id))

    def all(self) -> list[Watch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, what, created, until, by, note, checked, key FROM watches ORDER BY id"
            ).fetchall()
        return [Watch(*row) for row in rows]

    def get(self, watch_id: int) -> Watch | None:
        return next((w for w in self.all() if w.id == watch_id), None)

    def add(self, what: str, until: float | None, by: str, key: str = "") -> Watch:
        """Watch for ``what`` until then (Unix time), or with no end if ``until`` is None. ``key`` is what a DAG's
        triggers call it: made from ``what`` if it's not given."""
        what = _checked(what, until, self.clock.time())
        watches = self.all()
        if len(watches) >= MAX_WATCHES:
            raise ValueError(f"{MAX_WATCHES} things are being watched already: take some off first.")
        key = self._key(key, what, watches)
        now = self.clock.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO watches (what, created, until, by, key) VALUES (?, ?, ?, ?, ?)",
                (what, now, until, by, key),
            )
        return Watch(int(cur.lastrowid), what, now, until, by, key=key)  # type: ignore[arg-type]

    def change(self, watch_id: int, what: str, until: float | None, key: str = "") -> Watch:
        """Change what's watched for, and until when, and its key if it's given. Its note from the last check
        stays."""
        what = _checked(what, until, self.clock.time())
        watch = self.get(watch_id)
        if watch is None:
            raise ValueError(f"Nothing's watched as #{watch_id}.")
        key = self._key(key, what, [w for w in self.all() if w.id != watch_id]) if key else watch.key
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE watches SET what = ?, until = ?, key = ? WHERE id = ?", (what, until, key, watch_id)
            )
        return self.get(watch_id)  # type: ignore[return-value]

    @staticmethod
    def _key(key: str, what: str, others: list[Watch]) -> str:
        taken = {w.key for w in others}
        if not key:
            return _unique(slug(what), taken)
        key = key.strip().lower()
        if not KEY.match(key):
            raise ValueError("A key is lower case letters, digits and hyphens, like job-emails.")
        if key in taken:
            raise ValueError(f"Something else is watched as “{key}”: choose another key.")
        return key

    def note(self, watch_id: int, note: str) -> None:
        with self._lock, self._conn:
            changed = self._conn.execute(
                "UPDATE watches SET note = ? WHERE id = ?", (note.strip()[:NOTE_CHARS], watch_id)
            ).rowcount
        if not changed:
            raise ValueError(f"Nothing's watched as #{watch_id}.")

    def forget_notes(self) -> None:
        """Forget what Athena noted on each, as forgetting everything does."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE watches SET note = ''")

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
        gone = [w for w in self.all() if w.until is not None and w.until <= now]
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM watches WHERE id = ?", [(w.id,) for w in gone])
        return gone

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _checked(what: str, until: float | None, now: float) -> str:
    """What to watch for, as it's kept. ValueError if it, or when it ends, won't do."""
    what = " ".join(what.split())
    if not what:
        raise ValueError("Say what to watch for.")
    if len(what) > WHAT_CHARS:
        raise ValueError(f"Say what to watch for in up to {WHAT_CHARS} characters.")
    if until is not None and until <= now:
        raise ValueError("Choose a day that's still to come, or no end.")
    return what


def slug(what: str) -> str:
    """A key made from what's watched: its first few words that say something, such as "job-emails"."""
    words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if w not in _STOP_WORDS]
    key = "-".join(words[:3])[:40].strip("-")
    return key or "watch"


def _unique(key: str, taken: set[str]) -> str:
    if key not in taken:
        return key
    n = 2
    while f"{key[:36]}-{n}" in taken:
        n += 1
    return f"{key[:36]}-{n}"


def end_of(day: str, tz: tzinfo) -> float:
    """The end of a day, such as "2026-11-02", in Unix time: when something watched until then stops."""
    try:
        return datetime.combine(date.fromisoformat(day), datetime.max.time(), tz).timestamp()
    except ValueError:
        raise ValueError("Give the day as YYYY-MM-DD.") from None


def describe(watch: Watch, tz: tzinfo, starts: Sequence[str] = ()) -> str:
    """One line for the model: its id, what, since and until when, the agents whose DAG it starts, and the note
    from last time."""
    since = datetime.fromtimestamp(watch.created, tz)
    until = f"until {datetime.fromtimestamp(watch.until, tz):%a %-d %b}" if watch.until else "with no end"
    dags = f" When it happens, it starts the DAG of {', '.join(f'“{s}”' for s in starts)}." if starts else ""
    note = f" Your note from last time: {watch.note}" if watch.note else ""
    return f"#{watch.id} ({watch.key}): {watch.what} (since {since:%a %-d %b}, {until}).{dags}{note}"


def watch_tool(
    store: WatchStore,
    tz: tzinfo,
    every_minutes: float,
    on_change: Any = lambda: None,
    happened: Callable[[str, str], list[str]] = lambda key, text: [],
) -> Tool:
    async def watch(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "")
        try:
            if action == "add":
                if current_turn().alone:
                    raise ToolError(
                        "You can only start watching something while you're answering the user, when they ask."
                    )
                try:
                    days = DEFAULT_DAYS if args.get("days") is None else float(args["days"])
                except (TypeError, ValueError):
                    raise ToolError("days must be a number.") from None
                if days <= 0:
                    raise ToolError("days must be more than 0. To watch with no end, set forever.")
                until = None if args.get("forever") is True else store.clock.time() + days * 86400
                added = store.add(str(args.get("what") or ""), until, by="athena", key=str(args.get("key") or ""))
                on_change()
                end = f"until {datetime.fromtimestamp(until, tz):%a %-d %b}" if until else "with no end"
                return (
                    f"Watching #{added.id} ({added.key}): {added.what}. You'll check on it every {every_minutes:g} "
                    f"minutes, {end}, and tell the user when it needs their attention. An agent's DAG with the "
                    f"trigger “watch:{added.key}” runs when it happens."
                )
            if action == "happened":
                watch = store.get(int(args.get("id")))  # type: ignore[arg-type]
                if watch is None:
                    raise ToolError("Give the id of what's watched, from action list.")
                started = happened(watch.key, str(args.get("what") or "").strip())
                if not started:
                    return "No agent's DAG starts when that happens: tell the user in your reply, if they should know."
                names = " and ".join(f"“{name}”" for name in started)
                return f"Started the DAG of {names} with what you found, so you needn't tell the user yourself."
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
        raise ToolError("Say what to do: add, note, happened, done or list.")

    return Tool(
        name=WATCH,
        description=(
            "Keep an eye on something for the user and tell them when it needs their attention, such as "
            "“new emails about jobs” or “if gold drops below 4,000”. Everything watched is checked together every "
            f"{every_minutes:g} minutes. add: what to watch for and when to tell them (only when they ask; for days, "
            f"default {DEFAULT_DAYS}, or forever if they want no end; key: a short name an agent's DAG can be "
            "triggered by). note: keep what you'll need next time, such as the last price, or that you've told "
            "them. happened: it's happened, with what you found: starts the DAGs waiting for it. done: when it no "
            "longer matters. list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "note", "happened", "done", "list"]},
                "what": {
                    "type": "string",
                    "description": "add: what to watch for, and when to tell the user. happened: what you found",
                },
                "key": {"type": "string", "description": "add: a short name, like job-emails"},
                "days": {"type": "number", "description": "add: for how long"},
                "forever": {"type": "boolean", "description": "add: with no end, such as checking every time"},
                "id": {"type": "integer", "description": "note, happened and done: which"},
                "note": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=watch,
    )
