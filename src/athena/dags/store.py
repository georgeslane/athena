"""What agents keep, in Athena's database: each one's settings and DAG, its last runs, and what the runs propose.

An agent is a session (history.py), its conversation, with what's here besides: which model it uses, its
guidance, its DAG, and what its last run kept for the next one. Each run keeps what every task said and did, and
for a task that ran once for each item of a list, each of those runs, with its item. Only the last KEEP_RUNS of
each agent's runs are kept.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.memory import connect_db

KEEP_RUNS = 10  # of each agent's
MAX_GUIDANCE = 8000  # characters
MAX_CONTEXT = 12_000

# How a run, or a task in one, went.
QUEUED, RUNNING, SUCCESS, FAILED, SKIPPED, UPSTREAM_FAILED, STOPPED = (
    "queued",
    "running",
    "success",
    "failed",
    "skipped",
    "upstream_failed",
    "stopped",
)
DONE = frozenset({SUCCESS, FAILED, SKIPPED, UPSTREAM_FAILED, STOPPED})
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
    paused: bool = False  # its DAG doesn't run by itself
    next_run: float | None = None  # when its schedule's next due

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "guidance": self.guidance,
            "context": self.context,
            "context_at": self.context_at,
            "paused": self.paused,
            "next_run": self.next_run if not self.paused else None,
        }


@dataclass
class TaskRun:
    id: int
    task: str  # its task_id
    index: int  # which item it ran for, of a task that runs for each: 0, 1, ...
    state: str
    item: str = ""  # the item it ran for, if it ran for each
    source: str = ""  # what gave the item: the task, or "trigger"
    started: float | None = None
    finished: float | None = None
    output: str = ""
    steps: list[dict[str, str]] = field(default_factory=list)  # every tool it used (steps.steps())
    value: Any = None  # what it returned: a list of strings, or True or False, for a task that returns one

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "index": self.index,
            "state": self.state,
            "item": self.item,
            "source": self.source,
            "started": self.started,
            "finished": self.finished,
            "output": self.output,
            "steps": self.steps,
            "value": self.value,
        }


@dataclass
class Run:
    id: int
    agent: int
    number: int  # the agent's run #1, #2...
    trigger: str  # "schedule", "manual" or "event:<name>"
    input: str  # what came with it
    source: str  # the DAG's file, as it was
    state: str
    started: float | None
    finished: float | None
    summary: str = ""
    items: list[str] = field(default_factory=list)  # the items that came with it, for for_each=trigger
    tasks: list[TaskRun] = field(default_factory=list)

    def as_dict(self, tasks: bool = True) -> dict[str, Any]:
        out = {
            "id": self.id,
            "agent": self.agent,
            "number": self.number,
            "trigger": self.trigger,
            "input": self.input,
            "items": self.items,
            "state": self.state,
            "started": self.started,
            "finished": self.finished,
            "summary": self.summary,
        }
        if tasks:
            out["source"] = self.source
            out["tasks"] = [t.as_dict() for t in self.tasks]
        return out


@dataclass
class Proposal:
    """A change a run's review proposes, for you to approve or reject: a new DAG file ("workflow"), or which tools
    to keep out of the prompt and which may run without asking, in the DAG or one of its tasks ("tools")."""

    id: int
    agent: int
    run: int | None
    kind: str  # workflow or tools
    why: str
    source: str  # workflow: the whole new file
    # tools: {"hide": [tools], "show": [tools], "allow": {where: [tools]}, "ask": {where: [tools]}}, where "dag" is
    # where's the DAG's own allowed_tools, else a task's id.
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
    """Agents' settings, DAGs, runs and proposals."""

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
                    paused INTEGER NOT NULL DEFAULT 0,
                    next_run REAL
                );
                CREATE TABLE IF NOT EXISTS dag_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent INTEGER NOT NULL,
                    number INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    input TEXT NOT NULL DEFAULT '',
                    items TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started REAL,
                    finished REAL,
                    summary TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS dag_runs_agent ON dag_runs(agent, id);
                CREATE TABLE IF NOT EXISTS dag_task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run INTEGER NOT NULL,
                    task TEXT NOT NULL,
                    idx INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    item TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    started REAL,
                    finished REAL,
                    output TEXT NOT NULL DEFAULT '',
                    steps TEXT NOT NULL DEFAULT '[]',
                    value TEXT
                );
                CREATE INDEX IF NOT EXISTS dag_task_runs_run ON dag_task_runs(run, id);
                CREATE TABLE IF NOT EXISTS dag_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent INTEGER NOT NULL,
                    run INTEGER,
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

    _AGENT = "id, model, guidance, context, context_at, dag, dag_at, paused, next_run"

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
        """Change some of an agent's settings: model, guidance, context, dag, paused or next_run."""
        allowed = {"model", "guidance", "context", "dag", "paused", "next_run"}
        if unknown := set(values) - allowed:
            raise ValueError(f"Agents don't have {', '.join(sorted(unknown))}.")
        now = self.clock()
        if "dag" in values:
            values["dag_at"] = now
        if "context" in values:
            values["context_at"] = now
        if "paused" in values:
            values["paused"] = int(bool(values["paused"]))
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO agents (id) VALUES (?)", (agent_id,))
            sets = ", ".join(f"{column} = ?" for column in values)
            self._conn.execute(f"UPDATE agents SET {sets} WHERE id = ?", (*values.values(), agent_id))
        return self.get(agent_id)

    def delete(self, agent_id: int) -> None:
        """Forget an agent: its settings, runs and proposals."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")  # overwrite what's deleted, rather than just unlink it
            runs = [row[0] for row in self._conn.execute("SELECT id FROM dag_runs WHERE agent = ?", (agent_id,))]
            self._conn.executemany("DELETE FROM dag_task_runs WHERE run = ?", [(r,) for r in runs])
            self._conn.execute("DELETE FROM dag_runs WHERE agent = ?", (agent_id,))
            self._conn.execute("DELETE FROM dag_proposals WHERE agent = ?", (agent_id,))
            self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def forget(self) -> None:
        """Forget what runs found, said and kept, as forgetting everything does. The agents and DAGs stay."""
        with self._lock, self._conn:
            self._conn.execute("PRAGMA secure_delete = ON")
            self._conn.execute("DELETE FROM dag_task_runs")
            self._conn.execute("DELETE FROM dag_runs WHERE state != ?", (RUNNING,))
            self._conn.execute("DELETE FROM dag_proposals")  # why each was proposed can say what was found
            self._conn.execute("UPDATE agents SET context = '', context_at = NULL")

    # -- runs ----------------------------------------------------------------------------------------

    def start_run(self, agent_id: int, trigger: str, input: str, source: str, items: list[str] | None = None) -> Run:
        now = self.clock()
        items = list(items or [])
        with self._lock, self._conn:
            number = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM dag_runs WHERE agent = ?", (agent_id,)
            ).fetchone()[0]
            cur = self._conn.execute(
                "INSERT INTO dag_runs (agent, number, trigger, input, items, source, state, started) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, number, trigger, input, json.dumps(items), source, RUNNING, now),
            )
        return Run(int(cur.lastrowid), agent_id, number, trigger, input, source, RUNNING, now, None, items=items)  # type: ignore[arg-type]

    def finish_run(self, run: Run, state: str, summary: str = "") -> None:
        """Note how a run ended, and forget the agent's runs before its last KEEP_RUNS."""
        run.state, run.finished, run.summary = state, self.clock(), summary
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_runs SET state = ?, finished = ?, summary = ? WHERE id = ?",
                (state, run.finished, summary, run.id),
            )
            old = [
                row[0]
                for row in self._conn.execute(
                    "SELECT id FROM dag_runs WHERE agent = ? ORDER BY id DESC LIMIT -1 OFFSET ?", (run.agent, KEEP_RUNS)
                )
            ]
            if old:
                self._conn.execute("PRAGMA secure_delete = ON")
                self._conn.executemany("DELETE FROM dag_task_runs WHERE run = ?", [(r,) for r in old])
                self._conn.executemany("DELETE FROM dag_runs WHERE id = ?", [(r,) for r in old])

    def interrupted(self) -> int:
        """Runs that were going when Athena stopped: they're stopped, as are their tasks. Returns how many."""
        now = self.clock()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_task_runs SET state = ?, finished = ?, output = CASE WHEN output = '' THEN "
                "'Athena stopped while this was running.' ELSE output END WHERE state = ?",
                (STOPPED, now, RUNNING),
            )
            return self._conn.execute(
                "UPDATE dag_runs SET state = ?, finished = ?, summary = 'Athena stopped during the run.' "
                "WHERE state = ?",
                (STOPPED, now, RUNNING),
            ).rowcount

    def add_task(self, run_id: int, task: str, index: int, state: str, item: str = "", source: str = "") -> TaskRun:
        now = self.clock() if state == RUNNING else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO dag_task_runs (run, task, idx, state, item, source, started, finished) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task, index, state, item, source, now, None if state == RUNNING else self.clock()),
            )
        return TaskRun(int(cur.lastrowid), task, index, state, item, source, now)  # type: ignore[arg-type]

    def finish_task(
        self, task: TaskRun, state: str, output: str, steps: list[dict[str, str]], value: Any = None
    ) -> None:
        task.state, task.output, task.steps, task.finished, task.value = state, output, steps, self.clock(), value
        kept = None if value is None else json.dumps(value)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE dag_task_runs SET state = ?, output = ?, steps = ?, finished = ?, value = ? WHERE id = ?",
                (state, output, json.dumps(steps), task.finished, kept, task.id),
            )

    _RUN = "id, agent, number, trigger, input, source, state, started, finished, summary, items"

    def runs(self, agent_id: int, limit: int = KEEP_RUNS) -> list[Run]:
        """The agent's last runs, newest first, without their tasks."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._RUN} FROM dag_runs WHERE agent = ? ORDER BY id DESC LIMIT ?", (agent_id, limit)
            ).fetchall()
        return [_run(row) for row in rows]

    def run(self, run_id: int) -> Run | None:
        """A run, with what each of its tasks said and did."""
        with self._lock:
            row = self._conn.execute(f"SELECT {self._RUN} FROM dag_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            tasks = self._conn.execute(
                "SELECT id, task, idx, state, item, source, started, finished, output, steps, value FROM dag_task_runs "
                "WHERE run = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        run = _run(row)
        run.tasks = [
            TaskRun(*t[:9], steps=json.loads(t[9]), value=json.loads(t[10]) if t[10] is not None else None)
            for t in tasks
        ]
        return run

    def last_finished(self, agent_id: int) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(finished) FROM dag_runs WHERE agent = ? AND state != ?", (agent_id, RUNNING)
            ).fetchone()
        return row[0] if row else None

    # -- proposals -----------------------------------------------------------------------------------

    _PROPOSAL = "id, agent, run, kind, why, source, changes, base, state, created"

    def propose(
        self,
        agent_id: int,
        run_id: int | None,
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

    def proposals(self, agent_id: int | None = None, run_id: int | None = None) -> list[Proposal]:
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
    id_, model, guidance, context, context_at, dag, dag_at, paused, next_run = row
    return AgentConfig(id_, model, guidance, context, context_at, dag, dag_at, bool(paused), next_run)


def _run(row: tuple[Any, ...]) -> Run:
    *values, items = row
    return Run(*values, items=json.loads(items))


def _proposal(row: tuple[Any, ...]) -> Proposal:
    values = list(row)
    values[6] = json.loads(values[6])
    return Proposal(*values)
