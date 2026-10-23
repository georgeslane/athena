"""What agents keep, in Athena's database: each one's settings and DAG.

An agent is a session (history.py), its conversation, with what's here besides: which model it uses, its
guidance, its DAG's file, what its last run kept for the next one, and how many runs it's had, to number the
next. The runs themselves, whether a DAG's paused, and what's proposed for it, are the engine's (agentic-airflow's
state store, in dags.db: dags/service.py and dags/proposals.py).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.memory import connect_db

MAX_GUIDANCE = 8000  # characters
MAX_CONTEXT = 12_000


@dataclass
class AgentConfig:
    """How an agent works: what it's kept besides its conversation."""

    id: int  # its session's
    model: str = ""  # "" for [llm] model
    guidance: str = ""  # how to act, at a high level, in its chat and its runs
    context: str = ""  # what its last run kept for the next
    context_at: float | None = None
    dag: str = ""  # its DAG's file: "" for none yet
    dag_at: float | None = None
    runs: int = 0  # how many runs it's started: the next is #runs + 1

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "guidance": self.guidance, "context": self.context, "context_at": self.context_at}


class AgentStore:
    """Agents' settings and DAGs."""

    def __init__(self, db_path: Path, clock: Any = time.time):
        self.clock = clock
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL DEFAULT '',
                    guidance TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    context_at REAL,
                    dag TEXT NOT NULL DEFAULT '',
                    dag_at REAL,
                    runs INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    # -- agents --------------------------------------------------------------------------------------

    _AGENT = "id, model, guidance, context, context_at, dag, dag_at, runs"

    def get(self, agent_id: int) -> AgentConfig:
        """An agent's settings: those of one that's never been changed, if it hasn't."""
        with self._lock:
            row = self._conn.execute(f"SELECT {self._AGENT} FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent(row) if row else AgentConfig(agent_id)

    def all(self) -> list[AgentConfig]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._AGENT} FROM agents ORDER BY id").fetchall()
        return [_agent(row) for row in rows]

    def update(self, agent_id: int, **values: Any) -> AgentConfig:
        """Change some of an agent's settings: model, guidance, context or dag."""
        allowed = {"model", "guidance", "context", "dag"}
        if unknown := set(values) - allowed:
            raise ValueError(f"Agents don't have {', '.join(sorted(unknown))}.")
        now = self.clock()
        if "dag" in values:
            values["dag_at"] = now
        if "context" in values:
            values["context_at"] = now
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO agents (id) VALUES (?)", (agent_id,))
            sets = ", ".join(f"{column} = ?" for column in values)
            self._conn.execute(f"UPDATE agents SET {sets} WHERE id = ?", (*values.values(), agent_id))
        return self.get(agent_id)

    def next_number(self, agent_id: int) -> int:
        """Count a new run of the agent's DAG: its number, #1, #2..."""
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO agents (id) VALUES (?)", (agent_id,))
            self._conn.execute("UPDATE agents SET runs = runs + 1 WHERE id = ?", (agent_id,))
            return int(self._conn.execute("SELECT runs FROM agents WHERE id = ?", (agent_id,)).fetchone()[0])

    def delete(self, agent_id: int) -> None:
        """Forget an agent's settings."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def forget(self) -> None:
        """Forget what runs kept, as forgetting everything does. The agents and DAGs stay."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("UPDATE agents SET context = '', context_at = NULL")

    def take_old_proposals(self) -> list[tuple[int, str, str, str, dict[str, Any], str]]:
        """Proposals still waiting from when they were kept here, before the engine's queue: (agent, "workflow" or
        "tools", why, the file, the tool lists, the file it was proposed against). They're forgotten here."""
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'dag_proposals'").fetchone():
                return []
            rows = self._conn.execute(
                "SELECT agent, kind, why, source, changes, base FROM dag_proposals WHERE state = 'pending' ORDER BY id"
            ).fetchall()
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("DROP TABLE dag_proposals")
        return [(agent, kind, why, file, json.loads(changes), base) for agent, kind, why, file, changes, base in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _agent(row: tuple[Any, ...]) -> AgentConfig:
    return AgentConfig(*row)
