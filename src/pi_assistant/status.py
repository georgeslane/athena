"""What the assistant is doing right now, published for the status board.

The agent process keeps a StatusTracker up to date and writes every change to
``data/status.json``. The status board (``pi-assistant display``) is a separate
process that reads it. While an agent process runs it holds a shared lock on
``data/status.lock``; the kernel drops that lock when the process exits, even
if it crashes, so the board can tell "offline" from "idle" without the agent
rewriting anything on a timer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)

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
    OFFLINE = "offline"  # no agent process is running


@dataclass
class Snapshot:
    """Everything the board shows. Times are Unix timestamps."""

    state: State = State.OFFLINE
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
    updated: float = 0.0  # when this snapshot was written

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        raw = json.loads(text)
        known = {f.name for f in dataclasses.fields(cls)}
        snapshot = cls(**{k: v for k, v in raw.items() if k in known})
        snapshot.state = State(snapshot.state)
        return snapshot


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
    """Tracks what the agent is doing and reports every change to ``on_change``."""

    def __init__(self, *, show_task: bool = True, clock: Callable[[], float] = time.time):
        self.show_task = show_task
        self.clock = clock
        self.channel = ""  # set by the front end, e.g. "Telegram"
        self.approval_timeout = 0.0  # seconds before an unanswered approval is denied (0: no limit)
        self.on_change: Callable[[Snapshot], None] | None = None
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
        if self.on_change:
            self.on_change(dataclasses.replace(snapshot, updated=self.clock()))


class StatusFile:
    """``status.json``, plus the lock that says an agent process is running."""

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self._lock_fd: int | None = None

    # -- agent side -----------------------------------------------------------------------

    def publish(self, tracker: StatusTracker) -> None:
        """Hold the "running" lock until close() or exit, and write every change from ``tracker``."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            # Shared, so several agent processes (the bot and `chat`) can run at once.
            # Blocks only for the instant the board holds the lock to check it.
            fcntl.flock(fd, fcntl.LOCK_SH)
            self._lock_fd = fd
            self.write(tracker.snapshot())
        except OSError as exc:
            log.warning("Status board disabled: can't write %s: %s", self.path, exc)
            return
        tracker.on_change = self._write_or_stop(tracker)

    def _write_or_stop(self, tracker: StatusTracker) -> Callable[[Snapshot], None]:
        def write(snapshot: Snapshot) -> None:
            try:
                self.write(snapshot)
            except OSError as exc:  # e.g. disk full: keep the assistant running, stop updating
                log.warning("Status board updates stopped: can't write %s: %s", self.path, exc)
                tracker.on_change = None

        return write

    def write(self, snapshot: Snapshot) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(snapshot.to_json())
        os.replace(tmp, self.path)  # atomic: the board never sees half a file

    def close(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # releases the lock
            self._lock_fd = None

    # -- board side -----------------------------------------------------------------------

    def agent_running(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # an agent process holds its shared lock
        finally:
            os.close(fd)  # also releases our lock if we got it
        return False

    def read(self) -> Snapshot:
        if not self.agent_running():
            return Snapshot(State.OFFLINE)
        try:
            return Snapshot.from_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):  # running, but nothing (valid) written yet
            return Snapshot(State.IDLE)
