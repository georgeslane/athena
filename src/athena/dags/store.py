"""What agents keep, in Athena's database: each one's settings and DAG, and what its runs propose.

An agent is a session (history.py), its conversation, with what's here besides: which model it uses, its
guidance, its DAG's file, what its last run kept for the next one, and how many runs it's had, to number the
next. The runs themselves, and whether a DAG's paused, are the engine's (agentic-airflow's state store, in
dags.db: dags/service.py).
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
PENDING, APPROVED, REJECTED, STALE = "pending", "approved", "rejected", "stale"


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


@dataclass
class Proposal:
    """A change a run's review proposes, for you to approve or reject: a new DAG file ("workflow"), or which tools
    to keep out of the prompt and which may run without asking, in the DAG or one of its tasks ("tools")."""

    id: int
    agent: int
    run: str | None  # the engine's run id
    kind: str  # workflow or tools
    why: str
    source: str  # workflow: the whole new file
    # tools: the lists it sets, whole: {"hidden_tools": [...], "allowed_tools": {where: [...]}}, where "dag" is the
    # DAG's own, else a task's id (reader.with_tools).
    changes: dict[str, Any]
    base: str  # the file it was proposed against
    state: str
    created: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "run": self.run,
            "kind": self.kind,
            "why": self.why,
            "source": self.source,
            "changes": self.changes,
            "state": self.state,
            "created": self.created,
        }


class AgentStore:
    """Agents' settings and DAGs, and what their runs propose."""

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
                CREATE TABLE IF NOT EXISTS dag_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent INTEGER NOT NULL,
                    run TEXT,
                    kind TEXT NOT NULL,
                    why TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    changes TEXT NOT NULL DEFAULT '{}',
                    base TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL,
                    decided REAL
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
        """Forget an agent: its settings and proposals."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            self._conn.execute("DELETE FROM dag_proposals WHERE agent = ?", (agent_id,))
            self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def forget(self) -> None:
        """Forget what runs proposed and kept, as forgetting everything does. The agents and DAGs stay."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("DELETE FROM dag_proposals")  # why each was proposed can say what was found
            self._conn.execute("UPDATE agents SET context = '', context_at = NULL")

    # -- proposals -----------------------------------------------------------------------------------

    _PROPOSAL = "id, agent, run, kind, why, source, changes, base, state, created"

    def propose(
        self,
        agent_id: int,
        run_id: str | None,
        kind: str,
        why: str,
        base: str,
        *,
        source: str = "",
        changes: dict[str, Any] | None = None,
    ) -> Proposal:
        """A change for you to approve or reject. An agent's earlier proposal of the same kind that's still
        waiting is out of date now."""
        now = self.clock()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_proposals SET state = ?, decided = ? WHERE agent = ? AND kind = ? AND state = ?",
                (STALE, now, agent_id, kind, PENDING),
            )
            cur = self._conn.execute(
                "INSERT INTO dag_proposals (agent, run, kind, why, source, changes, base, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, run_id, kind, why, source, json.dumps(changes or {}), base, now),
            )
        return self.proposal(int(cur.lastrowid))  # type: ignore[arg-type,return-value]

    def proposal(self, proposal_id: int) -> Proposal | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._PROPOSAL} FROM dag_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return _proposal(row) if row else None

    def proposals(self, agent_id: int | None = None, run_id: str | None = None) -> list[Proposal]:
        """Proposals waiting for you: an agent's, or a run's (whatever became of them), or everyone's."""
        where, args = ("state = ?", [PENDING]) if run_id is None else ("run = ?", [run_id])
        if agent_id is not None:
            where, args = f"{where} AND agent = ?", [*args, agent_id]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._PROPOSAL} FROM dag_proposals WHERE {where} ORDER BY id", args
            ).fetchall()
        return [_proposal(row) for row in rows]

    def decide(self, proposal: Proposal, state: str) -> None:
        """Note what became of a proposal: approved, rejected or stale."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_proposals SET state = ?, decided = ? WHERE id = ?", (state, self.clock(), proposal.id)
            )
        proposal.state = state

    def rebase(self, proposal: Proposal, source: str, base: str) -> None:
        """A proposal still waiting, against the DAG as it's changed since."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_proposals SET source = ?, base = ? WHERE id = ?", (source, base, proposal.id)
            )
        proposal.source, proposal.base = source, base

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _agent(row: tuple[Any, ...]) -> AgentConfig:
    return AgentConfig(*row)


def _proposal(row: tuple[Any, ...]) -> Proposal:
    values = list(row)
    values[6] = json.loads(values[6])
    return Proposal(*values)
