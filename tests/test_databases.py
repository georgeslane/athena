"""The built-in SQLite tools, on real database files."""

import sqlite3
from pathlib import Path

import pytest

from pi_assistant.app import build_services
from pi_assistant.config import SQLiteConfig
from pi_assistant.databases import Databases, _first_word, _ReadOnly
from pi_assistant.tools import ToolError


def make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        '''
        CREATE TABLE expenses (id INTEGER PRIMARY KEY, day TEXT, amount REAL, note TEXT, receipt BLOB);
        INSERT INTO expenses (day, amount, note, receipt) VALUES
            ('2026-10-01', 12.5, 'lunch', NULL),
            ('2026-10-02', 3.2, 'coffee
            with a newline', x'00ff'),
            ('2026-10-03', 40, NULL, NULL);
        CREATE INDEX by_day ON expenses (day);
        CREATE TABLE "odd ""name""" (x);
        CREATE VIEW big_spends AS SELECT * FROM expenses WHERE amount > 10;
        '''
    )
    conn.commit()
    conn.close()
    return path


def snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


@pytest.fixture
def db(tmp_path: Path) -> Databases:
    folder = tmp_path / "dbs"
    folder.mkdir()
    return Databases(SQLiteConfig(max_rows=100, timeout_seconds=2), {"budget": make_db(folder / "budget.db")})


async def call(dbs: Databases, tool: str, **args) -> str:
    (found,) = [t for t in dbs.tools() if t.name == tool]
    return await found.handler(args)


async def test_shows_tables_columns_and_row_counts(db):
    out = await call(db, "database_tables", database="budget")
    assert "CREATE TABLE expenses (id INTEGER PRIMARY KEY, day TEXT" in out and "-- 3 rows" in out
    assert 'CREATE TABLE "odd ""name""" (x);  -- 0 rows' in out
    assert "CREATE VIEW big_spends" in out
    assert "sqlite_" not in out


async def test_answers_a_query(db):
    out = await call(db, "database_query", database="budget", sql="SELECT day, amount, note, receipt FROM expenses")
    assert out.splitlines() == [
        "3 rows:",
        "day | amount | note | receipt",
        "2026-10-01 | 12.5 | lunch | NULL",
        "2026-10-02 | 3.2 | coffee with a newline | <2 bytes>",
        "2026-10-03 | 40.0 | NULL | NULL",
    ]
    assert await call(db, "database_query", database="budget", sql="select * from big_spends where 0") == (
        "No rows. Columns: id | day | amount | note | receipt"
    )
    # Comments first, a trailing semicolon, WITH and VALUES are all fine.
    sql = "-- how much?\n/* all of it */ WITH t AS (SELECT sum(amount) AS total FROM expenses) SELECT total FROM t;"
    assert (await call(db, "database_query", database="budget", sql=sql)).splitlines()[-1] == "55.7"
    assert (await call(db, "database_query", database="budget", sql="values (1, 'a')")).endswith("1 | a")


async def test_caps_rows_and_long_values(db):
    db.cfg.max_rows = 2
    out = await call(db, "database_query", database="budget", sql="SELECT day FROM expenses")
    assert out.splitlines() == [
        "First 2 rows (there are more: narrow the query or add a LIMIT):",
        "day",
        *["2026-10-01", "2026-10-02"],
    ]
    long = await call(db, "database_query", database="budget", sql="SELECT printf('%.500c', 'x')")
    assert long.splitlines()[-1] == "x" * 199 + "…"


WRITES = [
    "INSERT INTO expenses (amount) VALUES (1)",
    "UPDATE expenses SET amount = 0",
    "DELETE FROM expenses",
    "REPLACE INTO expenses (id, amount) VALUES (1, 0)",
    "DROP TABLE expenses",
    "CREATE TABLE more (x)",
    "CREATE TEMP TABLE scratch (x)",
    "ALTER TABLE expenses ADD COLUMN y",
    "CREATE INDEX by_amount ON expenses (amount)",
    "REINDEX",
    "ANALYZE",
    "VACUUM",
    "VACUUM INTO '{folder}/copy.db'",
    "ATTACH DATABASE '{folder}/new.db' AS other",
    "PRAGMA journal_mode = WAL",
    "PRAGMA query_only = OFF",
    "PRAGMA user_version = 7",
    "BEGIN",
    "SAVEPOINT a",
]


@pytest.mark.parametrize(
    "sql",
    [
        *WRITES,
        "WITH x AS (SELECT 1) DELETE FROM expenses",
        "SELECT 1; DELETE FROM expenses",
        "/* SELECT */ DELETE FROM expenses",
        "-- SELECT\nDROP TABLE expenses",
        "INSERT INTO expenses (amount) VALUES (1) RETURNING id",
        "",
    ],
)
async def test_the_tool_never_writes(db, sql):
    folder = db.paths["budget"].parent
    before = snapshot(folder)
    with pytest.raises(ToolError):
        await call(db, "database_query", database="budget", sql=sql.format(folder=folder))
    assert snapshot(folder) == before  # nothing changed, and no files were made


@pytest.mark.parametrize("sql", [*WRITES, "WITH x AS (SELECT 1) DELETE FROM expenses"])
def test_the_connection_refuses_writes_without_the_statement_check(db, sql):
    """The check on the first word is one guard; the read-only connection is another, on its own."""
    folder = db.paths["budget"].parent
    before = snapshot(folder)
    with pytest.raises(ToolError), _ReadOnly(db.paths["budget"], 2) as conn:
        conn.execute(sql.format(folder=folder)).fetchall()
    assert snapshot(folder) == before


def test_finds_the_first_word_after_comments():
    assert _first_word("  -- a\n /* b */ SeLeCt 1") == "select"
    assert _first_word("/* unclosed SELECT") == ""
    assert _first_word("(select 1)") == ""


async def test_stops_long_queries_and_huge_values(db):
    db.cfg.timeout_seconds = 0.2
    forever = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    with pytest.raises(ToolError, match="longer than 0s"):
        await call(db, "database_query", database="budget", sql=forever)
    db.cfg.timeout_seconds = 5
    with pytest.raises(ToolError, match="too big"):
        await call(db, "database_query", database="budget", sql="SELECT randomblob(100000000)")


async def test_reads_a_database_another_program_is_writing(tmp_path):
    path = tmp_path / "live.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE t (x)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    writer.execute("INSERT INTO t VALUES (2)")  # not committed yet
    dbs = Databases(SQLiteConfig(), {"live": path})
    try:
        assert (await call(dbs, "database_query", database="live", sql="SELECT x FROM t")).splitlines() == [
            "1 rows:",
            "x",
            "1",
        ]
    finally:
        writer.close()  # also removes the -wal and -shm files
    assert "2 rows" not in await call(dbs, "database_query", database="live", sql="SELECT x FROM t")
    assert "1 rows" in await call(dbs, "database_query", database="live", sql="SELECT x FROM t")


async def test_explains_what_it_cant_open(tmp_path, db):
    with pytest.raises(ToolError, match="no database called 'nope'. The databases are: budget"):
        await call(db, "database_query", database="nope", sql="SELECT 1")
    # With only one database, leaving it out means that one.
    assert (await call(db, "database_query", sql="SELECT 2")).endswith("2")

    missing = Databases(SQLiteConfig(), {"gone": tmp_path / "gone.db"})
    with pytest.raises(ToolError, match="no database file at"):
        await call(missing, "database_tables", database="gone")
    assert not (tmp_path / "gone.db").exists()

    (tmp_path / "notes.txt").write_text("not a database " * 100)
    text = Databases(SQLiteConfig(), {"notes": tmp_path / "notes.txt"})
    with pytest.raises(ToolError, match="not a database"):
        await call(text, "database_query", database="notes", sql="SELECT 1 FROM x")


def test_tools_exist_only_when_databases_are_listed(config, tmp_path):
    services = build_services(config)
    assert not {"database_tables", "database_query"} & {t.name for t in services.tools.all()}
    services.memory.store.close()
    services.history.close()

    config.sqlite.databases = {"budget": "budget.db"}  # relative to the config's folder
    services = build_services(config)
    tools = {t.name: t for t in services.tools.all()}
    assert not tools["database_query"].needs_confirmation  # local and read-only
    assert services.databases.paths == {"budget": tmp_path / "budget.db"}
    services.memory.store.close()
    services.history.close()
