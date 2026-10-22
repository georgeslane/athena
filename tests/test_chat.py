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


async def test_naming_and_deleting_sessions(chat):
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "My CV: Python and data."})
        await answered(http)
        renamed = await http.post("/api/chat/1/name", json={"name": "  Job   hunting "})
        assert renamed.json()["name"] == "Job hunting"
        assert (await http.post("/api/chat/1/name", json={"name": 3})).status_code == 400
        assert (await http.post("/api/chat/9/name", json={"name": "x"})).status_code == 404
        state = (await http.get("/api/chat")).json()
        assert state["sessions"][0]["title"] == "Job hunting" and state["sessions"][0]["last"] > 0
        task = chat.tasks.add("Find jobs", "Look for new postings.", "0 9 * * *", [], "you", session=1)
        assert (await http.get("/api/chat")).json()["sessions"][0]["tasks"] == 1
        assert [t["title"] for t in (await http.get("/api/chat/1")).json()["tasks"]] == ["Find jobs"]

        # Deleted while it's being answered: no.
        await http.post("/api/chat/1", json={"text": "Please use wait_here"})
        refused = await http.delete("/api/chat/1")
        assert refused.status_code == 409 and "answering you in that session" in refused.json()["error"]
        chat.let_go.set()
        await answered(http)
        await until(lambda: chat.memory.store.count().get("conversation") == 2)  # what was said, in memory

        state = (await http.delete("/api/chat/1")).json()
        assert [s["id"] for s in state["sessions"]] == [2] and state["current"] == 2  # a new one
        assert (await http.get("/api/chat/1")).status_code == 404
        assert chat.tasks.store.get(task.id) is None  # its tasks go with it
        assert chat.memory.store.count().get("conversation", 0) == 0  # and out of it
        assert (await http.delete("/api/chat/1")).status_code == 404


async def test_deleting_an_earlier_session_leaves_the_current_one(chat):
    async with client(chat) as http:
        await http.post("/api/chat/1", json={"text": "Hello"})
        await answered(http)
        await http.post("/api/chat", json={})
        await http.post("/api/chat/2", json={"text": "Please use wait_here"})
        state = (await http.delete("/api/chat/1")).json()  # while Athena's answering in another: that's fine
        assert state["current"] == 2 and [s["id"] for s in state["sessions"]] == [2]
        chat.let_go.set()
        await answered(http)


async def test_photos_and_files_come_with_a_message(chat):
    import base64

    from athena.photos import red_square

    photo = red_square()
    seen = []
    chat.llm = chat.agent.llm = FakeLLMServer(lambda body: seen.append(body) or completion("A red square.")).client(
        chat.config.llm
    )
    attach = [
        {"name": "square.png", "data": base64.b64encode(photo.data).decode()},
        {"name": "notes.md", "data": base64.b64encode(b"# Notes\nBuy milk.").decode()},
    ]
    async with client(chat) as http:
        sent = await http.post("/api/chat/1", json={"text": "What's this?", "attachments": attach})
        assert sent.status_code == 200, sent.text
        await answered(http)
        [question, _] = (await http.get("/api/chat/1")).json()["messages"]
        assert question["text"] == "[Photo] [File: inbox/notes.md] What's this?" and question["photos"] == [photo.name]
        parts = seen[0]["messages"][-1]["content"]
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")  # the model sees it
        assert (chat.config.resolve(chat.config.data_dir) / "workspace" / "inbox" / "notes.md").read_bytes() == (
            b"# Notes\nBuy milk."
        )
        shown = await http.get(f"/api/photos/{photo.name}")
        assert shown.content == photo.data and shown.headers["content-type"] == "image/png"
        assert (await http.get("/api/photos/" + "0" * 32 + ".png")).status_code == 404
        assert (await http.post("/api/chat/1", json={"text": "", "attachments": attach[:1]})).status_code == 200
        await answered(http)

        for wrong, why in (
            ([{"name": "a.exe", "data": base64.b64encode(b"MZ").decode()}], "PDFs and text files"),
            ([{"name": "a.heic", "data": base64.b64encode(b"....ftypheic").decode()}], "JPEG, PNG, WebP and GIF"),
            ([{"name": "a.md", "data": "not base64!"}], "didn't arrive whole"),
            ([{"name": "a.md"}], "Send photos and files"),
            (attach[:1] * 6, "up to 5 photos"),
        ):
            response = await http.post("/api/chat/1", json={"text": "Hi", "attachments": wrong})
            assert response.status_code == 400 and why in response.json()["error"], (why, response.text)
        command = await http.post("/api/chat/1", json={"text": "/help", "attachments": attach[:1]})
        assert command.status_code == 400 and "by itself" in command.json()["error"]


async def test_only_a_message_from_you_can_be_large(chat):
    larger = {"text": "Hi", "attachments": [{"name": "larger.md", "data": "QUFB" * 100_000}]}  # 400 KB
    async with client(chat, signed_in=False) as http:
        assert (await http.post("/api/chat/1", json=larger)).status_code == 413  # it's not taken in
    async with client(chat) as http:
        assert (await http.post("/api/chat/1/name", json={"name": "x" * 20_000})).status_code == 413
        assert (await http.post("/api/chat/1", json=larger)).status_code == 200
        await answered(http)
        big = {"text": "Hi", "attachments": [{"name": "big.md", "data": "QUFB" * 2_500_000}]}  # 10 MB
        assert (await http.post("/api/chat/1", json=big)).status_code == 200
        await answered(http)


async def test_tasks_are_added_in_a_session_or_a_new_one(chat):
    async with client(chat) as http:
        listed = (await http.get("/api/tasks")).json()
        assert listed["sessions"] == [{"id": 1, "title": ""}] and listed["current"] == 1
        task = {"title": "Primes", "instructions": "Look for patterns.", "schedule": "0 8 * * mon", "tools": []}
        added = (await http.post("/api/tasks", json={**task, "session": "Maths research"})).json()
        [made] = added["tasks"]
        assert made["session"] == 2 and chat.history.title(2) == "Maths research"
        assert chat.history.session.id == 1  # a new one for a task isn't the current session
        assert (await http.post("/api/tasks", json={**task, "session": 9})).status_code == 400
        assert (await http.post("/api/tasks", json={**task, "session": True})).status_code == 400
        assert (await http.post("/api/tasks", json={**task, "title": "", "session": "Empty"})).status_code == 400
        assert [s["title"] for s in (await http.get("/api/tasks")).json()["sessions"]] == ["", "Maths research"]
        moved = await http.post(f"/api/tasks/{made['id']}", json={**task, "session": 1})
        assert moved.json()["tasks"][0]["session"] == 1
