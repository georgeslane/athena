"""What the assistant is doing right now, for the status board and the dashboard.

The agent keeps a StatusTracker up to date as it works, and status_api.py serves it
over HTTP. The status board itself is a separate service (pi-display-microservice) that asks
for it, so either can run, restart or change without the other. When nothing answers,
the board knows the assistant isn't running.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

TASK_CHARS = 160  # about three lines on the board

# Friendlier progress text for the built-in tools; other tools show as "Using <name>".
_TOOL_STEPS = {
    "remember": "Saving a memory",
    "search_memory": "Searching memory",
    "forget_memory": "Forgetting a memory",
}
_ERRORS = {  # matched against the exception's class and its base classes
    "APITimeoutError": "The model took too long",
    "APIConnectionError": "Couldn't reach the model server",
    "APIStatusError": "The model server returned an error",
    "CancelledError": "Stopped",
    "KeyboardInterrupt": "Stopped",
}


class State(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    APPROVAL = "approval"  # waiting for the user to allow or deny a tool


@dataclass
class Snapshot:
    """What the assistant is doing, as the status API reports it. Times are Unix timestamps."""

    state: State = State.IDLE
    task: str = ""  # the request being worked on ("" when idle, or when show_task is off)
    step: str = ""  # what's happening right now, e.g. "Using fetch"
    tools: list[str] = field(default_factory=list)  # tools used for this task so far
    tool: str = ""  # approval: the tool waiting for an answer
    channel: str = ""  # where approvals happen, e.g. "Telegram"
    started: float = 0.0  # when the task started
    deadline: float = 0.0  # approval: when an unanswered request is denied (0: no limit)
    last_task: str = ""  # idle: the previous task,
    last_error: str = ""  # why it failed ("" if it didn't),
    last_finished: float = 0.0  # and when it ended
    updated: float = 0.0  # when it last changed


def shorten(text: str, limit: int = TASK_CHARS) -> str:
    """Collapse whitespace and cut at a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def describe_error(exc: BaseException) -> str:
    for cls in type(exc).__mro__:
        if cls.__name__ in _ERRORS:
            return _ERRORS[cls.__name__]
    return f"Something went wrong ({type(exc).__name__})"


class Task:
    """One request being worked on. Created by StatusTracker.begin()."""

    def __init__(self, tracker: StatusTracker, text: str, started: float):
        self._tracker = tracker
        self.text = text
        self.started = started
        self.step = "Thinking"
        self.tools: list[str] = []
        self.waiting_for = ""  # tool awaiting approval
        self.waiting_since = 0.0

    def thinking(self) -> None:
        self.step = "Thinking"
        self._tracker._changed()

    def using(self, tool: str) -> None:
        self.tools.append(tool)
        self.step = _TOOL_STEPS.get(tool, f"Using {tool}")
        self._tracker._changed()

    @contextlib.contextmanager
    def approval(self, tool: str) -> Iterator[None]:
        self.waiting_for, self.waiting_since = tool, self._tracker.clock()
        self._tracker._changed()
        try:
            yield
        finally:
            self.waiting_for = ""
            self._tracker._changed()

    def finish(self, error: BaseException | None = None) -> None:
        self._tracker._finish(self, error)


class StatusTracker:
    """Tracks what the agent is doing and reports every change to each of ``listeners``."""

    def __init__(self, *, show_task: bool = True, clock: Callable[[], float] = time.time):
        self.show_task = show_task
        self.clock = clock
        self.channel = ""  # set by the front end, e.g. "Telegram"
        self.approval_timeout = 0.0  # seconds before an unanswered approval is denied (0: no limit)
        self.listeners: list[Callable[[Snapshot], None]] = []
        self._tasks: list[Task] = []
        self._last: tuple[str, str, float] = ("", "", 0.0)  # previous task, its error, when it ended
        self._published: Snapshot | None = None

    def begin(self, text: str) -> Task:
        task = Task(self, shorten(text) if self.show_task else "", self.clock())
        self._tasks.append(task)
        self._changed()
        return task

    def snapshot(self) -> Snapshot:
        last_task, last_error, last_finished = self._last
        if not self._tasks:
            return Snapshot(State.IDLE, last_task=last_task, last_error=last_error, last_finished=last_finished)
        # Show the task that most needs the user: one waiting for approval, else the newest.
        task = next((t for t in reversed(self._tasks) if t.waiting_for), self._tasks[-1])
        waiting = bool(task.waiting_for)
        return Snapshot(
            State.APPROVAL if waiting else State.WORKING,
            task=task.text,
            step=task.step,
            tools=list(task.tools),
            tool=task.waiting_for,
            channel=self.channel,
            started=task.started,
            deadline=task.waiting_since + self.approval_timeout if waiting and self.approval_timeout else 0.0,
        )

    def _finish(self, task: Task, error: BaseException | None) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
            self._last = (task.text, describe_error(error) if error else "", self.clock())
            self._changed()

    def _changed(self) -> None:
        snapshot = self.snapshot()
        if snapshot == self._published:
            return
        self._published = snapshot
        stamped = dataclasses.replace(snapshot, updated=self.clock())
        for listener in list(self.listeners):
            listener(stamped)


class StatusFeed:
    """The tracker's status, with a version that changes with it, so a reader can wait for the next change.

    Versions look like "3f9a1c2e-17". The part before the dash is new each time a feed
    starts, so a version from before Athena restarted never matches.
    """

    def __init__(self, tracker: StatusTracker):
        self.tracker = tracker
        self._boot = secrets.token_hex(4)
        self._changes = 0
        self.snapshot = Snapshot()
        self._changed = asyncio.Event()
        self.running = False

    @property
    def version(self) -> str:
        return f"{self._boot}-{self._changes}"

    def start(self) -> None:
        self.running = True
        self._publish(dataclasses.replace(self.tracker.snapshot(), updated=self.tracker.clock()))
        self.tracker.listeners.append(self._publish)

    def stop(self) -> None:
        if self.running:
            self.tracker.listeners.remove(self._publish)
            self.running = False
            self._changed.set()  # so readers that are waiting hear now, not when their wait runs out

    async def wait(self, after: str | None, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the version to move on from ``after``. Returns at once if it has."""
        if timeout > 0 and after == self.version and self.running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)

    def _publish(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self._changes += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
