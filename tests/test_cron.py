"""Cron schedules: reading them as Unix cron does, when they're next due, across the clocks changing, and in words."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from athena.cron import Cron, CronError

LONDON = ZoneInfo("Europe/London")
THURSDAY = datetime(2026, 10, 15, 12, 0, tzinfo=LONDON)  # a Thursday, at noon


def runs(text, start=THURSDAY, n=5):
    cron, at, out = Cron.parse(text), start, []
    for _ in range(n):
        at = cron.after(at)
        out.append(at.strftime("%a %d %b %H:%M"))
    return out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0 9 * * *", ["Fri 16 Oct 09:00", "Sat 17 Oct 09:00", "Sun 18 Oct 09:00"]),
        ("*/20 12 * * *", ["Thu 15 Oct 12:20", "Thu 15 Oct 12:40", "Fri 16 Oct 12:00"]),
        ("5/20 12 * * *", ["Thu 15 Oct 12:05", "Thu 15 Oct 12:25", "Thu 15 Oct 12:45"]),  # from 5, every 20
        ("0 9-17/4 * * *", ["Thu 15 Oct 13:00", "Thu 15 Oct 17:00", "Fri 16 Oct 09:00"]),
        ("0 9 * * sat,SUN", ["Sat 17 Oct 09:00", "Sun 18 Oct 09:00", "Sat 24 Oct 09:00"]),
        ("0 9 * * 7", ["Sun 18 Oct 09:00", "Sun 25 Oct 09:00", "Sun 01 Nov 09:00"]),  # 7 is Sunday, as 0 is
        ("0 9 1,15 * *", ["Sun 01 Nov 09:00", "Sun 15 Nov 09:00", "Tue 01 Dec 09:00"]),
        ("0 9 31 * *", ["Sat 31 Oct 09:00", "Thu 31 Dec 09:00", "Sun 31 Jan 09:00"]),  # months without one skip it
        ("0 9 * jan-feb mon", ["Mon 04 Jan 09:00", "Mon 11 Jan 09:00", "Mon 18 Jan 09:00"]),
        ("@weekly", ["Sun 18 Oct 00:00", "Sun 25 Oct 00:00", "Sun 01 Nov 00:00"]),
        ("@monthly", ["Sun 01 Nov 00:00", "Tue 01 Dec 00:00", "Fri 01 Jan 00:00"]),
        ("@hourly", ["Thu 15 Oct 13:00", "Thu 15 Oct 14:00", "Thu 15 Oct 15:00"]),
    ],
)
def test_when_its_due_next(text, expected):
    assert runs(text, n=3) == expected


def test_both_days_given_means_either_as_in_vixie_cron():
    # The 1st of the month, or any Friday.
    assert runs("0 9 1 * fri", n=4) == ["Fri 16 Oct 09:00", "Fri 23 Oct 09:00", "Fri 30 Oct 09:00", "Sun 01 Nov 09:00"]
    # A field that starts with * doesn't count as given: */2 days of the month and Fridays means both.
    assert runs("0 9 */2 * fri", n=2) == ["Fri 23 Oct 09:00", "Fri 13 Nov 09:00"]  # odd days that are Fridays


def test_the_29th_of_february_is_found_years_ahead():
    assert Cron.parse("0 9 29 2 *").after(THURSDAY) == datetime(2028, 2, 29, 9, 0, tzinfo=LONDON)


def test_clocks_going_forward_skip_a_time_that_doesnt_happen_and_going_back_run_it_once():
    # 29 March 2026: 01:00 GMT becomes 02:00 BST, so 01:30 doesn't happen that day.
    spring = datetime(2026, 3, 28, 12, 0, tzinfo=LONDON)
    assert runs("30 1 * * *", spring, 2) == ["Mon 30 Mar 01:30", "Tue 31 Mar 01:30"]
    # 25 October 2026: 02:00 BST becomes 01:00 GMT, so 01:30 happens twice; it runs at the first.
    autumn = datetime(2026, 10, 24, 12, 0, tzinfo=LONDON)
    first = Cron.parse("30 1 * * *").after(autumn)
    assert first == datetime(2026, 10, 25, 1, 30, tzinfo=LONDON) and first.utcoffset() == timedelta(hours=1)
    assert Cron.parse("30 1 * * *").after(first) == datetime(2026, 10, 26, 1, 30, tzinfo=LONDON)
    # Every 15 minutes runs through the repeated hour by the clock, never twice in the same minute.
    times = []
    at = datetime(2026, 10, 25, 0, 50, tzinfo=LONDON)
    for _ in range(6):
        at = Cron.parse("*/15 * * * *").after(at)
        times.append(at.timestamp())
    assert all(b > a for a, b in zip(times, times[1:], strict=False))


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("* * * *", "five fields"),
        ("0 9 * * * *", "five fields"),
        ("@reboot", "isn't one of"),
        ("60 * * * *", "The minute must be 0-59, not '60'"),
        ("0 24 * * *", "The hour must be 0-23"),
        ("0 9 0 * *", "The day of the month must be 1-31"),
        ("0 9 * 13 *", "The month must be 1-12, or jan-dec"),
        ("0 9 * * 8", "The day of the week must be 0-7, or sun-sat"),
        ("0 9 * * fun", "not 'fun'"),
        ("0 17-9 * * *", "goes backwards"),
        ("*/0 * * * *", "step"),
        ("*/x * * * *", "step"),
        ("0,,5 * * * *", "not ''"),
        ("-1 * * * *", "not ''"),
        ("* * * * *", "at most every 15 minutes"),
        ("0,10 * * * *", "at most every 15 minutes"),
        ("0,50 9,10 * * *", "at most every 15 minutes"),  # 09:50, then 10:00
        ("0 9 31 2 *", "never happens"),
        ("0 9 30 feb *", "never happens"),
    ],
)
def test_what_isnt_a_cron_schedule_or_runs_too_often_says_why(text, why):
    with pytest.raises(CronError, match=why):
        Cron.parse(text)


@pytest.mark.parametrize(
    "text",
    ["*/15 * * * *", "0,15,30,45 * * * *", "50 9 * * *", "50 9,11 * * *", "0 * * * *", "45 23 * * *"],
)
def test_every_fifteen_minutes_or_less_often_is_fine(text):
    Cron.parse(text)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("30 7 * * *", "every day at 07:30"),
        ("30 7 * * 1-5", "every weekday at 07:30"),
        ("0 10 * * sat,sun", "every Saturday and Sunday at 10:00"),
        ("0 18 * * 0,4,1", "every Monday, Thursday and Sunday at 18:00"),
        ("0 9 1 * *", "on the 1st of the month at 09:00"),
        ("0 9 2,3,11,22 * *", "on the 2nd, 3rd, 11th and 22nd of the month at 09:00"),
        ("0 9 1 * mon", "on the 1st of the month and every Monday at 09:00"),
        ("0 9 * 12 *", "every day at 09:00 in December"),
        ("0 9,13,17 * * *", "every day at 09:00, 13:00 and 17:00"),
        ("0 * * * *", "every hour on the hour"),
        ("15 * * * 1-5", "every weekday, every hour at 15 minutes past"),
        ("*/15 * * * *", "every 15 minutes"),
        ("0 9 */2 * fri", "on the cron schedule “0 9 */2 * fri”"),
        ("0 9 */2 * *", "on the cron schedule “0 9 */2 * *”"),
        ("@daily", "every day at 00:00"),
        ("5,20,40 9 * * *", "on the cron schedule “5,20,40 9 * * *”"),
    ],
)
def test_said_in_words(text, said):
    assert Cron.parse(text).describe() == said
