"""Tool registry shared by built-in tools and tools discovered from MCP servers."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
ToolProvider = Callable[[], Iterable["Tool"]]

_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
# Tools that always ask you first, even where others may be allowed without asking (a task's, or a session's):
# starting more tasks, and pushing to GitHub.
NEVER_UNASKED = frozenset({"schedule_task", "hand_to_claude", "answer_claude", "extend_claude_project"})


class ToolError(Exception):
    """A problem the model can fix, such as a bad argument. Its message is shown to the model as-is."""


@dataclass
class Turn:
    """What Athena is doing while a tool runs: answering you, or working by itself.

    Tools that save things keep what Athena saves by itself for you to approve, since you aren't
    there to see it, and what it reads from the web could have put the idea there.
    """

    alone: str | None = None  # None: answering you. Else what it's doing, such as "your task “Briefing”"
    chat: str | None = None  # the chat it's in, such as "session:3"
    # What tools noted, such as {"skills": ["Trip plans"], "suggested": ["memory", "memory"]}.
    notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, key: str, value: str) -> None:
        self.notes.setdefault(key, []).append(value)


_turn: ContextVar[Turn | None] = ContextVar("turn", default=None)


def current_turn() -> Turn:
    """The turn a tool is running in: one answering you, if it's run outside the agent."""
    return _turn.get() or Turn()


def begin_turn(turn: Turn) -> Callable[[], None]:
    """Make ``turn`` the current one, in this task and those it starts. Returns what ends it."""
    token = _turn.set(turn)
    return lambda: _turn.reset(token)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    needs_confirmation: bool = False
    source: str = "builtin"
    # For tools that ask first: checks the arguments and says in plain words what the call
    # will do, for the approval message. Raising ToolError stops the call before you're asked.
    preview: ToolHandler | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def flags(tool: Tool, registry: ToolRegistry) -> str:
    """How a tool's listed for you: whether it asks first, and whether it's found when it's needed."""
    said = [said for said, yes in (("asks first", tool.needs_confirmation),
                                   ("when needed", matches_any(tool.name, registry.on_demand))) if yes]  # fmt: skip
    return f" ({', '.join(said)})" if said else ""


def safe_tool_name(name: str) -> str:
    """Tool names must match ^[a-zA-Z0-9_-]{1,64}$ for most OpenAI-compatible servers."""
    return _INVALID_NAME_CHARS.sub("_", name)[:64] or "tool"


def matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class ToolRegistry:
    """Holds static tools plus providers (e.g. the MCP manager) whose tools can change on reload.

    The order of tools is stable so the prompt sent to the model stays identical
    between requests, which keeps the model server's prompt cache warm.

    Tools ``on_demand`` names are found by the model when it needs them, rather than sent with every
    request: find_tools and use_tool are sent in their place (tool_search.py).
    """

    def __init__(self) -> None:
        self._static: dict[str, Tool] = {}
        self._providers: list[ToolProvider] = []
        self.on_demand: list[str] = []  # patterns of tool names: [tools] on_demand

    def add(self, tool: Tool) -> None:
        if tool.name in self._static:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._static[tool.name] = tool

    def add_provider(self, provider: ToolProvider) -> None:
        self._providers.append(provider)

    def all(self) -> list[Tool]:
        tools = list(self._static.values())
        seen = set(self._static)
        for provider in self._providers:
            for tool in provider():
                if tool.name not in seen:
                    seen.add(tool.name)
                    tools.append(tool)
        return tools

    def get(self, name: str) -> Tool | None:
        """Any tool by its name: one that's found when needed, or find_tools itself, as well."""
        for tool in self.all():
            if tool.name == name:
                return tool
        return next((t for t in self._meta_tools() if t.name == name), None)

    def deferred(self) -> list[Tool]:
        """The tools found when they're needed, rather than sent with every request."""
        return [t for t in self.all() if matches_any(t.name, self.on_demand)]

    def in_prompt(self) -> list[Tool]:
        """The tools sent with every request: all but those found when needed, and what finds them."""
        return [t for t in self.all() if not matches_any(t.name, self.on_demand)] + self._meta_tools()

    def _meta_tools(self) -> list[Tool]:
        from athena.tool_search import meta_tools  # it builds on Tool, from here

        return meta_tools(self.deferred()) if self.on_demand else []

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.in_prompt()]
