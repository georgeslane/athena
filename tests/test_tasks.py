"""Tasks Athena does by itself: their schedules, waiting while you chat, what they may do without
asking, and what they tell you."""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeLLMServer, completion

from athena.agent import Agent
from athena.config import TasksConfig
from athena.history import ConversationStore
from athena.llm import LLMReply
from athena.status import StatusTracker
from athena.tasks import NOTHING, Schedule, ScheduleError, Scheduler, TaskStore
from athena.tasks import scheduler as scheduler_module
from athena.timeline import Timeline
from athena.tools import Tool, ToolError, ToolRegistry
from athena.watching import WatchStore

LONDON = ZoneInfo("Europe/London")
THURSDAY = datetime(2026, 10, 15, 12, 0, tzinfo=LONDON)  # a Thursday, at noon


# -- schedules ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "kept", "first", "described"),
    [
        ("now", "once now", THURSDAY, "once, as soon as you're not chatting"),
        (
            "in 2h",
            "once 2026-10-15 14:00",
            datetime(2026, 10, 15, 14, 0, tzinfo=LONDON),
            "once, on Thu 15 Oct at 14:00",
        ),
        (
            "once 09:00",
            "once 2026-10-16 09:00",
            datetime(2026, 10, 16, 9, 0, tzinfo=LONDON),
            "once, on Fri 16 Oct at 09:00",
        ),
        ("Once 2026-10-20 18:30", "once 2026-10-20 18:30", datetime(2026, 10, 20, 18, 30, tzinfo=LONDON), None),
        ("30 7 * * *", "30 7 * * *", datetime(2026, 10, 16, 7, 30, tzinfo=LONDON), "every day at 07:30"),
        ("0 13 * * 1-5", "0 13 * * 1-5", datetime(2026, 10, 15, 13, 0, tzinfo=LONDON), "every weekday at 13:00"),
        (
            "0  18 * *  MON,thu",
            "0 18 * * mon,thu",
            datetime(2026, 10, 15, 18, 0, tzinfo=LONDON),
            "every Monday and Thursday at 18:00",
        ),
        (
            "cron 0 */6 * * *",
            "0 */6 * * *",
            datetime(2026, 10, 15, 18, 0, tzinfo=LONDON),
            "every day at 00:00, 06:00, 12:00 and 18:00",
        ),  # fmt: skip
        ("@daily", "@daily", datetime(2026, 10, 16, 0, 0, tzinfo=LONDON), "every day at 00:00"),
        ("on arrive-home", "on arrive-home", None, "each time “arrive-home” comes in"),
    ],
)
def test_schedules_are_understood_and_kept_the_same_way(said, kept, first, described):
    schedule = Schedule.parse(said, LONDON, THURSDAY)
    assert schedule.text == kept
    assert Schedule.parse(schedule.text, LONDON, THURSDAY) == schedule  # read back as it was
    assert schedule.first(THURSDAY) == first
    if described:
        assert schedule.describe() == described


def test_when_each_kind_runs_next():
    friday_evening = datetime(2026, 10, 16, 19, 0, tzinfo=LONDON)
    assert Schedule.parse("30 7 * * 1-5", LONDON).after(friday_evening) == datetime(2026, 10, 19, 7, 30, tzinfo=LONDON)
    weekly = Schedule.parse("0 18 * * mon,thu", LONDON)
    assert weekly.after(datetime(2026, 10, 15, 18, 0, tzinfo=LONDON)) == datetime(2026, 10, 19, 18, 0, tzinfo=LONDON)
    assert Schedule.parse("0 */2 * * *", LONDON).after(THURSDAY) == THURSDAY + timedelta(hours=2)
    assert Schedule.parse("now", LONDON).after(THURSDAY) is None  # only once
    assert Schedule.parse("on arrive-home", LONDON).after(THURSDAY) is None
    # Across the clocks going back (Sunday 25 October): still 07:30 on the clock.
    after_change = Schedule.parse("30 7 * * *", LONDON).after(datetime(2026, 10, 24, 8, 0, tzinfo=LONDON))
    assert after_change == datetime(2026, 10, 25, 7, 30, tzinfo=LONDON) and after_change.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("said", "why"),
    [
        ("whenever", "I don't understand the schedule 'whenever': A cron schedule has five fields"),
        ("30 25 * * *", "The hour must be 0-23"),
        ("*/5 * * * *", "at most every 15 minutes"),
        ("0 0 30 2 *", "never happens"),
        ("once 2026-10-14 09:00", "has already passed"),
        ("once soon", "Say when"),
        ("on Arrive Home", "Name the event in lower case"),
    ],
)
def test_schedules_that_cant_be_understood_say_why(said, why):
    with pytest.raises(ScheduleError, match=why):
        Schedule.parse(said, LONDON, THURSDAY)


# -- running them -------------------------------------------------------------------------------------


class FakeChannel:
    """Telegram, as far as tasks are concerned: what they say, and what they ask."""

    chat_id = "111"

    def __init__(self, allow=True):
        self.said: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.sessions: list[int | None] = []  # what each was told or asked about
        self.allow = allow

    async def tell(self, text, *, session=None):
        self.said.append(text)
        self.sessions.append(session)

    async def ask(self, tool, args, summary, *, why, timeout, session=None):
        self.asked.append((tool, why))
        self.sessions.append(session)
        return self.allow


@pytest.fixture
def athena(config, memory, tmp_path):
    """An agent with a tool that asks first (publish) and one that doesn't (look), and a scheduler."""

    def make(replies, channel=None, **settings):
        published, looked = [], []

        async def publish(args):
            published.append(args)
            return "Published."

        async def look(args):
            looked.append(args)
            return "Seen."

        tools = ToolRegistry()
        for tool in memory.tools():
            tools.add(tool)
        tools.add(Tool("publish", "Publish it", {"type": "object", "properties": {}}, publish, needs_confirmation=True))
        tools.add(Tool("look", "Look", {"type": "object", "properties": {}}, look))
        server = FakeLLMServer(replies)
        history = ConversationStore(config.db_path)
        status = StatusTracker()
        agent = Agent(config.agent, server.client(config.llm), tools, history, memory, status=status)
        channel = channel or FakeChannel()
        ran = []
        scheduler = Scheduler(
            TasksConfig(**{"quiet_minutes": 0, **settings}),
            TaskStore(config.db_path),
            agent,
            timezone="Europe/London",
            channel=lambda: channel,
            tools=tools.all,
            on_ran=lambda: ran.append(1),
            poll_seconds=0.01,
            watches=WatchStore(config.db_path),
            timeline=Timeline(config.db_path),
        )
        for tool in scheduler.tools():
            tools.add(tool)
        return dict(
            scheduler=scheduler,
            server=server,
            agent=agent,
            channel=channel,
            history=history,
            published=published,
            looked=looked,
            ran=ran,
            status=status,
            timeline=scheduler.timeline,
        )

    return make


async def test_a_task_reports_what_it_found_in_its_session(athena):
    a = athena([completion(None, [("look", {})]), completion("Rain at 4pm, so take an umbrella.")])
    task = a["scheduler"].add("Weather", "Check the weather for my commute.", "30 7 * * *", [], created_by="you")
    assert task.session_id == a["history"].session.id  # the current session, unless it's given another
    await a["scheduler"].run(task)

    assert a["channel"].said == ["📋 **Weather**\n\nRain at 4pm, so take an umbrella."]
    assert a["channel"].sessions == [task.session_id]
    assert a["looked"] == [{}]
    # It's in the session's conversation, so you can ask about it, and its next run reads it.
    assert a["history"].load(a["history"].current_chat) == [
        {"role": "user", "content": "(Your task “Weather”) Check the weather for my commute."},
        {"role": "assistant", "content": "Rain at 4pm, so take an umbrella."},
    ]
    [said, _] = a["history"].messages(task.session_id)[0]
    assert said.channel == "Task"
    first = a["server"].requests[0]
    assert "This is your task “Weather”" in first["messages"][-1]["content"]
    assert first["chat_template_kwargs"] == {"enable_thinking": True}  # tasks think, whatever [llm] says
    saved = a["scheduler"].store.get(task.id)
    assert (saved.runs, saved.last_outcome, saved.status) == (1, "told you", "active")
    ran_at = datetime.fromtimestamp(saved.last_run, LONDON)
    assert saved.next_run == Schedule.parse("30 7 * * *", LONDON).after(ran_at).timestamp()
    assert a["ran"] == [1]  # so the model server reads your chat again, with the report in it


async def test_a_run_keeps_the_steps_it_took_for_the_dashboard(athena):
    a = athena([completion(None, [("look", {"where": "x" * 1000}), ("publish", {})]), completion("Done.")],
               FakeChannel(allow=False))  # fmt: skip
    task = a["scheduler"].add("Weather", "Check the weather.", "30 7 * * *", [], created_by="you")
    await a["scheduler"].run(task)
    look, publish = a["scheduler"].describe(a["scheduler"].store.get(task.id))["last_steps"]
    assert (look["tool"], look["outcome"], look["said"]) == ("look", "ran", "Seen.")
    assert len(look["given"]) == 400 and look["given"].endswith("…")  # cut short
    assert (publish["tool"], publish["outcome"]) == ("publish", "declined")


def test_tasks_kept_from_before_steps_were_kept_still_load(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "tasks.db")
    db.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, instructions TEXT NOT NULL, "
        "schedule TEXT NOT NULL, allowed_tools TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active', "
        "created_by TEXT NOT NULL, created_at REAL NOT NULL, next_run REAL, last_run REAL, last_outcome TEXT NOT NULL "
        "DEFAULT '', last_result TEXT NOT NULL DEFAULT '', runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL "
        "DEFAULT 0)"
    )
    db.execute(
        "INSERT INTO tasks (title, instructions, schedule, created_by, created_at) VALUES ('B', 'b', 'now', 'you', 1)"
    )
    db.commit()
    db.close()
    [task] = TaskStore(tmp_path / "tasks.db").all()
    assert (task.title, task.last_steps) == ("B", [])


async def test_a_tasks_message_with_nowhere_to_go_isnt_logged_either(athena, caplog):
    caplog.set_level(logging.INFO)
    scheduler = athena([])["scheduler"]
    scheduler.channel = lambda: None
    await scheduler._tell("Your locker number is 42")
    assert "Nowhere to send a task's message of 24 characters" in caplog.text and "locker" not in caplog.text


async def test_nothing_to_report_stays_quiet(athena):
    a = athena([completion(f"I looked, and {NOTHING}.")])
    task = a["scheduler"].add("Servers", "Find MCP servers.", "now", [], created_by="athena")
    await a["scheduler"].run(task)
    assert a["channel"].said == []
    assert a["history"].messages(task.session_id)[0] == []  # nor does it fill the session
    saved = a["scheduler"].store.get(task.id)
    assert (saved.last_outcome, saved.status, saved.next_run) == ("nothing new", "done", None)


async def test_a_task_asks_before_tools_it_may_not_use_without_asking(athena):
    replies = [completion(None, [("publish", {})]), completion("Done.")]
    a = athena(replies, channel=FakeChannel(allow=False))
    task = a["scheduler"].add("Post", "Publish the notes.", "now", [], created_by="you")
    await a["scheduler"].run(task)
    assert a["channel"].asked == [("publish", "For your task “Post”")] and a["published"] == []

    b = athena([completion(None, [("publish", {})]), completion("Done.")])
    task = b["scheduler"].add("Post", "Publish the notes.", "now", ["publish"], created_by="you")
    await b["scheduler"].run(task)
    assert b["channel"].asked == [] and b["published"] == [{}]  # allowed without asking


def test_a_task_cant_be_allowed_to_schedule_others_or_use_tools_that_dont_exist(athena):
    scheduler = athena([])["scheduler"]
    with pytest.raises(ValueError, match="always has to ask before it uses schedule_task"):
        scheduler.add("Loop", "Make more tasks.", "now", ["schedule_task"], created_by="you")
    with pytest.raises(ValueError, match="no tool called nope"):
        scheduler.add("X", "Y", "now", ["nope"], created_by="you")
    with pytest.raises(ValueError, match="title and instructions"):
        scheduler.add(" ", "Y", "now", [], created_by="you")
    assert scheduler.store.all() == []


async def test_a_task_that_fails_tries_again_then_tells_you(athena):
    a = athena([])  # the model server has nothing to say: every call fails
    task = a["scheduler"].add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    before = time.time()
    await a["scheduler"].run(task)
    saved = a["scheduler"].store.get(task.id)
    assert saved.failures == 1 and saved.last_outcome.startswith("failed: The model server returned an error")
    assert before + 590 < saved.next_run < time.time() + 610  # again in 10 minutes
    await a["scheduler"].run(saved)
    saved = a["scheduler"].store.get(task.id)
    assert saved.failures == 2 and saved.next_run > time.time() + 1700  # then in 30
    assert a["channel"].said == []
    await a["scheduler"].run(saved)
    saved = a["scheduler"].store.get(task.id)
    assert a["channel"].said[0].startswith("⚠️ I couldn't do your task “Briefing”: The model server returned an error")
    assert saved.failures == 0 and saved.status == "active"  # and on to its next run
    assert saved.next_run == Schedule.parse("30 7 * * 1-5", LONDON).after(datetime.now(LONDON)).timestamp()


async def test_a_task_that_runs_out_of_time_says_so(athena):
    a = athena([completion(None, [("look", {})]), completion("Done.")], max_minutes=0)
    task = a["scheduler"].add("Long", "Think hard.", "now", [], created_by="you")
    await a["scheduler"].run(task)
    assert a["channel"].said == ["⏱️ Your task “Long” ran out of time (0 minutes) before it finished."]
    assert a["scheduler"].store.get(task.id).last_outcome == "ran out of time"


async def test_stopping_a_task(athena):
    started = asyncio.Event()

    async def slow(args):
        started.set()
        await asyncio.Event().wait()

    a = athena([completion(None, [("slow", {})])])
    a["agent"].tools.add(Tool("slow", "Takes a while", {"type": "object", "properties": {}}, slow))
    task = a["scheduler"].add("Slow", "Do the slow thing.", "now", [], created_by="you")
    running = asyncio.create_task(a["scheduler"].run(task))
    await asyncio.wait_for(started.wait(), 2)
    assert a["scheduler"].describe(task)["status"] == "running"
    # A change to the tools doesn't wait for it, as it would for your messages.
    async with asyncio.timeout(1), a["agent"].paused():
        pass
    assert a["scheduler"].stop_running()
    await asyncio.wait_for(running, 2)
    saved = a["scheduler"].store.get(task.id)
    assert (saved.last_outcome, saved.status) == ("stopped", "paused")  # a one-off: so you can run it again
    assert a["history"].load(f"task:{task.id}") == [] and not a["scheduler"].stop_running()


# -- you come first ------------------------------------------------------------------------------------


async def test_a_due_task_waits_until_you_have_stopped_chatting(athena, monkeypatch):
    monkeypatch.setattr(scheduler_module, "FIRST_WAIT", 0.02)
    monkeypatch.setattr(scheduler_module, "LONGEST_WAIT", 0.08)
    a = athena([completion("Done.")], quiet_minutes=1)
    scheduler, agent = a["scheduler"], a["agent"]
    agent.last_answered_you = time.monotonic()  # you've just had a reply
    task = scheduler.add("Later", "Do it.", "now", [], created_by="you")
    waits = []
    sleep = scheduler._sleep

    async def watching(seconds):
        waits.append(round(seconds, 2))
        if len(waits) == 4:
            agent.last_answered_you = time.monotonic() - 61  # a minute has gone by
        await sleep(seconds)

    scheduler._sleep = watching
    assert await scheduler._wait_for_you(task)
    assert waits == [0.02, 0.04, 0.08, 0.08]  # backing off, up to the longest wait
    assert scheduler.waiting is None

    # Paused while it waited: it doesn't run.
    agent.last_answered_you = time.monotonic()
    waits.clear()
    scheduler._sleep = sleep

    async def pause_it():
        await asyncio.sleep(0.03)
        scheduler.pause(task.id)

    pausing = asyncio.create_task(pause_it())
    assert not await scheduler._wait_for_you(task)
    await pausing

    # Run now: it doesn't wait for the quiet minute, only for a reply in progress.
    scheduler.run_now(task.id)
    assert await asyncio.wait_for(scheduler._wait_for_you(scheduler.store.get(task.id)), 1)


async def test_a_running_task_stops_for_you_and_carries_on_after(athena):
    a = athena([completion(None, [("check", {})]), completion("All done.")])
    scheduler, agent = a["scheduler"], a["agent"]

    async def check(args):  # you send a message while it's using a tool
        agent.answering_you = 1
        return "Checked."

    agent.tools.add(Tool("check", "Check", {"type": "object", "properties": {}}, check))
    task = scheduler.add("Errand", "Check, then report.", "now", [], created_by="you")
    running = asyncio.create_task(scheduler.run(task))
    while agent.answering_you == 0:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)
    assert len(a["server"].requests) == 1  # it waited, rather than asking the model again
    assert a["status"].snapshot().step == "Waiting until you've finished chatting"
    agent.answering_you = 0  # you've had your reply
    await asyncio.wait_for(running, 2)
    assert len(a["server"].requests) == 2 and a["channel"].said == ["📋 **Errand**\n\nAll done."]


async def test_your_message_stops_a_tasks_step_and_it_takes_it_again_after(athena):
    a = athena([])
    agent, scheduler = a["agent"], a["scheduler"]
    calls, stopped = [], []
    thinking = asyncio.Event()

    async def chat(messages, tools=None, **kwargs):  # the model, taking its time over the task's first step
        calls.append(messages[-1]["content"][-30:])
        if len(calls) == 1:
            thinking.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.append("the first step")
                raise
        return LLMReply(content="Done.")

    agent.llm.chat = chat
    task = scheduler.add("Think", "Think it through.", "now", [], created_by="you")
    running = asyncio.create_task(scheduler.run(task))
    await asyncio.wait_for(thinking.wait(), 2)

    reply = await asyncio.wait_for(agent.respond("111", "hello"), 2)  # you don't wait for the task
    assert reply.text == "Done."
    await asyncio.wait_for(running, 2)
    assert stopped == ["the first step"]
    assert [c.endswith("Think it through.") for c in calls] == [True, False, True]  # task, you, the task again
    assert a["channel"].said == ["📋 **Think**\n\nDone."]


async def test_your_messages_count_as_chatting_and_tasks_dont(athena):
    a = athena([completion("Hi!"), completion("Done.")])
    agent, scheduler = a["agent"], a["scheduler"]
    assert scheduler.quiet()
    await agent.respond("111", "hello")
    assert agent.answering_you == 0 and time.monotonic() - agent.last_answered_you < 1
    scheduler.cfg.quiet_minutes = 5
    assert not scheduler.quiet()
    before = agent.last_answered_you
    task = scheduler.add("Bg", "Do it.", "now", [], created_by="you")
    scheduler.run_now(task.id)  # runs now, as it's not waiting on a reply in progress
    await scheduler.run(scheduler.store.get(task.id))
    assert agent.last_answered_you == before


async def test_the_scheduler_runs_what_is_due_and_skips_long_missed_runs(athena):
    a = athena([completion("Here's your briefing.")])
    scheduler = a["scheduler"]
    old = scheduler.add("Old", "Daily thing.", "0 7 * * *", [], created_by="you")
    old.next_run = time.time() - 86400 * 2  # Athena was off for two days
    scheduler.store.save(old)
    scheduler.start()
    try:
        assert scheduler.store.get(old.id).next_run > time.time()  # skipped to its next run, not run late
        scheduler.add("Brief", "Brief me.", "now", [], created_by="you")
        async with asyncio.timeout(3):
            while not a["channel"].said:
                await asyncio.sleep(0.01)
        assert a["channel"].said == ["📋 **Brief**\n\nHere's your briefing."]
    finally:
        await scheduler.stop()


async def test_a_one_off_runs_once_its_time_has_come(athena):
    a = athena([completion("Call Sam now.")])
    task = a["scheduler"].add("Call", "Remind me to call Sam.", "in 2h", [], created_by="you")
    task.schedule = "once 2026-10-01 09:00"  # as it's kept, once that time has passed
    a["scheduler"].store.save(task)
    assert a["scheduler"].describe(task)["when"] == "once, on Thu 1 Oct at 09:00"
    await a["scheduler"].run(task)
    assert a["channel"].said == ["📋 **Call**\n\nCall Sam now."]
    assert a["scheduler"].store.get(task.id).status == "done"


# -- changing them -------------------------------------------------------------------------------------


def test_changing_a_task(athena):
    scheduler = athena([])["scheduler"]
    task = scheduler.add("Daily", "Check.", "30 7 * * *", [], created_by="athena")
    changed = scheduler.change(task.id, " Weekly ", "Check more. ", "0 9 * * 1", ["publish"])
    kept = scheduler.store.get(task.id)
    assert (kept.title, kept.instructions, kept.schedule, kept.allowed_tools) == (
        "Weekly", "Check more.", "0 9 * * 1", ["publish"],
    )  # fmt: skip
    assert kept.next_run == Schedule.parse("0 9 * * 1", LONDON).after(datetime.now(LONDON)).timestamp()
    assert changed.created_by == "athena" and kept.status == "active"
    scheduler.change(task.id, "Weekly", "Check again.", "0 9 * * 1", [])  # the same schedule: still due then
    assert scheduler.store.get(task.id).next_run == kept.next_run


def test_a_change_that_wont_do_changes_nothing(athena):
    scheduler = athena([])["scheduler"]
    task = scheduler.add("Daily", "Check.", "30 7 * * *", [], created_by="you")
    for title, schedule, tools, why in [
        ("", "30 7 * * *", [], "needs a title"),
        ("Daily", "sometime", [], "I don't understand"),
        ("Daily", "30 7 * * *", ["schedule_task"], "always has to ask"),
    ]:
        with pytest.raises(ValueError, match=why):
            scheduler.change(task.id, title, "Changed.", schedule, tools)
    assert scheduler.store.get(task.id).instructions == "Check."
    with pytest.raises(ValueError, match="no task #99"):
        scheduler.change(99, "X", "Y", "now", [])


def test_a_done_one_off_can_be_renamed_or_scheduled_again(athena):
    scheduler = athena([])["scheduler"]
    task = scheduler.add("Call", "Remind me.", "in 2h", [], created_by="you")
    task.schedule, task.status, task.next_run = "once 2026-10-01 09:00", "done", None  # it's run
    scheduler.store.save(task)
    scheduler.change(task.id, "Call Sam", "Remind me.", "once 2026-10-01 09:00", [])  # its time has passed
    assert (scheduler.store.get(task.id).title, scheduler.store.get(task.id).status) == ("Call Sam", "done")
    later = (datetime.now(LONDON) + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    scheduler.change(task.id, "Call Sam", "Remind me.", f"once {later:%Y-%m-%d %H:%M}", [])
    kept = scheduler.store.get(task.id)
    assert kept.status == "active" and kept.next_run == later.timestamp()


def test_a_paused_task_stays_paused_and_resumes_on_its_new_schedule(athena):
    scheduler = athena([])["scheduler"]
    task = scheduler.add("Call", "Remind me.", "now", [], created_by="you")
    task.last_run = time.time() - 60  # it ran, and was stopped, so it's paused
    scheduler.store.save(task)
    scheduler.pause(task.id)
    later = (datetime.now(LONDON) + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    scheduler.change(task.id, "Call", "Remind me.", f"once {later:%Y-%m-%d %H:%M}", [])
    assert (scheduler.store.get(task.id).status, scheduler.store.get(task.id).next_run) == ("paused", None)
    assert scheduler.resume(task.id).next_run == later.timestamp()  # not straight away


async def test_a_running_task_cant_be_changed(athena):
    a = athena([completion("Done.")])
    scheduler = a["scheduler"]
    task = scheduler.add("Sums", "Work it out.", "now", [], created_by="you")
    scheduler.running = task
    with pytest.raises(ValueError, match="is running"):
        scheduler.change(task.id, "Sums", "Something else.", "now", [])


# -- the model's tools ---------------------------------------------------------------------------------


async def test_the_model_schedules_tasks_with_your_ok(athena):
    a = athena([])
    scheduler = a["scheduler"]
    tools = {t.name: t for t in scheduler.tools()}
    schedule = tools["schedule_task"]
    assert schedule.needs_confirmation  # always asks
    args = {
        "title": "Papers",
        "instructions": "Find new papers on X.",
        "schedule": "0 8 * * mon",
        "tools": ["publish"],
    }
    summary = await schedule.preview(args)
    assert summary == (
        "A task, “Papers”, every Monday at 08:00, in the current session:\nFind new papers on X.\n"
        " It may use publish without asking you."
    )
    with pytest.raises(ToolError, match="The hour must be 0-23"):
        await schedule.preview({**args, "schedule": "0 24 * * mon"})
    with pytest.raises(ToolError, match="always has to ask"):
        await schedule.preview({**args, "tools": ["schedule_task"]})

    assert await schedule.handler(args) == "Scheduled task #1, “Papers”, every Monday at 08:00."
    assert scheduler.store.get(1).created_by == "athena"
    listed = await tools["list_tasks"].handler({})
    assert listed.startswith("#1 Papers (active): every Monday at 08:00, next Mon")

    change = tools["change_task"].handler
    assert await change({"id": 1, "action": "pause"}) == "Task #1, “Papers”, is paused."
    assert await change({"id": 1, "action": "resume"}) == "Task #1, “Papers”, is running again."
    assert await change({"id": 1, "action": "run"}) == "Task #1, “Papers”, will run now."
    with pytest.raises(ToolError, match="no task #9"):
        await change({"id": 9, "action": "run"})
    with pytest.raises(ToolError, match="run, pause or resume"):
        await change({"id": 1, "action": "delete"})  # deleting is only for you
    scheduler.delete(1)
    assert await tools["list_tasks"].handler({}) == "There are no tasks."


# -- in sessions --------------------------------------------------------------------------------------


async def test_a_task_works_with_what_its_session_knows_and_carries_it_on(athena):
    a = athena([completion("Two new jobs: a data role at Acme, and one at Initech.")])
    history = a["history"]
    jobs = history.current_chat
    history.rename(history.session.id, "Job hunting")
    history.append_exchange(jobs, "My CV: Python, data, five years.", "Got it.", channel="Dashboard")
    elsewhere = history.new_session()  # you've moved on to something else since
    task = a["scheduler"].add("Find jobs", "Look for new postings.", "0 9 * * *", [], "you", session=1)
    await a["scheduler"].run(task)

    asked = a["server"].requests[0]["messages"]
    assert asked[1] == {"role": "user", "content": "My CV: Python, data, five years."}  # the session's, to go on
    assert a["channel"].said == [
        "📋 **Find jobs** · Job hunting\n\nTwo new jobs: a data role at Acme, and one at Initech."
    ]
    assert [m.text for m in history.messages(1)[0]][-1].startswith("Two new jobs")
    assert history.messages(elsewhere.id)[0] == []
    # Its session is the current one now, so what you say next, wherever you say it, carries on from it.
    assert history.session.id == 1


async def test_a_report_doesnt_take_you_from_a_session_youre_talking_in(athena):
    a = athena([])
    agent, history = a["agent"], a["history"]

    def reply(body):
        agent.answering_you = 1  # you've started talking to Athena as the task finishes
        return completion("Done.")

    a["server"].replies = reply
    task = a["scheduler"].add("Errand", "Do it.", "now", [], "you")
    yours = history.new_session()
    await a["scheduler"].run(task)
    agent.answering_you = 0
    assert history.session.id == yours.id and a["channel"].said == ["📋 **Errand**\n\nDone."]


async def test_you_can_talk_in_a_session_while_its_task_is_working_in_it(athena, config):
    let_go = asyncio.Event()

    async def wait_here(args):
        await let_go.wait()
        return "Waited."

    def model(body):
        last = body["messages"][-1]
        if last["role"] == "tool":
            return completion("The task's done.")
        if "This is your task" in last["content"]:
            return completion(None, [("wait_here", {})])
        return completion("Hello to you.")

    a = athena([], quiet_minutes=0)
    a["server"].replies = model
    a["agent"].tools.add(Tool("wait_here", "Waits.", {"type": "object", "properties": {}}, wait_here))
    task = a["scheduler"].add("Slow", "Take your time.", "now", [], "you")
    running = asyncio.create_task(a["scheduler"].run(task))
    for _ in range(200):
        if a["status"].snapshot().step == "Using wait_here":
            break
        await asyncio.sleep(0.01)
    # Yours is answered while the task's waiting, not after it: neither waits on the other.
    answer = await asyncio.wait_for(a["agent"].respond(a["history"].current_chat, "Hi"), 2)
    assert answer.text == "Hello to you."
    let_go.set()
    await asyncio.wait_for(running, 2)
    assert [m.text for m in a["history"].messages(task.session_id)[0]] == [
        "Hi",
        "Hello to you.",
        "(Your task “Slow”) Take your time.",
        "The task's done.",
    ]


async def test_a_task_asks_for_your_ok_in_its_session(athena):
    a = athena([completion(None, [("publish", {})]), completion("Done.")], FakeChannel(allow=False))
    task = a["scheduler"].add("Post", "Publish the notes.", "now", [], "you")
    a["history"].new_session()
    await a["scheduler"].run(task)
    assert a["channel"].asked == [("publish", "For your task “Post”")]
    assert a["channel"].sessions[0] == task.session_id


async def test_tasks_belong_to_sessions_that_exist(athena):
    a = athena([])
    scheduler, history = a["scheduler"], a["history"]
    with pytest.raises(ValueError, match="no session #9"):
        scheduler.add("A", "Do it.", "now", [], "you", session=9)
    other = history.new_session("Research", current=False)
    task = scheduler.add("A", "Do it.", "now", [], "you")
    moved = scheduler.change(task.id, "A", "Do it.", "now", [], session=other.id)
    assert moved.session_id == other.id and scheduler.in_session(other.id) == [scheduler.store.get(task.id)]
    # One whose session's gone runs in a new one, named after it.
    history.delete_session(other.id)
    a["server"].replies = [completion("Done.")]
    await scheduler.run(scheduler.store.get(task.id))
    again = scheduler.store.get(task.id)
    assert again.session_id not in (None, other.id) and history.title(again.session_id) == "A"


async def test_a_task_athena_schedules_runs_in_the_session_its_asked_in(athena):
    from athena.tools import Turn, begin_turn

    a = athena([])
    history = a["history"]
    history.rename(1, "Maths")
    history.new_session()
    schedule = {t.name: t for t in a["scheduler"].tools()}["schedule_task"]
    args = {"title": "Primes", "instructions": "Look for patterns.", "schedule": "0 8 * * mon"}
    end = begin_turn(Turn(chat="session:1"))
    try:
        assert "in “Maths”" in await schedule.preview(args)
        await schedule.handler(args)
    finally:
        end()
    assert a["scheduler"].store.get(1).session_id == 1


async def test_tasks_from_before_sessions_get_sessions_of_their_own(config, memory):
    history = ConversationStore(config.db_path)
    store = TaskStore(config.db_path)
    store.add("Briefing", "Brief me.", "30 7 * * *", [], "you", None)
    store.add("Never ran", "Something.", "now", [], "you", None)
    history.append_exchange("task:1", "Brief me.", "Dentist at 2.")
    history.append_exchange("task:1", "Brief me.", NOTHING)
    history.append_exchange("task:1", "Brief me.", "Rain later.")
    yours = history.session
    history.append_exchange(yours.chat_id, "Hello", "Hi")
    agent = Agent(config.agent, None, ToolRegistry(), history, memory)
    scheduler = Scheduler(TasksConfig(), store, agent, timezone="Europe/London", channel=lambda: None, tools=lambda: [])
    first, second = scheduler.store.all()
    assert first.session_id and second.session_id and first.session_id != second.session_id
    assert history.title(first.session_id) == "Briefing" and history.title(second.session_id) == "Never ran"
    said = history.messages(first.session_id)[0]
    assert [(m.role, m.text, m.channel) for m in said] == [
        ("user", "(Your task “Briefing”) Brief me.", "Task"),
        ("assistant", "Dentist at 2.", ""),
        ("user", "(Your task “Briefing”) Brief me.", "Task"),
        ("assistant", "Rain later.", ""),
    ]
    assert history.load("task:1") == [] and len(history.load(f"session:{first.session_id}")) == 4
    assert history.session.id == yours.id  # yours is still the current one
    assert [s.id for s in history.sessions()][0] == yours.id
    # Done once: they're not moved again.
    Scheduler(TasksConfig(), store, agent, timezone="Europe/London", channel=lambda: None, tools=lambda: [])
    assert [t.session_id for t in store.all()] == [first.session_id, second.session_id]
