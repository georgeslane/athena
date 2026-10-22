"""What the dashboard's Tools tab can change: which tools Athena has, their settings and their keys.

Settings are written to config.toml, keeping your comments and layout (config_files.py). Keys and passwords
go in .env and are never sent back to the browser: the dashboard only learns whether each is set.

CATALOG describes the tools Athena knows about: the built-in ones (integrations.py), and the MCP servers it
recommends (recommended.py). Any other MCP server in config.toml is shown too, with its command or URL, and
can be added and removed from the dashboard.
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

from athena.catalog import Field, Integration, Secret
from athena.config import Config, ConfigError, parse_config
from athena.config_files import (
    ENV_NAME,
    MAX_VALUE_CHARS,
    EnvFile,
    SettingsError,
    get_path,
    remove_table,
    set_key,
    set_path,
    table_at,
    update_map,
    write_private,
)
from athena.integrations import BUILTINS
from athena.recommended import RECOMMENDED
from athena.tools import matches_any

# Athena's own secrets. The dashboard can't change them or pass them to a server.
PROTECTED_SECRETS = {"TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "SIRI_TOKEN", "DASHBOARD_TOKEN"}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


CATALOG: tuple[Integration, ...] = (*(builtin.entry for builtin in BUILTINS), *RECOMMENDED)
BY_ID = {i.id: i for i in CATALOG}
# Settings of recommended servers that came after some had been set up, which those are given (upgrade).
ADDED_LATER = ("arguments", "about")
# Settings that no longer exist, taken out (upgrade): a config.toml with one wouldn't load. Checking what was
# watched every hour is what the master agent's DAG does now.
REMOVED = ("display.led", "tasks.heartbeat_minutes", "tasks.heartbeat_tools")
# Settings left at a default that no longer works, taken out so the new default applies (upgrade): the system
# prompt was in the git checkout Athena ran from, and comes with Athena now.
OLD_DEFAULTS = {"agent.system_prompt_file": "prompts/system.md"}

# The settings of an MCP server that isn't in the catalog.
CUSTOM_FIELDS = (
    Field("command", "Command", help="The program that runs it, such as uvx, npx or ssh."),
    Field("args", "Arguments", "lines", "One per line."),
    Field("url", "URL", help="For a server on the internet, instead of a command."),
    Field("about", "What it's for", help="A line for Athena, if its tools don't say, such as whose account it uses."),
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


# -- what the dashboard shows -----------------------------------------------------------------


@dataclass
class Running:
    """What's running now, which the config file can't say."""

    # For each MCP server: (connected, error, the tools it offers as (name, description)), if it's been tried.
    servers: dict[str, tuple[bool, str | None, list[tuple[str, str]]]] = field(default_factory=dict)
    # For each built-in: its tools that are in use, and why it isn't running if it should be.
    builtins: dict[str, tuple[list[str], str | None]] = field(default_factory=dict)
    applying: bool = False  # changes are being applied
    # For each MCP server: its tools that are new or changed since you approved them, as the page shows them.
    held: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # For each MCP server whose dependencies have been checked: what was found (server_security.py).
    security: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        section = get_path(raw, integ.section, {}) or {}
        tools, problem = running.builtins.get(integ.id, ([], None))
        enabled = bool(section.get("enabled", Config.model_fields[integ.section].default_factory().enabled))
        entry = self._entry(integ, integ.id, enabled, section, custom=False)
        if not enabled:
            entry["state"], entry["detail"] = "off", "Off"
        elif problem:
            entry["state"], entry["detail"] = "error", problem
        elif tools:
            entry["state"], entry["detail"] = "on", _count(len(tools), "tool")
        elif empty := next((f for f in integ.fields if f.kind == "map" and not section.get(f.key)), None):
            entry["state"], entry["detail"] = "idle", f"No {empty.label.lower()} yet"  # such as no news feeds
        else:
            entry["state"], entry["detail"] = _not_started(running)  # changed in config.toml since it started
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
        held = running.held.get(name, []) if connected and enabled else []
        entry["held"] = held
        security = running.security.get(name) if enabled else None
        entry["security"] = security
        vulnerable = [f["package"] for f in security["findings"]] if security and security["clean"] is False else []
        if not enabled:
            entry["state"], entry["detail"] = "off", "Off" if added else "Not set up yet"
        elif error:
            entry["state"], entry["detail"] = "error", error
        elif connected and held:
            entry["state"], entry["detail"] = "attention", f"{_count(len(held), 'tool')} changed: waiting for you"
        elif vulnerable:
            entry["state"], entry["detail"] = "attention", f"Known vulnerability in {', '.join(vulnerable)}"
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
        section = table_at(doc, section_path, create=True)
        assert section is not None

        # Secrets first: switching on below checks they're set.
        env_changes: dict[str, str | None] = {}
        for secret_name, value in (change.get("secrets") or {}).items():
            secret = self._find_secret(integ, raw, name, secret_name)
            if value is not None and not isinstance(value, str):
                raise SettingsError("A key or password must be text.")
            env_changes[secret_name] = (value or "").strip() or None
            if secret is not None and secret.path and secret.name not in str(get_path(section, secret.path, "")):
                set_path(section, secret.path, secret.template)  # so the config uses it

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
            set_key(section, "enabled", change["enabled"])

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
                set_key(target, key, tomlkit.inline_table() if custom else tomlkit.table())
            update_map(target[key], values)
        elif fld.kind == "lines":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise SettingsError(f"{fld.label} must be a list of text.")
            lines = [v.strip() for v in value if v.strip()]
            _check_refs(lines, fld.label)
            set_path(section, fld.key, lines)
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
            set_path(section, fld.key, value)

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
            value = get_path(section, fld.key)
            if fld.required and (not value or (fld.example and value == get_path(defaults, fld.key))):
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
        if about := " ".join(str(spec.get("about") or "").split()):
            table["about"] = about[:300]
        _check_refs([str(v) for v in table.get("args", [])] + [url], "The server")
        _add_server_table(doc, name, table, "Added from the dashboard.")
        return name, self._save(doc, env_changes)

    def set_values(self, values: Mapping[str, Any]) -> Config:
        """Set settings such as {"embeddings.model": "x"}, adding their section if it isn't there."""
        doc, _ = self._load()
        for dotted, value in values.items():
            table, _, key = dotted.rpartition(".")
            section = table_at(doc, table, create=True)
            assert section is not None
            set_key(section, key, value)
        return self._save(doc, {})

    def set_on_demand(self, names: Iterable[str], known: Iterable[str]) -> Config:
        """Choose the tools found when they're needed (tool_search.py), by name, from those ``known``. Patterns
        written in config.toml by hand, such as "trading212_*", are kept, and so are the tools they name."""
        doc, raw = self._load()
        names, known = list(dict.fromkeys(names)), set(known)
        if unknown := [n for n in names if n not in known]:
            raise SettingsError(f"There's no tool called {', '.join(unknown)}.")
        patterns = [p for p in raw.get("tools", {}).get("on_demand", []) if hand_written(p)]
        if kept := [(t, p) for t in sorted(known - set(names)) for p in patterns if matches_any(t, [p])]:
            tool, pattern = kept[0]
            raise SettingsError(f"{tool} is found when needed because of “{pattern}” in config.toml: change it there.")
        section = table_at(doc, "tools", create=True)
        assert section is not None
        set_key(section, "on_demand", patterns + [n for n in names if not matches_any(n, patterns)])
        return self._save(doc, {})

    def upgrade(self) -> list[str]:
        """Bring a config.toml from an older Athena up to date, before it's loaded: take out settings that no longer
        exist (REMOVED), and give recommended servers set up before a setting of theirs came in (ADDED_LATER) that
        setting, as one set up now would have, such as the email server's account. Returns what changed."""
        if not self.editable:
            return []
        doc, raw = self._load()
        servers = raw.get("mcp_servers", {})
        changed = []
        stale = [d for d, old in OLD_DEFAULTS.items() if get_path(raw, d) == old]
        for dotted in [*REMOVED, *stale]:
            table, _, key = dotted.rpartition(".")
            if key in (get_path(raw, table) or {}):
                del table_at(doc, table)[key]  # type: ignore[index]
                changed.append(f"removed {dotted}")
        for integ in CATALOG:
            if integ.builtin or integ.id not in servers:
                continue
            missing = [k for k in ADDED_LATER if k in integ.defaults and k not in servers[integ.id]]
            if not missing:
                continue
            section = table_at(doc, integ.section)
            for key in missing:
                value = copy.deepcopy(integ.defaults[key])
                if isinstance(value, dict):
                    value, plain = tomlkit.inline_table(), value
                    value.update(plain)
                set_key(section, key, value)
            changed.append(f"gave {integ.id} {', '.join(missing)}")
        if changed:
            self._save(doc, {})
        return changed

    def remove_server(self, name: str) -> Config:
        doc, raw = self._load()
        if name in BY_ID:
            raise SettingsError(f"{BY_ID[name].title} can be switched off, but not removed.")
        if name not in raw.get("mcp_servers", {}):
            raise SettingsError(f"There's no server called {name!r}.")
        remove_table(doc["mcp_servers"], name)
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
            before = self.path.read_text()
            try:
                write_private(self.path.with_name(f"{self.path.name}.bak"), before)
            except OSError:  # config.toml's folder can't be written to: Athena's data folder can
                write_private(cfg.resolve(cfg.data_dir) / f"{self.path.name}.bak", before)
            write_private(self.path, text)
        return cfg


def _add_server_table(doc: tomlkit.TOMLDocument, name: str, values: Mapping[str, Any], comment: str) -> None:
    servers = table_at(doc, "mcp_servers", create=True)
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
    pattern, kind = (ENV_NAME, "an environment variable") if where == "env" else (_HEADER_NAME, "a header")
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
            set_key(section, key, value)
        elif key in section:
            del section[key]  # left out: include all of them, exclude none, and ask for every one


def _flag(choice: Any, key: str) -> bool:
    if not isinstance(choice, dict) or not isinstance(choice.get(key), bool):
        raise SettingsError("Each tool needs on and asks, as true or false.")
    return choice[key]


def hand_written(pattern: str) -> bool:
    """A pattern of [tools] on_demand that names tools by a pattern, as only config.toml can, not one by name."""
    return any(c in pattern for c in "*?[")


def _tool_on(tool: str, section: Mapping[str, Any]) -> bool:
    include, exclude = section.get("include") or [], section.get("exclude") or []
    return (not include or matches_any(tool, include)) and not matches_any(tool, exclude)


def _field_value(fld: Field, section: Mapping[str, Any], integ: Integration | None) -> Any:
    value = get_path(section, fld.key)
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


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    text = match.group(1) if match else text
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
