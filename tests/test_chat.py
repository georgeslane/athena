"""Chatting on the dashboard (chat.py), through its API as the page uses it: sending to an agent, one message at a
time, commands, approvals answered here, tools allowed without asking, and trying again what didn't get an
answer; agents' settings, files and DAGs."""

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
        state = (await http.get("/api/agents")).json()
        if not state["busy"]:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError("still answering")


async def test_a_message_is_answered_in_its_session(chat):
    async with client(chat) as http:
        state = (await http.get("/api/agents")).json()
        assert state["master"] == 1 and [(s["id"], s["master"]) for s in state["sessions"]] == [(1, True)]
        assert {c["name"] for c in state["commands"]} >= {"new", "retry", "stop", "help", "dags"}
        assert [t["name"] for t in state["askable"]] == ["forget_memory", "send_note"]  # can be allowed unasked

        sent = await http.post("/api/agents/1/messages", json={"text": "  Hi Athena  "})
        assert sent.status_code == 200 and sent.json()["said"] is None
        state = await answered(http)
        session = (await http.get("/api/agents/1")).json()
        assert [(m["role"], m["text"], m["channel"]) for m in session["messages"]] == [
            ("user", "Hi Athena", "Dashboard"),
            ("assistant", "Hello there.", ""),
        ]
        assert state["sessions"][0]["title"] == "Master" and state["failed"] == {}

        for wrong, status in (({"text": " "}, 400), ({"text": "x" * 4001}, 400), ({"text": "hi", "allow": "x"}, 400)):
            assert (await http.post("/api/agents/1/messages", json=wrong)).status_code == status, wrong
        assert (await http.post("/api/agents/9/messages", json={"text": "hi"})).status_code == 404
        assert (await http.get("/api/agents/9")).status_code == 404


async def test_one_message_at_a_time_to_any_agent(chat):
    async with client(chat) as http:
        await http.post("/api/agents/1/messages", json={"text": "Hello"})
        await answered(http)
        new = (await http.post("/api/agents", json={})).json()
        assert new["agent"] == 2 and new["master"] == 1
        assert (await http.post("/api/agents", json={"name": "Shopping"})).json()["agent"] == 3

        await http.post("/api/agents/2/messages", json={"text": "Please use wait_here"})
        state = (await http.get("/api/agents")).json()
        assert state["busy"] and state["replying"]["text"] == "Please use wait_here"
        for busy in (
            http.post("/api/agents/1/messages", json={"text": "Back to the first"}),
            http.post("/api/agents/2/messages", json={"text": "And another"}),
        ):
            response = await busy
            assert response.status_code == 409 and "still answering" in response.json()["error"]
        assert (await http.post("/api/agents", json={})).status_code == 200  # making an agent is fine meanwhile
        chat.let_go.set()
        await answered(http)

        await http.post("/api/agents/1/messages", json={"text": "Back to the first"})
        state = await answered(http)
        assert [s["id"] for s in state["sessions"]] == [1, 2, 3]  # the master first, then the latest to be used
        first = (await http.get("/api/agents/1")).json()["messages"]
        assert [m["text"] for m in first] == ["Hello", "Hello there.", "Back to the first", "Hello there."]


async def test_stopping_and_trying_again(chat):
    async with client(chat) as http:
        await http.post("/api/agents/1/messages", json={"text": "Please use wait_here"})
        await until(lambda: chat.status.snapshot().step == "Using wait_here")
        assert (await http.post("/api/agents/stop", json={})).json() == {"stopped": True}
        state = await answered(http)
        assert (
            state["failed"]["1"]["text"] == "Please use wait_here"
            and state["failed"]["1"]["error"] == "You stopped it."
        )
        assert (await http.get("/api/agents/1")).json()["messages"] == []  # nothing's kept of it

        chat.let_go.set()
        said = (await http.post("/api/agents/1/messages", json={"text": "/retry"})).json()["said"]
        assert said == ""  # it's being answered again
        state = await answered(http)
        assert state["failed"] == {}
        assert [m["text"] for m in (await http.get("/api/agents/1")).json()["messages"]] == [
            "Please use wait_here",
            "Done: Waited.",
        ]
        # The last answer can be tried again too: it's taken out first.
        await http.post("/api/agents/1/messages", json={"text": "/retry"})
        await answered(http)
        assert len((await http.get("/api/agents/1")).json()["messages"]) == 2


async def test_a_reply_that_fails_is_kept_to_try_again(chat):
    chat.llm = chat.agent.llm = FakeLLMServer([]).client(chat.config.llm)  # the model server errors
    async with client(chat) as http:
        await http.post("/api/agents/1/messages", json={"text": "Hello?"})
        state = await answered(http)
        assert state["failed"]["1"]["error"].startswith("The model server returned an error (500)")
        chat.llm = chat.agent.llm = FakeLLMServer(Model()).client(chat.config.llm)
        await http.post("/api/agents/1/messages", json={"text": "/retry"})
        assert (await answered(http))["failed"] == {}


async def test_commands_say_what_they_did(chat):
    async with client(chat) as http:

        async def say(text, session=1):
            response = await http.post(f"/api/agents/{session}/messages", json={"text": text})
            assert response.status_code == 200, response.text
            return response.json()["said"]

        assert (await say("/help")).startswith("Hi! I'm Athena.")
        assert await say("/remember George's bike is blue.") == "Saved as memory #1."
        assert await say("/stop") == "I'm not working on anything."
        assert await say("/retry") == "There's nothing to try again here."
        assert await say("/frobnicate") == "There's no /frobnicate command. /help lists them."
        await http.post("/api/agents/1/messages", json={"text": "Hello"})
        await answered(http)
        assert (await say("/new")).startswith("Started afresh: I won't see what we've said here so far.")
        assert chat.history.load("session:1") == [] and chat.history.messages(1)[0]  # it's still there to read


async def test_approving_here_and_allowing_tools_without_asking(chat):
    async with client(chat) as http:
        await http.post("/api/agents/1/messages", json={"text": "Please use send_note"})
        await until(lambda: chat.approvals.pending())
        [request] = (await http.get("/api/agents")).json()["approvals"]
        assert (request["tool"], request["args"], request["session"]) == ("send_note", {"text": "hello"}, 1)
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": "yes"})).status_code == 400
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": True})).status_code == 200
        await answered(http)
        assert (await http.post(f"/api/approvals/{request['id']}", json={"allow": True})).status_code == 409

        # For one message: it doesn't ask.
        await http.post("/api/agents/1/messages", json={"text": "Please use send_note", "allow": ["send_note"]})
        await answered(http)
        assert chat.approvals.pending() == []

        # For the session: nor do its later messages, until you take it back.
        allowed = await http.post("/api/agents/1/tools", json={"allowed": ["send_note"]})
        assert allowed.json()["allowed_tools"] == ["send_note"]
        assert (await http.post("/api/agents/1/tools", json={"allowed": ["wait_here"]})).status_code == 400
        await http.post("/api/agents/1/messages", json={"text": "Please use send_note"})
        await answered(http)
        assert chat.approvals.pending() == []
        await http.post("/api/agents/1/tools", json={"allowed": []})
        await http.post("/api/agents/1/messages", json={"text": "Please use send_note"})
        await until(lambda: chat.approvals.pending())
        await http.post(f"/api/approvals/{chat.approvals.pending()[0].id}", json={"allow": False})
        await answered(http)


async def test_answering_a_tasks_question_here(chat):
    async with client(chat) as http:
        asked = chat.approvals.ask("Apply to Acme?", ["Yes", "No"], timeout=60, why="“apply” asks", session=1)
        waiting = asyncio.create_task(chat.approvals.wait_reply(asked))
        [question] = (await http.get("/api/agents")).json()["approvals"]
        assert (question["question"], question["choices"], question["session"]) == ("Apply to Acme?", ["Yes", "No"], 1)
        url = f"/api/approvals/{question['id']}"
        assert (await http.post(url, json={"reply": "  "})).status_code == 400
        assert (
            await http.post(url, json={"allow": True})
        ).status_code == 409  # a question isn't allowed: it's answered
        assert (await http.post(url, json={"reply": "Yes, if remote"})).status_code == 200
        assert await waiting == "Yes, if remote"
        assert (await http.post(url, json={"reply": "No"})).status_code == 409

        skipped = chat.approvals.ask("Which CV?", [], timeout=60, session=1)
        waiting = asyncio.create_task(chat.approvals.wait_reply(skipped))
        await asyncio.sleep(0)
        assert (await http.post(f"/api/approvals/{skipped.id}", json={"allow": False})).status_code == 200
        assert await waiting is None and skipped.verdict == "Skipped"


async def test_the_page_hears_of_new_messages_as_it_does_the_status(chat):
    async with client(chat) as http:
        before = (await http.get("/api/status")).json()
        assert before["chat"] == {"master": 1, "busy": False, "approvals": 0, "last": None}
        waiting = asyncio.create_task(http.get(f"/api/status?after={before['version']}&wait=10"))
        await asyncio.sleep(0.05)
        chat.keep("(Claude's projects changed.)", "All quiet.")  # nothing's changed about what Athena's doing
        after = (await asyncio.wait_for(waiting, 2)).json()
        assert after["version"] != before["version"]
        assert after["chat"]["last"]["role"] == "assistant"  # so the page can say Athena's written


async def test_naming_and_deleting_agents(chat):
    async with client(chat) as http:
        agent = (await http.post("/api/agents", json={})).json()["agent"]
        await http.post(f"/api/agents/{agent}/messages", json={"text": "My CV: Python and data."})
        await answered(http)
        renamed = await http.post(f"/api/agents/{agent}", json={"name": "  Job   hunting "})
        assert renamed.json()["name"] == "Job hunting"
        assert (await http.post(f"/api/agents/{agent}", json={"name": 3})).status_code == 400
        assert (await http.post("/api/agents/9", json={"name": "x"})).status_code == 404
        state = (await http.get("/api/agents")).json()
        assert state["sessions"][1]["title"] == "Job hunting" and state["sessions"][1]["last"] > 0
        chat.dags.save(
            agent,
            "with Dag('postings', prompt='p', schedule=['0 9 * * *']):\n    AgenticTask('find', prompt='Look.')\n",
        )
        listed = (await http.get("/api/agents")).json()["sessions"][1]["dag"]
        assert listed["when"] == "every day at 09:00" and not listed["running"] and listed["next_run"]

        # Deleted while it's being answered: no. Nor the master agent, ever.
        await http.post(f"/api/agents/{agent}/messages", json={"text": "Please use wait_here"})
        refused = await http.delete(f"/api/agents/{agent}")
        assert refused.status_code == 409 and "answering you in that agent" in refused.json()["error"]
        chat.let_go.set()
        await answered(http)
        master = await http.delete("/api/agents/1")
        assert master.status_code == 409 and "can't be deleted" in master.json()["error"]
        await until(lambda: chat.memory.store.count().get("conversation") == 2)  # what was said, in memory

        state = (await http.delete(f"/api/agents/{agent}")).json()
        assert [s["id"] for s in state["sessions"]] == [1]
        assert (await http.get(f"/api/agents/{agent}")).status_code == 404
        assert chat.dags.store.get(agent).dag == ""  # its DAG goes with it
        assert chat.memory.store.count().get("conversation", 0) == 0  # and out of it
        assert (await http.delete(f"/api/agents/{agent}")).status_code == 404


async def test_deleting_one_agent_while_athena_answers_in_another(chat):
    async with client(chat) as http:
        await http.post("/api/agents", json={"name": "First"})
        await http.post("/api/agents/2/messages", json={"text": "Hello"})
        await answered(http)
        await http.post("/api/agents/1/messages", json={"text": "Please use wait_here"})
        state = (await http.delete("/api/agents/2")).json()  # while Athena's answering in another: that's fine
        assert [s["id"] for s in state["sessions"]] == [1]
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
        sent = await http.post("/api/agents/1/messages", json={"text": "What's this?", "attachments": attach})
        assert sent.status_code == 200, sent.text
        await answered(http)
        [question, _] = (await http.get("/api/agents/1")).json()["messages"]
        assert question["text"] == "[Photo] [File: inbox/notes.md] What's this?" and question["photos"] == [photo.name]
        parts = seen[0]["messages"][-1]["content"]
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")  # the model sees it
        assert (chat.config.resolve(chat.config.data_dir) / "workspace" / "inbox" / "notes.md").read_bytes() == (
            b"# Notes\nBuy milk."
        )
        shown = await http.get(f"/api/photos/{photo.name}")
        assert shown.content == photo.data and shown.headers["content-type"] == "image/png"
        assert (await http.get("/api/photos/" + "0" * 32 + ".png")).status_code == 404
        assert (
            await http.post("/api/agents/1/messages", json={"text": "", "attachments": attach[:1]})
        ).status_code == 200
        await answered(http)

        for wrong, why in (
            ([{"name": "a.exe", "data": base64.b64encode(b"MZ").decode()}], "PDFs and text files"),
            ([{"name": "a.heic", "data": base64.b64encode(b"....ftypheic").decode()}], "JPEG, PNG, WebP and GIF"),
            ([{"name": "a.md", "data": "not base64!"}], "didn't arrive whole"),
            ([{"name": "a.md"}], "Send photos and files"),
            (attach[:1] * 6, "up to 5 photos"),
        ):
            response = await http.post("/api/agents/1/messages", json={"text": "Hi", "attachments": wrong})
            assert response.status_code == 400 and why in response.json()["error"], (why, response.text)
        command = await http.post("/api/agents/1/messages", json={"text": "/help", "attachments": attach[:1]})
        assert command.status_code == 400 and "by itself" in command.json()["error"]


async def test_only_a_message_from_you_can_be_large(chat):
    larger = {"text": "Hi", "attachments": [{"name": "larger.md", "data": "QUFB" * 100_000}]}  # 400 KB
    async with client(chat, signed_in=False) as http:
        assert (await http.post("/api/agents/1/messages", json=larger)).status_code == 413  # it's not taken in
    async with client(chat) as http:
        assert (await http.post("/api/agents/1", json={"name": "x" * 20_000})).status_code == 413
        assert (await http.post("/api/agents/1/messages", json=larger)).status_code == 200
        await answered(http)
        big = {"text": "Hi", "attachments": [{"name": "big.md", "data": "QUFB" * 2_500_000}]}  # 10 MB
        assert (await http.post("/api/agents/1/messages", json=big)).status_code == 200
        await answered(http)


async def test_an_agents_settings(chat):
    async with client(chat) as http:
        made = (await http.post("/api/agents", json={"name": "Job hunting"})).json()
        agent = made["agent"]
        assert made["sessions"][1]["name"] == "Job hunting"
        changed = await http.post(
            f"/api/agents/{agent}", json={"model": "big-model", "guidance": " Only remote jobs. ", "context": "CV."}
        )
        page = changed.json()
        assert (page["model"], page["guidance"], page["context"]) == ("big-model", "Only remote jobs.", "CV.")
        assert chat.dags.intro_for(agent).startswith("<agent>\nThis is George's agent “Job hunting”.")
        for wrong in ({}, {"colour": "red"}, {"model": 3}, {"muted": "yes"}):
            assert (await http.post(f"/api/agents/{agent}", json=wrong)).status_code == 400, wrong
        assert (await http.post("/api/agents/9", json={"name": "x"})).status_code == 404
        models = (await http.get("/api/models")).json()
        assert models == {"models": ["test-model"], "default": "test-model"}


DAG = "with Dag('jobs', prompt='p', schedule=['0 9 * * 1-5']):\n    find = AgenticTask('find', prompt='Find jobs.')\n"


async def test_an_agents_dag_from_the_dashboard(chat):
    async with client(chat) as http:
        fresh = (await http.get("/api/agents/1/dag")).json()
        assert not fresh["written"] and fresh["source"].startswith("from agentic_airflow import")
        assert fresh["dag"]["when"] == "only when it's run by hand" and fresh["dag"]["end"][-1]["id"] == "end.summary"
        assert "send_note" in fresh["tools"] and fresh["master"] and "job_emails = AgenticTask(" in fresh["source"]
        assert fresh["events"] == {
            "assets": {},
            "webhooks": {},
            "sources": {},
            "claude": "claude-done",
            "hooks": False,
        }

        empty = "with Dag('d', prompt='p'):\n    pass\n"
        checked = (await http.post("/api/agents/1/dag/check", json={"source": empty})).json()
        assert checked["dag"] is None and checked["error"]["line"] == 1 and "no tasks" in checked["error"]["message"]
        unknown = "with Dag('d', prompt='p', hidden_tools=['nope_*']):\n    AgenticTask('a', prompt='x')\n"
        assert (await http.post("/api/agents/1/dag/check", json={"source": unknown})).json()["unknown"] == ["nope_*"]
        assert (await http.post("/api/agents/1/dag", json={"source": empty})).status_code == 400
        assert (await http.post("/api/agents/1/dag", json={"source": 3})).status_code == 400

        saved = (await http.post("/api/agents/1/dag", json={"source": DAG})).json()
        assert saved["written"] and saved["dag"]["when"] == "every weekday at 09:00" and saved["next_run"]
        assert (await http.post("/api/agents/1/dag/pause", json={})).json()["next_run"] is None
        assert (await http.post("/api/agents/1/dag/resume", json={})).json()["next_run"]
        assert (await http.post("/api/agents/1/dag/stop", json={})).status_code == 409  # it isn't running
        queued = (await http.post("/api/agents/1/dag/run", json={})).json()
        assert queued["queued"] == ["manual"] or queued["running"]  # it starts when it's its turn
        assert (await http.post("/api/agents/9/dag/run", json={})).status_code == 404
        await until(lambda: chat.dags.runs(1) and chat.dags.runs(1)[0].is_terminal)
        [run] = chat.dags.runs(1)
        runs = (await http.get("/api/agents/1/runs")).json()["runs"]
        assert [(r["number"], r["state"]) for r in runs] == [(1, "success")] and "tasks" not in runs[0]
        detail = (await http.get(f"/api/runs/{run.run_id}")).json()
        assert [t["task"] for t in detail["tasks"]][:2] == ["find", "end.review"]
        assert detail["dag"]["id"] == "jobs" and detail["agent"] == 1
        assert (await http.get("/api/runs/run_nope")).status_code == 404

        better = DAG.replace("Find jobs.", "Find roles.")
        proposal = chat.dags.proposals.propose(1, run.run_id, "workflow", "Better.", better)
        shown = (await http.get("/api/agents/1/dag")).json()["proposals"]
        assert (
            shown[0]["id"] == proposal["id"]
            and "+    find = AgenticTask('find', prompt='Find roles.')" in shown[0]["diff"]
        )
        assert next(s for s in (await http.get("/api/agents")).json()["sessions"] if s["id"] == 1)["proposals"] == 1
        assert (await http.get(f"/api/runs/{run.run_id}")).json()["proposals"][0]["id"] == proposal["id"]
        url = f"/api/proposals/{proposal['id']}"
        assert (await http.post(url, json={"approve": "yes"})).status_code == 400
        assert (await http.post("/api/proposals/12", json={"approve": True})).status_code == 405  # not an id
        approved = (await http.post(url, json={"approve": True})).json()
        assert "Find roles." in approved["source"] and approved["proposals"] == []
        assert (await http.post(url, json={"approve": True})).status_code == 409


async def test_files_that_come_with_an_agents_guidance(chat):
    import base64

    def sent(name, data):
        return {"name": name, "data": base64.b64encode(data).decode()}

    async with client(chat) as http:
        agent = (await http.post("/api/agents", json={"name": "Jobs"})).json()["agent"]
        assert (await http.get(f"/api/agents/{agent}/files")).json() == {"files": [], "on": True, "max": 10}
        given = [sent("cv.pdf", b"%PDF-1.4 CV"), sent("../notes.md", b"# Notes")]
        added = await http.post(f"/api/agents/{agent}/files", json={"files": given})
        assert added.status_code == 200, added.text
        assert added.json()["files"] == [
            {"name": "cv.pdf", "size": 11, "path": f"agents/{agent}/cv.pdf"},
            {"name": "notes.md", "size": 7, "path": f"agents/{agent}/notes.md"},  # only its name is kept
        ]
        assert f"agents/{agent}/cv.pdf (11 bytes), agents/{agent}/notes.md (7 bytes)." in chat.dags.intro_for(agent)
        again = (await http.post(f"/api/agents/{agent}/files", json={"files": [sent("cv.pdf", b"v2")]})).json()
        assert [f["name"] for f in again["files"]] == ["cv (2).pdf", "cv.pdf", "notes.md"]  # nothing's replaced
        got = await http.get(f"/api/agents/{agent}/files/cv.pdf")
        assert got.content == b"%PDF-1.4 CV" and got.headers["content-type"] == "application/pdf"
        assert "attachment" in got.headers["content-disposition"]
        for wrong in ({"files": []}, {"files": [{"name": "x.md"}]}, {"files": [sent("photo.exe", b"MZ")]}):
            assert (await http.post(f"/api/agents/{agent}/files", json=wrong)).status_code == 400, wrong
        left = (await http.delete(f"/api/agents/{agent}/files/cv%20%282%29.pdf")).json()["files"]
        assert [f["name"] for f in left] == ["cv.pdf", "notes.md"]
        assert (await http.delete(f"/api/agents/{agent}/files/nope.md")).status_code == 404
        assert (await http.get("/api/agents/9/files")).status_code == 404
        await http.delete(f"/api/agents/{agent}")
        assert chat.dags.intro_for(agent) == "" and not (chat.dags.workspace / "agents" / str(agent)).exists()


async def test_deleting_an_agent_while_its_dag_runs_stops_the_run_first(chat):
    agent = chat.history.new_session("Doomed").id
    dag = (
        "with Dag('doomed', prompt='p', allowed_tools=['wait_here']):\n"
        "    a = AgenticTask('a', prompt='Please use wait_here')\n    a >> AgenticTask('b', prompt='b')\n"
    )
    chat.dags.save(agent, dag)
    chat.history.append_exchange(f"session:{agent}", "Hello", "Hi")
    run = await chat.dags.run_now(agent)
    await until(lambda: chat.status.snapshot().step == "Using wait_here")
    async with client(chat) as http:
        state = (await http.delete(f"/api/agents/{agent}")).json()
    assert [s["id"] for s in state["sessions"]] == [1]
    assert chat.dags.engine.list_runs("doomed") == [] and chat.dags.run(run.run_id) is None  # nothing left behind
    assert chat.dags.dag(agent) is None and "doomed" not in chat.dags.engine.dags
