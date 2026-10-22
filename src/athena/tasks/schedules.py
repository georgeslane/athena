"""When tasks run: once, now or at a time; regularly, as cron says (cron.py); or each time an event comes in.

A schedule is written as text, which is how it's kept: "once now", "once 2026-10-16 09:00", "30 7 * * 1-5" or
"on arrive-home". Schedule.parse reads what you or the model wrote, and Schedule.kept what a task keeps,
whose one-off time may have passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as clock

from athena.cron import Cron, CronError

SCHEDULES = (
    '"now", "in 2h", "once 2026-10-16 09:00", "once 09:00" (the next 09:00), cron for a regular one, such as '
    '"30 7 * * 1-5" (07:30 on weekdays; at most every 15 minutes), or "on arrive-home" (each time something, such '
    "as an iPhone Shortcut, calls /hooks/arrive-home)"
)
EVENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")  # an event's name: what /hooks/<name> fires
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE_CLOCK = re.compile(r"^(\d{4}-\d{2}-\d{2})[ t]([01]?\d|2[0-3]):([0-5]\d)$")
_SPAN = re.compile(r"^(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)$")


class ScheduleError(ValueError):
    """A schedule that can't be understood, saying what's allowed."""


# -- schedules ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    kind: str  # once, cron (a regular one) or on (an event)
    text: str  # as it's kept, which reads the same back
    when: datetime | None = None  # once; None: as soon as you're not chatting
    cron: Cron | None = None
    event: str = ""  # on

    @classmethod
    def parse(cls, text: str, tz: tzinfo, now: datetime | None = None) -> Schedule:
        now = (now or datetime.now(tz)).astimezone(tz)
        words = " ".join(text.strip().lower().split())
        if words in ("now", "once now", "once"):
            return cls("once", "once now")
        if words.startswith("in "):
            words = "once " + words
        kind, _, rest = words.partition(" ")
        if kind == "once":
            when = cls._once(rest, tz, now)
            if when <= now:
                raise ScheduleError(f"{when:%Y-%m-%d %H:%M} has already passed.")
            return cls("once", f"once {when:%Y-%m-%d %H:%M}", when=when)
        if kind == "on":
            if not EVENT.match(rest):
                raise ScheduleError(
                    f'Name the event in lower case, with hyphens between words, like "on arrive-home". '
                    f"Schedules: {SCHEDULES}."
                )
            return cls("on", f"on {rest}", event=rest)
        try:
            regular = Cron.parse(rest if kind == "cron" else words)
        except CronError as exc:
            raise ScheduleError(f"I don't understand the schedule {text!r}: {exc} Schedules: {SCHEDULES}.") from None
        return cls("cron", regular.text, cron=regular)

    @classmethod
    def kept(cls, text: str, tz: tzinfo) -> Schedule:
        """A schedule as a task keeps it, including a one-off's whose time has come and gone."""
        kind, _, rest = text.partition(" ")
        if kind == "once" and (match := _DATE_CLOCK.match(rest)):
            return cls("once", text, when=_at(match, tz))
        return cls.parse(text, tz)

    @staticmethod
    def _once(rest: str, tz: tzinfo, now: datetime) -> datetime:
        if rest.startswith("in "):
            return (now + _span(rest[3:])).replace(second=0, microsecond=0)
        if match := _DATE_CLOCK.match(rest):
            return _at(match, tz)
        if _CLOCK.match(rest):
            at = datetime.combine(now.date(), _clock(rest), tz)
            return at if at > now else at + timedelta(days=1)
        raise ScheduleError(f'Say when, like "once 2026-10-16 09:00" or "in 2h". Schedules: {SCHEDULES}.')

    def first(self, now: datetime) -> datetime | None:
        """When it's first due. None for one that runs when an event comes in."""
        if self.kind == "on":
            return None
        if self.kind == "once":
            return self.when or now
        return self.after(now)

    def after(self, last: datetime) -> datetime | None:
        """When it's next due after ``last``: None if it only runs once, or when an event comes in."""
        return self.cron.after(last) if self.cron else None

    def describe(self) -> str:
        if self.kind == "on":
            return f"each time “{self.event}” comes in"
        if self.kind == "once":
            return f"once, on {self.when:%a %-d %b at %H:%M}" if self.when else "once, as soon as you're not chatting"
        assert self.cron is not None
        return self.cron.describe()


def _at(match: re.Match[str], tz: tzinfo) -> datetime:
    """The date and time _DATE_CLOCK matched."""
    return datetime.combine(date.fromisoformat(match.group(1)), clock(int(match.group(2)), int(match.group(3))), tz)


def _clock(text: str) -> clock:
    match = _CLOCK.match(text.strip())
    if not match:
        raise ScheduleError(f"Say the time as HH:MM, like 07:30. Schedules: {SCHEDULES}.")
    return clock(int(match.group(1)), int(match.group(2)))


def _span(text: str) -> timedelta:
    match = _SPAN.match(text.strip())
    if not match:
        raise ScheduleError(f"Say how long like 30m, 2h or 1d. Schedules: {SCHEDULES}.")
    n, unit = int(match.group(1)), match.group(2)[0]
    return timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)
