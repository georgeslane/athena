"""Asking you before a tool runs (approvals.py), wherever you answer, and allowing tools without asking, for a
message, a session or a task (Agent.respond's allowed)."""

import asyncio

import pytest
from conftest import FakeLLMServer, completion
from test_agent import make_agent

from athena.approvals import ALLOWED, DENIED, NO_ANSWER, STOPPED, Approvals
from athena.timeline import steps
from athena.tools import Tool


async def test_a_request_is_open_until_its_answered_wherever_that_is():
    changes = []
    approvals = Approvals()
    approvals.listeners.append(lambda a: changes.append((a.tool, a.verdict)))
    first = approvals.open("send_email", {"to": "sam"}, "Send it.", timeout=5, session=2)
    second = approvals.open("fetch", {}, None, timeout=5, why="For your task “News”")
    assert [a.tool for a in approvals.pending()] == ["send_email", "fetch"]
    assert first.as_dict()["session"] == 2 and second.as_dict()["why"] == "For your task “News”"

    waiting = asyncio.create_task(approvals.wait(first))
    await asyncio.sleep(0)
    assert approvals.answer(first.id, True)  # say, on the dashboard
    assert await waiting is True and first.verdict == ALLOWED
    assert not approvals.answer(first.id, False)  # and then Telegram's button: it's been answered
    assert approvals.answer(second.id, False) and await approvals.wait(second) is False
    assert second.verdict == DENIED and approvals.pending() == []
    assert changes == [("send_email", ""), ("fetch", ""), ("send_email", ALLOWED), ("fetch", DENIED)]


async def test_a_request_not_answered_in_time_is_denied_and_a_stopped_one_closes():
    approvals = Approvals()
    late = approvals.open("send_email", {}, None, timeout=0.05)
    assert await approvals.wait(late) is False and late.verdict == NO_ANSWER and approvals.pending() == []

    stopped = approvals.open("send_email", {}, None, timeout=5)
    waiting = asyncio.create_task(approvals.wait(stopped))
    await asyncio.sleep(0)
    waiting.cancel()  # /stop
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert stopped.verdict == STOPPED and approvals.pending() == []


def tools():
    """A tool that asks first, and one that always does (scheduling tasks), which say they ran."""

    async def ran(args):
        return "Done."

    params = {"type": "object", "properties": {}}
    return [
        Tool("send_email", "Sends.", params, ran, needs_confirmation=True),
        Tool("schedule_task", "Schedules.", params, ran, needs_confirmation=True),
    ]


async def test_tools_allowed_here_run_without_asking_but_some_always_ask(config, memory):
    server = FakeLLMServer(
        [completion(None, [("send_email", {}), ("schedule_task", {})]), completion("Sent, and asked about the task.")]
    )
    agent = make_agent(config, memory, server, extra_tools=tools())
    asked = []

    async def confirm(tool, args, summary=None):
        asked.append(tool)
        return False

    taken, on_tool = steps()
    allowed = ["send_email", "schedule_task"]
    await agent.respond("chat", "Email Sam, and remind me", confirm=confirm, allowed=allowed, on_tool=on_tool)
    assert asked == ["schedule_task"]  # never without asking, even when allowed
    assert [(s["tool"], s["outcome"], s["approval"]) for s in taken] == [
        ("send_email", "ran", "allowed"),
        ("schedule_task", "declined", "you"),
    ]


async def test_an_allowed_tool_runs_where_no_one_can_be_asked(config, memory):
    server = FakeLLMServer([completion(None, [("send_email", {})]), completion("Sent.")])
    agent = make_agent(config, memory, server, extra_tools=tools())
    taken, on_tool = steps()
    await agent.respond("chat", "Email Sam", allowed=["send_email"], on_tool=on_tool)
    assert taken[0]["outcome"] == "ran"
