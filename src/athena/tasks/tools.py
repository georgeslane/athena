"""The model's tools for tasks: scheduling one, which asks you first, listing them, and running, pausing or
resuming one. And watch, for what Athena keeps an eye on (watching.py)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from athena.tasks.schedules import SCHEDULES, Schedule
from athena.tools import Tool, ToolError
from athena.watching import watch_tool

if TYPE_CHECKING:
    from athena.tasks.scheduler import Scheduler


def task_tools(scheduler: Scheduler) -> list[Tool]:
    """schedule_task, list_tasks and change_task, and watch if anything can be watched."""

    async def check(args: dict[str, Any]) -> str:
        try:
            schedule = Schedule.parse(str(args.get("schedule") or ""), scheduler.tz, scheduler.clock.now(scheduler.tz))
            allowed = scheduler.check_tools(args.get("tools") or [])
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        title, instructions = str(args.get("title") or "").strip(), str(args.get("instructions") or "").strip()
        if not title or not instructions:
            raise ToolError("A task needs a title and instructions.")
        unasked = f" It may use {', '.join(allowed)} without asking you." if allowed else ""
        return f"A task, “{title}”, {schedule.describe()}:\n{instructions}\n{unasked}".rstrip()

    async def schedule_task(args: dict[str, Any]) -> str:
        try:
            task = scheduler.add(
                str(args.get("title") or ""),
                str(args.get("instructions") or ""),
                str(args.get("schedule") or ""),
                list(args.get("tools") or []),
                created_by="athena",
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return f"Scheduled task #{task.id}, “{task.title}”, {Schedule.kept(task.schedule, scheduler.tz).describe()}."

    async def list_tasks(args: dict[str, Any]) -> str:
        tasks = scheduler.store.all()
        if not tasks:
            return "There are no tasks."
        lines = []
        for task in tasks:
            d = scheduler.describe(task)
            when = datetime.fromtimestamp(d["next_run"], scheduler.tz) if d["next_run"] else None
            nxt = f", next {when:%a %-d %b %H:%M}" if when else ""
            last = f"; last run: {task.last_outcome}" if task.last_outcome else ""
            lines.append(f"#{task.id} {task.title} ({d['status']}): {d['when']}{nxt}{last}")
        return "\n".join(lines)

    async def change_task(args: dict[str, Any]) -> str:
        action = str(args.get("action") or "")
        try:
            task_id = int(args.get("id"))  # type: ignore[arg-type]
            act = {"run": scheduler.run_now, "pause": scheduler.pause, "resume": scheduler.resume}[action]
            task = act(task_id)
        except (KeyError, TypeError, ValueError) as exc:
            missing = isinstance(exc, ValueError) and "no task" in str(exc)
            raise ToolError(str(exc) if missing else "Give the task's id, and run, pause or resume.") from exc
        done = {"run": "will run now", "pause": "is paused", "resume": "is running again"}[action]
        return f"Task #{task.id}, “{task.title}”, {done}."

    watching = (
        [watch_tool(scheduler.watches, scheduler.tz, scheduler._heartbeat_minutes(), scheduler.wake)]
        if scheduler.watches
        else []
    )
    return [
        *watching,
        Tool(
            name="schedule_task",
            description=(
                "Set yourself a task to do by yourself, now in the background or on a schedule, and message "
                "the user what you find: research, a regular briefing, checking on something. Asks the user "
                "first. `tools` are ones that normally ask first which the task may use without asking."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "A few words"},
                    "instructions": {
                        "type": "string",
                        "description": "What to do and what to report, for you to follow later on your own",
                    },
                    "schedule": {"type": "string", "description": f"One of: {SCHEDULES}"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "instructions", "schedule"],
            },
            handler=schedule_task,
            needs_confirmation=True,
            preview=check,
        ),
        Tool(
            name="list_tasks",
            description="The tasks you do by yourself: their ids, schedules and how their last runs went.",
            parameters={"type": "object", "properties": {}},
            handler=list_tasks,
        ),
        Tool(
            name="change_task",
            description="Run a task now, pause it, or resume it.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "action": {"type": "string", "enum": ["run", "pause", "resume"]},
                },
                "required": ["id", "action"],
            },
            handler=change_task,
        ),
    ]
