"""The model's tools for agents' DAGs: trigger_task, for a task in a run to start a triggered task after it;
change_dag, which changes the DAG of the agent it's talking in, asking you first and showing what changes; and
run_dag, which runs it now."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from athena.dags.spec import DagError, parse
from athena.tools import Tool, ToolError, current_turn

if TYPE_CHECKING:
    from athena.dags.scheduler import Dags


def dag_tools(dags: Dags) -> list[Tool]:
    def here() -> int:
        if current_turn().dag is not None:
            raise ToolError("A run can't change or start its own DAG: the review at its end proposes changes.")
        return dags.agent_here()

    async def trigger_task(args: dict[str, Any]) -> str:
        run = current_turn().dag
        if run is None:
            raise ToolError("trigger_task only works in a DAG run, for the task that's running.")
        return run.trigger(str(args.get("task_id") or ""), str(args.get("input") or ""))

    def checked(args: dict[str, Any]) -> tuple[int, str]:
        agent_id, source = here(), str(args.get("source") or "")
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
        agent_id = here()
        try:
            dags.run_now(agent_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return "It'll run as soon as you've finished answering: what it finds goes into this chat."

    return [
        Tool(
            name="trigger_task",
            description=(
                "In a DAG run: start a task marked triggered=True that comes after the task you're doing, once for "
                "each thing it should do, with what it needs to know. It runs after your task's finished."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "input": {"type": "string", "description": "What it should work on, such as a job's link"},
                },
                "required": ["task_id", "input"],
            },
            handler=trigger_task,
        ),
        Tool(
            name="change_dag",
            description=(
                "Change what this agent does by itself: replace its DAG file with `source`, the whole new file, in "
                "the same Airflow-style Python. Asks the user first, showing what changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "The whole DAG file"},
                    "why": {"type": "string", "description": "What changes, in a sentence"},
                },
                "required": ["source"],
            },
            handler=change_dag,
            needs_confirmation=True,
            preview=preview,
        ),
        Tool(
            name="run_dag",
            description="Run this agent's DAG now, rather than wait for its schedule or triggers.",
            parameters={"type": "object", "properties": {}},
            handler=run_dag,
        ),
    ]
