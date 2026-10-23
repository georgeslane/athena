"""Agents' DAGs, run by agentic-airflow's engine: reading the files they're written in; running them with Athena's
model and tools; tools that ask first; alerts and summaries; webhooks, assets and Claude; the tasks every DAG ends
with and what they propose; your messages and their tasks taking turns with the model; and the master agent."""

import asyncio
import json

import pytest
from agentic_airflow import DagRun, RunStatus
from conftest import FakeLLMServer, completion

from athena.agent import Agent
from athena.config import TasksConfig
from athena.dags import ends, runs
from athena.dags import files as agent_files
from athena.dags.reader import (
    MASTER_STARTER,
    STARTER,
    DagError,
    as_dict,
    read,
    unknown_tools,
    when,
    with_tools,
)
from athena.dags.service import Dags
from athena.dags.store import AgentStore
from athena.history import ConversationStore
from athena.status import StatusTracker
from athena.tools import Tool, ToolRegistry
from athena.workspace import workspace_of

EMAILS = """from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, TaskGroup, Webhook
from athena.dags import alert

with Dag(
    "emails",
    prompt="Keep on top of my emails.",
    schedule=["0 8 * * *", Webhook("new-email")],
    allowed_tools=["look", "publish"],
):
    emails = AgenticTask("emails", prompt="Which of my emails need a reply? One item for each.", return_type="list")
    with TaskGroup("each_email") as each_email:
        urgent = AgenticBranch("urgent", prompt="Is this one urgent?", allow_none=True)
        draft = AgenticTask("draft", prompt="Draft a reply to it.", follow_mapping=True, on_complete=alert)
        urgent >> draft
    report = AgenticTask("report", prompt="Say what was drafted.", outlets=[Asset("drafted")])
    emails >> each_email >> report
"""


# -- the language --------------------------------------------------------------------------------------


def test_a_dag_file_is_read_into_the_engines_objects():
    dag = read(EMAILS, timezone="Europe/London")
    assert dag.id == "emails" and dag.prompt == "Keep on top of my emails."
    assert [t.id for t in dag.topological_order()] == ["emails", "urgent", "draft", "report"]
    assert dag.get("urgent").upstream_ids == ["emails"] and dag.get("report").upstream_ids == ["draft"]
    assert dag.get("emails").fan_out and dag.get("draft").follow_mapping
    assert dag.get("report").outlets == ["drafted"]
    assert dag.schedule[0].timezone == "Europe/London"  # cron's in your time zone
    assert when(dag) == "every day at 08:00, or when new-email comes in"
    shown = as_dict(dag)
    assert [(t["id"], t["kind"], t["group"], t["alert"]) for t in shown["tasks"]] == [
        ("emails", "agentic_task", None, False),
        ("urgent", "agentic_branch", "each_email", False),
        ("draft", "agentic_task", "each_email", True),
        ("report", "agentic_task", None, False),
    ]
    assert shown["tools"]["allowed_tools"] == ["look", "publish"]
    assert read(MASTER_STARTER).get("job_emails").outlets == ["job-emails"]
    assert when(read(STARTER.format(dag_id="x", purpose="p"))) == "only when it's run by hand"


@pytest.mark.parametrize(
    ("source", "line", "says"),
    [
        ("import os\nos.system('rm -rf /')\n", 2, "Outside the DAG, only imports go here"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x')\n    print('hi')\n", 3, "A DAG can call"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt=open('x').read())\n", 2, "Write values out"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt=f'{x}')\n", 2, "Write values out"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x', priority=3)\n", 2, "not priority"),
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x', retries=9)\n",
            2,
            "retries is a whole number from 0 to 3",
        ),
        (
            "with Dag('d', prompt='p', retry_delay=-1):\n    a = AgenticTask('a', prompt='x')\n",
            1,
            "retry_delay is a number",
        ),
        (
            "with Dag('d', prompt='p', catchup=1):\n    a = AgenticTask('a', prompt='x')\n",
            1,
            "catchup is True or False",
        ),
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x', ask_user='yes')\n",
            2,
            "ask_user is True or False",
        ),
        ("with Dag('d', prompt='p'):\n    a = Task('a', fn=print)\n", 2, "There's no task called “print”"),
        ("with Dag('d', prompt='p'):\n    a = Task('a', fn=found_nothing)\n", 2, "runs trigger_items"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x', on_complete=trigger_items)\n", 2, "alert"),
        ("with Dag('d', prompt='p'):\n    a = Branch('a', fn=alert)\n", 2, "use AgenticBranch"),
        ("with Dag('d', prompt='p', schedule=['* * * * *']):\n    a = AgenticTask('a', prompt='x')\n", 1, "15"),
        (
            "with Dag('d', prompt='p', schedule=[Webhook('Arrive Home')]):\n    a = AgenticTask('a', prompt='x')\n",
            1,
            "lower case with hyphens",
        ),  # fmt: skip
        ("with Dag('d', prompt='p'):\n    pass\n", 1, "has no tasks"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='')\n", 2, "prompt is required"),
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x')\n    b = AgenticTask('b', prompt='y')\n"
            "    a >> b >> a\n",
            2,
            "cycle",
        ),  # fmt: skip
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x')\n    a >> b\n", 3, "no task called “b”"),
        ("with Dag('d', prompt='p'):\n    a = AgenticTask('end.review', prompt='x')\n", 2, "every DAG ends with"),
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x')\n    a = AgenticTask('b', prompt='y')\n",
            3,
            "already used",
        ),  # fmt: skip
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x')\n    b = AgenticTask('a', prompt='y')\n",
            3,
            "Duplicate task id",
        ),  # fmt: skip
        ("with Dag('d', prompt='p'):\n    a = AgenticBranch('a', prompt='x')\n", 2, "no downstream tasks"),
        (
            "with Dag('d', prompt='p', schedule=[Asset('x')]):\n    a = AgenticTask('a', prompt='x', outlets=['x'])\n",
            2,
            "A DAG can't start itself",
        ),
        (
            "with Dag('d', prompt='p'):\n    a = AgenticTask('a', prompt='x', outlets=[Asset('Not One')])\n",
            2,
            "lower case with hyphens",
        ),  # fmt: skip
        ("with Dag('d'):\n    a = AgenticTask('a', prompt='x')\n", 1, "Dag needs id, prompt"),
        ("with Dag(:\n", 1, "isn't valid Python"),
        ("x = 1\n", 1, "Outside the DAG"),
        ("", 1, "There's no `with Dag"),
    ],
)
def test_a_dag_that_wont_do_says_what_and_where(source, line, says):
    with pytest.raises(DagError) as caught:
        read(source)
    assert says in caught.value.message and caught.value.line == line, caught.value.as_dict()


def test_a_dags_tool_lists_are_changed_where_theyre_written():
    changed = with_tools(EMAILS, {"hidden_tools": ["news_*"], "allowed_tools": {"dag": ["look"], "report": []}})
    dag = read(changed)
    assert dag.scope.hidden_tools == ["news_*"] and dag.scope.allowed_tools == ["look"]
    assert dag.get("report").scope.allowed_tools == []
    assert '    allowed_tools=["look"],\n    hidden_tools=["news_*"],\n):' in changed  # one to a line, as it's laid out
    assert 'outlets=[Asset("drafted")], allowed_tools=[])' in changed
    assert unknown_tools(read(changed), ["look", "publish"]) == ["news_*"]
    with pytest.raises(DagError, match="no task called “nope”"):
        with_tools(EMAILS, {"allowed_tools": {"nope": ["look"]}})


# -- running them ----------------------------------------------------------------------------------------


class FakeChannel:
    """Telegram and your devices, as far as DAGs are concerned."""

    def __init__(self, allow=True):
        self.said: list[tuple[str, int | None]] = []
        self.asked: list[tuple[str, str, str | None, int | None]] = []
        self.allow = allow
        self.files: list[str] = []
        self.questions: list[tuple[str, list[str], str, int | None]] = []
        self.reply: str | None = None  # your answer to a task's question

    async def tell(self, text, *, session=None):
        self.said.append((text, session))

    async def ask(self, tool, args, summary, *, why, timeout, session=None):
        self.asked.append((tool, why, summary, session))
        return self.allow

    async def send_file(self, noted):
        self.files.append(noted)

    async def question(self, text, choices, *, why, timeout, session=None):
        self.questions.append((text, choices, why, session))
        return self.reply


END = {
    "run's own tasks have finished": "NO CHANGES",
    "Look at the tools this run used": "NO CHANGES",
    "Decide what this DAG's next run should know": "UNCHANGED",
    "Write George a short summary": "All done: nothing much.",
}


class Model:
    """The model, answering by what it's asked: ``answers`` maps a phrase in the last message from the user to a
    reply, or to a list of replies for the turns it takes (a tool call, then an answer), or to a function of the
    request."""

    def __init__(self, answers=None):
        self.answers = {**END, **(answers or {})}
        self.turns: dict[str, int] = {}
        self.asked: list[str] = []  # the last message from the user, in each request

    def __call__(self, body):
        ask = next(m for m in reversed(body["messages"]) if m["role"] == "user")
        text = ask["content"] if isinstance(ask["content"], str) else ask["content"][0]["text"]
        self.asked.append(text)
        for phrase, answer in self.answers.items():
            if phrase in text:
                if callable(answer):
                    return answer(body)
                if isinstance(answer, list):
                    turn = self.turns.get(phrase, 0)
                    self.turns[phrase] = turn + 1
                    answer = answer[min(turn, len(answer) - 1)]
                return completion(answer) if isinstance(answer, str) else answer
        return completion("Done.")


def calls(*named):
    """The model asking for tools: calls(("look", {"what": "x"}))."""
    return completion(None, list(named))


@pytest.fixture
def athena(config, memory):
    """An agent loop with tools (look; publish, which asks first; wait_here, which waits for the test), agents and
    their DAGs, and a model that answers as each test says. Agent 1 is the master agent; ``jobs`` is another."""
    made = []

    def make(answers=None, channel=None, **settings):
        let_go, looked = asyncio.Event(), []

        async def look(args):
            looked.append(args)
            return f"Seen {args.get('what', '')}."

        async def publish(args):
            return "Published."

        async def wait_here(args):
            await let_go.wait()
            return "Waited."

        params = {"type": "object", "properties": {"what": {"type": "string"}}}
        tools = ToolRegistry()
        tools.add(Tool("look", "Looks.", params, look))
        tools.add(Tool("publish", "Publishes.", params, publish, needs_confirmation=True))
        tools.add(Tool("wait_here", "Waits.", params, wait_here))
        model = Model(answers)
        server = FakeLLMServer(model)
        history = ConversationStore(config.db_path)
        agent = Agent(config.agent, server.client(config.llm), tools, history, memory, status=StatusTracker())
        store = AgentStore(config.db_path)
        channel = channel or FakeChannel()
        dags = Dags(
            TasksConfig(**settings),
            store,
            agent,
            timezone="Europe/London",
            channel=lambda: channel,
            state=config.db_path.with_name("dags.db"),
            workspace=workspace_of(config),
        )
        for tool in dags.tools():
            tools.add(tool)
        agent.directory = dags
        jobs = history.new_session("Job hunting").id
        made.append(dags)
        return dict(
            dags=dags, store=store, history=history, agent=agent, server=server, model=model, channel=channel,
            let_go=let_go, looked=looked, tools=tools, db=config.db_path, jobs=jobs, workspace=workspace_of(config),
        )  # fmt: skip

    yield make
    for dags in made:
        dags.engine.close()


async def run_once(a, agent_id, timeout=10):
    """Run an agent's DAG by hand, and wait for it to finish, with what follows from it."""
    run = await a["dags"].run_now(agent_id)
    run = await a["dags"].engine.wait(run.run_id, timeout=timeout, stop_on_pause=False)
    await asyncio.sleep(0.05)  # what the service does once it's finished
    return run


def went(run):
    """How each of a run's tasks went: {instance: state}."""
    return {i.instance_id: i.status.value for i in run.instances.values()}


THREE = """from agentic_airflow import AgenticTask, Dag
from athena.dags import alert

with Dag("jobs", prompt="Find me jobs.", allowed_tools=["look"]):
    find = AgenticTask("find_jobs", prompt="Find new jobs that fit my CV.")
    apply = AgenticTask("apply", prompt="Apply to them.", on_complete=alert)
    report = AgenticTask("report", prompt="Say what was applied for.")
    find >> apply >> report
"""


@pytest.mark.asyncio
async def test_a_dag_runs_its_tasks_in_order_with_athenas_model_and_tools(athena):
    a = athena(
        {
            "Find new jobs": [calls(("look", {"what": "the job boards"})), "Two jobs: Acme and Globex."],
            "Apply to them.": "Applied to Acme and Globex.",
            "Say what was applied for": "Acme and Globex.",
            "Write George a short summary": "Applied to two jobs: Acme and Globex.",
        }
    )
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert run.status == RunStatus.SUCCESS and run.trigger["number"] == 1
    assert went(run)["find_jobs"] == "success" and went(run)["end.summary"] == "success"
    assert a["looked"] == [{"what": "the job boards"}]  # the tool ran, as the DAG allows it
    assert run.outputs()["apply"] == "Applied to Acme and Globex."
    apply = next(t for t in a["model"].asked if t.endswith("Apply to them."))
    assert "Two jobs: Acme and Globex." in apply  # what the task before it found
    request = next(b for b in a["server"].requests if b["messages"][-1]["content"].endswith("Apply to them."))
    system = request["messages"][0]["content"]
    assert "This is George's agent “Job hunting”." in system and "This is run #1 of your agent's DAG jobs" in system
    # What apply found was told, as it alerts; then the summary, here and in the master agent's chat.
    said = [text for text, _ in a["channel"].said]
    assert said[0].startswith("📋 **Job hunting** · task “apply”") and "Applied to Acme" in said[0]
    assert said[-1] == "📋 **Job hunting**\n\nApplied to two jobs: Acme and Globex."
    assert run.summary == "Applied to two jobs: Acme and Globex."
    master = [m.text for m in a["history"].messages(a["dags"].master)[0]]
    assert "(From your agent “Job hunting”, its DAG run #1: summary)" in master


@pytest.mark.asyncio
async def test_lists_run_whats_after_them_for_each_item_and_branches_pick(athena):
    a = athena(
        {
            "Which of my emails need a reply": '["From Ann: lunch?", "From Bob: the report is late"]',
            "Is this one urgent?": lambda body: completion(
                "draft" if "Bob" in body["messages"][-1]["content"] else "none"
            ),
            "Draft a reply to it.": "Sorry, Bob: it's coming today.",
            "Say what was drafted": "One reply, to Bob.",
        }
    )
    a["dags"].save(a["jobs"], EMAILS)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    states = went(run)
    assert states["urgent_0"] == states["urgent_1"] == "success"
    assert states["draft_0"] == "skipped" and states["draft_1"] == "success"  # only Bob's was urgent
    report = next(t for t in a["model"].asked if t.endswith("Say what was drafted."))
    assert "Sorry, Bob" in report and "From Ann" not in report.split("<<<BEGIN")[0]
    ann = next(t for t in a["model"].asked if t.endswith("Is this one urgent?") and "Ann" in t)
    assert "Bob" not in ann  # each item on its own
    assert a["channel"].said[0][0].endswith("Sorry, Bob: it's coming today.")  # draft alerts, for the one item


@pytest.mark.asyncio
async def test_a_tool_that_asks_first_asks_you_and_runs_once_you_allow_it(athena):
    source = THREE.replace('allowed_tools=["look"]', "").replace("Apply to them.", "Apply, then publish it.")
    a = athena(
        {
            "Find new jobs": "Acme.",
            "Apply, then publish it.": [calls(("publish", {"what": "Acme"})), "Applied and published."],
        }
    )
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert a["channel"].asked == [("publish", "For “apply”, in Job hunting", None, a["jobs"])]
    assert run.status == RunStatus.SUCCESS and run.outputs()["apply"] == "Applied and published."
    assert run.instances["apply"].tool_calls[0]["status"] == "approved"


@pytest.mark.asyncio
async def test_a_tool_you_decline_isnt_run(athena):
    source = THREE.replace('allowed_tools=["look"]', "").replace("Apply to them.", "Apply, then publish it.")
    a = athena(
        {"Apply, then publish it.": [calls(("publish", {"what": "Acme"})), "I couldn't publish it."]},
        channel=FakeChannel(allow=False),
    )
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    call = run.instances["apply"].tool_calls[0]
    assert call["status"] == "rejected" and "declined" in call["result"]
    assert run.outputs()["apply"] == "I couldn't publish it."


@pytest.mark.asyncio
async def test_a_run_that_finds_nothing_skips_the_end_and_tells_no_one(athena):
    source = """from agentic_airflow import AgenticTask, Asset, Dag

with Dag("master", prompt="Keep an eye on things."):
    found = AgenticTask("job_emails", prompt="Any new emails about jobs?", return_type="list", fan_out=False,
                        outlets=[Asset("job-emails")])
"""
    a = athena({"Any new emails about jobs?": "[]"})
    a["dags"].save(a["dags"].master, source)
    await a["dags"].start()
    try:
        run = await run_once(a, a["dags"].master)
    finally:
        await a["dags"].stop()
    assert all(state == "skipped" for i, state in went(run).items() if i.startswith("end."))
    assert a["channel"].said == [] and run.summary is None
    assert len(a["model"].asked) == 1  # one task: checking costs little


@pytest.mark.asyncio
async def test_what_the_master_agent_finds_starts_the_dags_that_wait_for_it(athena):
    master = """from agentic_airflow import AgenticTask, Asset, Dag

with Dag("master", prompt="Keep an eye on things."):
    AgenticTask("job_emails", prompt="Any new emails about jobs?", return_type="list", fan_out=False,
                outlets=[Asset("job-emails")])
"""
    jobs = """from agentic_airflow import AgenticTask, Asset, Dag, Task, trigger_items

with Dag("jobs", prompt="Apply for jobs.", schedule=[Asset("job-emails")]):
    each = Task("each", fn=trigger_items, fan_out=True)
    apply = AgenticTask("apply", prompt="Apply for this job.")
    each >> apply
"""
    a = athena({"Any new emails about jobs?": '["Acme: data engineer", "Globex: analyst"]'})
    a["dags"].save(a["dags"].master, master)
    a["dags"].save(a["jobs"], jobs)
    assert a["dags"].assets() == {"job-emails": ["Job hunting"]}
    assert a["dags"].sources() == {"job-emails": [{"agent": a["dags"].master, "name": "Master", "task": "job_emails"}]}
    await a["dags"].start()
    try:
        await run_once(a, a["dags"].master)
        for _ in range(100):
            started = a["dags"].runs(a["jobs"])
            if started and started[0].is_terminal:
                break
            await asyncio.sleep(0.05)
    finally:
        await a["dags"].stop()
    run = started[0]
    assert run.trigger["source"] == "asset" and run.trigger["chain"] == ["master"]
    assert sorted(i for i in went(run) if i.startswith("apply")) == ["apply_0", "apply_1"]
    acme = next(t for t in a["model"].asked if t.endswith("Apply for this job.") and "Acme" in t)
    assert "Globex" not in acme


@pytest.mark.asyncio
async def test_a_webhook_starts_the_dags_that_wait_for_it_with_what_came(athena):
    source = """from agentic_airflow import AgenticTask, Dag, Task, Webhook, trigger_items

with Dag("home", prompt="Welcome me home.", schedule=[Webhook("arrive-home")]):
    Task("items", fn=trigger_items, fan_out=True) >> AgenticTask("each", prompt="Put it on the list.")
"""
    a = athena()
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        assert a["dags"].webhooks() == {"arrive-home": ["Job hunting"]}
        assert await a["dags"].fire("arrive-home", "Home at 18:02", ["milk", "bread"]) == ["Job hunting"]
        assert await a["dags"].fire("arrive-home", "Again") == []  # not again within a minute
        assert await a["dags"].fire("nobody-listens") == []
        run = a["dags"].runs(a["jobs"])[0]
        run = await a["dags"].engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await a["dags"].stop()
    assert run.trigger["payload"] == {"text": "Home at 18:02", "items": ["milk", "bread"]}
    assert run.outputs()["items"] == ["milk", "bread"] and {"each_0", "each_1"} <= set(run.outputs())


@pytest.mark.asyncio
async def test_claude_finishing_starts_the_dags_that_wait_for_it(athena):
    source = """from agentic_airflow import AgenticTask, Asset, Dag

with Dag("review", prompt="Review what Claude did.", schedule=[Asset("claude-done")]):
    AgenticTask("look", prompt="Review the pull request.")
"""
    a = athena()
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        await a["dags"].claude_done("Claude has finished “Fix the bug”.\nhttps://github.com/x/y/pull/1")
        run = a["dags"].runs(a["jobs"])[0]
        run = await a["dags"].engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await a["dags"].stop()
    asked = next(t for t in a["model"].asked if t.endswith("Review the pull request."))
    assert "pull/1" in asked and run.trigger["asset"] == "claude-done"


@pytest.mark.asyncio
async def test_the_reviews_propose_changes_you_approve_or_reject(athena):
    better = THREE.replace("Say what was applied for.", "List what was applied for, with links.")
    a = athena(
        {
            "Find new jobs": [calls(("look", {"what": "boards"})), "Acme."],
            "run's own tasks have finished": f"The report should link each job.\n```python\n{better}```",
            "Look at the tools this run used": (
                'Hide publish, and let apply publish.\n```json\n{"hidden_tools": ["publish"], '
                '"allowed_tools": {"apply": ["look", "publish"]}, "why": "unused"}\n```'
            ),
            "Write George a short summary": "Applied to Acme. I've proposed two changes.",
        }
    )
    a["dags"].save(a["jobs"], THREE)
    proposals = a["dags"].proposals
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        shown = a["dags"].describe(a["jobs"])["proposals"]
        workflow, tools = proposals.waiting(a["jobs"])
        assert workflow["payload"]["what"] == "workflow" and workflow["payload"]["definition"] == better
        assert workflow["kind"] == "definition" and workflow["run_id"] == run.run_id
        changes = {"hidden_tools": ["publish"], "allowed_tools": {"apply": ["look", "publish"]}}
        assert tools["payload"]["what"] == "tools" and tools["payload"]["changes"] == changes
        summary = next(t for t in a["model"].asked if "Write George a short summary" in t)
        assert "A change to the DAG: The report should link each job." in summary
        assert "Tools: Find when needed: publish; Use without asking in apply: look, publish" in summary
        assert "⚠️ It would let these run without asking you: publish (in apply)." in summary
        assert '+    report = AgenticTask("report", prompt="List what was applied for' in shown[0]["diff"]
        assert shown[0]["unasked"] == [] and shown[1]["unasked"] == ["publish (in apply)"]
        assert shown[1]["summary"].startswith("Find when needed: publish") and shown[1]["by"] == "a run"
        # Approving the tools change changes the file where it's written, and the workflow change still applies.
        assert await proposals.decide(tools["id"], True) == a["jobs"]
        assert read(a["store"].get(a["jobs"]).dag).scope.hidden_tools == ["publish"]
        waiting = proposals.waiting(a["jobs"])[0]
        assert waiting["id"] == workflow["id"] and 'hidden_tools=["publish"]' in waiting["payload"]["definition"]
        await proposals.decide(workflow["id"], True)
        dag = read(a["store"].get(a["jobs"]).dag)
        assert dag.scope.hidden_tools == ["publish"] and "with links" in dag.get("report").prompt
        with pytest.raises(ValueError, match="isn't waiting"):
            await proposals.decide(workflow["id"], False)
        assert [p["state"] for p in map(proposals.show, proposals.of_run(run.run_id))] == ["approved", "approved"]
    finally:
        await a["dags"].stop()


@pytest.mark.asyncio
async def test_a_proposed_dag_that_wont_read_is_sent_back_once(athena):
    a = athena(
        {
            "run's own tasks have finished": "Change it.\n```python\nwith Dag('jobs'):\n    pass\n```",
            "That DAG file won't do": "NO CHANGES",
        }
    )
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        assert a["dags"].proposals.waiting(a["jobs"]) == [] and run.status == RunStatus.SUCCESS
    finally:
        await a["dags"].stop()
    assert any("That DAG file won't do: Line 1: Dag needs id, prompt" in t for t in a["model"].asked)


@pytest.mark.asyncio
async def test_saving_context_keeps_what_the_next_run_should_know(athena):
    a = athena({"Decide what this DAG's next run should know": "Applied: Acme (22 Oct)."})
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        await run_once(a, a["jobs"])
        assert a["store"].get(a["jobs"]).context == "Applied: Acme (22 Oct)."
        await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    second = [b for b in a["server"].requests if "This is run #2" in b["messages"][0]["content"]]
    assert second and "Applied: Acme (22 Oct)." in second[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_your_message_waits_for_the_task_then_goes_before_the_next(athena):
    source = THREE.replace('allowed_tools=["look"]', 'allowed_tools=["wait_here"]')
    a = athena({"Find new jobs": [calls(("wait_here", {})), "Found Acme."]})
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        run = await a["dags"].run_now(a["jobs"])
        while not a["agent"].gate.busy(a["agent"].llm.cfg.model):
            await asyncio.sleep(0.01)
        assert "This agent's DAG is running now (run #1)" in a["dags"].note(f"session:{a['jobs']}")
        answer = asyncio.create_task(a["agent"].respond(f"session:{a['jobs']}", "How's it going?"))
        await asyncio.sleep(0.2)
        assert not answer.done()  # the task holds the model until it's finished
        a["let_go"].set()
        await answer
        run = await a["dags"].engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await a["dags"].stop()
    order = [t.split("\n")[-1] for t in a["model"].asked]
    found = order.index("Find new jobs that fit my CV.")
    assert order[found + 2].endswith("How's it going?")  # after find_jobs' two steps, before apply
    assert order[found + 3] == "Apply to them."


@pytest.mark.asyncio
async def test_pausing_stops_it_starting_by_itself_and_stopping_stops_it(athena):
    source = THREE.replace('allowed_tools=["look"]', 'allowed_tools=["wait_here"]').replace(
        'prompt="Find me jobs."', 'prompt="Find me jobs.", schedule=["0 9 * * *"]'
    )
    a = athena({"Find new jobs": [calls(("wait_here", {})), "Found."]})
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        assert a["dags"].describe(a["jobs"])["next_run"] is not None
        a["dags"].pause(a["jobs"])
        described = a["dags"].describe(a["jobs"])
        assert described["paused"] and described["next_run"] is None
        run = await a["dags"].run_now(a["jobs"])  # by hand, it still runs
        waiting = await a["dags"].run_now(a["jobs"])  # after it
        with pytest.raises(ValueError, match="already waiting"):
            await a["dags"].run_now(a["jobs"])
        while a["dags"].describe(a["jobs"])["running"] is None:
            await asyncio.sleep(0.01)
        assert a["dags"].describe(a["jobs"])["queued"] == ["manual"]
        assert await a["dags"].stop_running(a["jobs"])  # both of them
        assert a["dags"].run(run.run_id).status == a["dags"].run(waiting.run_id).status == RunStatus.CANCELLED
        a["dags"].resume(a["jobs"])
        assert not a["dags"].describe(a["jobs"])["paused"]
    finally:
        a["let_go"].set()
        await a["dags"].stop()


@pytest.mark.asyncio
async def test_two_agents_dags_cant_have_the_same_id(athena):
    a = athena()
    a["dags"].save(a["jobs"], THREE)
    with pytest.raises(DagError, match="“Job hunting”'s DAG is called 'jobs' already"):
        a["dags"].save(a["dags"].master, THREE)


@pytest.mark.asyncio
async def test_renaming_a_dag_keeps_it_paused_and_starts_its_runs_afresh(athena):
    a = athena()
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        await run_once(a, a["jobs"])
        a["dags"].pause(a["jobs"])
        a["dags"].save(a["jobs"], THREE.replace('Dag("jobs"', 'Dag("job_hunt"'))
        assert a["dags"].paused(a["jobs"]) and a["dags"].dag(a["jobs"]).id == "job_hunt"
        assert a["dags"].runs(a["jobs"]) == [] and a["dags"].engine.list_runs("jobs") == []
    finally:
        await a["dags"].stop()


@pytest.mark.asyncio
async def test_a_file_that_wont_read_doesnt_run_and_says_why(athena):
    a = athena()
    a["store"].update(a["jobs"], dag="from athena import DAG\nwith DAG():\n    x = Task(prompt='old')\n")
    await a["dags"].start()
    try:
        described = a["dags"].describe(a["jobs"])
        assert described["dag"] is None and "written as Athena's DAGs were before" in described["error"]["message"]
        with pytest.raises(ValueError, match="has a problem"):
            await a["dags"].run_now(a["jobs"])
    finally:
        await a["dags"].stop()


@pytest.mark.asyncio
async def test_the_master_agent_hears_of_the_others_and_can_act_on_them(athena):
    a = athena()
    a["store"].update(a["jobs"], guidance="Find me data jobs in London.")
    a["dags"].save(a["jobs"], THREE)
    intro = a["dags"].intro_for(a["dags"].master)
    assert "- “Job hunting” (#2). Its DAG, jobs, runs only when it's run by hand. Find me data jobs" in intro
    tools = {t.name: t for t in a["dags"].tools()}
    assert "Job hunting" in await tools["agent_runs"].handler({})
    with pytest.raises(Exception, match="There's no one agent called"):
        await tools["run_dag"].handler({"agent": "nobody"})


@pytest.mark.asyncio
async def test_change_dag_shows_what_changes_and_checks_the_file(athena):
    a = athena()
    a["dags"].save(a["jobs"], THREE)
    change = next(t for t in a["dags"].tools() if t.name == "change_dag")
    assert change.needs_confirmation
    new = THREE.replace("Apply to them.", "Apply to the best one.")
    said = await change.preview({"source": new, "agent": "Job hunting", "why": "Fewer, better applications."})
    assert said.startswith("A new DAG for Job hunting, which runs only when it's run by hand. Fewer, better")
    assert '+    apply = AgenticTask("apply", prompt="Apply to the best one.", on_complete=alert)' in said
    assert "without asking" not in said
    wider = THREE.replace('allowed_tools=["look"]', 'allowed_tools=["look", "pub*"]')  # publish asks first
    said = await change.preview({"source": wider, "agent": "Job hunting"})
    assert "⚠️ It would let these run without asking you: publish (in find_jobs, apply, report)." in said
    with pytest.raises(Exception, match="That DAG file won't do"):
        await change.handler({"source": "with Dag('x'):\n    pass\n", "agent": "Job hunting"})
    assert (
        await change.handler({"source": new, "agent": "Job hunting"})
        == "Saved. It runs only when it's run by hand, with 3 tasks."
    )


CHECKIN = """from agentic_airflow import AgenticTask, Dag

with Dag("daily_checkin", prompt="Check in with me.", schedule=["0 7,13,19 * * *"]):
    check = AgenticTask("check", prompt="Is there anything I need to act on?")
"""
SAID_SAVED = f"Here it is:\n\n```python\n{CHECKIN}```\n\nSaved as your daily_checkin agent."


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow", "said"), [
    (True, "_Saved. It runs every day at 07:00, 13:00 and 19:00, with 1 task._"),
    (False, "_You declined it, so it wasn't done._"),
])  # fmt: skip
async def test_a_dag_the_answer_writes_out_but_didnt_save_is_saved_asking_first(athena, allow, said):
    a = athena({"Change your DAG": SAID_SAVED})
    master = a["dags"].master
    asked = []

    async def confirm(tool, args, summary=None):
        asked.append((tool, summary))
        return allow

    result = await a["agent"].respond(f"session:{master}", "Change your DAG to check in with me", confirm=confirm)
    assert [tool for tool, _ in asked] == ["change_dag"]
    assert asked[0][1].startswith("A new DAG for Master, which runs every day at 07:00, 13:00 and 19:00.")
    assert result.text.startswith("Here it is:") and result.text.endswith(said)
    assert result.tools_used == ["change_dag"]
    assert (a["store"].get(master).dag == CHECKIN) is allow
    assert a["history"].messages(master)[0][-1].text.endswith(said)  # as the conversation keeps it


@pytest.mark.asyncio
async def test_a_dag_in_an_answer_is_only_saved_if_the_model_didnt_and_its_new(athena):
    a = athena(
        {
            "Save it": [calls(("change_dag", {"source": CHECKIN})), SAID_SAVED],
            "Show me": SAID_SAVED,
            "Two ideas": f"```python\n{CHECKIN}```\nor\n```python\n{CHECKIN.replace('check in', 'see')}```",
            "Jobs": f"```python\n{THREE}```",
        }
    )
    asked = []

    async def confirm(tool, args, summary=None):
        asked.append(summary)
        return True

    master = f"session:{a['dags'].master}"
    await a["agent"].respond(master, "Save it", confirm=confirm)
    assert len(asked) == 1  # the model's own call: not asked again
    for text in ("Show me", "Two ideas"):  # the file as it's saved; or more than one, to choose from
        await a["agent"].respond(master, text, confirm=confirm)
    assert len(asked) == 1
    a["dags"].save(a["jobs"], THREE.replace("Apply to them.", "Apply."))
    await a["agent"].respond(master, "Jobs", confirm=confirm)  # the DAG with that id is Job hunting's
    assert asked[-1].startswith("A new DAG for Job hunting") and a["store"].get(a["jobs"]).dag == THREE


def test_an_agent_with_no_dag_is_told_change_dag_gives_it_one(athena):
    a = athena()
    for agent_id in (a["dags"].master, a["jobs"]):
        assert "It has no DAG yet, so it does nothing by itself: change_dag gives it one" in a["dags"].intro_for(
            agent_id
        )
    assert "change_dag and run_dag act on this agent, or on one of them by name." in a["dags"].intro_for(
        a["dags"].master
    )
    a["dags"].save(a["dags"].master, CHECKIN)
    assert "It has no DAG yet" not in a["dags"].intro_for(a["dags"].master)


def test_agents_files_are_listed_in_what_their_conversations_start_with(athena):
    a = athena()
    agent_files.keep(a["workspace"], a["jobs"], "cv.pdf", b"%PDF-1.4 x")
    assert "agents/2/cv.pdf (10 bytes)" in a["dags"].intro_for(a["jobs"])


def test_a_new_agents_dag_starts_with_its_name(athena):
    a = athena()
    starter = a["dags"].starter(a["jobs"])
    assert 'Dag("job_hunting", prompt="What Job hunting is for."' in starter
    assert read(starter).id == "job_hunting" and a["dags"].starter(a["dags"].master) == MASTER_STARTER


def test_the_tool_reviews_answer_is_checked():
    known, always, tasks = ["look", "publish", "change_dag"], ["change_dag"], ["find"]
    assert ends.proposed_tools("NO CHANGES", known, always, tasks) is None
    changes, why = ends.proposed_tools(
        '```json\n{"allowed_tools": {"find": ["look", "change_dag"]}, "hidden_tools": ["pub*"], "why": "w"}\n```',
        known,
        always,
        tasks,
    )
    assert changes == {"hidden_tools": ["pub*"], "allowed_tools": {"find": ["look"]}} and why == "w"
    for bad, says in [
        ('```json\n{"hidden_tools": ["nope"]}\n```', "No tool is called nope"),
        ('```json\n{"allowed_tools": {"other": ["look"]}}\n```', "There's no task called 'other'"),
        ("```json\nnot json\n```", "isn't valid JSON"),
    ]:
        with pytest.raises(ValueError, match=says):
            ends.proposed_tools(bad, known, always, tasks)
    assert json.dumps(ends.describe_tools(changes)) == json.dumps(
        "Find when needed: pub*; Use without asking in find: look"
    )


@pytest.mark.asyncio
async def test_failures_are_skipped_after_and_said_in_the_summary(athena):
    a = athena(
        {
            "Which of my emails need a reply": '["Ann", "Bob"]',
            "Is this one urgent?": lambda body: completion(
                "draft" if "Ann" in body["messages"][-1]["content"] else "?"
            ),
            "Draft a reply to it.": "Drafted.",
        }
    )
    a["dags"].save(a["jobs"], EMAILS)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    states = went(run)
    assert states["urgent_1"] == "failed" and states["draft_1"] == "upstream_failed"  # Bob's answer was no choice
    assert states["draft_0"] == "success" and states["report"] == "upstream_failed"
    assert run.status == RunStatus.FAILED
    summary = next(t for t in a["model"].asked if "Write George a short summary" in t)
    assert "These tasks didn't finish: urgent (for 1 of its 2 items), draft (for 1 of its 2 items), report." in summary


@pytest.mark.asyncio
async def test_when_the_model_breaks_the_run_says_its_summary_couldnt_be_written(athena):
    a = athena()
    a["server"].error = "model broken"  # every request fails
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        run = a["dags"].run(run.run_id)
    finally:
        await a["dags"].stop()
    states = went(run)
    assert states["find_jobs"] == "failed" and states["apply"] == "upstream_failed"
    assert states["end.review_workflow"] == "failed" and states["end.summary"] == "failed"  # the end tasks try
    assert run.summary == "The run finished, but its summary couldn't be written." and run.status == RunStatus.FAILED
    assert a["channel"].said[-1][0].endswith("its summary couldn't be written.")


@pytest.mark.asyncio
async def test_a_quiet_summary_doesnt_disturb_you_and_nor_does_a_muted_agent(athena):
    for muted, said in ((False, "[quiet] Nothing new: no jobs today."), (True, "Found a job!")):
        a = athena({"Write George a short summary": said})
        a["history"].set_muted(a["jobs"], muted)
        a["dags"].save(a["jobs"], THREE.replace(", on_complete=alert", ""))
        await a["dags"].start()
        try:
            await run_once(a, a["jobs"])
        finally:
            await a["dags"].stop()
        assert a["channel"].said == [] and a["history"].messages(a["history"].master.id)[0] == []  # not told
        assert a["history"].messages(a["jobs"])[0][-1].text == said.removeprefix("[quiet] ")  # in its chat, though


async def test_a_proposal_from_before_the_dag_changed_is_out_of_date(athena):
    a = athena()
    dags, jobs = a["dags"], a["jobs"]
    proposals, state = dags.proposals, dags.engine.state
    dags.save(jobs, EMAILS)
    proposal = proposals.propose(jobs, None, "workflow", "Better.", EMAILS.replace("my emails", "my mail"))
    assert proposals.propose(jobs, None, "workflow", "Nothing.", EMAILS) is None  # the file already
    dags.save(jobs, EMAILS.replace("0 8 * * *", "0 9 * * *"))
    with pytest.raises(ValueError, match="out of date"):
        await proposals.decide(proposal["id"], True)
    assert state.get_recommendation(proposal["id"])["status"] == "stale"
    now = a["store"].get(jobs).dag
    other = proposals.propose(
        jobs, None, "tools", "", with_tools(now, {"hidden_tools": ["look"]}), {"hidden_tools": ["look"]}
    )
    assert other["rationale"] == "The tool review recommends it."
    both = {"hidden_tools": ["look", "publish"]}
    newer = proposals.propose(jobs, None, "tools", "", with_tools(now, both), both)
    assert state.get_recommendation(other["id"])["status"] == "withdrawn"  # the newer one's waiting instead
    await proposals.decide(newer["id"], False)
    assert state.get_recommendation(newer["id"])["status"] == "rejected"
    dags.engine.state.close()


async def test_one_queue_whatever_proposes_a_change(athena):
    """Something besides a run's reviews, such as an MCP client, proposes through agentic-airflow's queue: only
    whole DAG files, read as they're proposed, which wait for you as the runs' do."""
    from agentic_airflow import proposals as queue
    from agentic_airflow.errors import ConfigurationError

    a = athena()
    dags, jobs = a["dags"], a["jobs"]
    dags.save(jobs, THREE)
    with pytest.raises(ConfigurationError, match="doesn't take 'topology'"):
        queue.propose_prompt_change(dags.engine, "jobs", "apply", "Apply carefully.", "r")
    with pytest.raises(ConfigurationError, match="won't do: Line 1"):
        queue.propose_definition(dags.engine, "jobs", "with Dag('jobs'):\n    pass\n", "r", source="mcp")
    better = THREE.replace("Apply to them.", "Apply to them carefully.")
    made = queue.propose_definition(dags.engine, "jobs", better, "Be careful.", source="mcp", proposed_by="claude")
    shown = dags.describe(jobs)["proposals"]
    assert [(p["id"], p["agent"], p["kind"], p["by"]) for p in shown] == [(made["id"], jobs, "workflow", "claude")]
    assert await dags.proposals.decide(made["id"], True) == jobs and "carefully" in a["store"].get(jobs).dag
    dags.engine.state.close()


async def test_whats_proposed_is_forgotten_with_its_runs_and_agent(athena):
    a = athena()
    dags, jobs = a["dags"], a["jobs"]
    dags.save(jobs, THREE)
    for n in range(2):
        dags.engine.state.save_run({"run_id": f"run_{n}", "dag_id": "jobs", "status": "success"})
    hide = {"hidden_tools": ["look"]}
    kept = dags.proposals.propose(jobs, "run_0", "tools", "", with_tools(THREE, hide), hide)
    gone = dags.proposals.propose(jobs, "run_1", "workflow", "w", THREE.replace("Apply to", "Apply for"))
    await dags.proposals.decide(gone["id"], False)
    dags.engine.state.delete_run("run_1")
    dags.proposals.prune()  # decided, and its run's not kept: forgotten
    assert [r["id"] for r in dags.engine.list_recommendations()] == [kept["id"]]
    await dags.delete(jobs)
    assert dags.engine.list_recommendations() == []
    dags.engine.state.close()


async def test_proposals_from_before_the_queue_are_carried_over(athena):
    a = athena()
    dags, store, jobs = a["dags"], a["store"], a["jobs"]
    dags.save(jobs, THREE)
    better = THREE.replace("Apply to them.", "Apply to them carefully.")
    store._conn.executescript(
        """
        CREATE TABLE dag_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, agent INTEGER NOT NULL, run TEXT,
            kind TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            changes TEXT NOT NULL DEFAULT '{}', base TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
            created REAL NOT NULL, decided REAL);
        """
    )
    rows = [
        ("workflow", "Careful.", better, "{}", THREE, "pending"),
        ("workflow", "Old.", better, "{}", "with Dag('old', prompt='p'): ...", "pending"),  # against another file
        ("tools", "Hide it.", "", '{"hidden_tools": ["look"]}', "", "pending"),
        ("tools", "Rejected.", "", '{"hidden_tools": ["look"]}', "", "rejected"),
    ]
    store._conn.executemany(
        "INSERT INTO dag_proposals (agent, kind, why, source, changes, base, state, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        [(jobs, *row) for row in rows],
    )
    await dags.start()
    try:
        waiting = dags.proposals.waiting(jobs)
        assert [(r["payload"]["what"], r["rationale"]) for r in waiting] == [
            ("workflow", "Careful."),
            ("tools", "Hide it."),
        ]
        assert store.take_old_proposals() == []  # and they're not there any more
    finally:
        await dags.stop()


@pytest.mark.asyncio
async def test_a_skill_read_in_a_run_is_looked_back_on_at_its_end(athena):
    from athena.skills import Skills, SkillStore

    a = athena(
        {
            "Find new jobs": [calls(("read_skill", {"name": "Applying"})), "Found."],
            "You used your skill “Applying”": "NO CHANGE",
        }
    )
    skills = Skills(SkillStore(a["db"]), a["history"], timezone="UTC")
    skills.save(None, "Applying", "When applying for jobs", "Use the CV.", by="you")
    for tool in skills.tools():
        a["tools"].add(tool)
    a["dags"].save(a["jobs"], THREE.replace('allowed_tools=["look"]', ""))
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert went(run)["end.review_skills"] == "success" and run.outputs()["end.review_skills"] == "NO CHANGE"


@pytest.mark.asyncio
async def test_a_task_that_runs_out_of_time_fails_and_the_run_goes_on(athena):
    a = athena({"Find new jobs": [calls(("wait_here", {})), "Done."]}, max_minutes=0.1 / 60, retries=0)  # 0.1 s
    a["dags"].save(a["jobs"], THREE.replace('allowed_tools=["look"]', 'allowed_tools=["wait_here"]'))
    await a["dags"].start()
    try:
        asyncio.get_running_loop().call_later(0.3, a["let_go"].set)  # the tool takes longer than the task may
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert went(run)["find_jobs"] == "failed" and "task_timeout" in run.instances["find_jobs"].error
    assert went(run)["end.summary"] == "success"


@pytest.mark.asyncio
async def test_the_model_server_unreachable_is_tried_again_then_the_run_stops(athena, monkeypatch):
    import openai

    from athena.dags import backend

    monkeypatch.setattr(backend, "RETRIES", (0.0, 0.0))
    a = athena()
    real, tries = a["agent"].llm.chat, []

    async def flaky(*args, **kwargs):
        tries.append(1)
        if len(tries) < 3:
            raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]
        return await real(*args, **kwargs)

    a["agent"].llm.chat = flaky
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        assert went(run)["find_jobs"] == "success" and len(tries) >= 3

        async def down(*args, **kwargs):
            tries.append(1)
            raise openai.APIConnectionError(request=None)  # type: ignore[arg-type]

        a["agent"].llm.chat = down
        tries.clear()
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert len(tries) == 3  # three tries for the first task, and none for the rest
    assert run.instances["find_jobs"].attempt == 1  # and the task isn't tried again: it can't help
    assert went(run)["find_jobs"] == "failed" and run.status == RunStatus.FAILED
    assert "couldn't be reached" in a["history"].messages(a["jobs"])[0][-1].text
    assert not any("couldn't be reached" in text for text, _ in a["channel"].said)  # the alerts say it's down


@pytest.mark.asyncio
async def test_while_a_task_waits_for_your_ok_your_messages_go_ahead(athena):
    allow = asyncio.Event()

    class Slow(FakeChannel):
        async def ask(self, tool, args, summary, *, why, timeout, session=None):
            self.asked.append((tool, why, summary, session))
            await allow.wait()
            return True

    channel = Slow()
    a = athena({"Quick question": "Yes?", "Find new jobs": [calls(("publish", {})), "Published."]}, channel)
    a["dags"].save(a["jobs"], THREE.replace('allowed_tools=["look"]', ""))
    await a["dags"].start()
    try:
        run = await a["dags"].run_now(a["jobs"])
        for _ in range(300):
            if channel.asked:
                break
            await asyncio.sleep(0.01)
        assert channel.asked == [("publish", "For “find_jobs”, in Job hunting", None, a["jobs"])]
        answer = await asyncio.wait_for(a["agent"].respond(f"session:{a['jobs']}", "Quick question"), 2)
        assert answer.text == "Yes?"  # not held up
        allow.set()
        run = await a["dags"].engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await a["dags"].stop()
    assert run.outputs()["find_jobs"] == "Published."


@pytest.mark.asyncio
async def test_athena_restarting_asks_again_about_a_tool_waiting_for_your_ok(athena):
    class Never(FakeChannel):
        async def ask(self, tool, args, summary, *, why, timeout, session=None):
            self.asked.append((tool, why, summary, session))
            await asyncio.Event().wait()

    a = athena({"Find new jobs": [calls(("publish", {})), "Published."]}, Never())
    a["dags"].save(a["jobs"], THREE.replace('allowed_tools=["look"]', ""))
    await a["dags"].start()
    run = await a["dags"].run_now(a["jobs"])
    while not a["channel"].asked:
        await asyncio.sleep(0.01)
    await a["dags"].stop()  # Athena stops, with the task waiting

    b = athena({"Find new jobs": "Published."}, FakeChannel())
    await b["dags"].start()
    try:
        run = await b["dags"].engine.wait(run.run_id, timeout=10, stop_on_pause=False)
    finally:
        await b["dags"].stop()
    assert [asked[0] for asked in b["channel"].asked] == ["publish"]  # asked again, and allowed
    assert run.status == RunStatus.SUCCESS and run.instances["find_jobs"].tool_calls[0]["status"] == "approved"


@pytest.mark.asyncio
async def test_the_model_isnt_warmed_up_while_something_has_it(athena):
    a = athena()
    async with a["agent"].gate.background(a["agent"].llm.cfg.model, "“a” in Jobs"):
        assert await a["agent"].warm_up("session:1") is None and a["server"].requests == []
    await a["agent"].warm_up("session:1")
    assert len(a["server"].requests) == 1


@pytest.mark.asyncio
async def test_agent_runs_reads_what_agents_runs_did(athena):
    a = athena(
        {
            "Find new jobs": "Acme.",
            "Apply to them.": "Applied to Acme.",
            "Say what was applied for": "Acme.",
            "Write George a short summary": "Applied to Acme.",
        }
    )
    dags, jobs = a["dags"], a["jobs"]
    dags.save(jobs, THREE)
    await dags.start()
    try:
        await run_once(a, jobs)
        tool = {t.name: t for t in a["tools"].all()}["agent_runs"]
        everyone = await tool.handler({})
        runs = await tool.handler({"agent": "job"})
        one = await tool.handler({"agent": f"#{jobs}", "run": 1})
        missing = await tool.handler({"agent": "Job hunting", "run": 7})
    finally:
        await dags.stop()
    assert everyone.splitlines()[0] == "- “Master” (#1) (the master agent): no DAG."
    assert f"- “Job hunting” (#{jobs}): its DAG jobs runs only when it's run by hand." in everyone
    assert "run by the user, done: Applied to Acme." in everyone
    assert runs.startswith("The last runs of “Job hunting”'s DAG, newest first:\n- #1,")
    assert "- apply (done): Applied to Acme." in one and one.endswith("Its summary: Applied to Acme.")
    assert "only its last 10 are kept" in missing


def test_an_evaluator_that_runs_out_of_tries_passes_on_what_it_has():
    source = "with Dag('d', prompt='p'):\n    AgenticTask('a', prompt='x') >> AgenticEvaluator('c', prompt='ok?')\n"
    assert read(source).get("c").on_exhausted == "pass_latest"
    with pytest.raises(DagError, match="pass_latest"):
        read(source.replace("prompt='ok?'", "prompt='ok?', on_exhausted='pause_for_user'"))


@pytest.mark.asyncio
async def test_an_evaluator_has_a_task_done_again_until_it_will_do(athena):
    source = (
        "with Dag('drafts', prompt='p'):\n    d = AgenticTask('draft', prompt='Draft a reply.')\n"
        "    d >> AgenticEvaluator('check', prompt='Is it polite?') >> AgenticTask('send', prompt='Send it.')\n"
    )
    a = athena(
        {
            "Previous attempts were rejected": "Dear Ann, thank you.",
            "Draft a reply.": "Ann, no.",
            "Is it polite?": lambda body: completion(
                '{"rerun": false, "reason": "fine"}'
                if "thank you" in body["messages"][-1]["content"]
                else '{"rerun": true, "reason": "Too curt."}'
            ),
        }
    )
    a["dags"].save(a["jobs"], source)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert run.instances["draft"].attempt == 2 and run.outputs()["draft"] == "Dear Ann, thank you."
    send = next(t for t in a["model"].asked if t.endswith("Send it."))
    assert "Dear Ann, thank you." in send and "Too curt" not in send


# -- upgrades ---------------------------------------------------------------------------------------------


def test_starting_afresh_deletes_conversations_and_agents_once(config, memory):
    import sqlite3

    from conftest import DIMS

    from athena.dags.migrate import start_afresh

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.db_path)
    db.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL, tools TEXT, photos TEXT, channel TEXT);
        CREATE TABLE chat_state (chat_id TEXT PRIMARY KEY, window_start INTEGER NOT NULL DEFAULT 0, summary TEXT,
            summary_session INTEGER);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL,
            first_message INTEGER NOT NULL, last_used REAL, allowed_tools TEXT NOT NULL DEFAULT '[]',
            name TEXT NOT NULL DEFAULT '', muted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE agents (id INTEGER PRIMARY KEY, dag TEXT NOT NULL DEFAULT '');
        CREATE TABLE dag_runs (id INTEGER PRIMARY KEY, input TEXT);
        CREATE TABLE watches (id INTEGER PRIMARY KEY, what TEXT);
        CREATE TABLE timeline (id INTEGER PRIMARY KEY, what TEXT);
        INSERT INTO sessions (started, first_message, name) VALUES (1, 0, 'Old'), (2, 0, 'Older');
        INSERT INTO messages (chat_id, role, content, created_at) VALUES ('session:1', 'user', 'My secret', 'x'),
            ('session:1', 'assistant', 'Noted', 'x');
        INSERT INTO agents VALUES (1, 'with DAG(): ...');
        """
    )
    db.commit()
    db.close()
    memory.store.add_conversations([("You: My secret\nMe: Noted", [0.1] * DIMS, "x")], 2)
    photos = config.db_path.parent / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "a.jpg").write_bytes(b"jpeg")
    asyncio.run(memory.remember("George likes tea", source="you"))

    assert start_afresh(config.db_path, memory.store, photos) is True
    history = ConversationStore(config.db_path)
    assert history.master.id == 3  # ids carry on from where they were
    assert [s.id for s in history.sessions()] == [3] and history.messages(1)[0] == []
    assert memory.store.count() == {"fact": 1}  # what you saved stays; what was said doesn't
    assert not photos.exists()
    tables = {row[0] for row in sqlite3.connect(config.db_path).execute("SELECT name FROM sqlite_master")}
    assert not tables & {"agents", "dag_runs", "watches", "timeline"}
    assert AgentStore(config.db_path).all() == []  # made anew, as it is now
    history.append_exchange(history.master_chat, "Hello", "Hi")
    assert start_afresh(config.db_path, memory.store, photos) is False  # once
    assert len(history.messages(3)[0]) == 2


def test_runs_become_the_engines_once(config):
    import sqlite3

    from athena.dags.migrate import upgrade

    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.db_path)
    db.executescript(
        """
        CREATE TABLE agents (id INTEGER PRIMARY KEY, model TEXT NOT NULL DEFAULT '', guidance TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '', context_at REAL, dag TEXT NOT NULL DEFAULT '', dag_at REAL,
            paused INTEGER NOT NULL DEFAULT 0, next_run REAL);
        CREATE TABLE dag_runs (id INTEGER PRIMARY KEY, input TEXT);
        CREATE TABLE dag_task_runs (id INTEGER PRIMARY KEY, run INTEGER);
        CREATE TABLE dag_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, agent INTEGER NOT NULL, run INTEGER,
            kind TEXT NOT NULL, why TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            changes TEXT NOT NULL DEFAULT '{}', base TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
            created REAL NOT NULL, decided REAL);
        INSERT INTO agents (id, guidance, dag, paused) VALUES (2, 'Jobs in London.', 'with DAG(): ...', 1);
        INSERT INTO dag_proposals (agent, kind, created) VALUES (2, 'workflow', 1);
        """
    )
    db.commit()
    db.close()
    assert upgrade(config.db_path) is True
    store = AgentStore(config.db_path)
    agent = store.get(2)
    assert (agent.guidance, agent.dag, agent.runs) == ("Jobs in London.", "with DAG(): ...", 0)  # kept, as it was
    assert store.next_number(2) == 1 and store.take_old_proposals() == []  # the proposal's out of date
    tables = {row[0] for row in sqlite3.connect(config.db_path).execute("SELECT name FROM sqlite_master")}
    assert not tables & {"dag_runs", "dag_task_runs"}
    assert upgrade(config.db_path) is False  # once


@pytest.mark.asyncio
async def test_deleting_an_agent_deletes_its_runs_even_of_a_dag_that_wont_read_now(athena):
    a = athena()
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        assert run.trigger["number"] == 1 and run.trigger["agent"] == a["jobs"]
    finally:
        await a["dags"].stop()
    a["store"].update(a["jobs"], dag="with DAG():\n    pass\n")  # Athena starts again, and it won't read now

    b = athena()
    await b["dags"].start()
    try:
        assert b["dags"].dag(a["jobs"]) is None and b["dags"].run(run.run_id) is not None
        await b["dags"].delete(a["jobs"])
        assert b["dags"].run(run.run_id) is None and b["store"].get(a["jobs"]).dag == ""
    finally:
        await b["dags"].stop()


# -- asking you, trying again, and catching up -------------------------------------------------------------

ASKS = THREE.replace('prompt="Apply to them.",', 'prompt="Apply to them.", ask_user=True,')


@pytest.mark.parametrize("reply", ["Yes, with my London CV", None])
async def test_a_task_with_ask_user_asks_you_and_carries_on(athena, reply):
    asked = calls(("ask_user", {"question": "Apply to Acme? It's in Leeds.", "choices": ["Yes", "No"]}))
    a = athena({"Apply to them.": [asked, "Done."], **END})
    a["channel"].reply = reply
    a["dags"].save(a["jobs"], ASKS)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
    finally:
        await a["dags"].stop()
    assert a["channel"].questions == [
        ("Apply to Acme? It's in Leeds.", ["Yes", "No"], "“apply”, in Job hunting, asks", a["jobs"])
    ]
    told = run.instances["apply"].tool_calls[-1]["result"]
    if reply:
        assert told == "The user answered: Yes, with my London CV"
    else:
        assert told.startswith("No answer: George didn't answer within 240 minutes. Decide for yourself")
    assert run.status == RunStatus.SUCCESS
    # Only the task that may ask is offered it.
    offering = [
        b for b in a["server"].requests if any(t["function"]["name"] == "ask_user" for t in b.get("tools") or [])
    ]
    first = [next(m["content"] for m in b["messages"] if m["role"] == "user") for b in offering]
    assert first and all(text.endswith("Apply to them.") for text in first)


async def test_a_failed_task_is_tried_again_and_the_dags_file_says_how_often(athena):
    a = athena({"Find new jobs": "Acme.", **END})
    real, failures = a["dags"].backend.generate, {"left": 1}

    async def flaky(request):
        if request.metadata["task_id"] == "find_jobs" and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("the model fell over")
        return await real(request)

    a["dags"].backend.generate = flaky
    a["dags"].save(a["jobs"], THREE)
    await a["dags"].start()
    try:
        run = await run_once(a, a["jobs"])
        assert run.status == RunStatus.SUCCESS and run.instances["find_jobs"].attempt == 2  # [tasks] retries = 1
        assert "### Attempt 1 failed\nRuntimeError: the model fell over" in a["model"].asked[0]  # it never got far
        a["dags"].save(a["jobs"], THREE.replace('allowed_tools=["look"]', 'allowed_tools=["look"], retries=0'))
        failures["left"] = 1
        run = await run_once(a, a["jobs"])
        assert run.instances["find_jobs"].attempt == 1 and went(run)["find_jobs"] == "failed"
    finally:
        await a["dags"].stop()


def test_a_dag_catches_up_unless_it_says_not_to():
    assert read(THREE).catchup is True
    assert read(THREE.replace('allowed_tools=["look"]', 'allowed_tools=["look"], catchup=False')).catchup is False
    run = DagRun("run_x", "jobs", RunStatus.SUCCESS, {"source": "cron", "catchup": True})
    assert runs.started_by(run) == "on its schedule, late: it was missed while Athena was off"
