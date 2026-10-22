"""The timeline (timeline.py), for the dashboard's Activity tab: what Athena did, newest first. Your messages, task
runs and checks, each with its steps and who allowed them, alerts, and how long it's all kept."""

import pytest
from conftest import FakeLLMServer, completion
from test_agent import make_agent
from test_alerts import Checks
from test_dashboard import athena as services  # noqa: F401 - a fixture
from test_dashboard import client
from test_tasks import FakeChannel, athena  # noqa: F401 - a fixture

from athena.alerts import HealthMonitor
from athena.config import AlertsConfig
from athena.privacy import Finding, Verdict
from athena.tasks import NOTHING
from athena.timeline import KEEP_DAYS, PAGE, Timeline
from athena.tools import Tool


def test_what_was_done_is_kept_newest_first_a_page_at_a_time(tmp_path):
    now = [1_000_000.0]
    timeline = Timeline(tmp_path / "athena.db", clock=lambda: now[0])
    for i in range(5):
        now[0] += 60
        timeline.add("message" if i % 2 else "task", f"thing {i}", "answered")
    newest = timeline.page(limit=2)
    assert [e.title for e in newest] == ["thing 4", "thing 3"]
    assert [e.title for e in timeline.page(before=newest[-1].id, limit=2)] == ["thing 2", "thing 1"]
    assert [e.title for e in timeline.page(kinds=["message"])] == ["thing 3", "thing 1"]

    # What's older than KEEP_DAYS goes when something new is added, and what's long is cut short.
    now[0] += KEEP_DAYS * 86400 - 150  # thing 0 and thing 1 are older than that now
    timeline.add("message", "a  long\n message " + "x" * 400, "answered", said="y" * 5000)
    latest, *rest = timeline.page()
    assert [e.title for e in rest] == ["thing 4", "thing 3", "thing 2"]
    assert latest.title.startswith("a long message x") and len(latest.title) == 300 and latest.title.endswith("…")
    assert len(latest.said) == 4000 and latest.at == now[0]
    timeline.clear()
    assert timeline.page() == []


class Privacy:
    """Finds an email address in what's sent, as privacy.py does."""

    def check(self, tool, args, *, asks):
        to = args.get("to", "")
        return Verdict((Finding("an email address", to),)) if "@" in to else Verdict()


async def test_your_message_is_noted_with_each_step_and_who_allowed_it(config, memory):
    async def send(args):
        return "Sent."

    tool = Tool("send", "Sends.", {"type": "object", "properties": {}}, handler=send, needs_confirmation=True)
    server = FakeLLMServer(
        [completion(None, [("send", {"to": "sam@example.com"}), ("send", {"to": "Sam"})]), completion("Sent one.")]
    )
    agent = make_agent(config, memory, server, extra_tools=[tool])
    agent.timeline, agent.privacy = Timeline(config.db_path), Privacy()
    answers = iter([True, False])  # you allow the first, and not the second

    async def confirm(tool, args, summary=None):
        return next(answers)

    await agent.respond("chat1", "Email Sam the notes", confirm=confirm, channel="Telegram")
    [entry] = agent.timeline.page()
    assert (entry.kind, entry.title, entry.outcome, entry.said) == (
        "message",
        "Email Sam the notes",
        "answered",
        "Sent one.",
    )
    assert entry.channel == "Telegram" and entry.seconds > 0
    allowed, declined = entry.steps
    assert allowed == {
        "tool": "send",
        "given": '{"to": "sam@example.com"}',
        "outcome": "ran",
        "said": "Sent.",
        "approval": "you",
        "flagged": "an email address (“sam@example.com”)",
    }
    assert (declined["outcome"], declined["approval"], "flagged" in declined) == ("declined", "you", False)


async def test_a_message_that_wasnt_answered_is_noted_with_why(config, memory):
    agent = make_agent(config, memory, FakeLLMServer([]))  # the model server errors
    agent.timeline = Timeline(config.db_path)
    with pytest.raises(Exception, match="Error code"):
        await agent.respond("chat1", "hello")
    [entry] = agent.timeline.page()
    assert (entry.title, entry.outcome, entry.said) == ("hello", "failed: The model server returned an error", "")


async def test_a_task_run_is_noted_with_what_it_may_do_unasked(athena):  # noqa: F811
    a = athena([completion(None, [("publish", {}), ("look", {})]), completion("Published the notes.")])
    a["agent"].timeline = a["timeline"]  # it notes your messages: a task's run, the scheduler does
    task = a["scheduler"].add("Post", "Publish the notes.", "now", ["publish"], created_by="you")
    await a["scheduler"].run(task)
    [entry] = a["timeline"].page()
    assert (entry.kind, entry.title, entry.outcome, entry.said) == ("task", "Post", "told you", "Published the notes.")
    assert [(s["tool"], s.get("approval")) for s in entry.steps] == [("publish", "allowed"), ("look", None)]

    b = athena([completion(None, [("publish", {})]), completion("Done.")])
    task = b["scheduler"].add("Post", "Publish the notes.", "now", [], created_by="you")
    await b["scheduler"].run(task)
    assert b["timeline"].page()[0].steps[0]["approval"] == "you"  # it asked you


async def test_each_try_of_a_failing_task_is_noted(athena):  # noqa: F811
    a = athena([])  # every call to the model fails
    task = a["scheduler"].add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    await a["scheduler"].run(task)
    await a["scheduler"].run(a["scheduler"].store.get(task.id))
    second, first = a["timeline"].page()
    for entry, tries in ((first, "(try 1 of 3)"), (second, "(try 2 of 3)")):
        assert entry.outcome.startswith("failed: The model server returned an error") and entry.outcome.endswith(tries)


async def test_each_check_on_whats_watched_is_noted(athena):  # noqa: F811
    a = athena([completion(None, [("look", {})]), completion(NOTHING), completion("The parcel's here.")])
    a["scheduler"].watches.add("the parcel", None, by="you")
    await a["scheduler"].heartbeat()
    await a["scheduler"].heartbeat()
    told, quiet = a["timeline"].page()
    assert (told.kind, told.title, told.outcome, told.said) == (
        "check",
        "Checked what you're watching",
        "told you",
        "The parcel's here.",
    )
    assert (quiet.outcome, quiet.said, [s["tool"] for s in quiet.steps]) == ("nothing new", "", ["look"])


async def test_alerts_are_noted_once_youve_been_told(tmp_path):
    c = Checks()
    timeline = Timeline(tmp_path / "athena.db")
    monitor = HealthMonitor(AlertsConfig(after_failures=1), c.check, c.tell, timeline)
    c.now = {"model": ("The model server", "it didn't answer")}
    c.can_tell = False
    await monitor.check_once()
    assert timeline.page() == []  # you weren't told: it tries again next time
    c.can_tell = True
    await monitor.check_once()
    c.now = {"model": ("The model server", None)}
    await monitor.check_once()
    assert [(e.kind, e.title, e.outcome, e.said) for e in timeline.page()] == [
        ("alert", "The model server is working again", "fixed", ""),
        ("alert", "The model server isn't working", "problem", "it didn't answer"),
    ]


async def test_the_dashboard_shows_it_a_page_at_a_time(services):  # noqa: F811
    async with client(services) as http:
        await services.agent.respond("111", "What's the time?", channel="Telegram")
        first = (await http.get("/api/activity")).json()
        assert [e["kind"] for e in first["entries"]] == ["message", "athena"]  # and when Athena started
        said = first["entries"][0]
        assert (said["title"], said["outcome"], said["said"], said["channel"]) == (
            "What's the time?",
            "answered",
            "All done.",
            "Telegram",
        )
        assert first["more"] is False and first["keep_days"] == KEEP_DAYS

        for i in range(PAGE):
            services.timeline.add("task", f"Run {i}", "nothing new")
        page = (await http.get("/api/activity?kinds=task")).json()
        assert len(page["entries"]) == PAGE and page["more"] is False
        page = (await http.get("/api/activity")).json()
        assert page["more"] is True
        rest = (await http.get(f"/api/activity?before={page['entries'][-1]['id']}")).json()
        assert [e["kind"] for e in rest["entries"]] == ["message", "athena"] and rest["more"] is False
        for wrong in ("?before=soon", "?kinds=task,secrets"):
            assert (await http.get(f"/api/activity{wrong}")).status_code == 400

        await http.post("/api/forget", json={"confirm": "forget"})
        assert (await http.get("/api/activity")).json()["entries"] == []
