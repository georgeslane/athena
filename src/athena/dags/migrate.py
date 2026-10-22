"""From before agents had DAGs, when each task had a schedule of its own: each session's tasks become its agent's
DAG, once. Its schedule is its first regular task's, it starts on the events they did, and it's paused if they
all were. Other schedules, and one-offs, are noted in the file, as a DAG has one schedule. The tasks are kept, in
the table tasks_before_dags, but nothing reads them any more."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from athena.cron import Cron, CronError
from athena.dags.spec import END_IDS, EVENT, TASK_ID, render
from athena.memory import connect_db

if TYPE_CHECKING:
    from athena.dags.store import AgentStore
    from athena.history import ConversationStore

log = logging.getLogger(__name__)

NOTHING = "NOTHING TO REPORT"  # what tasks of before said when they'd found nothing


def tasks_to_dags(db_path: Path, store: AgentStore, history: ConversationStore) -> int:
    """Make each session's tasks its agent's DAG, if there are tasks from before. Returns how many DAGs it made."""
    conn = connect_db(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone():
            return 0
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        session = "session_id" if "session_id" in columns else "NULL"
        rows = conn.execute(
            f"SELECT id, title, instructions, schedule, allowed_tools, status, {session} FROM tasks ORDER BY id"
        ).fetchall()
        by_agent: dict[int, list[tuple[str, str, str, list[str], str]]] = {}
        for task_id, title, instructions, schedule, allowed, status, agent_id in rows:
            if agent_id is None:  # from before tasks ran in sessions: its conversation becomes one of its own
                prefix = f"(Your task “{title}”) "
                agent_id = history.adopt(f"task:{task_id}", title, prefix=prefix, drop=NOTHING).id
            by_agent.setdefault(agent_id, []).append((title, instructions, schedule, json.loads(allowed), status))
        made = 0
        for agent_id, tasks in by_agent.items():
            if history.get_session(agent_id) is None or store.get(agent_id).dag:
                continue
            store.update(agent_id, dag=_dag(tasks), paused=all(t[4] != "active" for t in tasks))
            made += 1
        with conn:
            conn.execute("ALTER TABLE tasks RENAME TO tasks_before_dags")
        if made:
            log.info("Made %d agent DAG%s from the tasks of before", made, "" if made == 1 else "s")
        return made
    finally:
        conn.close()


def _dag(tasks: list[tuple[str, str, str, list[str], str]]) -> str:
    schedule, triggers, notes, specs = None, [], [], []
    ids: set[str] = set()
    for title, instructions, kept, allowed, _ in tasks:
        task_id = _id(title, ids)
        ids.add(task_id)
        specs.append({"id": task_id, "prompt": instructions, "allow": allowed})
        if kept.startswith("on ") and EVENT.match(kept[3:]):
            triggers.append(kept[3:])
            continue
        try:
            cron = Cron.parse(kept)
        except CronError:
            notes.append(f"“{title}” was to run {kept}: press Run to run it now.")
            continue
        if schedule is None:
            schedule = cron.text
        elif cron.text != schedule:
            notes.append(f"“{title}” ran on its own schedule, {cron.describe()}: this DAG runs on the first one's.")
    comments = ["Made from your tasks of before, which run side by side here.", *notes]
    return render({"schedule": schedule, "triggers": list(dict.fromkeys(triggers))}, specs, comments)


def _id(title: str, taken: set[str]) -> str:
    base = "_".join(re.findall(r"[a-z0-9]+", title.lower()))[:40] or "task"
    base = base if base[0].isalpha() else f"task_{base}"[:40]
    task_id, n = base, 2
    while task_id in taken or task_id in END_IDS or not TASK_ID.match(task_id):
        task_id, n = f"{base[:36]}_{n}", n + 1
    return task_id
