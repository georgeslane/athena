"""Who uses the model next: your messages before what Athena does by itself, but never in the middle of a task.

The model server answers one thing at a time, and what's cached is what it read last. So each model has a queue
(agentic-airflow's, which the engine of agents' DAGs shares, dags/service.py): a DAG's task holds the model from
its first step to its answer, and a message of yours waits for it, then goes before anything Athena's waiting to
do by itself, in the order you sent it. A task waiting for your OK to use a tool doesn't hold the model, so your
messages go ahead meanwhile. What else Athena does by itself, such as looking back at how a skill went, waits its
turn as a task does.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator

from agentic_airflow.priority import DAG_PRIORITY, USER_PRIORITY, ModelQueueSet


class Gate:
    def __init__(self, queues: ModelQueueSet | None = None):
        self.queues = queues or ModelQueueSet()
        self.doing: dict[str, str] = {}  # what Athena's doing by itself with each model, while it is

    def busy(self, model: str) -> bool:
        """Whether something's using the model now, so a message of yours would wait."""
        return self.queues.get(model).active > 0

    @contextlib.asynccontextmanager
    async def yours(self, model: str) -> AsyncIterator[None]:
        """For a message of yours: once the task using the model, if one is, has finished."""
        async with self.queues.get(model).slot(USER_PRIORITY, 0.0):
            yield

    @contextlib.asynccontextmanager
    async def background(self, model: str, what: str) -> AsyncIterator[None]:
        """For something Athena does by itself, besides a DAG's tasks: after your messages, and those before it."""
        async with self.queues.get(model).slot(DAG_PRIORITY, time.time()):
            self.doing[model] = what
            try:
                yield
            finally:
                self.doing.pop(model, None)
