"""The built-in integrations, in the order the dashboard shows them. To add one, see builtin.py."""

from __future__ import annotations

from athena.builtin import Builtin
from athena.claude_projects import ClaudeProjects
from athena.databases import Databases
from athena.files import Files
from athena.markets import Markets
from athena.mcp_registry import MCPRegistry
from athena.news import NewsReader
from athena.run_code import CodeRunner
from athena.trading212 import Trading212

BUILTINS: tuple[type[Builtin], ...] = (
    NewsReader,
    Markets,
    Trading212,
    Databases,
    CodeRunner,
    Files,
    MCPRegistry,
    ClaudeProjects,
)
