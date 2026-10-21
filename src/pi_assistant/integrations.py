"""The built-in integrations, in the order the dashboard shows them. To add one, see builtin.py."""

from __future__ import annotations

from pi_assistant.builtin import Builtin
from pi_assistant.claude_projects import ClaudeProjects
from pi_assistant.databases import Databases
from pi_assistant.markets import Markets
from pi_assistant.mcp_registry import MCPRegistry
from pi_assistant.news import NewsReader
from pi_assistant.run_code import CodeRunner
from pi_assistant.trading212 import Trading212

BUILTINS: tuple[type[Builtin], ...] = (
    NewsReader,
    Markets,
    Trading212,
    Databases,
    CodeRunner,
    MCPRegistry,
    ClaudeProjects,
)
