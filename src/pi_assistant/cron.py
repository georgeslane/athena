"""Cron schedules, as Unix cron writes them: five fields, for when a task is due.

    ┌ minute (0-59)
    │ ┌ hour (0-23)
    │ │ ┌ day of the month (1-31)
    │ │ │ ┌ month (1-12, or jan-dec)
    │ │ │ │ ┌ day of the week (0-7: Sunday is 0 and 7, or sun-sat)
    30 7 * * 1-5      07:30 on weekdays

Each field is *, a number, a range (1-5), a list (1,15), or any of those with a step (*/15, 9-17/2, 5/10).
As in Vixie cron, when the day of the month and the day of the week are both given (neither starts with *),
a day matching either is due. @hourly, @daily (or @midnight), @weekly, @monthly and @yearly (or @annually)
stand for what they do there.

Times are wall-clock times in the task's time zone. One that doesn't happen, as the clocks go forward, is
skipped; one that happens twice, as they go back, is due once, the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # as cron numbers them, from 0
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")  # fmt: skip
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
MIN_GAP = timedelta(minutes=15)  # a task runs at most this often
SEARCH_DAYS = 366 * 9  # far enough ahead to find 29 February, even across 2100, which hasn't one
HOW = "minute, hour, day of the month, month and day of the week, like 30 7 * * 1-5"


class CronError(ValueError):
    """A cron schedule that can't be used, saying why."""


@dataclass(frozen=True)
class Cron:
    text: str  # as written, tidied: what's kept, which reads the same back
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]  # of the month
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    any_day: bool  # the day of the month starts with *: only the day of the week decides
    any_weekday: bool  # the day of the week starts with *: only the day of the month decides

    @classmethod
    def parse(cls, text: str) -> Cron:
        tidied = " ".join(text.strip().lower().split())
        fields = MACROS.get(tidied, tidied).split()
        if tidied.startswith("@") and tidied not in MACROS:
            raise CronError(f"{tidied} isn't one of {', '.join(MACROS)}.")
        if len(fields) != 5:
            raise CronError(f"A cron schedule has five fields: {HOW}.")
        minute, hour, day, month, weekday = fields
        cron = cls(
            tidied,
            tuple(sorted(_field(minute, 0, 59, (), "minute"))),
            tuple(sorted(_field(hour, 0, 23, (), "hour"))),
            frozenset(_field(day, 1, 31, (), "day of the month")),
            frozenset(_field(month, 1, 12, MONTHS, "month")),
            frozenset(d % 7 for d in _field(weekday, 0, 7, WEEKDAYS, "day of the week")),  # 7 is Sunday too
            day.startswith("*"),
            weekday.startswith("*"),
        )
        cron._check_gap()
        if cron.after(datetime(2026, 1, 1, tzinfo=UTC)) is None:
            raise CronError(f"{tidied} never happens: no month has that day.")
        return cron

    def due_on(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        weekday = (day.weekday() + 1) % 7  # Python's Monday is 0; cron's Sunday is
        if self.any_day or self.any_weekday:  # as Vixie cron has it: both, each as its field says (*/2 is odd days)
            return day.day in self.days and weekday in self.weekdays
        return day.day in self.days or weekday in self.weekdays

    def after(self, last: datetime) -> datetime | None:
        """When it's next due after ``last``, in ``last``'s time zone. None if it never is."""
        tz = last.tzinfo
        assert tz is not None
        for offset in range(SEARCH_DAYS):
            day = last.date() + timedelta(days=offset)
            if not self.due_on(day):
                continue
            for hour in self.hours:
                for minute in self.minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    # Compared as instants: two times with the same tzinfo would be compared by the clock alone.
                    if candidate.timestamp() > last.timestamp() and _exists(candidate):
                        return candidate
        return None

    def _check_gap(self) -> None:
        """Refuse schedules due more often than MIN_GAP."""
        gaps = [b - a for a, b in zip(self.minutes, self.minutes[1:], strict=False)]
        if any((h + 1) % 24 in self.hours for h in self.hours):  # from the end of one hour into the next
            gaps.append(self.minutes[0] + 60 - self.minutes[-1])
        if gaps and min(gaps) < MIN_GAP.total_seconds() // 60:
            raise CronError(f"A task can run at most every {int(MIN_GAP.total_seconds() // 60)} minutes.")

    def describe(self) -> str:
        """In words, such as "every weekday at 07:30", for the common shapes; otherwise the schedule itself."""
        days = self._describe_days()
        if days is None:
            return f"on the cron schedule “{self.text}”"
        months = "" if len(self.months) == 12 else f" in {_and([MONTH_NAMES[m - 1] for m in sorted(self.months)])}"
        all_hours = len(self.hours) == 24
        if len(self.minutes) == 1 and not all_hours:
            return f"{days} at {_and([f'{h:02d}:{self.minutes[0]:02d}' for h in self.hours])}{months}"
        on = "" if days == "every day" else f"{days}, "
        if len(self.minutes) == 1:
            past = "on the hour" if self.minutes[0] == 0 else f"at {self.minutes[0]} minutes past"
            return f"{on}every hour {past}{months}"
        if all_hours and (step := _step(self.minutes, 60)):
            return f"{on}every {step} minutes{months}"
        return f"on the cron schedule “{self.text}”"

    def _describe_days(self) -> str | None:
        """The days it's due, in words; None if that would take too many."""
        every_date, every_weekday = len(self.days) == 31, len(self.weekdays) == 7
        if every_date and every_weekday:
            return "every day"
        weekdays = sorted(self.weekdays, key=lambda d: (d + 6) % 7)  # Monday first
        if len(self.days) > 6 and not every_date:
            return None
        if self.any_day != self.any_weekday and not (every_date or every_weekday):
            return None  # both, as Vixie cron has it when one starts with *: too odd to put simply
        if every_date:
            if self.weekdays == {1, 2, 3, 4, 5}:
                return "every weekday"
            if self.weekdays == {0, 6}:
                return "every Saturday and Sunday"
            return "every " + _and([DAY_NAMES[d] for d in weekdays])
        dates = "on the " + _and([_ordinal(d) for d in sorted(self.days)]) + " of the month"
        if every_weekday:
            return dates
        return f"{dates} and every {_and([DAY_NAMES[d] for d in weekdays])}"


def _field(text: str, lo: int, hi: int, names: tuple[str, ...], what: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"The {what}'s step, after the /, must be a whole number from 1: {part!r}.")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _value(first, lo, hi, names, what), _value(last, lo, hi, names, what)
            if start > end:
                raise CronError(f"The {what}'s range {base!r} goes backwards.")
        else:
            start = _value(base, lo, hi, names, what)
            end = hi if slash else start
        values.update(range(start, end + 1, step))
    return values


def _value(text: str, lo: int, hi: int, names: tuple[str, ...], what: str) -> int:
    if text.isdigit() and lo <= int(text) <= hi:
        return int(text)
    if text in names:
        return names.index(text) + (1 if names is MONTHS else 0)
    allowed = f"{lo}-{hi}" + (f", or {names[0]}-{names[-1]}" if names else "")
    raise CronError(f"The {what} must be {allowed}, not {text!r}. Cron's fields are {HOW}.")


def _exists(moment: datetime) -> bool:
    """Whether a wall-clock time happens in its time zone: not when the clocks go forward past it."""
    return moment.astimezone(UTC).astimezone(moment.tzinfo).replace(tzinfo=None) == moment.replace(tzinfo=None)


def _step(values: tuple[int, ...], size: int) -> int | None:
    """The step, if ``values`` are every n from 0 and it divides ``size``."""
    if len(values) < 2 or values[0] != 0:
        return None
    step = values[1]
    return step if size % step == 0 and values == tuple(range(0, size, step)) else None


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
