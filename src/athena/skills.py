"""Skills: notes on how to do things your way, which Athena reads before doing them.

A skill has a name, when to use it (a line Athena sees in every conversation), and how (the
instructions, which it reads with read_skill when it needs them, so they cost nothing until then).
You can read, write, change and delete them on the dashboard's Skills tab. Athena writes and
improves them too:

  - Answering you, such as when you say "that's how I like trip plans: remember it", it saves
    the skill straight away.
  - Working by itself, on a task or looking back at how a skill went, what it writes is a
    suggestion: the Skills tab shows what it would change, to approve, edit or discard. A skill
    is instructions Athena follows, so something it read on the web mustn't rewrite one unseen.
  - After using a skill, Athena looks back at how it went once you've finished chatting, and
    suggests a change if one would help next time (tasks.py runs this, as it does tasks).

Every version of a skill is kept, so you can see what changed and go back to an earlier one.
"""

from __future__ import annotations

import difflib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from athena.memory import connect_db
from athena.tools import Tool, ToolError, current_turn

if TYPE_CHECKING:
    from athena.history import ConversationStore

log = logging.getLogger(__name__)

NAME_CHARS, DESCRIPTION_CHARS, BODY_CHARS = 60, 200, 5000
READ_SKILL, SAVE_SKILL, RECENT_WORK = "read_skill", "save_skill", "recent_work"
WORK_CHARS = 5000  # how much of recent_work comes back at a time
NO_CHANGE = "NO CHANGE"
# What Athena's asked after using a skill, with what it did still in front of it.
REVIEW = (
    "(From the system, not {user}.) You used {skills} for that. Look back at how it went: a step that was "
    "missing, wrong or slower than it needed to be, or something {user} corrected or wanted done differently. "
    "Would changing the skill make it go better next time? If so, save the whole improved skill with "
    f"{SAVE_SKILL}, saying in `why` what you changed and why. Change it only for what will matter again, not "
    f"one-offs, and keep it short. If it's fine as it is, reply with just: {NO_CHANGE}"
)


@dataclass
class Skill:
    id: int
    name: str
    description: str  # when to use it
    body: str  # how
    created_by: str  # you, or athena
    created_at: float
    updated_at: float
    updated_by: str
    uses: int = 0
    last_used: float | None = None


@dataclass
class Version:
    id: int
    skill_id: int
    name: str
    description: str
    body: str
    changed_by: str
    changed_at: float


@dataclass
class Suggestion:
    """A skill, or a change to one, that Athena made by itself, waiting for you to approve it."""

    id: int
    skill_id: int | None  # None: a new skill
    name: str
    description: str
    body: str
    why: str
    source: str  # what Athena was doing, such as "your task “Weekly skills”"
    created_at: float


def check(name: str, description: str, body: str) -> tuple[str, str, str]:
    """A skill's parts, tidied. ValueError, saying what's wrong, if one isn't right."""
    name, description, body = " ".join(name.split()), " ".join(description.split()), body.strip()
    if not name or not description or not body:
        raise ValueError("A skill needs a name, when to use it, and how.")
    if len(name) > NAME_CHARS:
        raise ValueError(f"A skill's name can be at most {NAME_CHARS} characters.")
    if len(description) > DESCRIPTION_CHARS:
        raise ValueError(f"When to use it can be at most {DESCRIPTION_CHARS} characters: it's in every prompt.")
    if len(body) > BODY_CHARS:
        raise ValueError(f"A skill can be at most {BODY_CHARS:,} characters. Make it shorter, or split it in two.")
    return name, description, body


class SkillStore:
    """Skills, their versions, and suggestions, in Athena's database."""

    def __init__(self, db_path: Path):
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    last_used REAL
                );
                -- Every version of each skill, the newest being what it says now.
                CREATE TABLE IF NOT EXISTS skill_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    body TEXT NOT NULL,
                    changed_by TEXT NOT NULL,
                    changed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS skill_versions_skill ON skill_versions(skill_id, id);
                CREATE TABLE IF NOT EXISTS skill_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id INTEGER,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    body TEXT NOT NULL,
                    why TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    _SKILL = "id, name, description, body, created_by, created_at, updated_at, updated_by, uses, last_used"
    _SUGGESTION = "id, skill_id, name, description, body, why, source, created_at"

    def all(self) -> list[Skill]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._SKILL} FROM skills ORDER BY name COLLATE NOCASE").fetchall()
        return [Skill(*row) for row in rows]

    def get(self, skill_id: int) -> Skill | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._SKILL} FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return Skill(*row) if row else None

    def named(self, name: str) -> Skill | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._SKILL} FROM skills WHERE name = ? COLLATE NOCASE", (" ".join(name.split()),)
            ).fetchone()
        return Skill(*row) if row else None

    def put(self, skill_id: int | None, name: str, description: str, body: str, by: str) -> int:
        """Add a skill (no ``skill_id``) or change one, and keep it as its newest version."""
        now = time.time()
        with self._lock, self._conn:
            if skill_id is None:
                cur = self._conn.execute(
                    "INSERT INTO skills (name, description, body, created_by, created_at, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, description, body, by, now, now, by),
                )
                skill_id = int(cur.lastrowid)  # type: ignore[arg-type]
            else:
                self._conn.execute(
                    "UPDATE skills SET name = ?, description = ?, body = ?, updated_at = ?, updated_by = ? "
                    "WHERE id = ?",
                    (name, description, body, now, by, skill_id),
                )
            self._conn.execute(
                "INSERT INTO skill_versions (skill_id, name, description, body, changed_by, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (skill_id, name, description, body, by, now),
            )
        return skill_id

    def delete(self, skill_id: int) -> bool:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM skill_versions WHERE skill_id = ?", (skill_id,))
            self._conn.execute("DELETE FROM skill_suggestions WHERE skill_id = ?", (skill_id,))
            return self._conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,)).rowcount > 0

    def versions(self, skill_id: int) -> list[Version]:
        """Its versions, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, skill_id, name, description, body, changed_by, changed_at FROM skill_versions "
                "WHERE skill_id = ? ORDER BY id DESC",
                (skill_id,),
            ).fetchall()
        return [Version(*row) for row in rows]

    def used(self, skill_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE skills SET uses = uses + 1, last_used = ? WHERE id = ?", (time.time(), skill_id))

    # -- suggestions --

    def suggest(self, skill_id: int | None, name: str, description: str, body: str, why: str, source: str) -> int:
        """Keep a suggestion, in place of any waiting for the same skill: a later one builds on it."""
        with self._lock, self._conn:
            if skill_id is None:
                self._conn.execute(
                    "DELETE FROM skill_suggestions WHERE skill_id IS NULL AND name = ? COLLATE NOCASE", (name,)
                )
            else:
                self._conn.execute("DELETE FROM skill_suggestions WHERE skill_id = ?", (skill_id,))
            cur = self._conn.execute(
                "INSERT INTO skill_suggestions (skill_id, name, description, body, why, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (skill_id, name, description, body, why, source, time.time()),
            )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def suggestions(self) -> list[Suggestion]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._SUGGESTION} FROM skill_suggestions ORDER BY id").fetchall()
        return [Suggestion(*row) for row in rows]

    def suggestion(self, suggestion_id: int) -> Suggestion | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._SUGGESTION} FROM skill_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
        return Suggestion(*row) if row else None

    def waiting_for(self, skill_id: int | None, name: str) -> Suggestion | None:
        """The suggestion waiting for this skill, or for a new skill with this name."""
        with self._lock:
            if skill_id is None:
                row = self._conn.execute(
                    f"SELECT {self._SUGGESTION} FROM skill_suggestions "
                    "WHERE skill_id IS NULL AND name = ? COLLATE NOCASE",
                    (" ".join(name.split()),),
                ).fetchone()
            else:
                row = self._conn.execute(
                    f"SELECT {self._SUGGESTION} FROM skill_suggestions WHERE skill_id = ?", (skill_id,)
                ).fetchone()
        return Suggestion(*row) if row else None

    def drop_suggestion(self, suggestion_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM skill_suggestions WHERE id = ?", (suggestion_id,)).rowcount > 0

    def close(self) -> None:
        self._conn.close()


def diff(old: str, new: str) -> list[dict[str, str]]:
    """What changed, line by line: each line with " " (the same), "-" (taken out) or "+" (added)."""
    a, b = old.splitlines(), new.splitlines()
    lines: list[dict[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            lines += [{"op": " ", "text": line} for line in a[i1:i2]]
            continue
        lines += [{"op": "-", "text": line} for line in a[i1:i2]]
        lines += [{"op": "+", "text": line} for line in b[j1:j2]]
    return lines


class Skills:
    """Skills, as you and Athena change them, and the model's tools for them."""

    def __init__(
        self,
        store: SkillStore,
        history: ConversationStore | None = None,
        *,
        timezone: str = "UTC",
        chat_name: Callable[[str], str] | None = None,
        on_change: Callable[[], None] = lambda: None,
    ):
        self.store = store
        self.history = history
        try:
            self.tz: Any = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        # Who a chat is with, for recent_work: "you", or a task by its title.
        self.chat_name = chat_name or (lambda chat_id: "a task" if chat_id.startswith("task:") else "you")
        self.on_change = on_change  # e.g. so the model server reads the prompt again, with the new list

    # -- what the model sees in every prompt --

    def index(self) -> str:
        """The skills, by name and when to use each, for the end of the system prompt. Empty if there are none."""
        skills = self.store.all()
        if not skills:
            return ""
        lines = [f"- {s.name}: {s.description}" for s in skills]
        return "## Your skills\n" + "\n".join(lines)

    def _changed(self, before: str) -> None:
        if self.index() != before:
            self.on_change()

    # -- changes, from you or from Athena --

    def save(self, skill_id: int | None, name: str, description: str, body: str, by: str) -> Skill:
        """Add a skill (no ``skill_id``) or change one. ValueError, saying why, if it can't be."""
        name, description, body = check(name, description, body)
        same_name = self.store.named(name)
        if same_name and same_name.id != skill_id:
            raise ValueError(f"There's a skill called {same_name.name} already.")
        if skill_id is not None and self.store.get(skill_id) is None:
            raise ValueError(f"There's no skill #{skill_id}.")
        before = self.index()
        skill_id = self.store.put(skill_id, name, description, body, by)
        self._changed(before)
        log.info("Skill %d (%s) saved by %s", skill_id, name, by)
        skill = self.store.get(skill_id)
        assert skill is not None
        return skill

    def delete(self, skill_id: int) -> None:
        before = self.index()
        if not self.store.delete(skill_id):
            raise ValueError(f"There's no skill #{skill_id}.")
        self._changed(before)

    def restore(self, skill_id: int, version_id: int) -> Skill:
        """Go back to an earlier version, which becomes the newest."""
        version = next((v for v in self.store.versions(skill_id) if v.id == version_id), None)
        if version is None:
            raise ValueError("There's no version like that of this skill.")
        return self.save(skill_id, version.name, version.description, version.body, by="you")

    def approve(self, suggestion_id: int, edited: dict[str, str] | None = None) -> Skill:
        """Save a suggestion, as it is or as you've changed it."""
        suggestion = self.store.suggestion(suggestion_id)
        if suggestion is None:
            raise ValueError("There's no suggestion like that: it may have been approved or discarded already.")
        edited = edited or {}
        skill_id = suggestion.skill_id
        if skill_id is not None and self.store.get(skill_id) is None:
            skill_id = None  # you deleted the skill meanwhile: this makes it again
        skill = self.save(
            skill_id,
            edited.get("name") or suggestion.name,
            edited.get("description") or suggestion.description,
            edited.get("body") or suggestion.body,
            by="athena" if not edited else "you",
        )
        self.store.drop_suggestion(suggestion_id)
        return skill

    def reject(self, suggestion_id: int) -> None:
        if not self.store.drop_suggestion(suggestion_id):
            raise ValueError("There's no suggestion like that: it may have been approved or discarded already.")

    # -- for the dashboard --

    def describe(self, skill: Skill) -> dict[str, Any]:
        return {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
            "created_by": skill.created_by,
            "created_at": skill.created_at,
            "updated_at": skill.updated_at,
            "updated_by": skill.updated_by,
            "uses": skill.uses,
            "last_used": skill.last_used,
            "versions": len(self.store.versions(skill.id)),
        }

    def describe_suggestion(self, suggestion: Suggestion) -> dict[str, Any]:
        skill = self.store.get(suggestion.skill_id) if suggestion.skill_id is not None else None
        return {
            "id": suggestion.id,
            "skill_id": skill.id if skill else None,
            "name": suggestion.name,
            "description": suggestion.description,
            "body": suggestion.body,
            "why": suggestion.why,
            "source": suggestion.source,
            "created_at": suggestion.created_at,
            # What it would change, if it's a change to a skill there is.
            "was": {"name": skill.name, "description": skill.description} if skill else None,
            "diff": diff(skill.body, suggestion.body) if skill else None,
        }

    def describe_version(self, version: Version) -> dict[str, Any]:
        return {
            "id": version.id,
            "name": version.name,
            "description": version.description,
            "body": version.body,
            "changed_by": version.changed_by,
            "changed_at": version.changed_at,
        }

    # -- the model's tools --

    def tools(self) -> list[Tool]:
        async def read_skill(args: dict[str, Any]) -> str:
            name = str(args.get("name") or "")
            turn = current_turn()
            skill = self.store.named(name)
            # Working by itself, Athena builds on what it's suggested already, which you haven't seen yet.
            waiting = self.store.waiting_for(skill.id if skill else None, name) if turn.alone else None
            if skill is None and waiting is None:
                names = ", ".join(s.name for s in self.store.all()) or "none yet"
                raise ToolError(f"There's no skill called {name!r}. Skills: {names}.")
            if skill:
                self.store.used(skill.id)
            shown = waiting or skill
            assert shown is not None
            turn.note("skills", shown.name)
            pending = " (with the change you suggested, which the user hasn't approved yet)" if waiting else ""
            return f"# {shown.name}{pending}\nWhen to use it: {shown.description}\n\n{shown.body}"

        async def save_skill(args: dict[str, Any]) -> str:
            name = str(args.get("name") or "")
            existing = self.store.named(name)
            description = str(args.get("description") or "") or (existing.description if existing else "")
            body, why = str(args.get("instructions") or ""), str(args.get("why") or "").strip()
            try:
                name, description, body = check(name, description, body)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            turn = current_turn()
            if not turn.alone:  # answering you: saved now
                try:
                    skill = self.save(existing.id if existing else None, name, description, body, by="athena")
                except ValueError as exc:
                    raise ToolError(str(exc)) from exc
                return f"{'Updated' if existing else 'Saved'} the skill “{skill.name}”."
            # Working by itself: a suggestion, for the user to approve.
            waiting = self.store.waiting_for(existing.id if existing else None, name)
            latest = waiting or existing
            if latest and (latest.name, latest.description, latest.body) == (name, description, body):
                return "No change: that's what it says already."
            self.store.suggest(existing.id if existing else None, name, description, body, why[:500], turn.alone)
            turn.note("suggested", "skill")
            return (
                "Kept for the user to approve: skills you write or change while working by yourself are "
                "saved once they've approved them on the dashboard."
            )

        async def recent_work(args: dict[str, Any]) -> str:
            if self.history is None:
                return "There's no record of what you've done."
            days = max(1, min(int(args.get("days") or 7), 31))
            start = max(0, int(args.get("start") or 0))
            since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
            exchanges = self.history.exchanges_since(since)
            if not exchanges:
                return f"Nothing in the last {days} days."
            lines = []
            for e in exchanges:
                at = datetime.fromisoformat(e.created_at).astimezone(self.tz)
                asked = " ".join(e.question.split())
                asked = asked if len(asked) <= 160 else asked[:159].rstrip() + "…"
                tools = " → ".join(e.tools or []) or "no tools"
                lines.append(f"{at:%a %-d %b %H:%M} · {self.chat_name(e.chat_id)} · {tools} · {asked}")
            text = "\n".join(lines)
            part = text[start : start + WORK_CHARS]
            end = start + len(part)
            more = f"\n[to continue, use start={end}]" if end < len(text) else ""
            return f"{len(lines)} things you did in the last {days} days, oldest first:\n{part}{more}"

        return [
            Tool(
                name=READ_SKILL,
                description=(
                    "Read one of your skills (listed in your instructions) before doing what it's for, and follow it."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                handler=read_skill,
            ),
            Tool(
                name=SAVE_SKILL,
                description=(
                    "Save a skill: how to do something the user's way, for next time. Use the name of one you "
                    "have to change it, giving the whole new version."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": f"A few words, up to {NAME_CHARS} characters"},
                        "description": {"type": "string", "description": "When to use it, in one line"},
                        "instructions": {"type": "string", "description": "How: short steps, in Markdown"},
                        "why": {"type": "string", "description": "What's new or changed, and why"},
                    },
                    "required": ["name", "instructions"],
                },
                handler=save_skill,
            ),
            Tool(
                name=RECENT_WORK,
                description=(
                    "What you've done lately: each message and task you handled, when, and the tools you used "
                    "in order. For spotting things you do the same way often, to make skills of."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "description": "How far back (default 7)"},
                        "start": {"type": "integer", "description": "Where to continue from"},
                    },
                },
                handler=recent_work,
            ),
        ]
