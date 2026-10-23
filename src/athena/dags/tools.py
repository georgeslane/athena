"""The model's tools for agents' DAGs:

  change_dag   replaces an agent's DAG file, asking you first and showing what changes
  change_context  changes what an agent's DAG keeps for its next run, as you tell it to in a chat
  run_dag      runs an agent's DAG now
  agent_runs   reads what your agents' runs did: each agent's last, an agent's last ten, or one run's tasks

change_dag, change_context and run_dag act on the agent of the conversation they're used in, or another, by name:
so the master agent, which Telegram talks to, can act on the others. A run can't change or start a DAG, or change
what's kept: its reviews propose changes instead, and its end keeps what the next run should know. An answer that
writes out a DAG file without having saved it, as a model may when it says it has, is saved by change_dag all the
same, asking first (Tool.implied).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from athena.dags import runs
from athena.dags.ends import CONTEXT_WORDS
from athena.dags.reader import REFERENCE, DagError, read, when
from athena.tools import Tool, ToolError, current_turn

if TYPE_CHECKING:
    from athena.dags.service import Dags

OUTPUT_CHARS = 1500  # of what a task said, in a run's
CODE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)  # a block of code in an answer


def dag_tools(dags: Dags) -> list[Tool]:
    def which(args: dict[str, Any]) -> int:
        """The agent a tool's to act on: the one named, or the one it's used in."""
        if current_turn().dag is not None:
            raise ToolError("A run can't change or start a DAG: the review at its end proposes changes.")
        named = str(args.get("agent") or "").strip()
        if not named:
            return dags.agent_here()
        agent_id = dags.find(named)
        if agent_id is None:
            raise ToolError(f"There's no one agent called {named!r}: agent_runs lists them.")
        return agent_id

    def checked(args: dict[str, Any]) -> tuple[int, str, Any]:
        agent_id, source = which(args), str(args.get("source") or "")
        try:
            dag = dags.check(agent_id, source)
        except DagError as exc:
            raise ToolError(f"That DAG file won't do: {exc}") from None
        return agent_id, source, dag

    async def preview(args: dict[str, Any]) -> str:
        from athena.dags.proposals import diff

        agent_id, source, dag = checked(args)
        before = dags.store.get(agent_id).dag
        why = str(args.get("why") or "").strip()
        name = dags.history.title(agent_id) or "this agent"
        changes = diff(before, source) if before else source
        unasked = dags.proposals.unasked(before, source)
        warn = f"\n⚠️ It would let these run without asking you: {', '.join(unasked)}." if unasked else ""
        return f"A new DAG for {name}, which runs {when(dag)}.{f' {why}' if why else ''}{warn}\n{changes}"

    async def change_dag(args: dict[str, Any]) -> str:
        agent_id, source, _ = checked(args)
        dag = dags.save(agent_id, source)
        tasks = len(dag.own_tasks())
        return f"Saved. It runs {when(dag)}, with {tasks} task{'s' if tasks != 1 else ''}."

    def written_out(text: str) -> dict[str, Any] | None:
        """change_dag's arguments for the one DAG file an answer writes out, if it's not that agent's already: the
        agent whose DAG has its id, or this conversation's."""
        files = [block for block in CODE.findall(text) if "Dag(" in block]
        if len(files) != 1:
            return None
        source = files[0].strip() + "\n"
        try:
            dag = read(source, timezone=dags.timezone, hooks=dags.hooks)
        except DagError:
            return None
        here = dags.agent_here()
        agent_id = dags.owner(dag.id) or here
        if dags.store.get(agent_id).dag.strip() == source.strip():
            return None
        return {"source": source} if agent_id == here else {"source": source, "agent": f"#{agent_id}"}

    async def change_context(args: dict[str, Any]) -> str:
        agent_id, context = which(args), str(args.get("context") or "").strip()
        if len(context.split()) > CONTEXT_WORDS:
            raise ToolError(f"That's more than {CONTEXT_WORDS} words: keep only what its next run needs.")
        dags.store.update(agent_id, context=context)
        name = dags.history.title(agent_id) or "this agent"
        if not context:
            return f"{name}'s next run starts with nothing kept."
        return f"Changed what {name}'s next run starts with."

    async def run_dag(args: dict[str, Any]) -> str:
        agent_id = which(args)
        try:
            await dags.run_now(agent_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if agent_id == dags.agent_here():
            return "It'll run once it's its turn, after you've answered: what it finds goes into this chat."
        return "It'll run once it's its turn, after you've answered: news from it comes to its chat, and the master's."

    async def agent_runs(args: dict[str, Any]) -> str:
        named = str(args.get("agent") or "").strip()
        if not named:
            return everyone(dags)
        agent_id = dags.find(named)
        if agent_id is None:
            raise ToolError(f"There's no one agent called {named!r}. These are your agents:\n{everyone(dags)}")
        number = args.get("run")
        return one_run(dags, agent_id, int(number)) if number else runs_of(dags, agent_id)

    agent = {"type": "string", "description": "The agent's name, or #id: this conversation's if it's not given"}
    return [
        Tool(
            name="change_dag",
            description=(
                "Change what an agent does by itself: replace its DAG file with `source`, the whole new file. Asks "
                f"the user first, showing what changes.\n{REFERENCE}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "The whole DAG file"},
                    "why": {"type": "string", "description": "What changes, in a sentence"},
                    "agent": agent,
                },
                "required": ["source"],
            },
            handler=change_dag,
            needs_confirmation=True,
            preview=preview,
            implied=written_out,
        ),
        Tool(
            name="change_context",
            description=(
                "Change what an agent's DAG keeps for its next run, when the user tells you something in it is "
                "wrong, done with or missing: give the whole of it as it should be, with that changed and the rest "
                "as it is. Its next run starts with this."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "All of what to keep, replacing what's kept now"},
                    "agent": agent,
                },
                "required": ["context"],
            },
            handler=change_context,
        ),
        Tool(
            name="run_dag",
            description="Run an agent's DAG now, rather than wait for its schedule or what else starts it.",
            parameters={"type": "object", "properties": {"agent": agent}},
            handler=run_dag,
        ),
        Tool(
            name="agent_runs",
            description=(
                "Read what the user's agents' DAG runs did. With no agent: each agent, and its last run's summary. "
                "With an agent: its last 10 runs. With an agent and a run's number: what each of its tasks found."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "An agent's name, or #id"},
                    "run": {"type": "integer", "description": "A run's number, such as 12 for run #12"},
                },
            },
            handler=agent_runs,
        ),
    ]


def everyone(dags: Dags) -> str:
    """Each agent: when its DAG runs, and how its last run went."""
    lines = []
    for agent_id, name in dags.agents():
        dag = dags.dag(agent_id)
        said = f"its DAG {dag.id} runs {when(dag)}" if dag else "no DAG"
        if dag and dags.paused(agent_id):
            said += ", paused"
        last = dags.runs(agent_id)[:1]
        went = f" Last run: {runs.line(last[0], dags.tz)}" if last else ""
        master = " (the master agent)" if agent_id == dags.master else ""
        lines.append(f"- “{name}” (#{agent_id}){master}: {said}.{went}")
    return "\n".join(lines)


def runs_of(dags: Dags, agent_id: int) -> str:
    found = dags.runs(agent_id)
    name = dags.history.title(agent_id) or f"Agent {agent_id}"
    if not found:
        return f"“{name}” hasn't run its DAG yet."
    return f"The last runs of “{name}”'s DAG, newest first:\n" + "\n".join(f"- {runs.line(r, dags.tz)}" for r in found)


def one_run(dags: Dags, agent_id: int, number: int) -> str:
    run = next((r for r in dags.runs(agent_id) if runs.number(r) == number), None)
    if run is None:
        return f"There's no run #{number} of that agent's DAG: only its last 10 are kept."
    lines = [runs.line(run, dags.tz, summary=False), "Its tasks:", *runs.tasks_text(run, OUTPUT_CHARS)]
    if run.summary:
        lines.append(f"Its summary: {run.summary}")
    return "\n".join(lines)
