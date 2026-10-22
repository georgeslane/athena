"""What Athena does, tool by tool: each tool call, as the agent made it, and the steps a run keeps of them, for the
dashboard (an agent's DAG history) and the reviews at the end of each run."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_STEPS, STEP_CHARS = 40, 400  # tool calls kept of each thing it did, and how much of each's in and out


@dataclass
class ToolEvent:
    """A tool call, as the agent made it (Agent.respond's on_tool)."""

    tool: str
    args: dict[str, Any]
    output: str  # what the model was told
    outcome: str  # RAN, FAILED or DECLINED (stats.py)
    approval: str = ""  # if it asks first: "you" if you were asked, "allowed" if it may run without asking here
    flagged: str = ""  # what the privacy check found in what it would send (privacy.py)


def cut(text: str, limit: int = STEP_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def steps() -> tuple[list[dict[str, str]], Callable[[ToolEvent], Any]]:
    """What a run does, as it does it: a list it fills, and the on_tool for Agent.respond that fills it with each
    tool call, what it was given and gave back (cut short), what happened, and, if it asks first, whether you were
    asked or it was allowed without asking."""
    taken: list[dict[str, str]] = []

    async def on_tool(event: ToolEvent) -> None:
        if len(taken) >= MAX_STEPS:
            return
        given = json.dumps(event.args, ensure_ascii=False) if event.args else ""
        step = {"tool": event.tool, "given": cut(given), "outcome": event.outcome, "said": cut(event.output)}
        if event.approval:
            step["approval"] = event.approval
        if event.flagged:
            step["flagged"] = event.flagged
        taken.append(step)

    return taken, on_tool
