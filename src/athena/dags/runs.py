"""How a DAG run is shown: in a list of runs, on the dashboard's History view, and to the model (agent_runs).

Runs are the engine's (agentic_airflow.DagRun). Each started run has a number, kept in its trigger by
service.Dags when it's made: an agent's runs are #1, #2..., whatever its DAG's been called.
"""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from typing import Any

from agentic_airflow import DagRun, TaskStatus

from athena.dags.reader import END_GROUP

STEP_CHARS = 400  # of what a tool was given and gave back, in a run's steps
STATES = {"success": "done", "upstream_failed": "not run: a task before it failed", "cancelled": "stopped"}


def number(run: DagRun) -> int:
    return int(run.trigger.get("number") or 0)


def started_by(run: DagRun, user: str = "you") -> str:
    """Why a run started, in words: "on its schedule", "when arrive-home came in"..."""
    trigger = run.trigger
    source = trigger.get("source")
    if source == "cron":
        return "on its schedule"
    if source == "manual":
        return f"run by {user}"
    if source == "webhook":
        return f"when {trigger.get('webhook')} came in"
    if source == "asset":
        by = ""
        if trigger.get("from_task"):
            by = f" by the task “{trigger['from_task']}” of the DAG {trigger.get('from_dag')}"
        elif trigger.get("from_dag"):
            by = f" by the DAG {trigger['from_dag']}"
        return f"when {trigger.get('asset')} was updated{by}"
    if source == "dag":
        return f"when the DAG {trigger.get('upstream_dag')} finished"
    return str(source or "")


def brief(run: DagRun) -> dict[str, Any]:
    """A run, as a list of runs shows it."""
    return {
        "id": run.run_id,
        "number": number(run),
        "trigger": run.trigger.get("source"),
        "started_by": started_by(run),
        "state": run.status.value,
        "started": run.started_at or run.created_at,
        "finished": run.finished_at,
        "summary": run.summary or _said(run),
    }


def detail(run: DagRun) -> dict[str, Any]:
    """A run, with what each of its tasks was given, said and did, in the DAG's order, as the engine made them."""
    tasks = list(run.instances.values())
    return {
        **brief(run),
        "payload": text_of(run.trigger.get("payload")),
        "error": run.error,
        "tasks": [
            {
                "id": inst.instance_id,
                "task": inst.task_id,
                "end": inst.task_id.startswith(f"{END_GROUP}."),
                "index": inst.map_path[-1] if inst.map_path else None,
                "state": inst.status.value,
                "given": {k: text_of(v) for k, v in ((inst.input_context or {}).get("upstream") or {}).items()},
                "output": text_of(inst.output)
                if inst.status == TaskStatus.SUCCESS
                else inst.error or inst.reason or "",
                "selected": inst.selected,
                "attempt": inst.attempt,
                "started": inst.started_at,
                "finished": inst.finished_at,
                "steps": [_step(call) for call in inst.tool_calls],
            }
            for inst in tasks
        ],
    }


def line(run: DagRun, tz: tzinfo, summary: bool = True) -> str:
    """A run in a line, for the model: "#3, Mon 6 Oct 09:00, on its schedule, done: ..."."""
    when = datetime.fromtimestamp(run.started_at or run.created_at, tz).strftime("%a %-d %b %H:%M")
    said = f": {short(run.summary or _said(run), 400)}" if summary and (run.summary or _said(run)) else ""
    state = STATES.get(run.status.value, run.status.value)
    return f"#{number(run)}, {when}, {started_by(run, 'the user')}, {state}{said}"


def tasks_text(run: DagRun, limit: int = 1500) -> list[str]:
    """What each of a run's tasks did, a line each, for the model."""
    lines = []
    for task in detail(run)["tasks"]:
        item = f" for item {task['index'] + 1}" if task["index"] is not None else ""
        state = STATES.get(task["state"], task["state"])
        lines.append(f"- {task['task']}{item} ({state}): {short(task['output'], limit)}")
    return lines


def _said(run: DagRun) -> str:
    """What a run that wrote no summary says of itself."""
    if run.status.value == "cancelled":
        return "It was stopped."
    if run.error:
        return f"It failed: {short(run.error, 300)}"
    if any(i.reason == "the run found nothing" for i in run.instances.values()):
        return "It found nothing new."
    return ""


def _step(call: dict[str, Any]) -> dict[str, Any]:
    status = call.get("status")
    step = {
        "tool": call.get("name"),
        "given": short(json.dumps(call.get("arguments") or {}, ensure_ascii=False), STEP_CHARS),
        "outcome": {"ok": "ran", "approved": "ran", "error": "failed", "rejected": "declined"}.get(status, status),
        "said": short(str(call.get("result") or ""), STEP_CHARS),
    }
    if status == "approved":
        step["approval"] = "you"
    return step


def text_of(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
