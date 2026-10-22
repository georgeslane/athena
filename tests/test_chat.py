"""Chatting on the dashboard (chat.py), through its API as the page uses it: sending in a session, which makes it
the current one, one message at a time, commands, approvals answered here, tools allowed without asking, and
trying again what didn't get an answer."""

import asyncio

import pytest
from conftest import FakeLLMServer, completion
from test_dashboard import athena, client, until  # noqa: F401 - athena is a fixture

from athena.tools import Tool

PARAMS = {"type": "object", "properties": {}}


class Model:
    """The model: asks for the tool a message names ("use send_note"), then says what came back."""

    def __call__(self, body):
        last = body["messages"][-1]
        if last["role"] == "tool":
            return completion(f"Done: {last['content']}")
        for tool in ("send_note", "wait_here"):
            if f"use {tool}" in last["content"]:
                return completion(None, [(tool, {"text": "hello"})])
        return completion("Hello there.")


@pytest.fixture
def chat(athena):  # noqa: F811
    """Athena with a tool that asks first (send_note), and one that waits until the test lets it finish."""
    let_go = asyncio.Event()

    async def send_note(args):
        return "Sent."

    async def wait_here(args):
        await let_go.wait()
        return "Waited."

    athena.tools.add(Tool("send_note", "Sends a note.", PARAMS, send_note, needs_confirmation=True))
    athena.tools.add(Tool("wait_here", "Waits.", PARAMS, wait_here))
    athena.llm = athena.agent.llm = FakeLLMServer(Model()).client(athena.config.llm)
    athena.let_go = let_go
    return athena


async def answered(http):
    """The chat's state, once Athena's not answering any more."""
    for _ in range(400):
        state = (await http.get("/api/chat")).json()
        if not state["busy"]:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError("still answering")


async def test_a_message_is_answered_in_its_session(chat):
    async with client(chat) as http:
        state = (await http.get("/api/chat")).json()
        assert state["current"] == 1 and [s["id"] for s in state["sessions"]] == [1]
        assert {c["name"] for c in state["commands"]} >= {"session", "retry", "stop", "help"}
        assert [t["name"] for t in state["askable"]] == ["forget_memory", "send_note"]  # can be allowed unasked

        sent = await http.post("/api/chat/1", json={"text": "  Hi Athena  "})
        assert sent.status_code == 200 and sent.json()["said"] is None
        state = await answered(http)
        session = (await http.get("/api/chat/1")).json()
        assert [(m["role"], m["text"], m["channel"]) for m in session["messages"]] == [
            ("user", "Hi Athena", "Dashboard"),
            ("assistant", "Hello there.", ""),
        ]
        assert state["sessions"][0]["title"] == "Hi Athena" and state["failed"] == {}

        for wrong, status in (({"text": " "}, 400), ({"text": "x" * 4001}, 400), ({"text": "hi", "allow": "x"}, 400)):
            assert (await http.post("/api/chat/1", json=wrong)).status_code == status, wrong
        assert (await http.post("/api/chat/9", json={"text": "hi"})).status_code == 404
        assert (await http.get("/api/chat/9")).status_code == 404


async def test_one_message_at_a_time_and_sending_in_an_old_session_makes_it_current(chat):
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "Hello"})
        await answered(http)
        new = (await http.post("/api/chat", json={})).json()
        assert new["session"] == 2 and new["current"] == 2
        assert (await http.post("/api/chat", json={})).json()["session"] == 2  # nothing said in it yet: the same one

        await http.post("/api/chat/2", json={"text": "Please use wait_here"})
        state = (await http.get("/api/chat")).json()
        assert state["busy"] and state["replying"]["text"] == "Please use wait_here"
        for busy in (
            http.post("/api/chat/1", json={"text": "Back to the first"}),
            http.post("/api/chat/2", json={"text": "And another"}),
            http.post("/api/chat", json={}),
            http.post("/api/session", json={}),
        ):
            response = await busy
            assert response.status_code == 409 and "still answering" in response.json()["error"]
        chat.let_go.set()
        await answered(http)

        assert chat.history.session.id == 2
        await http.post("/api/chat/1", json={"text": "Back to the first"})
        state = await answered(http)
        assert state["current"] == 1 and [s["id"] for s in state["sessions"]] == [1, 2]  # the one used last first
        first = (await http.get("/api/chat/1")).json()["messages"]
        assert [m["text"] for m in first] == ["Hello", "Hello there.", "Back to the first", "Hello there."]


async def test_stopping_and_trying_again(chat):
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "Please use wait_here"})
        await until(lambda: chat.status.snapshot().step == "Using wait_here")
        assert (await http.post("/api/chat/stop", json={})).json() == {"stopped": True}
        state = await answered(http)
        assert (
            state["failed"]["1"]["text"] == "Please use wait_here"
            and state["failed"]["1"]["error"] == "You stopped it."
        )
        assert (await http.get("/api/chat/1")).json()["messages"] == []  # nothing's kept of it

        chat.let_go.set()
        said = (await http.post("/api/chat/1", json={"text": "/retry"})).json()["said"]
        assert said == ""  # it's being answered again
        state = await answered(http)
        assert state["failed"] == {}
        assert [m["text"] for m in (await http.get("/api/chat/1")).json()["messages"]] == [
            "Please use wait_here",
            "Done: Waited.",
        ]
        # The last answer can be tried again too: it's taken out first.
        await http.post("/api/chat/1", json={"text": "/retry"})
        await answered(http)
        assert len((await http.get("/api/chat/1")).json()["messages"]) == 2


async def test_a_reply_that_fails_is_kept_to_try_again(chat):
    chat.llm = chat.agent.llm = FakeLLMServer([]).client(chat.config.llm)  # the model server errors
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "Hello?"})
        state = await answered(http)
        assert state["failed"]["1"]["error"].startswith("The model server returned an error (500)")
        chat.llm = chat.agent.llm = FakeLLMServer(Model()).client(chat.config.llm)
        await http.post("/api/chat/1", json={"text": "/retry"})
        assert (await answered(http))["failed"] == {}


async def test_commands_say_what_they_did(chat):
    async with client(chat) as http:

        async def say(text, session=1):
            response = await http.post(f"/api/chat/{session}", json={"text": text})
            assert response.status_code == 200, response.text
            return response.json()["said"]

        assert (await say("/help")).startswith("Hi! I'm Athena.")
        assert await say("/remember George's bike is blue.") == "Saved as memory #1."
        assert await say("/stop") == "I'm not working on anything."
        assert await say("/retry") == "There's nothing to try again in this session."
        assert await say("/frobnicate") == "There's no /frobnicate command. /help lists them."
        await http.post("/api/chat/1", json={"text": "Hello"})
        await answered(http)
        assert (await say("/session")).startswith("Started session 2.")
        assert chat.history.session.id == 2 and chat.history.messages(1)[0]  # the first is kept as it was


async def test_approving_here_and_allowing_tools_without_asking(chat):
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "Please use send_note"})
        await until(lambda: chat.approvals.pending())
        [request] = (await http.get("/api/chat")).json()["approvals"]
        assert (request["tool"], request["args"], request["session"]) == ("send_note", {"text": "hello"}, 1)
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": "yes"})).status_code == 400
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": True})).status_code == 200
        await answered(http)
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": True})).status_code == 409

        # For one message: it doesn't ask.
        await http.post("/api/chat/1", json={"text": "Please use send_note", "allow": ["send_note"]})
        await answered(http)
        assert chat.approvals.pending() == []
        [steps] = [e.steps for e in chat.timeline.page(kinds=["message"])[:1]]
        assert steps[0]["approval"] == "allowed"

        # For the session: nor do its later messages, until you take it back.
        allowed = await http.post("/api/chat/1/tools", json={"allowed": ["send_note"]})
        assert allowed.json()["allowed_tools"] == ["send_note"]
        assert (await http.post("/api/chat/1/tools", json={"allowed": ["wait_here"]})).status_code == 400
        await http.post("/api/chat/1", json={"text": "Please use send_note"})
        await answered(http)
        assert chat.approvals.pending() == []
        await http.post("/api/chat/1/tools", json={"allowed": []})
        await http.post("/api/chat/1", json={"text": "Please use send_note"})
        await until(lambda: chat.approvals.pending())
        await http.post(f"/api/approvals/{chat.approvals.pending()[0].id}", json={"allow": False})
        await answered(http)


async def test_the_page_hears_of_new_messages_as_it_does_the_status(chat):
    async with client(chat) as http:
        before = (await http.get("/api/status")).json()
        assert before["chat"] == {"current": 1, "busy": False, "approvals": 0, "last": None}
        waiting = asyncio.create_task(http.get(f"/api/status?after={before['version']}&wait=10"))
        await asyncio.sleep(0.05)
        chat.keep("(Your task “Briefing” ran.)", "All quiet.")  # nothing's changed about what Athena's doing
        after = (await asyncio.wait_for(waiting, 2)).json()
        assert after["version"] != before["version"]
        assert after["chat"]["last"]["role"] == "assistant"  # so the page can say Athena's written
