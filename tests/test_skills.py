"""Skills: written by you or by Athena, read before doing what they're for, suggested rather than saved when
Athena's working by itself, and looked back on after they're used."""

import asyncio
import sqlite3
import time

import pytest
from conftest import FakeLLMServer, completion
from test_tasks import FakeChannel

from pi_assistant.agent import Agent, Background
from pi_assistant.config import TasksConfig
from pi_assistant.history import ConversationStore
from pi_assistant.skills import NO_CHANGE, Skills, SkillStore, check, diff
from pi_assistant.tasks import NOTHING, Scheduler, TaskStore
from pi_assistant.tools import ToolError, ToolRegistry, Turn, begin_turn

TRIP = (
    "Trip plans",
    "When George asks you to plan a trip",
    "1. Trains before flights.\n2. A day-by-day list.\n3. Costs at the end.",
)


@pytest.fixture
def skills(config):
    changed = []
    history = ConversationStore(config.db_path)
    s = Skills(SkillStore(config.db_path), history, timezone="Europe/London", on_change=lambda: changed.append(1))
    s.changed = changed
    yield s
    s.store.close()


def tool(skills, name):
    return next(t for t in skills.tools() if t.name == name).handler


async def alone(source, coro):
    """Run a tool as Athena would working by itself."""
    end = begin_turn(Turn(alone=source))
    try:
        return await coro
    finally:
        end()


# -- you, on the dashboard -----------------------------------------------------------------------------


def test_skills_are_kept_with_every_version(skills):
    skill = skills.save(None, *TRIP, by="you")
    assert (skill.name, skill.created_by, skill.updated_by, skill.uses) == ("Trip plans", "you", "you", 0)
    assert skills.index() == "## Your skills\n- Trip plans: When George asks you to plan a trip"
    assert skills.changed == [1]  # the list in the prompt changed

    changed = skills.save(skill.id, "Trip plans", "When George asks you to plan a trip", "Trains first.", by="you")
    assert changed.body == "Trains first." and skills.changed == [1]  # the list didn't change, so no new prompt
    versions = skills.store.versions(skill.id)
    assert [v.body for v in versions] == ["Trains first.", TRIP[2]]  # newest first

    restored = skills.restore(skill.id, versions[1].id)
    assert restored.body == TRIP[2] and len(skills.store.versions(skill.id)) == 3

    with pytest.raises(ValueError, match="There's a skill called Trip plans already"):
        skills.save(None, "trip  PLANS", "x", "y", by="you")
    skills.delete(skill.id)
    assert skills.store.all() == [] and skills.store.versions(skill.id) == [] and skills.index() == ""
    with pytest.raises(ValueError, match="no skill #"):
        skills.delete(skill.id)


@pytest.mark.parametrize(
    ("name", "description", "body", "why"),
    [
        ("", "d", "b", "needs a name"),
        ("n", " ", "b", "needs a name"),
        ("n" * 61, "d", "b", "at most 60"),
        ("n", "d" * 201, "b", "at most 200"),
        ("n", "d", "b" * 5001, "at most 5,000"),
    ],
)
def test_a_skill_that_isnt_right_says_why(name, description, body, why):
    with pytest.raises(ValueError, match=why):
        check(name, description, body)


def test_a_change_is_shown_line_by_line():
    assert diff("a\nb\nc", "a\nB\nc\nd") == [
        {"op": " ", "text": "a"},
        {"op": "-", "text": "b"},
        {"op": "+", "text": "B"},
        {"op": " ", "text": "c"},
        {"op": "+", "text": "d"},
    ]


# -- the model's tools ------------------------------------------------------------------------------------


async def test_answering_you_athena_saves_and_reads_skills(skills):
    save, read = tool(skills, "save_skill"), tool(skills, "read_skill")
    said = await save({"name": TRIP[0], "description": TRIP[1], "instructions": TRIP[2]})
    assert said == "Saved the skill “Trip plans”." and skills.store.named("trip plans").created_by == "athena"
    assert await save({"name": "Trip plans", "instructions": "Trains first."}) == "Updated the skill “Trip plans”."
    assert skills.store.named("Trip plans").description == TRIP[1]  # kept, when it's not given

    turn = Turn()
    end = begin_turn(turn)
    try:
        text = await read({"name": "TRIP PLANS"})
    finally:
        end()
    assert text == "# Trip plans\nWhen to use it: When George asks you to plan a trip\n\nTrains first."
    assert turn.notes == {"skills": ["Trip plans"]}  # so it's looked back on afterwards
    assert skills.store.named("Trip plans").uses == 1
    with pytest.raises(ToolError, match="no skill called 'Cooking'. Skills: Trip plans"):
        await read({"name": "Cooking"})
    with pytest.raises(ToolError, match="needs a name"):
        await save({"name": "Cooking", "instructions": "Stir."})  # a new skill says when to use it


async def test_working_by_itself_athena_only_suggests(skills):
    save, read = tool(skills, "save_skill"), tool(skills, "read_skill")
    skill = skills.save(None, *TRIP, by="you")
    source = "your task “Weekly skills”"

    said = await alone(source, save({"name": "Trip plans", "instructions": "Trains first.", "why": "Faster"}))
    assert said.startswith("Kept for the user to approve")
    assert skills.store.get(skill.id).body == TRIP[2]  # unchanged until you approve it
    (suggestion,) = skills.store.suggestions()
    assert (suggestion.skill_id, suggestion.body, suggestion.why, suggestion.source) == (
        skill.id,
        "Trains first.",
        "Faster",
        source,
    )
    # It builds on what it's suggested, which replaces the earlier suggestion.
    assert "with the change you suggested" in await alone(source, read({"name": "Trip plans"}))
    await alone(source, save({"name": "Trip plans", "instructions": "Trains first.\nHotels near stations."}))
    (suggestion,) = skills.store.suggestions()
    assert suggestion.body == "Trains first.\nHotels near stations."
    same = {"name": "Trip plans", "instructions": "Trains first.\nHotels near stations."}
    assert await alone(source, save(same)) == "No change: that's what it says already."
    # Answering you, it reads the skill as it is.
    assert (await read({"name": "Trip plans"})).endswith(TRIP[2])

    # A new skill is a suggestion too, and you can change it before saving it.
    await alone(source, save({"name": "Recipes", "description": "When I cook", "instructions": "Metric."}))
    new = next(s for s in skills.store.suggestions() if s.skill_id is None)
    assert skills.describe_suggestion(new)["was"] is None and skills.store.named("Recipes") is None
    saved = skills.approve(new.id, {"body": "Metric, and oven in °C."})
    assert (saved.body, saved.created_by) == ("Metric, and oven in °C.", "you")

    change = skills.store.suggestions()[0]
    described = skills.describe_suggestion(change)
    assert described["was"] == {"name": "Trip plans", "description": TRIP[1]}
    assert {"op": "+", "text": "Hotels near stations."} in described["diff"]
    approved = skills.approve(change.id)
    assert approved.body.endswith("Hotels near stations.") and approved.updated_by == "athena"
    assert skills.store.suggestions() == []
    with pytest.raises(ValueError, match="no suggestion like that"):
        skills.reject(change.id)


async def test_what_athena_did_lately(skills):
    history = skills.history
    history.append_exchange("111", "What's the weather?", "Rain.", tools=["get_weather"])
    history.append_exchange("111", "(Your task “Weather” ran.)", "Rain.")  # its tools aren't noted: left out
    history.append_exchange("task:3", "Find trains to Bath", "The 9:15.", tools=["search_web", "fetch", "fetch"])
    history.append_exchange("111", "Thanks", "You're welcome.", tools=[])
    skills.chat_name = lambda chat: "task “Trains”" if chat == "task:3" else "you"

    text = await tool(skills, "recent_work")({})
    lines = text.splitlines()
    assert lines[0] == "3 things you did in the last 7 days, oldest first:"
    assert lines[1].endswith("· you · get_weather · What's the weather?")
    assert lines[2].endswith("· task “Trains” · search_web → fetch → fetch · Find trains to Bath")
    assert lines[3].endswith("· you · no tools · Thanks")

    for i in range(60):
        history.append_exchange("111", f"Question {i} " + "x" * 100, "A.", tools=["look"])
    first = await tool(skills, "recent_work")({"days": 2})
    assert "[to continue, use start=" in first
    start = int(first.rsplit("start=", 1)[1].rstrip("]"))
    rest = await tool(skills, "recent_work")({"days": 2, "start": start})
    assert "Question 59" in rest and "Question 59" not in first


# -- in the agent ----------------------------------------------------------------------------------------


@pytest.fixture
def athena(config, memory, skills):
    def make(replies, channel=None):
        tools = ToolRegistry()
        for t in [*memory.tools(), *skills.tools()]:
            tools.add(t)
        server = FakeLLMServer(replies)
        agent = Agent(config.agent, server.client(config.llm), tools, skills.history, memory)
        agent.prompt_extra = skills.index
        channel = channel or FakeChannel()
        scheduler = Scheduler(
            TasksConfig(quiet_minutes=0),
            TaskStore(config.db_path),
            agent,
            timezone="Europe/London",
            channel=lambda: channel,
            tools=tools.all,
            poll_seconds=0.01,
        )
        agent.on_answered = scheduler.after_answer
        return agent, scheduler, server, channel

    return make


async def test_skills_are_listed_in_the_prompt_and_read_when_needed(athena, skills):
    skills.save(None, *TRIP, by="you")
    agent, scheduler, server, _ = athena(
        [completion(None, [("read_skill", {"name": "Trip plans"})]), completion("Here's your trip to Bath.")]
    )
    result = await agent.respond("111", "Plan a weekend in Bath")
    system = server.requests[0]["messages"][0]["content"]
    assert system.endswith("\n\n## Your skills\n- Trip plans: When George asks you to plan a trip")
    assert result.notes == {"skills": ["Trip plans"]}
    assert result.transcript[-1] == {"role": "assistant", "content": "Here's your trip to Bath."}
    assert result.transcript[-2]["content"].startswith("# Trip plans")  # what read_skill said
    assert skills.history.exchanges_since("2000-01-01")[0].tools == ["read_skill"]
    (review,) = scheduler.reviews  # to look back on how it went, once you've finished chatting
    assert (review.skills, review.what) == (["Trip plans"], "our chat")

    plain, *_ = athena([completion("Hi!")])
    assert (await plain.respond("111", "Hi")).transcript is None  # nothing kept without a skill


async def test_looking_back_suggests_a_better_skill(athena, skills):
    skill = skills.save(None, *TRIP, by="you")
    agent, scheduler, server, channel = athena(
        [
            completion(None, [("read_skill", {"name": "Trip plans"})]),
            completion("Here's your trip. You said you prefer hotels near the station."),
            # Looking back: it tries a tool it mayn't use now, then improves the skill.
            completion(None, [("remember", {"fact": "George likes hotels near stations."})]),
            completion(
                None,
                [
                    (
                        "save_skill",
                        {
                            "name": "Trip plans",
                            "instructions": TRIP[2] + "\n4. Hotels near the station.",
                            "why": "Asked",
                        },
                    )
                ],
            ),
            completion("Done."),
        ]
    )
    await agent.respond("111", "Plan a weekend in Bath")
    review = scheduler.reviews.pop(0)
    await scheduler.review(review)

    asked = server.requests[2]
    assert asked["messages"][: len(review.transcript)] == review.transcript  # what it did, read from the cache
    assert asked["messages"][-1]["content"].startswith(
        "(From the system, not George.) You used your skill “Trip plans”"
    )
    assert NO_CHANGE in asked["messages"][-1]["content"]
    assert asked["tools"] == server.requests[0]["tools"]  # the same tools, so the cache holds
    said_to_remember = server.requests[3]["messages"][-1]
    assert said_to_remember["content"] == "Error: only read_skill, save_skill can be used now."
    assert skills.store.get(skill.id).body == TRIP[2]  # a suggestion, for you to approve
    (suggestion,) = skills.store.suggestions()
    assert suggestion.source == "looking back at your skill “Trip plans”, after our chat"
    assert channel.said == [
        "✏️ After our chat, I've suggested a change to your skill “Trip plans”. "
        "Approve it, or not, on the dashboard's Skills tab."
    ]
    assert skills.history.load("111")[-1]["content"].startswith("Here's your trip")  # the review isn't in your chat
    assert scheduler.reviews == []  # and it isn't looked back on itself


async def test_a_review_that_changes_nothing_says_nothing(athena, skills):
    skills.save(None, *TRIP, by="you")
    agent, scheduler, _, channel = athena(
        [completion(None, [("read_skill", {"name": "Trip plans"})]), completion("Trip."), completion(NO_CHANGE)]
    )
    await agent.respond("111", "Plan a trip")
    await scheduler.review(scheduler.reviews.pop(0))
    assert channel.said == [] and skills.store.suggestions() == []


async def test_reviews_wait_until_youve_finished_chatting(athena, skills, config):
    skills.save(None, *TRIP, by="you")
    agent, scheduler, server, _ = athena(
        [completion(None, [("read_skill", {"name": "Trip plans"})]), completion("Trip."), completion(NO_CHANGE)]
    )
    scheduler.cfg.quiet_minutes = 1
    await agent.respond("111", "Plan a trip")  # you've just had a reply
    scheduler.poll = 0.01
    scheduler.start()
    try:
        await asyncio.sleep(0.2)
        assert len(server.requests) == 2 and len(scheduler.reviews) == 1  # still waiting for you
        agent.last_answered_you = time.monotonic() - 61
        scheduler.wake()
        for _ in range(500):
            if len(server.requests) == 3:
                break
            await asyncio.sleep(0.02)
        assert len(server.requests) == 3 and scheduler.reviews == []
    finally:
        await scheduler.stop()


async def test_a_task_suggests_memories_and_skills_and_says_so(athena, skills, memory):
    agent, scheduler, server, channel = athena(
        [
            completion(None, [("remember", {"fact": "George's train to work is the 7:52."})]),
            completion(
                None,
                [
                    (
                        "save_skill",
                        {"name": "Commute", "description": "Trains to work", "instructions": "Check the 7:52."},
                    )
                ],
            ),
            completion("The 7:52 is on time."),
        ]
    )
    task = scheduler.add("Commute", "Check my train.", "now", [], created_by="you")
    await scheduler.run(task)
    assert channel.said == [
        "📋 **Commute**\n\nThe 7:52 is on time.\n\n"
        "🧠 I've suggested a memory and a skill for you to review on the dashboard."
    ]
    (fact,) = memory.store.suggestions()
    assert (fact.text, fact.source) == ("George's train to work is the 7:52.", "your task “Commute”")
    assert memory.store.count().get("fact", 0) == 0  # not remembered until you approve it
    assert skills.store.named("Commute") is None and len(skills.store.suggestions()) == 1

    # With nothing to report, a skill it suggested is still worth a word.
    _, scheduler, _, channel = athena(
        [
            completion(None, [("save_skill", {"name": "Habits", "description": "Weekly", "instructions": "Steps."})]),
            completion(NOTHING),
        ]
    )
    task = scheduler.add("Skills from habits", "Look back.", "now", [], created_by="you")
    await scheduler.run(task)
    assert channel.said == [
        "📋 **Skills from habits**: nothing new to tell you.\n\n"
        "🧠 I've suggested a skill for you to review on the dashboard."
    ]


def test_a_database_from_before_replies_noted_their_tools(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO messages (chat_id, role, content, created_at) VALUES ('1', 'user', 'hi', '2026')")
    history = ConversationStore(db)
    history.append_exchange("1", "Weather?", "Rain.", tools=["get_weather"])
    assert [e.tools for e in history.exchanges_since("2000")] == [["get_weather"]]
    assert history.load("1")[0] == {"role": "user", "content": "hi"}
    history.close()


async def test_background_work_can_be_limited_to_some_tools(config, memory, skills):
    tools = ToolRegistry()
    for t in [*memory.tools(), *skills.tools()]:
        tools.add(t)
    server = FakeLLMServer([completion(None, [("search_memory", {"query": "x"})]), completion("ok")])
    agent = Agent(config.agent, server.client(config.llm), tools, skills.history, memory)

    async def step(status, call):
        return await call()

    background = Background(label="Testing", step=step, only_tools=frozenset({"read_skill"}), source="a test")
    result = await agent.follow_up([{"role": "system", "content": "Hi"}], "Search for x", background)
    assert result.text == "ok" and result.tools_used == ["search_memory"]
    assert server.requests[1]["messages"][-1]["content"] == "Error: only read_skill can be used now."
