"""Tasks: work Athena does by itself, on a schedule, and tells you about.

A task is instructions, when to carry them out, and the tools it may use without asking you each
time: a briefing every weekday morning, research to do now while you get on with something else,
or a weekly look for MCP servers that would help with what you've been asking about. Athena works
on it as it would a message from you, with more steps, more time and thinking on, then messages
you what it found, unless there was nothing worth saying. That message goes into your chat as
well, so you can reply to it.

You come first:
  - A task doesn't start while Athena's answering you, or within [tasks] quiet_minutes of your
    last message. It looks again after a minute, then two, four and so on, up to 15.
  - Once started, a message from you stops the step it's taking (oMLX stops generating when the
    request's dropped, so the Mac is yours), and it takes the step again once you've finished.
  - Running it now, from Telegram or the dashboard, skips the wait, but it still stops for you.

What it may do:
  - A task Athena suggests needs your OK: schedule_task asks first, like any tool that does
    something, showing the instructions, the schedule and the tools it may use without asking.
    Tasks you make on the dashboard are yours already.
  - A tool that asks first, and that the task may not use without asking, still asks you, in
    Telegram, saying which task wants it. Unanswered after [tasks] confirm_minutes, it's denied.
  - schedule_task always asks, even in a task, so a task can't start others by itself.
  - What it remembers, and skills it writes or changes, are suggestions for you to approve on
    the dashboard (memory.py, skills.py).

After Athena uses a skill, answering you or on a task, it looks back at how that went once
you've finished chatting, and suggests a change to the skill if one would help next time.
The scheduler runs these reviews too, one thing at a time, so they never compete for the Mac.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as clock
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pi_assistant.agent import AgentResult, Background
from pi_assistant.memory import connect_db
from pi_assistant.skills import READ_SKILL, REVIEW, SAVE_SKILL
from pi_assistant.status import Task as StatusTask
from pi_assistant.status import describe_error
from pi_assistant.tools import Tool, ToolError

if TYPE_CHECKING:
    from pi_assistant.agent import Agent
    from pi_assistant.channel import Channel
    from pi_assistant.config import TasksConfig
    from pi_assistant.llm import LLMReply

log = logging.getLogger(__name__)

NOTHING = "NOTHING TO REPORT"  # what a task replies when there's nothing worth telling you
# Tools a task can't be allowed to use without asking: starting more tasks, and pushing to GitHub.
NEVER_UNASKED = {"schedule_task", "hand_to_claude", "answer_claude", "extend_claude_project"}
MIN_EVERY = timedelta(minutes=15)
MISSED_GRACE = timedelta(hours=6)  # a regular run missed by more than this (Athena was off) is skipped
RETRIES = (600, 1800)  # after a run fails: try again in 10 minutes, then 30, then give up on that run
FIRST_WAIT, LONGEST_WAIT = 60.0, 900.0  # waiting for you to finish chatting: 1 minute, doubling to 15
REVIEW_MINUTES, REVIEW_ROUNDS = 10, 4  # looking back at how a skill went: the most time and steps it takes
MAX_REVIEWS = 5  # waiting to be done; the oldest is dropped for a new one past this
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SCHEDULES = (
    '"now" (once, straight away unless a chat is going on), "in 2h" or "in 30m", "once 2026-10-16 09:00", '
    '"once 09:00" (the next 09:00), "daily 07:30", "weekdays 07:30", "weekly mon,thu 18:00", or '
    '"every 6h" (also m and d; at least 15m)'
)
# Tasks to start from, on the dashboard.
EXAMPLES = (
    {
        "title": "Morning briefing",
        "schedule": "weekdays 07:30",
        "instructions": (
            "Brief me for the day ahead: what's on my calendar and reminders today, anything I've said is due, "
            "and the main news. Short, most important first."
        ),
    },
    {
        "title": "MCP servers worth adding",
        "schedule": "weekly sun 10:00",
        "instructions": (
            "Look through what I've asked you about lately (search_memory, in conversations) for things you "
            "couldn't do, or did badly, for want of a tool. Search the MCP Registry for servers that would help. "
            "Suggest at most three you haven't suggested before: what each would let you do for me, who publishes "
            "it, how it runs and whether it needs a key. Prefer known publishers and PyPI packages. If none are "
            f"worth it, say {NOTHING}."
        ),
    },
    {
        "title": "Skills from habits",
        "schedule": "weekly fri 18:00",
        "instructions": (
            "Look at what you've done this week with recent_work. Find things you did the same way three or more "
            "times: the same kind of request, handled with the same steps. For each, if none of your skills covers "
            "it, write one with save_skill: when to use it, and the steps that worked, short. If one covers it but "
            "you did it differently or better, improve it. Tell me what you suggested and why. If nothing's worth "
            f"a skill, say {NOTHING}."
        ),
    },
    {
        "title": "Research",
        "schedule": "now",
        "instructions": "Find out … and tell me what you learn, with your sources.",
    },
)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE_CLOCK = re.compile(r"^(\d{4}-\d{2}-\d{2})[ t]([01]?\d|2[0-3]):([0-5]\d)$")
_SPAN = re.compile(r"^(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)$")


class ScheduleError(ValueError):
    """A schedule that can't be understood, saying what's allowed."""


# -- schedules ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    kind: str  # once, daily, weekdays, weekly or every
    text: str  # as it's kept, which reads the same back
    at: clock | None = None  # daily, weekdays, weekly
    days: tuple[int, ...] = ()  # weekly: Monday is 0
    when: datetime | None = None  # once; None: as soon as you're not chatting
    every: timedelta | None = None

    @classmethod
    def parse(cls, text: str, tz: tzinfo, now: datetime | None = None) -> Schedule:
        now = (now or datetime.now(tz)).astimezone(tz)
        words = " ".join(text.strip().lower().split())
        if words in ("now", "once now", "once"):
            return cls("once", "once now")
        if words.startswith("in "):
            words = "once " + words
        kind, _, rest = words.partition(" ")
        if kind == "once":
            when = cls._once(rest, tz, now)
            if when <= now:
                raise ScheduleError(f"{when:%Y-%m-%d %H:%M} has already passed.")
            return cls("once", f"once {when:%Y-%m-%d %H:%M}", when=when)
        if kind in ("daily", "weekdays"):
            at = _clock(rest)
            return cls(kind, f"{kind} {at:%H:%M}", at=at, days=tuple(range(5 if kind == "weekdays" else 7)))
        if kind == "weekly":
            names, _, at_text = rest.rpartition(" ")
            days = sorted({_day(name) for name in re.split(r"[,\s]+", names) if name})
            if not days:
                raise ScheduleError(f'Say which days, like "weekly mon,thu 18:00". Schedules: {SCHEDULES}.')
            at = _clock(at_text)
            return cls("weekly", f"weekly {','.join(DAYS[d] for d in days)} {at:%H:%M}", at=at, days=tuple(days))
        if kind == "every":
            span = _span(rest)
            if span < MIN_EVERY:
                raise ScheduleError("A task can run at most every 15 minutes.")
            return cls("every", f"every {_show_span(span)}", every=span)
        raise ScheduleError(f"I don't understand the schedule {text!r}. Schedules: {SCHEDULES}.")

    @staticmethod
    def _once(rest: str, tz: tzinfo, now: datetime) -> datetime:
        if rest.startswith("in "):
            return (now + _span(rest[3:])).replace(second=0, microsecond=0)
        if match := _DATE_CLOCK.match(rest):
            day = date.fromisoformat(match.group(1))
            return datetime.combine(day, clock(int(match.group(2)), int(match.group(3))), tz)
        if _CLOCK.match(rest):
            at = datetime.combine(now.date(), _clock(rest), tz)
            return at if at > now else at + timedelta(days=1)
        raise ScheduleError(f'Say when, like "once 2026-10-16 09:00" or "in 2h". Schedules: {SCHEDULES}.')

    def first(self, now: datetime) -> datetime:
        """When it's first due."""
        if self.kind == "once":
            return self.when or now
        if self.kind == "every":
            return now
        return self.after(now)  # type: ignore[return-value]

    def after(self, last: datetime) -> datetime | None:
        """When it's next due after ``last``: None if it only runs once."""
        if self.kind == "once":
            return None
        if self.every:
            return last + self.every
        tz = last.tzinfo
        assert self.at is not None and tz is not None
        for offset in range(8):
            day = last.date() + timedelta(days=offset)
            candidate = datetime.combine(day, self.at, tz)
            if candidate > last and day.weekday() in self.days:
                return candidate
        raise AssertionError("every schedule has at least one day")  # pragma: no cover

    def describe(self) -> str:
        if self.kind == "once":
            return f"once, on {self.when:%a %-d %b at %H:%M}" if self.when else "once, as soon as you're not chatting"
        if self.kind == "every":
            return f"every {_show_span(self.every, words=True)}"  # type: ignore[arg-type]
        at = f"at {self.at:%H:%M}"
        if self.kind == "daily":
            return f"every day {at}"
        if self.kind == "weekdays":
            return f"every weekday {at}"
        names = [DAY_NAMES[d] for d in self.days]
        return f"every {' and '.join([', '.join(names[:-1]), names[-1]] if len(names) > 1 else names)} {at}"


def _clock(text: str) -> clock:
    match = _CLOCK.match(text.strip())
    if not match:
        raise ScheduleError(f"Say the time as HH:MM, like 07:30. Schedules: {SCHEDULES}.")
    return clock(int(match.group(1)), int(match.group(2)))


def _day(name: str) -> int:
    for i, day in enumerate(DAYS):
        if name.startswith(day):
            return i
    raise ScheduleError(f"{name!r} isn't a day: use mon, tue, wed, thu, fri, sat or sun.")


def _span(text: str) -> timedelta:
    match = _SPAN.match(text.strip())
    if not match:
        raise ScheduleError(f"Say how long like 30m, 2h or 1d. Schedules: {SCHEDULES}.")
    n, unit = int(match.group(1)), match.group(2)[0]
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)


def _show_span(span: timedelta, words: bool = False) -> str:
    minutes = int(span.total_seconds() // 60)
    for size, short, long in ((1440, "d", "day"), (60, "h", "hour"), (1, "m", "minute")):
        if minutes % size == 0:
            n = minutes // size
            if words:
                return long if n == 1 else f"{n} {long}s"
            return f"{n}{short}"
    raise AssertionError  # pragma: no cover


# -- keeping them --------------------------------------------------------------------------------------


@dataclass
class Task:
    id: int
    title: str
    instructions: str
    schedule: str  # Schedule.text
    allowed_tools: list[str]  # tools it may use without asking you
    status: str  # active, paused, or done (a one-off that's run)
    created_by: str  # you, or athena
    created_at: float
    next_run: float | None = None  # Unix time
    last_run: float | None = None
    last_outcome: str = ""  # told you, nothing new, stopped, ran out of time, or failed: why
    last_result: str = ""  # what it said last time
    runs: int = 0
    failures: int = 0  # in a row, for this run


class TaskStore:
    """Tasks, in Athena's database."""

    def __init__(self, db_path: Path):
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    allowed_tools TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    next_run REAL,
                    last_run REAL,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_result TEXT NOT NULL DEFAULT '',
                    runs INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    _COLUMNS = (
        "id, title, instructions, schedule, allowed_tools, status, created_by, created_at, "
        "next_run, last_run, last_outcome, last_result, runs, failures"
    )

    def _task(self, row: tuple[Any, ...]) -> Task:
        values = list(row)
        values[4] = json.loads(values[4])
        return Task(*values)

    def add(
        self, title: str, instructions: str, schedule: str, allowed_tools: list[str], created_by: str, next_run: float
    ) -> Task:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO tasks (title, instructions, schedule, allowed_tools, created_by, created_at, next_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, instructions, schedule, json.dumps(allowed_tools), created_by, time.time(), next_run),
            )
        task = self.get(int(cur.lastrowid))  # type: ignore[arg-type]
        assert task is not None
        return task

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._COLUMNS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def all(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._COLUMNS} FROM tasks ORDER BY id").fetchall()
        return [self._task(row) for row in rows]

    def due(self, now: float) -> Task | None:
        """The active task that's been due longest, if any is."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM tasks WHERE status = 'active' AND next_run <= ? "
                "ORDER BY next_run, id LIMIT 1",
                (now,),
            ).fetchone()
        return self._task(row) if row else None

    def next_due(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(next_run) FROM tasks WHERE status = 'active'").fetchone()
        return row[0] if row else None

    def save(self, task: Task) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET title = ?, instructions = ?, schedule = ?, allowed_tools = ?, status = ?, "
                "next_run = ?, last_run = ?, last_outcome = ?, last_result = ?, runs = ?, failures = ? WHERE id = ?",
                (
                    task.title,
                    task.instructions,
                    task.schedule,
                    json.dumps(task.allowed_tools),
                    task.status,
                    task.next_run,
                    task.last_run,
                    task.last_outcome,
                    task.last_result,
                    task.runs,
                    task.failures,
                    task.id,
                ),
            )

    def delete(self, task_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    def close(self) -> None:
        self._conn.close()


# -- running them ------------------------------------------------------------------------------------


class _OutOfTime(Exception):
    pass


@dataclass
class Review:
    """Looking back at how skills went, after Athena used them."""

    skills: list[str]
    transcript: list[dict[str, Any]] = field(repr=False)  # what it did, from AgentResult.transcript
    what: str  # what it used them for: "our chat", or "your task “Briefing”"


def suggested(notes: dict[str, list[str]]) -> str:
    """What a run suggested for you to approve, such as "2 memories and a skill". Empty if nothing."""
    said = notes.get("suggested", [])
    parts = []
    for kind, plural in (("memory", "memories"), ("skill", "skills")):
        n = said.count(kind)
        if n:
            parts.append(f"a {kind}" if n == 1 else f"{n} {plural}")
    return " and ".join(parts)


class Scheduler:
    """Runs tasks when they're due, one at a time, never while you're chatting (see the top of this file)."""

    def __init__(
        self,
        cfg: TasksConfig,
        store: TaskStore,
        agent: Agent,
        *,
        timezone: str,
        channel: Callable[[], Channel | None],
        tools: Callable[[], list[Tool]],
        on_ran: Callable[[], None] = lambda: None,
        poll_seconds: float = 15.0,
    ):
        self.cfg = cfg
        self.store = store
        self.agent = agent
        try:
            self.tz: tzinfo = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = UTC
        self.channel = channel
        self._tools = tools
        self.on_ran = on_ran  # e.g. so the model server reads your chat again, with the report in it
        self.poll = poll_seconds
        self.running: Task | None = None
        self.waiting: tuple[int, float] | None = None  # a due task waiting for you: (its id, when it looks again)
        self.reviews: list[Review] = []  # skills to look back on, once you've finished chatting
        self._now_please: set[int] = set()  # run these as soon as Athena isn't answering you
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._run: asyncio.Task[Any] | None = None

    # -- you, and whether you're chatting --

    def quiet(self) -> bool:
        """Athena isn't answering you, and hasn't for [tasks] quiet_minutes."""
        idle = time.monotonic() - self.agent.last_answered_you
        return self.agent.answering_you == 0 and idle >= self.cfg.quiet_minutes * 60

    def _free(self, task: Task) -> bool:
        return self.agent.answering_you == 0 if task.id in self._now_please else self.quiet()

    # -- starting and stopping --

    def start(self) -> None:
        if self._loop is None:
            self._skip_missed()
            self._loop = asyncio.create_task(self._forever(), name="tasks")

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
            self._loop = None

    def wake(self) -> None:
        self._wake.set()

    def _skip_missed(self) -> None:
        """Regular runs missed by a long way, say while the Pi was off, are skipped to the next one."""
        now = datetime.now(self.tz)
        for task in self.store.all():
            if task.status != "active" or task.next_run is None:
                continue
            schedule = Schedule.parse(task.schedule, self.tz)
            if schedule.kind != "once" and task.next_run < (now - MISSED_GRACE).timestamp():
                task.next_run = schedule.after(now).timestamp()  # type: ignore[union-attr]
                self.store.save(task)
                log.info("Task %d (%s) missed its run, so it's next due %s", task.id, task.title, task.next_run)

    async def _forever(self) -> None:
        while True:
            try:
                task = self.store.due(time.time())
                if task is None:
                    if self.reviews and self.quiet():
                        await self.review(self.reviews.pop(0))
                        continue
                    next_due = self.store.next_due()
                    await self._sleep(60 if next_due is None else min(60, max(1, next_due - time.time())))
                    continue
                if await self._wait_for_you(task):
                    await self.run(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug in one task shouldn't stop the others
                log.exception("The task scheduler hit a problem")
                await self._sleep(60)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def _wait_for_you(self, task: Task) -> bool:
        """Wait until you're not chatting, looking again after 1 minute, then 2, 4... up to 15.
        False if the task was paused or deleted meanwhile."""
        wait = FIRST_WAIT
        while not self._free(task):
            self.waiting = (task.id, time.time() + wait)
            await self._sleep(wait)
            wait = min(wait * 2, LONGEST_WAIT)
            latest = self.store.get(task.id)
            if latest is None or latest.status != "active":
                self.waiting = None
                return False
            task = latest
        self.waiting = None
        return True

    # -- running one --

    def _stepper(
        self, free: Callable[[], bool], minutes: float, what: str
    ) -> Callable[[StatusTask, Callable[[], Awaitable[LLMReply]]], Awaitable[LLMReply]]:
        """How background work takes each step: waiting while you chat, stopping the step if you start
        (to take it again after), and raising _OutOfTime after ``minutes`` of work."""
        started, paused = time.monotonic(), 0.0

        async def step(status: StatusTask, call: Callable[[], Awaitable[LLMReply]]) -> LLMReply:
            nonlocal paused
            while True:
                if time.monotonic() - started - paused > minutes * 60:
                    raise _OutOfTime
                if not free():
                    status.waiting_for_you()
                    since = time.monotonic()
                    while not free():
                        await asyncio.sleep(self.poll)
                    paused += time.monotonic() - since
                    status.thinking()
                # Take the step, unless you start chatting first: then stop it, so the model's yours,
                # and take it again once you've finished.
                thinking = asyncio.ensure_future(call())
                chatting = asyncio.ensure_future(self.agent.chatting.wait())
                await asyncio.wait({thinking, chatting}, return_when=asyncio.FIRST_COMPLETED)
                chatting.cancel()
                if thinking.done():
                    return thinking.result()
                thinking.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await thinking
                log.info("%s stopped a step for you, to take again once you've finished", what)

        return step

    async def run(self, task: Task) -> None:
        """Run ``task`` now, tell you what it found, and work out when it's next due."""
        schedule = Schedule.parse(task.schedule, self.tz)
        self.running = task
        step = self._stepper(lambda: self._free(task), self.cfg.max_minutes, f"Task {task.id}")

        async def confirm(tool: str, args: dict[str, Any], summary: str | None = None) -> bool:
            if tool in task.allowed_tools and tool not in NEVER_UNASKED:
                log.info("Task %d used %s without asking, as it may", task.id, tool)
                return True
            channel = self.channel()
            if channel is None:
                return False
            return await channel.ask(
                tool, args, summary, why=f"For your task “{task.title}”", timeout=self.cfg.confirm_minutes * 60
            )

        background = Background(
            label=f"Task: {task.title}",
            step=step,
            thinking=self.cfg.thinking,
            max_rounds=self.cfg.max_tool_rounds,
            source=f"your task “{task.title}”",
        )
        log.info("Running task %d (%s)", task.id, task.title)
        self._run = asyncio.create_task(
            self.agent.respond(
                f"task:{task.id}",
                task.instructions,
                confirm=confirm,
                note=self._note(task, schedule),
                channel="Task",
                background=background,
            ),
            name=f"task-{task.id}",
        )
        try:
            await asyncio.wait({self._run})
        except asyncio.CancelledError:  # Athena's stopping: the task stays due, for next time
            self._run.cancel()
            raise
        finally:
            self.running = None
            self._now_please.discard(task.id)
        task = self.store.get(task.id) or task  # changed meanwhile, say paused from the dashboard
        now = datetime.now(self.tz)
        if self._run.cancelled():
            await self._finished(task, schedule, now, "stopped", "")
            if schedule.kind == "once":
                task.status = "paused"  # so you can run it again
                self.store.save(task)
            return
        try:
            result = self._run.result()
        except _OutOfTime:
            minutes = self.cfg.max_minutes
            await self._tell(f"⏱️ Your task “{task.title}” ran out of time ({minutes:g} minutes) before it finished.")
            await self._finished(task, schedule, now, "ran out of time", "")
            return
        except Exception as exc:
            await self._failed(task, schedule, now, exc)
            return
        text = result.text.strip()
        to_review = suggested(result.notes)
        review = f"\n\n🧠 I've suggested {to_review} for you to review on the dashboard." if to_review else ""
        if not result.saved or NOTHING in text.upper():
            if "skill" in result.notes.get("suggested", []):  # worth a word: you'd not know otherwise
                await self._tell(f"📋 **{task.title}**: nothing new to tell you.{review}")
            await self._finished(task, schedule, now, "nothing new", text if result.saved else "")
            return
        await self._tell(f"📋 **{task.title}**\n\n{text}{review}", keep=(task.title, text))
        await self._finished(task, schedule, now, "told you", text)

    def _note(self, task: Task, schedule: Schedule) -> str:
        user = self.agent.cfg.user_name
        before = (
            f" It's run {task.runs} time{'' if task.runs == 1 else 's'} before, last on "
            f"{datetime.fromtimestamp(task.last_run, self.tz):%a %-d %b at %H:%M}: your earlier runs are above."
            if task.runs and task.last_run
            else ""
        )
        return (
            f"This is a task you're doing by yourself, not a message from {user}: “{task.title}”, "
            f"{schedule.describe()}.{before} {user} isn't watching, so don't ask them anything: decide for "
            "yourself, and use your tools. When you've finished, reply with what they should know, short "
            f"and specific, with sources where they help: they'll get it as a message. If there's nothing "
            f"new worth telling them, reply with just: {NOTHING}"
        )

    async def _finished(self, task: Task, schedule: Schedule, now: datetime, outcome: str, said: str) -> None:
        task.runs += 1
        task.failures = 0
        task.last_run = now.timestamp()
        task.last_outcome = outcome
        task.last_result = said
        nxt = schedule.after(now)
        task.next_run = nxt.timestamp() if nxt else None
        if nxt is None and task.status == "active":
            task.status = "done"
        self.store.save(task)
        log.info("Task %d (%s): %s", task.id, task.title, outcome)
        self.on_ran()

    async def _failed(self, task: Task, schedule: Schedule, now: datetime, exc: Exception) -> None:
        why = f"{describe_error(exc)}: {exc}" if str(exc) else describe_error(exc)
        log.warning("Task %d (%s) failed: %s", task.id, task.title, why)
        task.failures += 1
        task.last_outcome = f"failed: {why}"
        if task.failures <= len(RETRIES):
            task.next_run = time.time() + RETRIES[task.failures - 1]
            self.store.save(task)
            return
        await self._tell(f"⚠️ I couldn't do your task “{task.title}”: {why}. I tried three times.")
        task.failures = 0
        task.last_run = now.timestamp()
        nxt = schedule.after(now)
        task.next_run = nxt.timestamp() if nxt else None
        if nxt is None:
            task.status = "paused"  # a one-off: run it again when whatever was wrong is fixed
        self.store.save(task)

    async def _tell(self, text: str, keep: tuple[str, str] | None = None) -> None:
        channel = self.channel()
        if channel is None:
            log.warning("Nowhere to send a task's message: %s", text[:200])
            return
        try:
            await channel.tell(text)
        except Exception as exc:
            log.warning("Couldn't send a task's message: %s", exc)
            return
        if keep:  # in your chat too, so you can ask about it
            title, said = keep
            self.agent.history.append_exchange(channel.chat_id, f"(Your task “{title}” ran.)", said)

    # -- looking back at how skills went --

    def after_answer(self, result: AgentResult, background: Background | None) -> None:
        """Agent.on_answered: if a skill was used, look back at how it went once you've finished chatting."""
        skills = list(dict.fromkeys(result.notes.get("skills", [])))
        if not skills or not result.transcript or (background and background.review):
            return
        what = background.source if background and background.source else "our chat"
        self.reviews.append(Review(skills, result.transcript, what))
        del self.reviews[:-MAX_REVIEWS]
        self.wake()

    async def review(self, review: Review) -> None:
        """Ask the model, with what it did in front of it, whether a change to the skills would help."""
        names = " and ".join(f"“{name}”" for name in review.skills)
        used = f"your skill{'s' if len(review.skills) > 1 else ''} {names}"
        background = Background(
            label=f"Looking back at {used}",
            step=self._stepper(self.quiet, REVIEW_MINUTES, "A skill review"),
            thinking=self.cfg.thinking,
            max_rounds=REVIEW_ROUNDS,
            source=f"looking back at {used}, after {review.what}",
            only_tools=frozenset({READ_SKILL, SAVE_SKILL}),
            review=True,
        )
        text = REVIEW.format(user=self.agent.cfg.user_name, skills=used)
        log.info("Looking back at %s", used)
        try:
            result = await self.agent.follow_up(review.transcript, text, background)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # it's only a suggestion: not worth trying again
            log.warning("Couldn't look back at %s: %s", used, describe_error(exc))
            return
        if "skill" in result.notes.get("suggested", []):
            await self._tell(
                f"✏️ After {review.what}, I've suggested a change to {used}. "
                "Approve it, or not, on the dashboard's Skills tab."
            )

    # -- what you, and Athena, can do to them --

    def add(self, title: str, instructions: str, schedule: str, allowed_tools: list[str], created_by: str) -> Task:
        title, instructions = title.strip(), instructions.strip()
        if not title or not instructions:
            raise ValueError("A task needs a title and instructions.")
        parsed = Schedule.parse(schedule, self.tz)
        allowed = self.check_tools(allowed_tools)
        first = parsed.first(datetime.now(self.tz)).timestamp()
        task = self.store.add(title[:120], instructions, parsed.text, allowed, created_by, first)
        log.info("Task %d (%s) added by %s: %s", task.id, task.title, created_by, parsed.describe())
        self.wake()
        return task

    def check_tools(self, names: list[str]) -> list[str]:
        known = {t.name for t in self._tools()}
        names = list(dict.fromkeys(str(n) for n in names or []))
        if never := [n for n in names if n in NEVER_UNASKED]:
            raise ValueError(f"A task always has to ask before it uses {', '.join(never)}.")
        if unknown := [n for n in names if n not in known]:
            raise ValueError(f"There's no tool called {', '.join(unknown)}.")
        return names

    def run_now(self, task_id: int) -> Task:
        task = self._get(task_id)
        task.status, task.next_run = "active", time.time()
        self.store.save(task)
        self._now_please.add(task.id)
        self.wake()
        return task

    def pause(self, task_id: int) -> Task:
        task = self._get(task_id)
        task.status = "paused"
        self.store.save(task)
        return task

    def resume(self, task_id: int) -> Task:
        task = self._get(task_id)
        schedule = Schedule.parse(task.schedule, self.tz)
        now = datetime.now(self.tz)
        task.status = "active"
        task.next_run = (schedule.after(now) or now).timestamp() if task.last_run else schedule.first(now).timestamp()
        self.store.save(task)
        self.wake()
        return task

    def delete(self, task_id: int) -> None:
        if self.running and self.running.id == task_id:
            self.stop_running()
        if not self.store.delete(task_id):
            raise ValueError(f"There's no task #{task_id}.")

    def stop_running(self) -> bool:
        """Stop the task that's running. False if none is."""
        if self._run and not self._run.done():
            self._run.cancel()
            return True
        return False

    def _get(self, task_id: int) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"There's no task #{task_id}.")
        return task

    def describe(self, task: Task) -> dict[str, Any]:
        """A task as the dashboard and Telegram show it."""
        schedule = Schedule.parse(task.schedule, self.tz)
        running = bool(self.running and self.running.id == task.id)
        return {
            "id": task.id,
            "title": task.title,
            "instructions": task.instructions,
            "schedule": task.schedule,
            "when": schedule.describe(),
            "allowed_tools": task.allowed_tools,
            "status": "running" if running else task.status,
            "created_by": task.created_by,
            "next_run": task.next_run if task.status == "active" else None,
            "waiting_for_you": bool(self.waiting and self.waiting[0] == task.id),
            "last_run": task.last_run,
            "last_outcome": task.last_outcome,
            "last_result": task.last_result,
            "runs": task.runs,
        }

    # -- the model's tools --

    def tools(self) -> list[Tool]:
        async def check(args: dict[str, Any]) -> str:
            try:
                schedule = Schedule.parse(str(args.get("schedule") or ""), self.tz)
                allowed = self.check_tools(args.get("tools") or [])
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            title, instructions = str(args.get("title") or "").strip(), str(args.get("instructions") or "").strip()
            if not title or not instructions:
                raise ToolError("A task needs a title and instructions.")
            unasked = f" It may use {', '.join(allowed)} without asking you." if allowed else ""
            return f"A task, “{title}”, {schedule.describe()}:\n{instructions}\n{unasked}".rstrip()

        async def schedule_task(args: dict[str, Any]) -> str:
            try:
                task = self.add(
                    str(args.get("title") or ""),
                    str(args.get("instructions") or ""),
                    str(args.get("schedule") or ""),
                    list(args.get("tools") or []),
                    created_by="athena",
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            return f"Scheduled task #{task.id}, “{task.title}”, {Schedule.parse(task.schedule, self.tz).describe()}."

        async def list_tasks(args: dict[str, Any]) -> str:
            tasks = self.store.all()
            if not tasks:
                return "There are no tasks."
            lines = []
            for task in tasks:
                d = self.describe(task)
                when = datetime.fromtimestamp(d["next_run"], self.tz) if d["next_run"] else None
                nxt = f", next {when:%a %-d %b %H:%M}" if when else ""
                last = f"; last run: {task.last_outcome}" if task.last_outcome else ""
                lines.append(f"#{task.id} {task.title} ({d['status']}): {d['when']}{nxt}{last}")
            return "\n".join(lines)

        async def change_task(args: dict[str, Any]) -> str:
            action = str(args.get("action") or "")
            try:
                task_id = int(args.get("id"))  # type: ignore[arg-type]
                act = {"run": self.run_now, "pause": self.pause, "resume": self.resume}[action]
                task = act(task_id)
            except (KeyError, TypeError, ValueError) as exc:
                missing = isinstance(exc, ValueError) and "no task" in str(exc)
                raise ToolError(str(exc) if missing else "Give the task's id, and run, pause or resume.") from exc
            done = {"run": "will run now", "pause": "is paused", "resume": "is running again"}[action]
            return f"Task #{task.id}, “{task.title}”, {done}."

        return [
            Tool(
                name="schedule_task",
                description=(
                    "Set yourself a task to do by yourself, now in the background or on a schedule, and message "
                    "the user what you find: research, a regular briefing, checking on something. Asks the user "
                    "first. `tools` are ones that normally ask first which the task may use without asking."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "A few words"},
                        "instructions": {
                            "type": "string",
                            "description": "What to do and what to report, for you to follow later on your own",
                        },
                        "schedule": {"type": "string", "description": f"One of: {SCHEDULES}"},
                        "tools": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "instructions", "schedule"],
                },
                handler=schedule_task,
                needs_confirmation=True,
                preview=check,
            ),
            Tool(
                name="list_tasks",
                description="The tasks you do by yourself: their ids, schedules and how their last runs went.",
                parameters={"type": "object", "properties": {}},
                handler=list_tasks,
            ),
            Tool(
                name="change_task",
                description="Run a task now, pause it, or resume it.",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "action": {"type": "string", "enum": ["run", "pause", "resume"]},
                    },
                    "required": ["id", "action"],
                },
                handler=change_task,
            ),
        ]
