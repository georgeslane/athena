"""Agents' DAGs: what each agent does by itself, run by agentic-airflow's engine (service.py, docs/dags.md).

A DAG file imports ``alert`` from here, for a task's ``on_complete``: what that task finds is told to you. The
file's never run, so this one only stands for it: Athena gives its own when it reads the file (reader.HOOKS).
"""

from typing import Any


def alert(engine: Any, run: Any, instance: Any) -> None:
    """on_complete=alert: tell the user what the task found (service.Dags._alerted)."""
