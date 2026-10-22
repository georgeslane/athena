"""Editing config.toml and .env as the dashboard and `athena setup` do: changing only what changes, and keeping
your comments and layout, so the files stay the one place settings live and you can still edit them by hand."""

from __future__ import annotations

import errno
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import AoT, Comment, InlineTable, Table, Whitespace

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_VALUE_CHARS = 2000


class SettingsError(Exception):
    """A change that can't be made, with a message for the dashboard to show."""


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
            if not ENV_NAME.match(name):
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
    """Replace ``path`` with ``text`` in one step, so a crash never leaves it half-written.

    Where its folder can't be written to but it can, it's written in place, as when a container or systemd
    mounts the file itself.
    """
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(f"{path.name}.tmp")  # .env.tmp and config.toml.tmp are git-ignored
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM) or not os.access(path, os.W_OK):
            raise
        _write_in_place(path, text, mode)
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:  # systemd has mounted it in place, so it can't be replaced
                raise
            tmp.unlink()
            _write_in_place(path, text, mode)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_in_place(path: Path, text: str, mode: int) -> None:
    """Not in one step: a crash part way leaves it half-written, but it's small, so that's unlikely."""
    with path.open("r+b") as f:
        f.write(text.encode())
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, mode)


# -- config.toml ------------------------------------------------------------------------------


def table_at(doc: Container | Table, dotted: str, create: bool = False) -> Table | InlineTable | None:
    """The table at a dotted path such as "mcp_servers.github", made if ``create`` and it isn't there."""
    node: Any = doc
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if part not in node:
            if not create:
                return None
            last = i == len(parts) - 1
            # [mcp_servers] on its own would be an empty table: only its children get headers.
            set_key(node, part, tomlkit.table(is_super_table=not last))
        node = node[part]
        if not isinstance(node, (Table, InlineTable)):
            raise SettingsError(f"{'.'.join(parts[: i + 1])} in config.toml isn't a section.")
    return node


def set_key(table: Any, key: str, value: Any) -> None:
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


def set_path(section: Any, dotted: str, value: Any) -> None:
    """Set ``env.NAME`` and the like, making ``env`` (as an inline table) if it isn't there."""
    *parents, key = dotted.split(".")
    node = section
    for part in parents:
        if part not in node:
            set_key(node, part, tomlkit.inline_table())
        node = node[part]
    set_key(node, key, value)


def get_path(raw: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def update_map(table: Any, values: Mapping[str, str]) -> None:
    """Make a table hold exactly ``values``, changing it key by key so its comments stay."""
    for key in [k for k in table if k not in values]:
        del table[key]
    for key, value in values.items():
        if table.get(key) != value:
            set_key(table, key, value)


def remove_table(parent: Any, name: str) -> None:
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
