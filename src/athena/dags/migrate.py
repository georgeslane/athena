"""Upgrades to what's in Athena's database that are made once, and noted in athena_upgrades so they aren't again.

agentic-airflow: agents' DAGs became agentic-airflow's, run by its engine, which keeps their runs in dags.db. The
tables of runs, and whether each DAG was paused and when it was next due, which are the engine's now, are
dropped, and each agent's runs are counted afresh. A DAG file written before is kept, but won't read as the new
DAGs are written (docs/dags.md), so it doesn't run until it's written again: its agent's DAG view says why, and
what its runs proposed is out of date.

agents-v2: agents became what they are now: a master agent, which Telegram, Siri and the terminal talk to, and
DAGs whose tasks run for each item of a list, branch on a yes or no, and start each other's with outlets, which
replaced what was watched. You asked to start afresh with it, so what was said to Athena before, here and in
memory, with its photos, and the agents, their DAGs and runs, and what was watched, are deleted. Memories you
saved, skills, documents, tools, files and usage statistics stay. Messages and agents are deleted rather than
their tables, so their ids carry on from where they were, as usage statistics and memory count on.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

from athena.memory import connect_db

if TYPE_CHECKING:
    from athena.memory import MemoryStore

log = logging.getLogger(__name__)

AFRESH = "agents-v2"
ENGINE = "agentic-airflow"
EMPTIED = ("messages", "chat_state", "sessions")  # what was said, and the agents' conversations
DROPPED = ("agents", "dag_runs", "dag_task_runs", "dag_proposals", "watches", "tasks", "tasks_before_dags", "timeline")


def start_afresh(db_path: Path, memory: MemoryStore, photos: Path) -> bool:
    """Make the agents-v2 upgrade, if it hasn't been made: before anything else opens the database's agents and
    conversations. True if it's made now."""
    conn = connect_db(db_path)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS athena_upgrades (name TEXT PRIMARY KEY, made REAL NOT NULL)")
        if conn.execute("SELECT 1 FROM athena_upgrades WHERE name = ?", (AFRESH,)).fetchone():
            return False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
        with conn:
            for table in EMPTIED:
                if table in tables:
                    conn.execute(f"DELETE FROM {table}")
            if "sessions" in tables and "master" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
                conn.execute("ALTER TABLE sessions ADD COLUMN master INTEGER NOT NULL DEFAULT 0")
            for table in DROPPED:
                if table in tables:
                    conn.execute(f"DROP TABLE {table}")
            conn.execute("INSERT INTO athena_upgrades VALUES (?, ?)", (AFRESH, time.time()))
    finally:
        conn.close()
    forgotten = memory.forget_conversations()
    shutil.rmtree(photos, ignore_errors=True)  # every one was in a message
    if tables & {*EMPTIED, *DROPPED}:
        log.info("Started afresh: deleted the conversations, %d of them in memory, and the agents", forgotten)
    return True


def upgrade(db_path: Path) -> bool:
    """Make the agentic-airflow upgrade, if it hasn't been made: before the agents are read. True if it's made now."""
    conn = connect_db(db_path)
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS athena_upgrades (name TEXT PRIMARY KEY, made REAL NOT NULL)")
        if conn.execute("SELECT 1 FROM athena_upgrades WHERE name = ?", (ENGINE,)).fetchone():
            return False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        with conn:
            if "agents" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
                for column in ("paused", "next_run"):
                    if column in columns:
                        with contextlib.suppress(sqlite3.OperationalError):  # SQLite before 3.35: they stay, unread
                            conn.execute(f"ALTER TABLE agents DROP COLUMN {column}")
                if "runs" not in columns:
                    conn.execute("ALTER TABLE agents ADD COLUMN runs INTEGER NOT NULL DEFAULT 0")
            for table in ("dag_runs", "dag_task_runs"):
                if table in tables:
                    conn.execute(f"DROP TABLE {table}")
            if "dag_proposals" in tables:  # proposed against DAGs written as they were
                conn.execute("UPDATE dag_proposals SET state = 'stale' WHERE state = 'pending'")
            conn.execute("INSERT INTO athena_upgrades VALUES (?, ?)", (ENGINE, time.time()))
    finally:
        conn.close()
    if tables & {"dag_runs", "dag_task_runs"}:
        log.info("Agents' DAGs are agentic-airflow's now: their runs are kept in dags.db")
    return True
