"""What the dashboard's Tools tab can change: which tools Athena has, their settings and their keys.

Settings are written to config.toml, keeping your comments and layout, so the file stays
the one place they live and you can still edit it by hand. Keys and passwords go in .env
and are never sent back to the browser: the dashboard only learns whether each is set.

CATALOG describes the tools Athena knows about: the built-in ones, and the MCP servers in
config.example.toml (tests check the two agree). Any other MCP server in config.toml is
shown too, with its command or URL, and can be added and removed from the dashboard.
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import AoT, Comment, InlineTable, Table, Whitespace

from pi_assistant.config import Config, ConfigError, parse_config
from pi_assistant.tools import matches_any

# Athena's own secrets. The dashboard can't change them or pass them to a server.
PROTECTED_SECRETS = {"TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "SIRI_TOKEN", "DASHBOARD_TOKEN"}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_VALUE_CHARS = 2000


class SettingsError(Exception):
    """A change that can't be made, with a message for the dashboard to show."""


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


_MAC_SETUP = "Runs on your Mac, which needs setting up first: see the README, “Your Mac”."

CATALOG: tuple[Integration, ...] = (
    Integration(
        "news",
        "News",
        "Reads the news feeds you list. Only those are ever fetched.",
        "news",
        builtin=True,
        fields=(
            Field(
                "feeds",
                "Feeds",
                "map",
                "RSS or Atom feeds, by any name you like.",
                columns=("Name", "URL"),
                item="a feed",
            ),
        ),
        asks="Never asks: it only reads your feeds.",
    ),
    Integration(
        "trading212",
        "Trading 212",
        "Reads your Trading 212 account, and places or cancels the orders you approve.",
        "trading212",
        builtin=True,
        fields=(
            Field(
                "environment",
                "Account",
                "select",
                "The practice account needs a key made in practice mode.",
                options=(("live", "Real money"), ("demo", "Practice")),
            ),
        ),
        secrets=(
            Secret(
                "TRADING212_API_KEY",
                "API key",
                "From Settings > API in the Trading 212 app.",
                path="api_key",
                template="${TRADING212_API_KEY}",
            ),
            Secret(
                "TRADING212_API_SECRET",
                "API secret",
                "Shown with the key. Keys made before Trading 212 added secrets have none.",
                required=False,
                path="api_secret",
                template="${TRADING212_API_SECRET}",
            ),
        ),
        asks="Placing and cancelling orders always ask first.",
    ),
    Integration(
        "databases",
        "Databases",
        "Answers questions from SQLite databases on the Pi. It can only read them.",
        "sqlite",
        builtin=True,
        fields=(
            Field(
                "databases",
                "Databases",
                "map",
                "Files on the Pi, by any name you like. A path can be relative to the config's folder.",
                columns=("Name", "Path"),
                item="a database",
            ),
        ),
        asks="Never asks: it can only read.",
    ),
    Integration(
        "time",
        "Time",
        "Tells the time anywhere, and converts between time zones.",
        "mcp_servers.time",
        defaults={
            "command": "uvx",
            "args": ["mcp-server-time==2026.8.18", "--local-timezone=Europe/London"],
            "confirm": [],
        },
        asks="Never asks: it's local and only reads the clock.",
    ),
    Integration(
        "fetch",
        "Web pages",
        "Reads web pages.",
        "mcp_servers.fetch",
        defaults={"command": "uvx", "args": ["mcp-server-fetch==2026.8.18"]},
        asks="Every fetch asks first: a URL can carry your data to any website.",
    ),
    Integration(
        "search",
        "Web search",
        "Searches the web through DuckDuckGo, with no account.",
        "mcp_servers.search",
        defaults={"enabled": False, "command": "uvx", "args": ["duckduckgo-mcp-server==0.7.0"], "include": ["search"]},
        asks="Every search asks first, because what you search for leaves your network.",
    ),
    Integration(
        "files",
        "Files on your Mac",
        "Searches and reads the folders you chose on your Mac. It can't change anything.",
        "mcp_servers.files",
        defaults={"enabled": False, "command": "ssh", "args": ["athena-mac", "files"], "confirm": []},
        setup=_MAC_SETUP,
        asks="Never asks: it's read-only, and only sees the folders you chose.",
    ),
    Integration(
        "apple",
        "Calendar and Reminders",
        "Your Mac's Calendar and Reminders, through iMCP.",
        "mcp_servers.apple",
        defaults={
            "enabled": False,
            "command": "ssh",
            "args": ["athena-mac", "apple"],
            "include": [
                "calendars_list",
                "events_fetch",
                "events_create",
                "reminders_lists",
                "reminders_fetch",
                "reminders_create",
            ],
            "confirm": ["events_create", "reminders_create"],
        },
        setup=_MAC_SETUP,
        asks="Adding events and reminders asks first.",
    ),
    Integration(
        "github",
        "GitHub",
        "Reads public GitHub repositories, through GitHub's own server.",
        "mcp_servers.github",
        secrets=(
            Secret(
                "GITHUB_TOKEN",
                "GitHub token",
                "A fine-grained token with only “Public repositories (read-only)” access, and an expiry date.",
                path="headers.Authorization",
                template="Bearer ${GITHUB_TOKEN}",
            ),
        ),
        defaults={
            "enabled": False,
            "url": "https://api.githubcopilot.com/mcp/readonly",
            "headers": {
                "Authorization": "Bearer ${GITHUB_TOKEN}",
                "X-MCP-Lockdown": "true",
                "X-MCP-Tools": "search_repositories,get_file_contents,get_latest_release,list_commits,search_issues",
            },
            "include": [
                "search_repositories",
                "get_file_contents",
                "get_latest_release",
                "list_commits",
                "search_issues",
            ],
            "confirm": [],
        },
        asks="Never asks: it only reads public repositories, and lookups go only to GitHub.",
    ),
    Integration(
        "sec",
        "Company filings",
        "Searches and reads filings to the SEC: annual reports, financial statements and insider trades.",
        "mcp_servers.sec",
        secrets=(
            Secret(
                "EDGAR_IDENTITY",
                "Your name and email",
                "The SEC asks who's asking, like: Jane Doe jane@example.com",
                path="env.EDGAR_IDENTITY",
                template="${EDGAR_IDENTITY}",
            ),
        ),
        defaults={
            "enabled": False,
            "command": "uvx",
            "args": ["--from", "edgartools[ai]==5.60.0", "edgartools-mcp"],
            "env": {"EDGAR_IDENTITY": "${EDGAR_IDENTITY}"},
            "include": ["edgar_company", "edgar_search", "edgar_filing", "edgar_read", "edgar_text_search"],
            "confirm": [],
        },
        setup=(
            'Install it on the Pi first, which takes a while: uvx --from "edgartools[ai]==5.60.0" edgartools-mcp --help'
        ),
        asks="Never asks: filings are public, and lookups go only to the SEC.",
    ),
    Integration(
        "email",
        "Email",
        "An email address of Athena's own. It reads that inbox, and sends only to addresses you allow.",
        "mcp_servers.email",
        fields=(
            Field("env.MCP_EMAIL_SERVER_EMAIL_ADDRESS", "Athena's address", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_USER_NAME", "Login", "Usually the address.", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_IMAP_HOST", "IMAP server", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_IMAP_PORT", "IMAP port"),
            Field("env.MCP_EMAIL_SERVER_SMTP_HOST", "SMTP server", required=True, example=True),
            Field("env.MCP_EMAIL_SERVER_SMTP_PORT", "SMTP port"),
            Field(
                "env.MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS",
                "Who it may email",
                "Comma-separated, and * matches anything. The server refuses anyone else, even if you approve.",
                required=True,
                example=True,
            ),
        ),
        secrets=(
            Secret(
                "EMAIL_PASSWORD",
                "App password",
                "An app password for Athena's own account, not your main password.",
                path="env.MCP_EMAIL_SERVER_PASSWORD",
                template="${EMAIL_PASSWORD}",
            ),
        ),
        defaults={
            "enabled": False,
            "command": "uvx",
            "args": ["mcp-email-server==1.11.0", "stdio"],
            "include": ["list_available_accounts", "list_emails_metadata", "get_emails_content", "send_email"],
            "confirm": ["send_email"],
            "env": {
                "MCP_EMAIL_SERVER_ACCOUNT_NAME": "athena",
                "MCP_EMAIL_SERVER_FULL_NAME": "Athena",
                "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "athena@example.com",
                "MCP_EMAIL_SERVER_USER_NAME": "athena@example.com",
                "MCP_EMAIL_SERVER_PASSWORD": "${EMAIL_PASSWORD}",
                "MCP_EMAIL_SERVER_IMAP_HOST": "imap.example.com",
                "MCP_EMAIL_SERVER_IMAP_PORT": "993",
                "MCP_EMAIL_SERVER_IMAP_SSL": "true",
                "MCP_EMAIL_SERVER_SMTP_HOST": "smtp.example.com",
                "MCP_EMAIL_SERVER_SMTP_PORT": "465",
                "MCP_EMAIL_SERVER_SMTP_SSL": "true",
                "MCP_EMAIL_SERVER_ALLOWED_MUTATIONS": "send",
                "MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "you@example.com",
            },
        },
        asks="Sending asks first. It can never delete, move or flag mail.",
    ),
)
BY_ID = {i.id: i for i in CATALOG}
BUILTIN_SECTIONS = {i.section: i for i in CATALOG if i.builtin}

# The settings of an MCP server that isn't in the catalog.
CUSTOM_FIELDS = (
    Field("command", "Command", help="The program that runs it, such as uvx, npx or ssh."),
    Field("args", "Arguments", "lines", "One per line."),
    Field("url", "URL", help="For a server on the internet, instead of a command."),
    Field("env", "Environment", "map", "Settings the server reads, like an API key.", item="a variable"),
    Field(
        "headers",
        "Headers",
        "map",
        "HTTP headers, such as Authorization.",
        columns=("Header", "Value"),
        item="a header",
    ),
)


# -- .env -------------------------------------------------------------------------------------


class EnvFile:
    """The .env file, where secrets live. Lines it doesn't change are kept as they are."""

    def __init__(self, path: Path):
        self.path = path

    def is_set(self, name: str) -> bool:
        return bool(os.environ.get(name))

    def update(self, values: Mapping[str, str | None]) -> None:
        """Set each name to its value, or remove it if the value is None or empty. Also sets os.environ."""
        for name, value in values.items():
            if not _ENV_NAME.match(name):
                raise SettingsError(f"{name!r} can't be the name of a setting in .env.")
            if value:
                _env_line(name, value)  # checks it can be written
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        for name, value in values.items():
            pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
            found = [i for i, line in enumerate(lines) if pattern.match(line)]
            new = _env_line(name, value) if value else f"{name}="
            if found:
                lines[found[0]] = new
                for i in reversed(found[1:]):  # a name given twice: the first one wins, so drop the others
                    del lines[i]
            elif value:
                lines.append(new)
        write_private(self.path, "\n".join(lines) + "\n", 0o600)
        for name, value in values.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


def _env_line(name: str, value: str) -> str:
    """NAME=value, quoted so that systemd's EnvironmentFile and python-dotenv both read the same value."""
    if any(ch in value for ch in "\r\n\0") or len(value) > MAX_VALUE_CHARS:
        raise SettingsError("That can't go in .env: it's too long, or has a line break in it.")
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,~%-]*", value):
        return f"{name}={value}"
    if "'" not in value:
        return f"{name}='{value}'"  # single quotes: taken literally by both
    raise SettingsError("That can't go in .env: it has both a quote mark and characters that need quoting.")


def write_private(path: Path, text: str, mode: int | None = None) -> None:
    """Replace ``path`` with ``text`` in one step, so a crash never leaves it half-written."""
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(f"{path.name}.tmp")  # .env.tmp and config.toml.tmp are git-ignored
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# -- config.toml ------------------------------------------------------------------------------


def _section(doc: Container | Table, dotted: str, create: bool = False) -> Table | InlineTable | None:
    """The table at a dotted path such as "mcp_servers.github", made if ``create`` and it isn't there."""
    node: Any = doc
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if part not in node:
            if not create:
                return None
            last = i == len(parts) - 1
            # [mcp_servers] on its own would be an empty table: only its children get headers.
            _set(node, part, tomlkit.table(is_super_table=not last))
        node = node[part]
        if not isinstance(node, (Table, InlineTable)):
            raise SettingsError(f"{'.'.join(parts[: i + 1])} in config.toml isn't a section.")
    return node


def _set(table: Any, key: str, value: Any) -> None:
    """Set a key, keeping its comment if it has one. A new key goes after the table's last one,
    rather than after the comments that follow it, which belong to the next table."""
    if key in table:
        table[key] = value
        return
    container = table.value if isinstance(table, Table) else None
    last = None
    if container is not None and not isinstance(value, (Table, AoT)):
        for k, item in container.body:
            if k is not None and not isinstance(item, (Table, AoT)):
                last = k
    if last is not None:
        container._insert_after(last, key, value)  # tomlkit has no public way to do this
    else:
        table[key] = value


def _set_path(section: Any, dotted: str, value: Any) -> None:
    """Set ``env.NAME`` and the like, making ``env`` (as an inline table) if it isn't there."""
    *parents, key = dotted.split(".")
    node = section
    for part in parents:
        if part not in node:
            _set(node, part, tomlkit.inline_table())
        node = node[part]
    _set(node, key, value)


def _get_path(raw: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def _update_map(table: Any, values: Mapping[str, str]) -> None:
    """Make a table hold exactly ``values``, changing it key by key so its comments stay."""
    for key in [k for k in table if k not in values]:
        del table[key]
    for key, value in values.items():
        if table.get(key) != value:
            _set(table, key, value)


def _remove_table(parent: Any, name: str) -> None:
    """Remove a table, and the comment just above it, but not the comment above the table after it.

    tomlkit keeps the comments above a table at the end of whatever comes before it, so those
    are moved: the removed table's own heading comment goes, and the next one's is kept.
    """
    keys = list(parent.keys())
    index = keys.index(name)
    following = _trailing_trivia(parent[name])
    if index > 0:
        previous = _last_body(parent[keys[index - 1]])
        if previous is not None:
            _drop_heading_comment(previous)
            for item in following:
                previous.body.append((None, item))
    del parent[name]


def _last_body(item: Any) -> Container | None:
    """The container whose end is just before whatever follows ``item``: its last subtable's, if it has one."""
    if not isinstance(item, Table):
        return None
    body = item.value
    while body.body and isinstance(body.body[-1][1], Table):
        body = body.body[-1][1].value
    return body


def _trailing_trivia(item: Any) -> list[Any]:
    body = _last_body(item)
    if body is None:
        return []
    trailing = []
    for key, value in reversed(body.body):
        if key is not None or not isinstance(value, (Comment, Whitespace)):
            break
        trailing.insert(0, value)
    return trailing if any(isinstance(v, Comment) for v in trailing) else []


def _drop_heading_comment(body: Container) -> None:
    """Remove the comment lines at the very end of ``body``, back to the blank line before them."""
    while body.body and body.body[-1][0] is None and isinstance(body.body[-1][1], Comment):
        body.body.pop()


# -- what the dashboard shows -----------------------------------------------------------------


@dataclass
class Running:
    """What's running now, which the config file can't say."""

    # For each MCP server: (connected, error, the tools it offers as (name, description)), if it's been tried.
    servers: dict[str, tuple[bool, str | None, list[tuple[str, str]]]] = field(default_factory=dict)
    # For each built-in: its tools that are in use, and why it isn't running if it should be.
    builtins: dict[str, tuple[list[str], str | None]] = field(default_factory=dict)
    applying: bool = False  # changes are being applied


class Settings:
    def __init__(self, config_path: Path | None, env_path: Path):
        self.path = config_path
        self.env = EnvFile(env_path)

    @property
    def editable(self) -> bool:
        return self.path is not None and self.path.exists()

    # -- reading ------------------------------------------------------------------------------

    def _load(self) -> tuple[tomlkit.TOMLDocument, dict[str, Any]]:
        if not self.editable:
            raise SettingsError("There's no config.toml to change: Athena was started without one.")
        assert self.path is not None
        text = self.path.read_text()
        try:
            return tomlkit.parse(text), tomllib.loads(text)
        except Exception as exc:
            raise SettingsError(f"config.toml isn't valid TOML, so it can't be changed here: {exc}") from exc

    def describe(self, running: Running) -> list[dict[str, Any]]:
        """Every tool Athena knows about, for the Tools tab."""
        _, raw = self._load()
        servers = raw.get("mcp_servers", {})
        entries = [self._describe_builtin(i, raw, running) for i in CATALOG if i.builtin]
        names = list(servers) + [i.id for i in CATALOG if not i.builtin and i.id not in servers]
        for name in names:
            entries.append(self._describe_server(name, servers.get(name), running))
        return entries

    def _describe_builtin(self, integ: Integration, raw: dict[str, Any], running: Running) -> dict[str, Any]:
        section = _get_path(raw, integ.section, {}) or {}
        tools, problem = running.builtins.get(integ.id, ([], None))
        enabled = bool(section.get("enabled", Config.model_fields[integ.section].default_factory().enabled))
        entry = self._entry(integ, integ.id, enabled, section, custom=False)
        if not enabled:
            entry["state"], entry["detail"] = "off", "Off"
        elif problem:
            entry["state"], entry["detail"] = "error", problem
        elif tools:
            entry["state"], entry["detail"] = "on", _count(len(tools), "tool")
        elif integ.id == "trading212" or section.get(integ.fields[0].key):
            entry["state"], entry["detail"] = _not_started(running)  # changed in config.toml since it started
        else:
            entry["state"], entry["detail"] = "idle", _empty_detail(integ.id)
        entry["tools"] = [{"name": t, "description": "", "on": True, "asks": None} for t in tools] or None
        return entry

    def _describe_server(self, name: str, section: dict[str, Any] | None, running: Running) -> dict[str, Any]:
        integ = BY_ID.get(name) if name in BY_ID and not BY_ID[name].builtin else None
        added = section is not None
        section = section if added else dict((integ.defaults if integ else {}), enabled=False)
        enabled = bool(section.get("enabled", True)) if added else False
        entry = self._entry(integ, name, enabled, section, custom=integ is None)
        entry["added"] = added
        connected, error, offered = running.servers.get(name, (False, None, []))
        if not enabled:
            entry["state"], entry["detail"] = "off", "Off" if added else "Not set up yet"
        elif error:
            entry["state"], entry["detail"] = "error", error
        elif connected:
            on = [t for t, _ in offered if _tool_on(t, section)]
            entry["state"], entry["detail"] = "on", f"Connected · {_count(len(on), 'tool')}"
        elif name in running.servers:
            entry["state"], entry["detail"] = "starting", "Connecting…"
        else:
            entry["state"], entry["detail"] = _not_started(running)
        entry["tools"] = (
            [
                {
                    "name": t,
                    "description": _first_sentence(d),
                    "on": _tool_on(t, section),
                    "asks": matches_any(t, section.get("confirm", ["*"])),
                }
                for t, d in offered
            ]
            if connected
            else None
        )
        if integ is None:
            entry["summary"] = section.get("url") or " ".join([section.get("command", ""), *section.get("args", [])])
        return entry

    def _entry(
        self, integ: Integration | None, name: str, enabled: bool, section: dict[str, Any], custom: bool
    ) -> dict[str, Any]:
        fields = CUSTOM_FIELDS if integ is None else integ.fields
        secrets = self._secrets(integ, section)
        return {
            "id": name,
            "title": integ.title if integ else name,
            "summary": integ.summary if integ else "",
            "builtin": bool(integ and integ.builtin),
            "custom": custom,
            "added": True,
            "enabled": enabled,
            "setup": integ.setup if integ else "",
            "asks": integ.asks if integ else _asks_custom(section),
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "kind": f.kind,
                    "help": f.help,
                    "options": [{"value": v, "label": label} for v, label in f.options],
                    "columns": list(f.columns),
                    "item": f.item,
                    "required": f.required,
                    "value": _field_value(f, section, integ),
                }
                for f in fields
            ],
            "secrets": secrets,
        }

    def _secrets(self, integ: Integration | None, section: Mapping[str, Any]) -> list[dict[str, Any]]:
        if integ is not None:
            return [
                {
                    "name": s.name,
                    "label": s.label,
                    "help": s.help,
                    "required": s.required,
                    "set": self.env.is_set(s.name),
                }
                for s in integ.secrets
            ]
        # A custom server's secrets are the ${NAME}s its env and headers refer to.
        found = []
        for where in ("env", "headers"):
            for key, value in (section.get(where) or {}).items():
                for name in _ENV_REF.findall(str(value)):
                    found.append(
                        {
                            "name": name,
                            "label": key,
                            "help": f"In .env as {name}.",
                            "required": True,
                            "set": self.env.is_set(name),
                            "where": where,
                        }
                    )
        return found

    # -- changing -----------------------------------------------------------------------------

    def update(self, name: str, change: Mapping[str, Any], offered: Iterable[str] = ()) -> Config:
        """Change a tool's settings. ``offered``: the tools its server offers, for choosing which to use.
        Returns the new config, which the caller applies."""
        doc, raw = self._load()
        unknown = set(change) - {"enabled", "fields", "secrets", "tools"}
        if unknown:
            raise SettingsError(f"Unknown change: {', '.join(sorted(unknown))}.")
        for key in ("fields", "secrets"):
            if not isinstance(change.get(key) or {}, dict):
                raise SettingsError(f"{key} must be names and values.")
        builtin = BY_ID.get(name) if name in BY_ID and BY_ID[name].builtin else None
        servers = raw.get("mcp_servers", {})
        integ = builtin or (BY_ID.get(name) if name in BY_ID else None)
        if builtin is None and integ is None and name not in servers:
            raise SettingsError(f"There's no tool called {name!r}.")

        section_path = integ.section if integ else f"mcp_servers.{name}"
        added_now = integ is not None and not builtin and name not in servers
        if added_now:
            assert integ is not None
            _add_server_table(doc, name, integ.defaults, integ.summary)
        section = _section(doc, section_path, create=True)
        assert section is not None

        # Secrets first: switching on below checks they're set.
        env_changes: dict[str, str | None] = {}
        for secret_name, value in (change.get("secrets") or {}).items():
            secret = self._find_secret(integ, raw, name, secret_name)
            if value is not None and not isinstance(value, str):
                raise SettingsError("A key or password must be text.")
            env_changes[secret_name] = (value or "").strip() or None
            if secret is not None and secret.path and secret.name not in str(_get_path(section, secret.path, "")):
                _set_path(section, secret.path, secret.template)  # so the config uses it

        for key, value in (change.get("fields") or {}).items():
            fld = next((f for f in (integ.fields if integ else CUSTOM_FIELDS) if f.key == key), None)
            if fld is None:
                raise SettingsError(f"{key!r} isn't a setting of {name}.")
            self._set_field(section, fld, value, None if integ else name, env_changes)

        if "tools" in change:
            if builtin:
                raise SettingsError(f"{builtin.title}'s tools are fixed.")
            _choose_tools(section, change["tools"], list(offered))

        if "enabled" in change:
            if not isinstance(change["enabled"], bool):
                raise SettingsError("enabled must be true or false.")
            if change["enabled"]:
                self._check_ready(integ, section, env_changes)
            _set(section, "enabled", change["enabled"])

        return self._save(doc, env_changes)

    def _find_secret(
        self, integ: Integration | None, raw: dict[str, Any], name: str, secret_name: str
    ) -> Secret | None:
        if secret_name in PROTECTED_SECRETS:
            raise SettingsError(f"{secret_name} can't be changed here. It's in .env on the Pi.")
        if integ is not None:
            found = next((s for s in integ.secrets if s.name == secret_name), None)
            if found is None:
                raise SettingsError(f"{secret_name} isn't one of {integ.title}'s keys.")
            return found
        section = raw.get("mcp_servers", {}).get(name, {})
        if secret_name not in {s["name"] for s in self._secrets(None, section)}:
            raise SettingsError(f"{name} doesn't use {secret_name}.")
        return None

    def _set_field(
        self, section: Any, fld: Field, value: Any, server: str | None, env_changes: dict[str, str | None]
    ) -> None:
        """Set one of a tool's settings. ``server``: the name of a server that isn't in the catalog."""
        custom = server is not None
        if fld.kind == "map":
            *parents, key = fld.key.split(".")
            target = section
            for part in parents:
                target = target[part]
            if custom and key in ("env", "headers"):
                values = _keep_secrets(value, key, server, target.get(key) or {}, env_changes)
            else:
                values = _check_map(value, fld)
            if key not in target:
                _set(target, key, tomlkit.inline_table() if custom else tomlkit.table())
            _update_map(target[key], values)
        elif fld.kind == "lines":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SettingsError(f"{fld.label} must be a list of text.")
            lines = [v.strip() for v in value if v.strip()]
            _check_refs(lines, fld.label)
            _set_path(section, fld.key, lines)
        else:
            if not isinstance(value, str) or len(value) > MAX_VALUE_CHARS or "\n" in value:
                raise SettingsError(f"{fld.label} must be one line of text.")
            value = value.strip()
            if fld.kind == "select" and value not in {v for v, _ in fld.options}:
                raise SettingsError(f"{fld.label} must be one of: {', '.join(v for v, _ in fld.options)}.")
            _check_refs([value], fld.label)
            if custom and not value:
                if fld.key in section:
                    del section[fld.key]
                return
            _set_path(section, fld.key, value)

    def _check_ready(self, integ: Integration | None, section: Any, env_changes: Mapping[str, str | None]) -> None:
        """Refuse to switch a tool on that's missing a key or setting it needs."""
        if integ is None:
            return
        for secret in integ.secrets:
            value = env_changes[secret.name] if secret.name in env_changes else os.environ.get(secret.name)
            if secret.required and not value:
                raise SettingsError(f"Fill in “{secret.label}” first.")
        defaults = integ.defaults or {}
        for fld in integ.fields:
            value = _get_path(section, fld.key)
            if fld.required and (not value or (fld.example and value == _get_path(defaults, fld.key))):
                raise SettingsError(f"Fill in “{fld.label}” first.")

    def add_server(self, spec: Mapping[str, Any]) -> tuple[str, Config]:
        """Add an MCP server that isn't in the catalog. Returns its name and the new config."""
        doc, raw = self._load()
        name = str(spec.get("name", "")).strip().lower()
        if not _SERVER_NAME.match(name):
            raise SettingsError("Give it a name of lower-case letters, digits, - or _, starting with a letter.")
        if name in raw.get("mcp_servers", {}) or name in BY_ID:
            raise SettingsError(f"There's already a tool called {name}.")
        command, url = str(spec.get("command") or "").strip(), str(spec.get("url") or "").strip()
        if bool(command) == bool(url):
            raise SettingsError("Give it either a command (a server on the Pi) or a URL (one on the internet).")
        if url and not re.match(r"^https?://", url):
            raise SettingsError("The URL must start with https:// or http://.")
        table: dict[str, Any] = {"enabled": True}
        if command:
            table["command"] = command
            args = spec.get("args") or []
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise SettingsError("Arguments must be a list of text.")
            table["args"] = [a.strip() for a in args if a.strip()]
        else:
            table["url"] = url
        env_changes: dict[str, str | None] = {}
        for where in ("env", "headers"):
            if values := _keep_secrets(spec.get(where) or {}, where, name, {}, env_changes):
                table[where] = values
        _check_refs([str(v) for v in table.get("args", [])] + [url], "The server")
        _add_server_table(doc, name, table, "Added from the dashboard.")
        return name, self._save(doc, env_changes)

    def remove_server(self, name: str) -> Config:
        doc, raw = self._load()
        if name in BY_ID:
            raise SettingsError(f"{BY_ID[name].title} can be switched off, but not removed.")
        if name not in raw.get("mcp_servers", {}):
            raise SettingsError(f"There's no server called {name!r}.")
        _remove_table(doc["mcp_servers"], name)
        return self._save(doc, {})

    def _save(self, doc: tomlkit.TOMLDocument, env_changes: Mapping[str, str | None]) -> Config:
        """Check the changed config, then write it, and any secrets. Nothing is written if it's invalid."""
        assert self.path is not None
        text = tomlkit.dumps(doc).rstrip("\n") + "\n"
        saved = {name: os.environ.get(name) for name in env_changes}
        try:
            for name, value in env_changes.items():  # so ${NAME} in the config sees the new value
                if value:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            cfg = parse_config(text, self.path)
        except ConfigError as exc:
            raise SettingsError(f"That would make config.toml invalid, so nothing was changed.\n{exc}") from exc
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if env_changes:
            self.env.update(env_changes)
        if text != self.path.read_text():
            write_private(self.path.with_name(f"{self.path.name}.bak"), self.path.read_text())
            write_private(self.path, text)
        return cfg


def _add_server_table(doc: tomlkit.TOMLDocument, name: str, values: Mapping[str, Any], comment: str) -> None:
    servers = _section(doc, "mcp_servers", create=True)
    assert servers is not None
    table = tomlkit.table()
    table.add(tomlkit.comment(comment))
    for key, value in copy.deepcopy(dict(values)).items():
        if isinstance(value, dict):
            inline = tomlkit.inline_table()
            inline.update(value)
            value = inline
        table.add(key, value)
    servers[name] = table


def _keep_secrets(
    values: Any, where: str, server: str, current: Mapping[str, Any], env_changes: dict[str, str | None]
) -> dict[str, str]:
    """A server's env or headers, where {"secret": value} means keep that value in .env, not config.toml.

    The config refers to it as ${SERVER_NAME}, using the name it already has if there is one.
    """
    label = "Environment" if where == "env" else "Headers"
    if not isinstance(values, dict):
        raise SettingsError(f"{label} must be a list of names and values.")
    pattern, kind = (_ENV_NAME, "an environment variable") if where == "env" else (_HEADER_NAME, "a header")
    prefix = re.sub(r"[^A-Z0-9]", "_", server.upper())
    plain: dict[str, Any] = {}
    secret: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not pattern.match(key.strip()):
            raise SettingsError(f"{key!r} isn't a valid name for {kind}.")
        if isinstance(value, dict):
            if set(value) != {"secret"} or not isinstance(value["secret"], str):
                raise SettingsError(f"{key} must be text, or a secret.")
            secret[key.strip()] = value["secret"]
        else:
            plain[key.strip()] = value
    result = _check_map(plain, Field(where, label, "map"))
    for key, value in secret.items():
        existing = _ENV_REF.fullmatch(str(current.get(key, "")).strip())
        name = existing.group(1) if existing else re.sub(r"[^A-Z0-9_]", "_", f"{prefix}_{key.upper()}")
        if where == "env" and not existing and key.upper().startswith(prefix + "_"):
            name = re.sub(r"[^A-Z0-9_]", "_", key.upper())
        if name in PROTECTED_SECRETS:
            raise SettingsError(f"{name} is Athena's own, so it can't be used for a server.")
        if "\n" in value or len(value) > MAX_VALUE_CHARS:
            raise SettingsError(f"{key} must be one line of text.")
        env_changes[name] = value.strip() or None
        result[key] = "${" + name + "}"
    return result


def _check_map(value: Any, fld: Field) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SettingsError(f"{fld.label} must be a list of names and values.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise SettingsError(f"Each of {fld.label.lower()} needs a name.")
        if not isinstance(item, str) or "\n" in item or len(item) > MAX_VALUE_CHARS:
            raise SettingsError(f"{key} in {fld.label.lower()} must be one line of text.")
        result[key.strip()] = item.strip()
    _check_refs(list(result.values()), fld.label)
    return result


def _check_refs(values: Iterable[str], where: str) -> None:
    for value in values:
        for name in _ENV_REF.findall(value):
            if name in PROTECTED_SECRETS:
                raise SettingsError(f"{where} can't use {name}: that's Athena's own, and stays with Athena.")


def _choose_tools(section: Any, choices: Any, offered: list[str]) -> None:
    """Set include and confirm from a choice for each tool: whether the model can use it, and whether it asks.

    When some tools don't ask, include lists exactly the tools in use, so that a tool
    added in a server update can't run without asking until you've chosen for it.
    """
    if not offered:
        raise SettingsError("Its tools aren't known until it's connected.")
    if not isinstance(choices, dict) or set(choices) != set(offered):
        raise SettingsError("Choose for each of its tools.")
    on = [t for t in offered if _flag(choices[t], "on")]
    asks = [t for t in on if _flag(choices[t], "asks")]
    if not on:
        raise SettingsError("Choose at least one of its tools, or switch it off.")
    if len(asks) == len(on):  # everything in use asks, which is the default
        include, confirm = (None if len(on) == len(offered) else on), None
    else:
        include, confirm = on, asks
    for key, value in (("include", include), ("exclude", None), ("confirm", confirm)):
        if value is not None:
            _set(section, key, value)
        elif key in section:
            del section[key]  # left out: include all of them, exclude none, and ask for every one


def _flag(choice: Any, key: str) -> bool:
    if not isinstance(choice, dict) or not isinstance(choice.get(key), bool):
        raise SettingsError("Each tool needs on and asks, as true or false.")
    return choice[key]


def _tool_on(tool: str, section: Mapping[str, Any]) -> bool:
    include, exclude = section.get("include") or [], section.get("exclude") or []
    return (not include or matches_any(tool, include)) and not matches_any(tool, exclude)


def _field_value(fld: Field, section: Mapping[str, Any], integ: Integration | None) -> Any:
    value = _get_path(section, fld.key)
    if fld.kind == "map":
        return {str(k): str(v) for k, v in (value or {}).items()}
    if fld.kind == "lines":
        return [str(v) for v in value or []]
    if value is None and fld.kind == "select" and fld.options:
        return fld.options[0][0]
    return "" if value is None else str(value)


def _asks_custom(section: Mapping[str, Any]) -> str:
    confirm = section.get("confirm", ["*"])
    if confirm == ["*"]:
        return "Every tool asks first."
    if not confirm:
        return "Never asks."
    return "Asks first: " + ", ".join(confirm) + "."


def _not_started(running: Running) -> tuple[str, str]:
    """For one that's on in config.toml but isn't running: being started now, or switched on by hand since."""
    return ("starting", "Starting…") if running.applying else ("idle", "Not started yet: press Reconnect all")


def _empty_detail(integ_id: str) -> str:
    return {"news": "No feeds yet", "databases": "No databases yet"}.get(integ_id, "Not running")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    text = match.group(1) if match else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
