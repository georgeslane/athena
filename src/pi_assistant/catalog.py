"""How the dashboard's Tools tab describes a tool Athena knows about: a built-in integration, or
an MCP server it suggests. The settings it shows, the keys it asks for, and what asks first."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Field:
    key: str  # where it lives in the tool's section, e.g. "environment" or "env.MCP_EMAIL_SERVER_IMAP_HOST"
    label: str
    kind: str = "text"  # text, select or map (names to values, e.g. news feeds)
    help: str = ""
    options: tuple[tuple[str, str], ...] = ()  # for select: (value, label)
    columns: tuple[str, str] = ("Name", "Value")  # for map: what the names and values are
    item: str = "one"  # for map: what each entry is, for "Add a feed" and the like
    required: bool = False  # needed before the tool can be switched on
    example: bool = False  # the default is an example to replace, so it doesn't count as set


@dataclass(frozen=True)
class Secret:
    name: str  # in .env
    label: str
    help: str = ""
    required: bool = True
    # Where the config refers to it, and how, e.g. "headers.Authorization" and "Bearer ${GITHUB_TOKEN}".
    path: str = ""
    template: str = ""


@dataclass(frozen=True)
class Integration:
    id: str
    title: str
    summary: str
    section: str  # in config.toml
    builtin: bool = False
    fields: tuple[Field, ...] = ()
    secrets: tuple[Secret, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)  # an MCP server's section, when it isn't there yet
    setup: str = ""  # anything to do before switching it on
    asks: str = ""  # which of its tools ask first
