"""The model's tools for agents' DAGs:

  task_result  in a run, the result of a task that returns a list, or a yes or no
  change_dag   replaces an agent's DAG file, asking you first and showing what changes
  run_dag      runs an agent's DAG now
  agent_runs   reads what your agents' runs did: each agent's last, an agent's last ten, or one run's tasks

change_dag and run_dag act on the agent of the conversation they're used in, or another, by name: so the master
agent, which Telegram talks to, can act on the others. A run can't change or start its own DAG: its reviews
propose changes instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from athena.dags.spec import MAX_ITEMS, REFERENCE, DagError, parse
from athena.tools import Tool, ToolError, current_turn

if TYPE_CHECKING:
    from athena.dags.scheduler import Dags

SUMMARY_CHARS = 400  # of a run's summary, in a list of runs
OUTPUT_CHARS = 1500  # of what a task said, in a run's
STATES = {"success": "done", "upstream_failed": "not run: a task before it failed"}


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

    async def task_result(args: dict[str, Any]) -> str:
        turn = current_turn()
        if turn.dag is None:
            raise ToolError("task_result is for a task in a DAG run that returns a list, or a yes or no.")
        turn.finish(turn.dag.result(args))
        return "Got it."

    def checked(args: dict[str, Any]) -> tuple[int, str]:
        agent_id, source = which(args), str(args.get("source") or "")
        try:
            parse(source)
        except DagError as exc:
            raise ToolError(f"That DAG file won't do: {exc}") from None
        return agent_id, source

    async def preview(args: dict[str, Any]) -> str:
        from athena.dags.scheduler import diff

        agent_id, source = checked(args)
        before = dags.store.get(agent_id).dag
        why = str(args.get("why") or "").strip()
        name = dags.history.title(agent_id) or "this agent"
        changes = diff(before, source) if before else source
        return f"A new DAG for {name}, which runs {parse(source).describe()}.{f' {why}' if why else ''}\n{changes}"

    async def change_dag(args: dict[str, Any]) -> str:
        agent_id, source = checked(args)
        spec = dags.save(agent_id, source)
        return f"Saved. It runs {spec.describe()}, with {len(spec.tasks)} task{'s' if len(spec.tasks) != 1 else ''}."

    async def run_dag(args: dict[str, Any]) -> str:
        agent_id = which(args)
        try:
            dags.run_now(agent_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if agent_id == dags.agent_here():
            return "It'll run as soon as you've finished answering: what it finds goes into this chat."
        return "It'll run as soon as you've finished answering: news from it comes to its chat, and the master's."

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
            name="task_result",
            description=(
                "In a DAG run, for a task that returns a list or a yes or no: give its result, which ends the task. "
                "items: for a list, one string for each, with all a task working on it needs to know. answer: for "
                "a yes or no."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS},
                    "answer": {"type": "boolean"},
                    "summary": {"type": "string", "description": "What you found, in a sentence"},
                },
            },
            handler=task_result,
        ),
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
        ),
        Tool(
            name="run_dag",
            description="Run an agent's DAG now, rather than wait for its schedule or triggers.",
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
    configs = {c.id: c for c in dags.store.all()}
    lines = []
    for agent_id, name in dags.agents():
        config = configs.get(agent_id)
        spec = dags.spec(config.dag) if config else None
        runs = f"its DAG runs {spec.describe()}" if spec else "no DAG"
        if config and config.paused:
            runs += ", paused"
        last = dags.store.runs(agent_id, 1)
        went = f" Last run: {_run(dags, last[0])}" if last else ""
        master = " (the master agent)" if agent_id == dags.master else ""
        lines.append(f"- “{name}” (#{agent_id}){master}: {runs}.{went}")
    return "\n".join(lines)


def runs_of(dags: Dags, agent_id: int) -> str:
    runs = dags.store.runs(agent_id)
    name = dags.history.title(agent_id) or f"Agent {agent_id}"
    if not runs:
        return f"“{name}” hasn't run its DAG yet."
    return f"The last runs of “{name}”'s DAG, newest first:\n" + "\n".join(f"- {_run(dags, run)}" for run in runs)


def one_run(dags: Dags, agent_id: int, number: int) -> str:
    found = next((r for r in dags.store.runs(agent_id) if r.number == number), None)
    run = dags.store.run(found.id) if found else None
    if run is None:
        return f"There's no run #{number} of that agent's DAG: only its last 10 are kept."
    lines = [_run(dags, run, summary=False), "Its tasks:"]
    for task in run.tasks:
        state = STATES.get(task.state, task.state)
        item = f" for item {task.index + 1}: {_short(task.item, 150)}" if task.item else ""
        lines.append(f"- {task.task}{item} ({state}): {_short(task.output, OUTPUT_CHARS)}")
    if run.summary:
        lines.append(f"Its summary: {run.summary}")
    return "\n".join(lines)


def _run(dags: Dags, run: Any, summary: bool = True) -> str:
    when = datetime.fromtimestamp(run.started or 0, dags.tz).strftime("%a %-d %b %H:%M")
    started = {"schedule": "on its schedule", "manual": "run by the user"}.get(
        run.trigger, f"when {run.trigger.removeprefix('event:')} came in"
    )
    said = f": {_short(run.summary, SUMMARY_CHARS)}" if summary and run.summary else ""
    return f"#{run.number}, {when}, {started}, {STATES.get(run.state, run.state)}{said}"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
