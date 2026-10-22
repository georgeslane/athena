"""Tasks and checks over days, with a clock that moves on by itself whenever the scheduler waits: what runs, and
when, through weekends, the clocks going back, failures and you chatting. A week passes in about a second."""

import asyncio
from datetime import datetime, timedelta

import pytest
from conftest import FakeLLMServer, completion
from test_tasks import LONDON

from athena.agent import Agent
from athena.clock import Clock
from athena.config import TasksConfig
from athena.history import ConversationStore
from athena.tasks import NOTHING, Scheduler, TaskStore
from athena.tools import ToolRegistry
from athena.watching import WatchStore

THURSDAY_7AM = datetime(2026, 10, 22, 7, 0, tzinfo=LONDON)  # the clocks go back on Sunday 25 October


class FakeClock(Clock):
    """A clock that only moves when the scheduler waits: then it jumps to when it would have woken, unless
    something wakes it first."""

    def __init__(self, start: datetime):
        self.t = start.timestamp()

    def time(self) -> float:
        return self.t

    async def sleep(self, seconds: float, wake: asyncio.Event) -> None:
        for _ in range(5):  # let whatever's ready run first, which may wake it, such as a task being added
            await asyncio.sleep(0)
        if not wake.is_set():
            self.t += seconds


class Telegram:
    """What Athena tells you, and when, by the clock."""

    chat_id = "111"

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.said: list[tuple[datetime, str]] = []

    async def tell(self, text, *, session=None):
        self.said.append((self.clock.now(LONDON), text))

    async def ask(self, tool, args, summary, *, why, timeout, session=None):
        return False

    def when(self, starting: str) -> list[str]:
        return [f"{at:%a %d %H:%M}" for at, text in self.said if text.startswith(starting)]


@pytest.fixture
def week(config, memory):
    """A scheduler on a fake clock, from Thursday at 07:00, whose model says something new every time."""
    clock = FakeClock(THURSDAY_7AM)
    server = FakeLLMServer(lambda body: completion(f"News at {clock.now(LONDON):%a %H:%M}."))
    agent = Agent(config.agent, server.client(config.llm), ToolRegistry(), ConversationStore(config.db_path), memory)
    telegram = Telegram(clock)
    scheduler = Scheduler(
        TasksConfig(quiet_minutes=0, heartbeat_minutes=30),
        TaskStore(config.db_path),
        agent,
        timezone="Europe/London",
        channel=lambda: telegram,
        tools=lambda: [],
        poll_seconds=0.001,
        watches=WatchStore(config.db_path, clock),
        clock=clock,
    )

    async def until(when: datetime) -> None:
        """Let the scheduler run until the clock says ``when``."""
        scheduler.start()
        try:
            async with asyncio.timeout(30):
                while clock.time() < when.timestamp():
                    await asyncio.sleep(0)
        finally:
            await scheduler.stop()

    return scheduler, telegram, server, until


async def test_a_weekday_briefing_runs_each_weekday_at_its_time_across_the_clocks_going_back(week):
    scheduler, telegram, _, until = week
    scheduler.add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    await until(datetime(2026, 10, 27, 12, 0, tzinfo=LONDON))
    assert telegram.when("📋 **Briefing**") == ["Thu 22 07:30", "Fri 23 07:30", "Mon 26 07:30", "Tue 27 07:30"]
    # 07:30 on Monday is GMT now, an hour later than Thursday's in UTC.
    task = scheduler.store.all()[0]
    assert datetime.fromtimestamp(task.next_run, LONDON) == datetime(2026, 10, 28, 7, 30, tzinfo=LONDON)


async def test_a_one_off_runs_once_at_its_time_and_is_done(week):
    scheduler, telegram, _, until = week
    scheduler.add("Call Sam", "Remind me to call Sam.", "in 2h", [], created_by="you")
    await until(datetime(2026, 10, 23, 12, 0, tzinfo=LONDON))
    assert telegram.when("📋 **Call Sam**") == ["Thu 22 09:00"]
    assert scheduler.store.all()[0].status == "done"


async def test_a_task_that_keeps_failing_tries_again_after_10_then_30_minutes_then_waits(week):
    scheduler, telegram, server, until = week
    server.error = "the model isn't loaded"  # every call to the model fails
    scheduler.add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    await until(datetime(2026, 10, 22, 12, 0, tzinfo=LONDON))
    assert telegram.when("⚠️ I couldn't do your task “Briefing”") == ["Thu 22 08:10"]  # 07:30, 07:40 and 08:10
    task = scheduler.store.all()[0]
    assert task.failures == 0 and datetime.fromtimestamp(task.next_run, LONDON).day == 23  # tomorrow's run


async def test_a_due_task_waits_while_youre_chatting_and_runs_when_youve_stopped(week):
    scheduler, telegram, _, until = week
    scheduler.add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    scheduler.agent.answering_you = 1  # you're mid-conversation at 07:30
    clock = scheduler.clock

    async def stop_chatting_at_9():
        while clock.now(LONDON).hour < 9:
            await asyncio.sleep(0)
        scheduler.agent.answering_you = 0

    stopping = asyncio.create_task(stop_chatting_at_9())
    await until(datetime(2026, 10, 22, 11, 0, tzinfo=LONDON))
    await stopping
    [ran] = telegram.when("📋 **Briefing**")
    # It looked again after 1 minute, then 2, 4, 8, 15...: the first look after 09:00.
    assert "09:00" <= ran[-5:] <= "09:15", ran


async def test_whats_watched_is_checked_every_30_minutes_until_its_last_day(week):
    scheduler, telegram, server, until = week
    server.replies = lambda body: completion(NOTHING)
    scheduler.watches.add("the parcel", THURSDAY_7AM.timestamp() + 3600 * 2, by="you")  # until 09:00
    checks = []
    original = scheduler.watches.checked
    scheduler.watches.checked = lambda when: (checks.append(datetime.fromtimestamp(when, LONDON)), original(when))
    await until(datetime(2026, 10, 22, 12, 0, tzinfo=LONDON))
    assert [f"{c:%H:%M}" for c in checks] == ["07:30", "08:00", "08:30"]  # none once it's off the list
    assert telegram.when("👀 I've stopped watching for this") == ["Thu 22 09:00"]
    assert scheduler.watches.all() == []


async def test_nothing_runs_while_nothing_is_due(week):
    scheduler, telegram, server, until = week
    await until(datetime(2026, 10, 29, 7, 0, tzinfo=LONDON))  # a week, with no tasks and nothing watched
    assert telegram.said == [] and server.requests == []


def test_the_real_clock_is_now():
    clock = Clock()
    assert abs(clock.now(LONDON) - datetime.now(LONDON)) < timedelta(seconds=1)
