"""The tasks every DAG ends with: Athena's own, in place of agentic-airflow's DagEvaluator.

  review   looks back at the run, with tools: set_task_tools gives a task the tools it needs and no others,
           propose_dag proposes a new DAG file, and read_skill and save_skill improve a skill the run read. What it
           changes is proposed as one new file, for you to approve (proposals.py); a skill it saves waits for you too
  summary  tells you what's new, if anything, as a message in the agent's chat: you're notified only when there's
           news, or something to approve. What it says is what the next run starts with (the agent's context)

They're the task group "end", after the DAG's last tasks, one after the other, whatever happened before them, each
given the run's log. A run that found nothing skips both (agentic_airflow.found_nothing), so checking for
something often costs little. The review changes things through tools, which check what they're given as they're
called and say what's wrong, so a model that gets a file or a tool's name wrong can put it right there and then;
and a review that writes a DAG file out, rather than proposing it, has it proposed all the same.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentic_airflow import AgenticTask, DagRun, TaskGroup, TaskStatus, ToolContext, found_nothing
from agentic_airflow import Tool as EngineTool

from athena.dags.proposals import TOOLS, WORKFLOW, Proposals, describe_tools, listed, same
from athena.dags.reader import END_GROUP, REFERENCE, DagError, read, with_tools
from athena.dags.store import AgentStore
from athena.skills import READ_SKILL, SAVE_SKILL

NO_CHANGES = "NO CHANGES"
QUIET = "[quiet]"
CONTEXT_WORDS = 1200  # the most an agent keeps for its next run, as you change it (change_context)
REVIEW_STEPS = 8  # tool calls the review may make
PROPOSE_DAG, SET_TOOLS = "propose_dag", "set_task_tools"  # the review's own tools
REVIEW = f"{END_GROUP}.review"
_FENCE = re.compile(r"```(\w*)[ \t]*\n(.*?)```", re.S)


@dataclass
class RunNotes:
    """What a run's tasks noted as they went, and what its review changes and recommends, for the summary."""

    skills: list[str] = field(default_factory=list)  # read in the run
    suggested: list[str] = field(default_factory=list)  # "memory" or "skill", for each its tasks suggested
    recommendations: list[str] = field(default_factory=list)  # what the review proposed, for the summary
    draft: str | None = None  # the DAG file the review would make it, as it goes
    rewritten: bool = False  # by propose_dag: more than tool lists
    tools: dict[str, list[str]] = field(default_factory=dict)  # set_task_tools': where -> its whole list
    why: list[str] = field(default_factory=list)  # what the review said of each change


class Ends(Protocol):
    """What the end tasks need of the service that runs agents' DAGs (service.Dags)."""

    store: AgentStore
    proposals: Proposals
    timezone: str

    @property
    def user(self) -> str: ...

    def agent_of(self, run: DagRun) -> int: ...

    def notes(self, run: DagRun) -> RunNotes: ...

    def check(self, agent_id: int, source: str) -> Any: ...  # the DAG, or DagError

    def tool_names(self) -> tuple[list[str], list[str], list[str]]: ...  # (all, ask first, always ask)

    async def told(self, run: DagRun, text: str, quiet: bool) -> None: ...


def group(source: str, task_ids: Sequence[str], ends: Ends) -> TaskGroup:
    """The end tasks for a DAG with this file and these tasks of its own, as the engine attaches them
    (Engine.register's evaluator)."""
    tasks = TaskGroup(END_GROUP, allowed_tools=[], max_tool_steps=0, dag=None, parent=None)
    tasks.dag = None
    common: dict[str, Any] = {
        "context": [],
        "trigger_rule": "all_done",
        "include_run_log": True,
        "skip_if": found_nothing,
        "group": tasks,
    }
    review = AgenticTask(
        REVIEW,
        prompt=lambda engine, run: reviewing(ends, run, engine.dag_for(run), source),
        allowed_tools=[SET_TOOLS, PROPOSE_DAG, READ_SKILL, SAVE_SKILL],
        max_tool_steps=REVIEW_STEPS,
        on_complete=lambda engine, run, inst: _reviewed(ends, run, source, str(inst.output or "")),
        **common,
    )
    summary = AgenticTask(
        f"{END_GROUP}.summary",
        prompt=lambda engine, run: summarising(ends.user, _recommended(ends, run), failures(engine, run)),
        on_complete=lambda engine, run, inst: _summarised(ends, run, str(inst.output or "")),
        **common,
    )
    review >> summary
    return tasks


# -- the review ------------------------------------------------------------------------------------------


@dataclass
class ToolUse:
    """How a run used one tool."""

    calls: int = 0
    failed: int = 0
    declined: int = 0  # by you
    asked: int = 0  # times you were asked, and allowed it
    tasks: dict[str, int] = field(default_factory=dict)  # calls by task


def _uses(run: DagRun) -> dict[str, ToolUse]:
    """How the run's own tasks used each tool, from what the engine kept of each call."""
    uses: dict[str, ToolUse] = {}
    for inst in run.instances.values():
        if inst.task_id.startswith(f"{END_GROUP}."):
            continue
        for call in inst.tool_calls:
            if call.get("name") == "search_tools":
                continue
            use = uses.setdefault(str(call.get("name")), ToolUse())
            use.calls += 1
            status = call.get("status")
            use.failed += status == "error"
            use.declined += status == "rejected"
            use.asked += status == "approved"
            use.tasks[inst.task_id] = use.tasks.get(inst.task_id, 0) + 1
    return uses


def reviewing(ends: Ends, run: DagRun, dag: Any, source: str) -> str:
    """What the review is asked: how each task used its tools, the DAG file, and the skills the run read."""
    user = ends.user
    known, asks, always = ends.tool_names()
    uses = _uses(run)
    lines = []
    for task in dag.own_tasks():
        given = task.scope.allowed_tools if task.scope.allowed_tools is not None else dag.scope.allowed_tools
        used = []
        for name, use in uses.items():
            if n := use.tasks.get(task.id):
                notes = [said for count, said in ((use.failed, "failed"), (use.declined, f"{user} declined it"),
                                                 (use.asked, f"{user} allowed it")) if count]  # fmt: skip
                used.append(f"{name} ×{n}" + (f" ({', '.join(notes)})" if notes else ""))
        lines.append(f"- {task.id}: given {listed(given)}; used {', '.join(used) or 'none'}")
    skills = ends.notes(run).skills
    read_skills = (
        f"\n\n3. Skills. It read {' and '.join(f'“{s}”' for s in skills)}. If a change would make one go better "
        f"next time, read it with {READ_SKILL} and save the whole improved skill with {SAVE_SKILL}, for {user} to "
        "approve."
        if skills
        else ""
    )
    return (
        "The run's own tasks have finished: its log is above. Review it, to make the next run better, with the "
        "tools you have here. Most runs need no change: change only what clearly helps.\n\n"
        "1. Each task's tools. A task is given only the tools its list names: its own, or the DAG's. Others that "
        f"ask first it can find, and they ask {user}; others it can't use.\n"
        + "\n".join(lines)
        + f"\nTools there are: {', '.join(known)}. These ask first, and on a list run without asking, so list one "
        f"only if {user} allowed it here and it only reads or changes little: {', '.join(asks) or 'none'}. These "
        f"always ask: {', '.join(always) or 'none'}.\nIf a task used a tool it didn't need, or needed one it "
        f"wasn't given, call {SET_TOOLS} with its whole new list.\n\n"
        f"2. Its tasks. This is the DAG file now:\n```python\n{source.strip()}\n```\nIf a task did more than one "
        "job, did another's work again, or had an unclear prompt, or one's missing or not needed, call "
        f"{PROPOSE_DAG} with the whole new file, keeping what works and its layout.\n{REFERENCE}"
        f"{read_skills}\n\nWhen you're done, reply with a sentence saying what you changed, or just: {NO_CHANGES}"
    )


def review_tools(ends: Ends) -> list[EngineTool]:
    """The review's tools, for the engine's registry: each acts only for a run's review, on what it'll propose."""

    def notes_of(ctx: ToolContext) -> tuple[RunNotes, int, str]:
        if ctx.task_id != REVIEW or ctx.run is None:
            raise _Refused("Only the review at the end of a run can do that.")
        agent_id = ends.agent_of(ctx.run)
        notes = ends.notes(ctx.run)
        return notes, agent_id, notes.draft or ends.store.get(agent_id).dag

    def propose_dag(ctx: ToolContext, source: str = "", why: str = "") -> str:
        try:
            notes, agent_id, _ = notes_of(ctx)
            new = str(source).strip() + "\n"
            ends.check(agent_id, new)
            if notes.tools:  # what set_task_tools said, kept
                new = with_tools(new, {"allowed_tools": notes.tools})
                ends.check(agent_id, new)
        except _Refused as exc:
            return f"Error: {exc}"
        except (DagError, SyntaxError) as exc:
            return f"Error: that DAG file won't do: {exc} Call {PROPOSE_DAG} again with it put right, or leave it."
        notes.draft, notes.rewritten = new, True
        notes.why.append(" ".join(str(why).split()))
        return f"Noted: it'll be proposed to {ends.user}, who decides, once you're done."

    def set_task_tools(ctx: ToolContext, task: str = "", tools: Any = None, why: str = "") -> str:
        try:
            notes, agent_id, file = notes_of(ctx)
        except _Refused as exc:
            return f"Error: {exc}"
        names = tools if isinstance(tools, list) else []
        if not isinstance(tools, list) or not all(isinstance(n, str) for n in names):
            return 'Error: tools is a list of tool names, such as ["fetch"].'
        known = ends.tool_names()[0]
        if unknown := [n for n in names if not fnmatch.filter(known, n)]:
            return f"Error: no tool is called {', '.join(unknown)}. The tools there are: {', '.join(known)}."
        task_ids = [t.id for t in read(file, timezone=ends.timezone).own_tasks()]
        where = str(task).strip()
        if where not in ("dag", *task_ids):
            return f'Error: there\'s no task called {where!r}: give one of {", ".join(task_ids)}, or "dag".'
        names = list(dict.fromkeys(names))
        try:
            new = with_tools(file, {"allowed_tools": {where: names}})
            ends.check(agent_id, new)
        except (DagError, SyntaxError) as exc:
            return f"Error: {exc}"
        notes.draft = new
        notes.tools[where] = names
        notes.why.append(" ".join(str(why).split()))
        which = "every task" if where == "dag" else where
        return f"Noted: {which} would be given {listed(names)}, once {ends.user} approves it."

    text = {"type": "string"}
    return [
        EngineTool(
            SET_TOOLS,
            set_task_tools,
            "Give a task (or the DAG, for every task without its own list) a new list of the tools it's given, "
            "replacing its list: the review proposes it to the user.",
            {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": 'A task\'s id, or "dag"'},
                    "tools": {"type": "array", "items": text, "description": "Its whole list of tools"},
                    "why": {"type": "string", "description": "Why, in a sentence"},
                },
                "required": ["task", "tools"],
            },
        ),
        EngineTool(
            PROPOSE_DAG,
            propose_dag,
            "Propose a new DAG file: the whole of it, as it should be. The review proposes it to the user.",
            {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "The whole new DAG file"},
                    "why": {"type": "string", "description": "What changes, and why, in a sentence or two"},
                },
                "required": ["source"],
            },
        ),
    ]


class _Refused(Exception):
    pass


def written_out(answer: str) -> str | None:
    """The one DAG file an answer writes out, if it does: as a review may, rather than propose it."""
    files = [body for lang, body in _FENCE.findall(answer) if lang.lower() in ("python", "py", "") and "Dag(" in body]
    return files[0].strip() + "\n" if len(files) == 1 else None


def _reviewed(ends: Ends, run: DagRun, source: str, answer: str) -> None:
    """The review's done: what it changed is proposed, as one new file, if the DAG hasn't changed as it ran."""
    notes = ends.notes(run)
    agent_id = ends.agent_of(run)
    current = ends.store.get(agent_id).dag
    if not same(source, current):
        return
    if not notes.rewritten and (file := written_out(answer)):  # written out, not proposed
        try:
            ends.check(agent_id, file)
            notes.draft = with_tools(file, {"allowed_tools": notes.tools}) if notes.tools else file
            notes.rewritten = True
            notes.why.append(_FENCE.sub("", answer).strip())
        except (DagError, SyntaxError):
            pass
    if notes.draft is None or same(notes.draft, current):
        return
    why = " ".join(w for w in notes.why if w)
    if notes.rewritten:
        if p := ends.proposals.propose(agent_id, run.run_id, WORKFLOW, why, notes.draft):
            _recommend(ends, run, p, f"A change to the DAG: {_short(why or 'see the dashboard', 300)}")
    else:
        changes = {"allowed_tools": dict(notes.tools)}
        if p := ends.proposals.propose(agent_id, run.run_id, TOOLS, why, with_tools(current, changes), changes):
            _recommend(ends, run, p, f"Tools: {describe_tools(changes)}")


def _recommend(ends: Ends, run: DagRun, proposal: dict[str, Any], said: str) -> None:
    """For the summary to mention: what's proposed, and which tools it would let run without asking."""
    payload = proposal["payload"]
    if unasked := ends.proposals.unasked(payload["base"], payload["definition"]):
        said += f" ⚠️ It would let these run without asking you: {', '.join(unasked)}."
    ends.notes(run).recommendations.append(said)


# -- the summary, which the next run starts with ---------------------------------------------------------


def summarising(user: str, recommendations: Sequence[str], failed: Sequence[str]) -> str:
    recommended = (
        " These changes are waiting for them to approve or reject on the dashboard, so mention each in a few "
        "words:\n" + "\n".join(f"- {r}" for r in recommendations)
        if recommendations
        else ""
    )
    went_wrong = f" These tasks didn't finish, so say so: {', '.join(failed)}." if failed else ""
    return (
        f"Now tell {user} what this run found, as a message from you in this chat: friendly and short, a sentence "
        "or a few, with no title, heading, date or time, and not calling it a summary or a briefing. Say only "
        f"what's new that they'd want to know, by name, as the next run starts with what you say.{went_wrong}"
        f"{recommended}\nIf there's nothing new{', and nothing went wrong' if failed else ''}, reply with just: "
        f"{QUIET}"
    )


def is_quiet(answer: str) -> tuple[bool, str]:
    """Whether the summary says there's nothing new, wherever it says so, and the summary without saying so."""
    text = answer.strip()
    if QUIET in text.lower():
        return True, re.sub(re.escape(QUIET), "", text, flags=re.I).strip()
    return False, text


def _recommended(ends: Ends, run: DagRun) -> list[str]:
    notes = ends.notes(run)
    said = suggested(notes.suggested)
    extra = [f"It suggested {said}, to approve or not on the dashboard's Memory and Skills tabs"] if said else []
    return [*notes.recommendations, *extra]


async def _summarised(ends: Ends, run: DagRun, answer: str) -> None:
    quiet, said = is_quiet(answer)
    quiet = quiet and not _recommended(ends, run)
    run.summary = said or "Nothing new."
    if not quiet and said:  # what it told you is what the next run starts with
        ends.store.update(ends.agent_of(run), context=said)
    await ends.told(run, run.summary, quiet)


def suggested(kinds: list[str]) -> str:
    """What a run suggested for you to approve, such as "2 memories and a skill". Empty if nothing."""
    parts = []
    for kind, plural in (("memory", "memories"), ("skill", "skills")):
        if n := kinds.count(kind):
            parts.append(f"a {kind}" if n == 1 else f"{n} {plural}")
    return " and ".join(parts)


def failures(engine: Any, run: DagRun) -> list[str]:
    """The DAG's own tasks that failed, or weren't run for a failure before them, such as "apply (for 2 of its 5
    items)" for one that failed for some of the items it ran for."""
    failed = []
    for task in engine.dag_for(run).own_tasks():
        insts = run.instances_for(task.id)
        broken = [i for i in insts if i.status in (TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED)]
        if broken and len(insts) > 1 and len(broken) < len(insts):
            failed.append(f"{task.id} (for {len(broken)} of its {len(insts)} items)")
        elif broken:
            failed.append(task.id)
    return failed


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
