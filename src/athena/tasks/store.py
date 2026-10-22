"""Tasks, kept in Athena's database."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.memory import connect_db


@dataclass
class Task:
    id: int
    title: str
    instructions: str
    schedule: str  # Schedule.text
    allowed_tools: list[str]  # tools it may use without asking you
    status: str  # active, paused, or done (a one-off that's run)
    created_by: str  # you, or athena
    created_at: float
    next_run: float | None = None  # Unix time
    last_run: float | None = None
    last_outcome: str = ""  # told you, nothing new, stopped, ran out of time, or failed: why
    last_result: str = ""  # what it said last time
    runs: int = 0
    failures: int = 0  # in a row, for this run
    last_steps: list[dict[str, str]] = field(default_factory=list)  # the tools it used last time (steps())


class TaskStore:
    """Tasks, in Athena's database."""

    def __init__(self, db_path: Path):
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    allowed_tools TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    next_run REAL,
                    last_run REAL,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_result TEXT NOT NULL DEFAULT '',
                    runs INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    last_steps TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            if "last_steps" not in {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN last_steps TEXT NOT NULL DEFAULT '[]'")

    _COLUMNS = (
        "id, title, instructions, schedule, allowed_tools, status, created_by, created_at, "
        "next_run, last_run, last_outcome, last_result, runs, failures, last_steps"
    )

    def _task(self, row: tuple[Any, ...]) -> Task:
        values = list(row)
        values[4], values[14] = json.loads(values[4]), json.loads(values[14])
        return Task(*values)

    def add(
        self, title: str, instructions: str, schedule: str, allowed_tools: list[str], created_by: str, next_run: float
    ) -> Task:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO tasks (title, instructions, schedule, allowed_tools, created_by, created_at, next_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, instructions, schedule, json.dumps(allowed_tools), created_by, time.time(), next_run),
            )
        task = self.get(int(cur.lastrowid))  # type: ignore[arg-type]
        assert task is not None
        return task

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._COLUMNS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def all(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._COLUMNS} FROM tasks ORDER BY id").fetchall()
        return [self._task(row) for row in rows]

    def due(self, now: float) -> Task | None:
        """The active task that's been due longest, if any is."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM tasks WHERE status = 'active' AND next_run <= ? "
                "ORDER BY next_run, id LIMIT 1",
                (now,),
            ).fetchone()
        return self._task(row) if row else None

    def next_due(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(next_run) FROM tasks WHERE status = 'active'").fetchone()
        return row[0] if row else None

    def save(self, task: Task) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET title = ?, instructions = ?, schedule = ?, allowed_tools = ?, status = ?, "
                "next_run = ?, last_run = ?, last_outcome = ?, last_result = ?, runs = ?, failures = ?, last_steps = ? "
                "WHERE id = ?",
                (
                    task.title,
                    task.instructions,
                    task.schedule,
                    json.dumps(task.allowed_tools),
                    task.status,
                    task.next_run,
                    task.last_run,
                    task.last_outcome,
                    task.last_result,
                    task.runs,
                    task.failures,
                    json.dumps(task.last_steps),
                    task.id,
                ),
            )

    def delete(self, task_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    def close(self) -> None:
        self._conn.close()
