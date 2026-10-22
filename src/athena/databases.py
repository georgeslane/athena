"""SQLite databases you choose: the built-in `database_tables` and `database_query` tools.

Only the files listed under [sqlite.databases] in config.toml are ever opened, and only
for reading. Opening a file read-only isn't enough by itself: SQLite would still let a
query write a copy of the database anywhere (VACUUM INTO) or create files (ATTACH). So an
authorizer also refuses everything except reading, before any statement runs, and only
statements that start with SELECT, WITH or VALUES are tried at all. Queries that run too
long are stopped, and results are capped.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from pydantic import Field as Setting

from athena.builtin import FAIL, OK, Builtin, Report, Section
from athena.catalog import Field, Integration
from athena.tools import Tool, ToolError

if TYPE_CHECKING:
    from athena.config import Config

CELL_CHARS = 200
_READS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_STATEMENTS = ("select", "with", "values")
_WORD = re.compile(r"[A-Za-z]+")


class SQLiteConfig(Section):
    # SQLite databases the assistant can query, by name (see docs/tools.md). Only these
    # files are ever opened, and only for reading.
    enabled: bool = True
    databases: dict[str, str] = Setting(default_factory=dict)
    max_rows: int = 100
    timeout_seconds: float = 10.0  # a query running longer than this is stopped


class Databases(Builtin):
    section = "sqlite"
    Settings = SQLiteConfig
    entry = Integration(
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
    )

    @classmethod
    def wanted(cls, cfg: Config) -> bool:
        return cfg.sqlite.enabled and bool(cfg.sqlite.databases)

    @classmethod
    def start(cls, cfg: Config) -> Self:
        return cls(cfg.sqlite, {name: cfg.resolve(path) for name, path in cfg.sqlite.databases.items()})

    @classmethod
    async def check(cls, cfg: Config, report: Report) -> None:
        for name, path in cfg.sqlite.databases.items():
            file = cfg.resolve(path)
            try:
                tables = cls(cfg.sqlite, {name: file}).count_tables(name)
            except ToolError as exc:
                report(FAIL, f"{name}: {exc}")
            else:
                report(OK, f"{name}: {tables} table{'' if tables == 1 else 's'}, read-only ({file})")

    def __init__(self, cfg: SQLiteConfig, paths: dict[str, Path]):
        self.cfg = cfg
        self.paths = paths  # name -> file, already resolved against the config's folder

    def tools(self) -> list[Tool]:
        names = list(self.paths)
        listed = ", ".join(names)
        database = {"type": "string", "enum": names, "description": "Which database"}

        async def tables(args: dict[str, Any]) -> str:
            return await asyncio.to_thread(self.tables, self._name(args))

        async def query(args: dict[str, Any]) -> str:
            return await asyncio.to_thread(self.query, self._name(args), str(args.get("sql") or ""))

        return [
            Tool(
                name="database_tables",
                description=(
                    f"The tables in one of the user's SQLite databases ({listed}), with their columns and "
                    "how many rows each has. Look here before writing a query."
                ),
                parameters={"type": "object", "properties": {"database": database}, "required": ["database"]},
                handler=tables,
            ),
            Tool(
                name="database_query",
                description=(
                    f"Run one read-only SQL query (SELECT, WITH or VALUES) on one of the user's SQLite "
                    f"databases ({listed}). Returns at most {self.cfg.max_rows} rows."
                ),
                parameters={
                    "type": "object",
                    "properties": {"database": database, "sql": {"type": "string", "description": "SQLite SQL"}},
                    "required": ["database", "sql"],
                },
                handler=query,
            ),
        ]

    def _name(self, args: dict[str, Any]) -> str:
        name = str(args.get("database") or "")
        if not name and len(self.paths) == 1:
            name = next(iter(self.paths))  # small models sometimes leave out the only choice
        if name not in self.paths:
            raise ToolError(f"There's no database called {name!r}. The databases are: {', '.join(self.paths)}.")
        return name

    # -- the work, run in a thread ---------------------------------------------------------------

    def tables(self, name: str) -> str:
        with self._open(name) as conn:
            found = conn.execute(
                "SELECT type, name, sql FROM sqlite_schema"
                " WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            lines = []
            for kind, table, sql in found:
                rows = ""
                if kind == "table":
                    (count,) = conn.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()
                    rows = f"  -- {count:,} rows"
                lines.append(f"{sql};{rows}")
        if not lines:
            return f"{name} has no tables."
        return f"{name} ({self.paths[name]}):\n\n" + "\n\n".join(lines)

    def count_tables(self, name: str) -> int:
        """Opens the database the way the tools do, for `doctor`."""
        with self._open(name) as conn:
            (count,) = conn.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
        return count

    def query(self, name: str, sql: str) -> str:
        if _first_word(sql) not in _STATEMENTS:
            raise ToolError("Only reading is allowed: the query must start with SELECT, WITH or VALUES.")
        limit = self.cfg.max_rows
        with self._open(name) as conn:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(limit + 1)
            columns = [c[0] for c in cursor.description or []]
        if not columns:
            return "The query returned nothing."
        if not rows:
            return f"No rows. Columns: {' | '.join(columns)}"
        more = len(rows) > limit
        rows = rows[:limit]
        head = f"First {limit} rows (there are more: narrow the query or add a LIMIT)" if more else f"{len(rows)} rows"
        table = [" | ".join(columns), *(" | ".join(_cell(v) for v in row) for row in rows)]
        return f"{head}:\n" + "\n".join(table)

    def _open(self, name: str) -> _ReadOnly:
        path = self.paths[name]
        if not path.is_file():
            raise ToolError(f"There's no database file at {path}.")
        return _ReadOnly(path, self.cfg.timeout_seconds)


class _ReadOnly:
    """A connection to ``path`` that can only read, closed afterwards. SQLite errors become ToolErrors."""

    def __init__(self, path: Path, timeout: float):
        self.path, self.timeout = path, timeout

    def __enter__(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            raise ToolError(f"SQLite couldn't open {self.path}: {exc}.") from None
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000_000)  # so randomblob(1e9) can't fill the Pi's memory
            conn.set_authorizer(_authorize)
            deadline = time.monotonic() + self.timeout
            conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        except BaseException:
            conn.close()
            raise
        self.conn = conn
        return conn

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        self.conn.close()
        if isinstance(exc, sqlite3.ProgrammingError) and "one statement" in str(exc):
            raise ToolError("Run one statement at a time.") from None
        if isinstance(exc, sqlite3.OperationalError) and str(exc) == "interrupted":
            raise ToolError(
                f"The query took longer than {self.timeout:.0f}s, so it was stopped. Narrow it, or add a LIMIT."
            ) from None
        if isinstance(exc, sqlite3.DatabaseError):
            hint = " Only reading is allowed." if "not authorized" in str(exc) else ""
            raise ToolError(f"SQLite said: {exc}.{hint}") from None


def _authorize(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READS else sqlite3.SQLITE_DENY


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _first_word(sql: str) -> str:
    """The statement's first keyword, after any comments."""
    rest = sql
    while True:
        rest = rest.lstrip()
        if rest.startswith("--"):
            rest = rest.partition("\n")[2]
        elif rest.startswith("/*"):
            end = rest.find("*/", 2)
            rest = rest[end + 2 :] if end >= 0 else ""
        else:
            break
    match = _WORD.match(rest)
    return match.group(0).lower() if match else ""


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return f"<{len(value):,} bytes>"
    text = " ".join(str(value).split()) if isinstance(value, str) else str(value)
    return text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"
