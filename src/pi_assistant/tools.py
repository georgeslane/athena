"""Tool registry shared by built-in tools and tools discovered from MCP servers."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
ToolProvider = Callable[[], Iterable["Tool"]]

_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


class ToolError(Exception):
    """A problem the model can fix, such as a bad argument. Its message is shown to the model as-is."""


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


def safe_tool_name(name: str) -> str:
    """Tool names must match ^[a-zA-Z0-9_-]{1,64}$ for most OpenAI-compatible servers."""
    return _INVALID_NAME_CHARS.sub("_", name)[:64] or "tool"


def matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


class ToolRegistry:
    """Holds static tools plus providers (e.g. the MCP manager) whose tools can change on reload.

    The order of tools is stable so the prompt sent to the model stays identical
    between requests, which keeps the model server's prompt cache warm.
    """

    def __init__(self) -> None:
        self._static: dict[str, Tool] = {}
        self._providers: list[ToolProvider] = []

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
        for tool in self.all():
            if tool.name == name:
                return tool
        return None

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.all()]
