"""Tasks: work Athena does by itself, on a schedule, and tells you about.

A task is instructions, when to carry them out, the session it belongs to, and the tools it may use
without asking you each time: a briefing every weekday morning, research to do now while you get on
with something else, a weekly look for MCP servers that would help with what you've been asking
about, or something to do when an event comes in, such as you arriving home ("on arrive-home": see
triggers.py). Athena works on it in its session, as it would a message from you there, with
everything said there to go on, more steps, more time and thinking on, then messages you what it
found, unless there was nothing worth saying, which isn't kept. What it found is kept in the
session, which becomes the current one, so you can reply to it.

You come first:
  - A task doesn't start while Athena's answering you, or within [tasks] quiet_minutes of your
    last message. It looks again after a minute, then two, four and so on, up to 15.
  - Once started, a message from you stops the step it's taking (oMLX stops generating when the
    request's dropped, so the Mac is yours), and it takes the step again once you've finished.
  - Running it now, from Telegram or the dashboard, skips the wait, but it still stops for you.

What it may do:
  - A task Athena suggests needs your OK: schedule_task asks first, like any tool that does
    something, showing the instructions, the schedule and the tools it may use without asking.
    Tasks you make on the dashboard are yours already.
  - A tool that asks first, and that the task may not use without asking, still asks you, in
    Telegram, saying which task wants it. Unanswered after [tasks] confirm_minutes, it's denied.
  - schedule_task always asks, even in a task, so a task can't start others by itself.
  - What it remembers, and skills it writes or changes, are suggestions for you to approve on
    the dashboard (memory.py, skills.py).

After Athena uses a skill, answering you or on a task, it looks back at how that went once
you've finished chatting, and suggests a change to the skill if one would help next time.
The scheduler runs these reviews too, one thing at a time, so they never compete for the Mac.
So does the heartbeat: every [tasks] heartbeat_minutes, if Athena's watching anything for you, it
checks on all of it in one go, and tells you only what needs your attention (watching.py).
"""

from athena.tasks.examples import EXAMPLES
from athena.tasks.scheduler import NEVER_UNASKED, NOTHING, Event, Review, Scheduler, suggested
from athena.tasks.schedules import SCHEDULES, Schedule, ScheduleError
from athena.tasks.store import Task, TaskStore

__all__ = [
    "EXAMPLES",
    "NEVER_UNASKED",
    "NOTHING",
    "SCHEDULES",
    "Event",
    "Review",
    "Schedule",
    "ScheduleError",
    "Scheduler",
    "Task",
    "TaskStore",
    "suggested",
]
