"""Tasks Athena does by itself: their schedules, waiting while you chat, what they may do without
asking, and what they tell you."""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeLLMServer, completion

from pi_assistant import tasks as tasks_module
from pi_assistant.agent import Agent
from pi_assistant.config import TasksConfig
from pi_assistant.history import ConversationStore
from pi_assistant.llm import LLMReply
from pi_assistant.status import StatusTracker
from pi_assistant.tasks import NOTHING, Schedule, ScheduleError, Scheduler, TaskStore, old_to_cron
from pi_assistant.tools import Tool, ToolError, ToolRegistry
from pi_assistant.watching import WatchStore

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


@pytest.mark.parametrize(
    ("said", "why"),
    [
        ("daily 07:30", "cron schedules now .*: “daily 07:30” is “30 7 \\* \\* \\*”"),
        ("weekdays 13:00", "“weekdays 13:00” is “0 13 \\* \\* 1-5”"),
        ("weekly mon,thu 18:00", "“weekly mon,thu 18:00” is “0 18 \\* \\* 1,4”"),
        ("every 6h", "“every 6h” is “0 0,6,12,18 \\* \\* \\*”"),
        ("every 90m", "cron schedules now .*Schedules:"),  # cron can't say it: no suggestion that's wrong
        ("daily 25:00", "cron schedules now .*Schedules:"),
    ],
)
def test_schedules_from_before_cron_say_what_they_are_in_cron(said, why):
    with pytest.raises(ScheduleError, match=why):
        Schedule.parse(said, LONDON, THURSDAY)


@pytest.mark.parametrize(
    ("old", "due", "cron", "exact"),
    [
        ("daily 07:30", None, "30 7 * * *", True),
        ("weekdays 07:30", None, "30 7 * * 1-5", True),
        ("weekly sun,mon 10:00", None, "0 10 * * 0,1", True),
        ("every 15m", datetime(2026, 10, 15, 12, 7), "7-59/15 * * * *", True),  # keeps to when it was due
        ("every 30m", datetime(2026, 10, 15, 12, 30), "*/30 * * * *", True),
        ("every 1h", datetime(2026, 10, 15, 12, 20), "20 * * * *", True),
        ("every 6h", datetime(2026, 10, 15, 14, 20), "20 2,8,14,20 * * *", True),
        ("every 1d", datetime(2026, 10, 15, 9, 5), "5 9 * * *", True),
        ("every 90m", datetime(2026, 10, 15, 12, 0), "0 0,2,4,6,8,10,12,14,16,18,20,22 * * *", False),  # 2 hours
        ("every 5h", datetime(2026, 10, 15, 12, 0), "0 0,6,12,18 * * *", False),
        ("every 2d", datetime(2026, 10, 15, 9, 0), "0 9 */2 * *", False),
    ],
)
def test_schedules_from_before_cron_become_the_cron_that_does_the_same(old, due, cron, exact):
    assert old_to_cron(old, due and due.replace(tzinfo=LONDON)) == (cron, exact)
    Schedule.parse(cron, LONDON)  # and it's one that can be used


async def test_tasks_kept_from_before_cron_are_given_cron_schedules_and_you_hear_of_any_that_changed(athena):
    a = athena([])
    store = a["scheduler"].store
    due = datetime(2026, 10, 15, 14, 20, tzinfo=LONDON).timestamp()
    briefing = store.add("Briefing", "Brief me.", "weekdays 07:30", [], "you", due)
    often = store.add("Often", "Look.", "every 6h", [], "you", due)
    odd = store.add("Odd", "Look.", "every 90m", [], "you", due)
    paused = store.add("Paused", "Look.", "weekly fri 18:00", [], "you", None)
    paused.status = "paused"
    store.save(paused)

    scheduler = make_scheduler(a, store)
    assert [store.get(t.id).schedule for t in (briefing, often, odd, paused)] == [
        "30 7 * * 1-5",
        "20 2,8,14,20 * * *",
        "20 0,2,4,6,8,10,12,14,16,18,20,22 * * *",
        "0 18 * * 5",
    ]
    assert store.get(briefing.id).next_run == due and store.get(often.id).next_run == due  # as they were
    assert store.get(odd.id).next_run > time.time() and store.get(paused.id).status == "paused"
    scheduler.start()
    try:
        async with asyncio.timeout(3):
            while not a["channel"].said:
                await asyncio.sleep(0.01)
    finally:
        await scheduler.stop()
    [said] = a["channel"].said
    assert said.startswith("🗓️ Tasks' regular schedules are cron schedules now, and for some it's changed:")
    assert "“Odd” was every 90m, which cron can't say: it now runs every day at 00:20, 02:20" in said
    assert "Briefing" not in said and "Often" not in said  # they do just what they did

    again = make_scheduler(a, store)  # converted once: nothing to do or say the next time
    assert again._changed == []


def make_scheduler(a, store):
    old = a["scheduler"]
    return Scheduler(
        old.cfg, store, old.agent, timezone="Europe/London", channel=old.channel, tools=old._tools, poll_seconds=0.01
    )


# -- running them -------------------------------------------------------------------------------------


class FakeChannel:
    """Telegram, as far as tasks are concerned: what they say, and what they ask."""

    chat_id = "111"

    def __init__(self, allow=True):
        self.said: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.allow = allow

    async def tell(self, text):
        self.said.append(text)

    async def ask(self, tool, args, summary, *, why, timeout):
        self.asked.append((tool, why))
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
        )

    return make


async def test_a_task_reports_what_it_found_in_your_chat(athena):
    a = athena([completion(None, [("look", {})]), completion("Rain at 4pm, so take an umbrella.")])
    task = a["scheduler"].add("Weather", "Check the weather for my commute.", "30 7 * * *", [], created_by="you")
    await a["scheduler"].run(task)

    assert a["channel"].said == ["📋 **Weather**\n\nRain at 4pm, so take an umbrella."]
    assert a["looked"] == [{}]
    # It's in your chat, so you can ask about it, and in the task's own, for its next run.
    assert a["history"].load("111")[-2:] == [
        {"role": "user", "content": "(Your task “Weather” ran.)"},
        {"role": "assistant", "content": "Rain at 4pm, so take an umbrella."},
    ]
    assert a["history"].load(f"task:{task.id}")[0]["content"] == "Check the weather for my commute."
    first = a["server"].requests[0]
    assert "a task you're doing by yourself" in first["messages"][-1]["content"]
    assert first["chat_template_kwargs"] == {"enable_thinking": True}  # tasks think, whatever [llm] says
    saved = a["scheduler"].store.get(task.id)
    assert (saved.runs, saved.last_outcome, saved.status) == (1, "told you", "active")
    ran_at = datetime.fromtimestamp(saved.last_run, LONDON)
    assert saved.next_run == Schedule.parse("30 7 * * *", LONDON).after(ran_at).timestamp()
    assert a["ran"] == [1]  # so the model server reads your chat again, with the report in it


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
    monkeypatch.setattr(tasks_module, "FIRST_WAIT", 0.02)
    monkeypatch.setattr(tasks_module, "LONGEST_WAIT", 0.08)
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
    assert (
        summary
        == "A task, “Papers”, every Monday at 08:00:\nFind new papers on X.\n It may use publish without asking you."
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
