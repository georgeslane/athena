"""Tools found when they're needed, rather than described in every prompt: shorter prompts, read quicker.

The tools [tools] on_demand names are left out of the list sent with each request. Two tools take their
place: find_tools, whose description names each of them with a few words on what it does, and which gives
the full description and parameters of those that match what the model asks for; and use_tool, which runs
one of them.

The list of tools sent stays the same from one request to the next, so the model server's cache of the
prompt holds: what find_tools says goes at the end of the conversation, like any tool's result. A call to
use_tool is run as if the model had called that tool by name (agent.py): it asks you first if the tool
does, its arguments are checked for personal details, and it counts as that tool for a task's permissions,
the status board and the statistics. The model can call one by name, too, once it's found it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pi_assistant.llm import ToolCall
from pi_assistant.tools import Tool, ToolError

FIND_TOOLS, USE_TOOL = "find_tools", "use_tool"
NAMES = (FIND_TOOLS, USE_TOOL)
MAX_FOUND = 5  # tools find_tools describes at once
SUMMARY_CHARS = 80  # of what each does, in find_tools' own description
_WORD = re.compile(r"[a-z0-9]+")
_COMMON = frozenset("a an and for i in is it me my of on or the to what with".split())


def summary(tool: Tool) -> str:
    """The start of what a tool does: its first sentence, cut short."""
    first = re.split(r"(?<=[.!?])\s", " ".join(tool.description.split()), maxsplit=1)[0].rstrip(".")
    return first if len(first) <= SUMMARY_CHARS else first[: SUMMARY_CHARS - 1].rstrip() + "…"


def find(tools: Sequence[Tool], query: str, limit: int = MAX_FOUND) -> list[Tool]:
    """The tools that best match ``query``: one it names exactly, then those with the most of its words, counting
    words in a tool's name three times over those in its description."""
    query = query.strip()
    words = set(_WORD.findall(query.lower())) - _COMMON
    scored = []
    for order, tool in enumerate(tools):
        if tool.name == query:
            scored.append((-1000, order, tool))
            continue
        in_name = words & set(_WORD.findall(tool.name.lower()))
        in_text = words & set(_WORD.findall(tool.description.lower()))
        if score := 3 * len(in_name) + len(in_text):
            scored.append((-score, order, tool))
    return [tool for *_, tool in sorted(scored, key=lambda s: s[:2])][:limit]


def meta_tools(on_demand: Sequence[Tool]) -> list[Tool]:
    """find_tools and use_tool, for these tools. None of either if there aren't any."""
    if not on_demand:
        return []
    names = [tool.name for tool in on_demand]
    listing = "; ".join(f"{tool.name}: {summary(tool)}" for tool in on_demand)

    async def find_tools(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("Say which tool you need, by its name or what it should do.")
        found = find(on_demand, query)
        if not found:
            return f"No tool matches “{query}”. Those you can find are: {', '.join(names)}."
        described = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "asks_first": t.needs_confirmation,
            }
            for t in found
        ]
        return json.dumps(described, ensure_ascii=False)

    async def use_tool(args: dict[str, Any]) -> str:  # the agent runs the tool it names instead (unwrap)
        raise ToolError("use_tool can't run here.")

    return [
        Tool(
            name=FIND_TOOLS,
            description=(
                "Get the full description and parameters of tools you have that aren't listed with the others, "
                f"then run one with use_tool. Look one up when it would help. They are: {listing}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A tool's name, or what you need to do."},
                },
                "required": ["query"],
            },
            handler=find_tools,
        ),
        Tool(
            name=USE_TOOL,
            description="Run a tool find_tools described, with arguments as its parameters say.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": names},
                    "arguments": {"type": "object", "description": "The tool's arguments."},
                },
                "required": ["name", "arguments"],
            },
            handler=use_tool,
        ),
    ]


def unwrap(call: ToolCall) -> ToolCall | str:
    """The call a use_tool call stands for: the tool it names, with its arguments. What to tell the model instead,
    starting "Error:", if it doesn't say them properly. A call whose arguments weren't JSON is left for the agent
    to say so."""
    if call.arguments is None:
        return call
    name, arguments = call.arguments.get("name"), call.arguments.get("arguments", {})
    if isinstance(arguments, str):  # some models send an object's JSON as a string
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return "Error: use_tool's arguments must be an object of the tool's arguments."
    if not isinstance(name, str) or not name or name in NAMES:
        return "Error: say which tool to use, by the name find_tools gave it."
    if not isinstance(arguments, dict):
        return "Error: use_tool's arguments must be an object of the tool's arguments."
    return ToolCall(id=call.id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments))
