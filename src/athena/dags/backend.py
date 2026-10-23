"""How agentic-airflow's engine works with Athena: its model, and its tools.

Backend is the engine's ModelBackend: one call to the model, for one step of a DAG task. Each request starts as
the agent's chat does, with Athena's system prompt and the agent's guidance, files, context and DAG, so the
model server's cache of that serves both, then says what the run is (Runs.opening), then what the engine asks
of the task. If the model server can't be reached, it tries again a little later, and once a run's found it
can't, its other tasks don't try.

Toolbox is the engine's registry of tools: Athena's, as they are now (MCP servers' come and go). A tool that asks
you first asks through the engine, which pauses the task until you answer (Dags asks you); one that always asks
does so whatever allowed_tools says. Each call runs as Athena's own do (Agent.run_alone): the privacy check, the
tool's time limit, and what it saves kept for you to approve, since you aren't there to see it. What a tool
notes, such as a file to send you or a skill it read, is passed on.

Working is what each task that's running is doing, for the status board and the usage statistics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import openai
from agentic_airflow import GenerationRequest, GenerationResponse, Message, PermanentError, ToolCall, ToolContext
from agentic_airflow import Tool as EngineTool

from athena.stats import QueryRecord
from athena.tool_search import NAMES as SEARCH_NAMES
from athena.tools import NEVER_UNASKED, Tool, matches_any

if TYPE_CHECKING:
    from athena.agent import Agent
    from athena.status import Task

log = logging.getLogger(__name__)

RETRIES = (30.0, 120.0)  # the model server can't be reached: try again after these many seconds
UNREACHABLE = (openai.APIConnectionError, openai.APITimeoutError)


class Unreachable(PermanentError):
    """The model server couldn't be reached, even after trying again: the task isn't retried (retries)."""

    def __str__(self) -> str:
        return "the model server couldn't be reached, even after trying again"


@dataclass
class Doing:
    """A task that's running: what the status board shows of it, and what the statistics keep."""

    label: str  # such as "“apply_0” in Job hunting"
    status: Task
    record: QueryRecord
    model: str = ""  # the model it's holding, once it's asked it something
    notes: dict[str, list[str]] = field(default_factory=dict)  # what its tools noted (tools.Turn)


class Working:
    """What each DAG task that's running is doing, by run and instance; and which task holds each model, as the
    gate tells a message of yours waiting for it (gate.py)."""

    def __init__(self, agent: Agent, label: Callable[[str, str], tuple[int, str, str]]):
        self.agent = agent
        self.label = label  # (run_id, instance_id) -> (agent id, what the board says, what a message is told)
        self._doing: dict[tuple[str, str], Doing] = {}

    def of(self, run_id: str, instance_id: str) -> Doing:
        key = (run_id, instance_id)
        if key not in self._doing:
            agent_id, board, label = self.label(run_id, instance_id)
            status = self.agent.status.begin(board)
            self._doing[key] = Doing(label, status, QueryRecord(session=agent_id, channel="DAG"))
        return self._doing[key]

    def holding(self, doing: Doing, model: str) -> None:
        """The task's asking the model: it holds it until it finishes."""
        doing.model = model
        self.agent.gate.doing[model] = doing.label

    def finish(self, run_id: str, instance_id: str, error: str = "") -> Doing | None:
        """The task's stopped: finished, failed, or waiting for you. What it did, if it did anything."""
        doing = self._doing.pop((run_id, instance_id), None)
        if doing is not None:
            doing.status.finish(RuntimeError(error) if error else None)
            doing.record.error = error
            self.agent.record(doing.record)
            if doing.model and self.agent.gate.doing.get(doing.model) == doing.label:
                self.agent.gate.doing.pop(doing.model)
        return doing

    def finish_run(self, run_id: str) -> None:
        for run, instance in [key for key in self._doing if key[0] == run_id]:
            self.finish(run, instance)


class Backend:
    """The engine's model: Athena's."""

    def __init__(self, agent: Agent, working: Working, opening: Callable[[str], str], thinking: str | None):
        self.agent = agent
        self.working = working
        self.opening = opening  # run_id -> what the run's requests start with, after Athena's system prompt
        self.thinking = thinking  # [tasks] thinking, for a task that thinks
        self.unreachable: set[str] = set()  # runs that found the model server can't be reached

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        meta = request.metadata
        run_id = str(meta.get("run_id") or "")
        if run_id in self.unreachable:
            raise Unreachable
        doing = self.working.of(run_id, str(meta.get("instance_id") or ""))
        self.working.holding(doing, request.model)
        doing.status.thinking()
        system = "\n\n".join(p for p in (self.agent.prompt(), self.opening(run_id), request.system) if p)
        messages = [{"role": "system", "content": system}, *(_message(m) for m in request.messages)]
        tools = [{"type": "function", "function": t.to_dict()} for t in request.tools] or None
        thinking = (self.thinking if self.thinking != "off" else "on") if request.thinking else "off"
        for wait in (*RETRIES, None):
            try:
                reply = await self.agent.llm.chat(messages, tools, model=request.model, thinking=thinking)
                break
            except UNREACHABLE as exc:
                if wait is None:
                    self.unreachable.add(run_id)
                    raise Unreachable from exc
                log.warning("Can't reach the model for a DAG task (%s): trying again in %.0f seconds", exc, wait)
                await asyncio.sleep(wait)
        record = doing.record
        record.model_calls += 1
        record.prompt_tokens = max(record.prompt_tokens, reply.prompt_tokens or 0)
        record.completion_tokens += reply.completion_tokens or 0
        calls = [ToolCall(name=c.name, arguments=c.arguments or {}, id=c.id) for c in reply.tool_calls]
        usage = {"input_tokens": reply.prompt_tokens or 0, "output_tokens": reply.completion_tokens or 0}
        return GenerationResponse(text=reply.content, tool_calls=calls, usage=usage)

    def forget(self, run_id: str) -> None:
        self.unreachable.discard(run_id)


def _message(m: Message) -> dict[str, Any]:
    if m.role == "assistant" and m.tool_calls:
        calls = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in m.tool_calls
        ]
        return {"role": "assistant", "content": m.content or "", "tool_calls": calls}
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "name": m.name, "content": m.content}
    return {"role": m.role, "content": m.content}


class Toolbox:
    """Athena's tools, for the engine's registry (agentic_airflow.ToolRegistry.add_provider)."""

    def __init__(self, agent: Agent, working: Working, source: Callable[[str], str], noted: Callable[..., Any]):
        self.agent = agent
        self.working = working
        self.source = source  # run_id -> what it's doing, as what it saves says it came from
        self.noted = noted  # (run_id, Doing, notes) -> None: what its tools noted, once each has run
        self._made: dict[str, tuple[Tool, EngineTool]] = {}

    def __call__(self) -> Iterable[EngineTool]:
        """The tools there are now, as the engine's: made again only for those that have changed."""
        tools = [t for t in self.agent.tools.all() if t.name not in SEARCH_NAMES]
        made = {}
        for tool in tools:
            kept = self._made.get(tool.name)
            made[tool.name] = kept if kept and kept[0] is tool else (tool, self._wrap(tool))
        self._made = made
        return [engine for _, engine in made.values()]

    def _wrap(self, tool: Tool) -> EngineTool:
        async def run(ctx: ToolContext, **args: Any) -> str:
            return await self.run(tool.name, args, ctx)

        return EngineTool(
            name=tool.name,
            fn=run,
            description=tool.description,
            parameters=tool.parameters,
            hidden=matches_any(tool.name, self.agent.tools.on_demand),
            approval="always" if tool.name in NEVER_UNASKED else "ask" if tool.needs_confirmation else "none",
        )

    async def run(self, name: str, args: dict[str, Any], ctx: ToolContext) -> str:
        """Run a tool for a DAG task: the engine's let it, by allowed_tools or your OK."""
        doing = self.working.of(ctx.run_id, ctx.instance_id)
        started = time.monotonic()
        event, notes = await self.agent.run_alone(name, args, doing.status, self.source(ctx.run_id), ctx.run)
        doing.record.tools.append((event.tool, event.outcome))
        log.info("tool %s: %s (%.1fs)", event.tool, event.outcome, time.monotonic() - started)
        for key, values in notes.items():
            doing.notes.setdefault(key, []).extend(values)
        if notes:
            await self.noted(ctx.run_id, doing, notes)
        return event.output
