"""Watching: what Athena keeps an eye on for you, and the heartbeat that checks on it (watching.py, tasks.py)."""

import asyncio
import time

import pytest
from conftest import completion
from test_tasks import LONDON, athena  # noqa: F401 - athena is a fixture

from athena.tasks import NOTHING
from athena.tools import ToolError, Turn, begin_turn
from athena.watching import MAX_WATCHES, WatchStore, describe, watch_tool


def days(n: float) -> float:
    """When something watched for ``n`` days from now ends."""
    return time.time() + n * 86400


def test_whats_watched_is_kept_with_limits(tmp_path):
    store = WatchStore(tmp_path / "w.db")
    added = store.add("  when the   parcel arrives ", days(7), by="you")
    assert added.what == "when the parcel arrives" and round((added.until - added.created) / 86400) == 7
    for bad in ("", "x" * 301):
        with pytest.raises(ValueError):
            store.add(bad, days(7), by="you")
    with pytest.raises(ValueError, match="still to come"):
        store.add("thing", time.time() - 1, by="you")
    store.note(added.id, "Out for delivery at 09:12.")
    assert store.get(added.id).note == "Out for delivery at 09:12."
    assert "Your note from last time: Out for delivery at 09:12." in describe(store.get(added.id), LONDON)
    with pytest.raises(ValueError):
        store.note(999, "x")
    for i in range(MAX_WATCHES - 1):
        store.add(f"thing {i}", days(1), by="you")
    with pytest.raises(ValueError, match="take some off first"):
        store.add("one too many", days(1), by="you")
    assert [w.what for w in store.expire(time.time() + 2 * 86400)] == [f"thing {i}" for i in range(MAX_WATCHES - 1)]
    assert store.remove(added.id).what == "when the parcel arrives" and store.all() == []
    store.close()


def test_something_can_be_watched_with_no_end_and_changed(tmp_path):
    store = WatchStore(tmp_path / "w.db")
    inbox = store.add("anything in my inbox that needs a reply", None, by="you")
    assert inbox.until is None and "(since " in describe(inbox, LONDON) and "with no end" in describe(inbox, LONDON)
    assert store.expire(time.time() + 3650 * 86400) == []  # it's never let go of by itself
    store.note(inbox.id, "Told them about the bank's email.")
    changed = store.change(inbox.id, "anything from the bank", days(30))
    assert (changed.what, round((changed.until - time.time()) / 86400)) == ("anything from the bank", 30)
    assert changed.note == "Told them about the bank's email."  # what it knew stays
    for what, until in (("", None), ("thing", time.time() - 1)):
        with pytest.raises(ValueError):
            store.change(inbox.id, what, until)
    with pytest.raises(ValueError, match="Nothing's watched as #9"):
        store.change(9, "thing", None)
    store.forget_notes()
    assert store.get(inbox.id).note == ""


def test_whats_watched_from_before_no_end_was_possible_is_kept(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "w.db")
    db.execute(
        "CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL, created REAL NOT NULL, "
        "until REAL NOT NULL, by TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', checked REAL)"
    )
    db.execute("INSERT INTO watches (what, created, until, by, note) VALUES ('the parcel', 1, 2e9, 'you', 'Due.')")
    db.commit()
    db.close()
    store = WatchStore(tmp_path / "w.db")
    [kept] = store.all()
    assert (kept.what, kept.until, kept.note) == ("the parcel", 2e9, "Due.")
    assert store.add("my inbox", None, by="you").until is None  # it can be empty now


def test_until_the_end_of_a_day():
    from datetime import datetime

    from athena.watching import end_of

    end = datetime.fromtimestamp(end_of("2026-11-02", LONDON), LONDON)
    assert (end.date().isoformat(), end.hour, end.minute) == ("2026-11-02", 23, 59)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        end_of("next tuesday", LONDON)


async def test_the_model_adds_only_when_youre_there_and_can_note_and_finish(tmp_path):
    store = WatchStore(tmp_path / "w.db")
    changed = []
    watch = watch_tool(store, LONDON, 30, lambda: changed.append(1)).handler
    said = await watch({"action": "add", "what": "Claude finishing the bank project", "days": 3})
    assert said.startswith("Watching #1: Claude finishing the bank project. You'll check on it every 30 minutes")
    assert store.get(1).by == "athena" and changed == [1]
    forever = await watch({"action": "add", "what": "my inbox, every time", "forever": True})
    assert "with no end" in forever and store.get(2).until is None
    with pytest.raises(ToolError, match="more than 0"):
        await watch({"action": "add", "what": "thing", "days": 0})
    await watch({"action": "done", "id": 2})

    end = begin_turn(Turn(alone="your task “Research”"))  # working by itself: a page it read can't add one
    try:
        with pytest.raises(ToolError, match="only start watching something while you're answering"):
            await watch({"action": "add", "what": "anything"})
        assert await watch({"action": "note", "id": 1, "note": "S3 of 4 done."}) == "Noted on #1, for next time."
        assert "#1: Claude finishing the bank project" in await watch({"action": "list"})
        assert await watch({"action": "done", "id": 1}) == "Stopped watching #1: Claude finishing the bank project."
    finally:
        end()
    assert await watch({"action": "list"}) == "Nothing's being watched."
    for bad in ({"action": "done"}, {"action": "done", "id": 5}, {"action": "sing"}, {"action": "add", "what": ""}):
        with pytest.raises(ToolError):
            await watch(bad)


async def test_the_heartbeat_checks_everything_at_once_and_tells_you_only_what_matters(athena):  # noqa: F811
    a = athena(
        [
            completion(None, [("look", {}), ("watch", {"action": "note", "id": 1, "note": "Gold at 4,190."})]),
            completion(NOTHING),
            completion(None, [("watch", {"action": "done", "id": 2})]),
            completion("Claude has finished the bank project: merge it when you're ready."),
        ]
    )
    scheduler = a["scheduler"]
    scheduler.watches.add("if gold drops below 4,000", days(7), by="you")
    scheduler.watches.add("when Claude finishes the bank project", days(7), by="athena")

    await scheduler.heartbeat()
    first = a["server"].requests[0]["messages"]
    assert "#1: if gold drops below 4,000" in first[-1]["content"]
    assert "#2: when Claude finishes the bank project" in first[-1]["content"]
    assert "your regular check, every 30 minutes" in first[-1]["content"]
    assert a["channel"].said == [] and scheduler.check_result["outcome"] == "nothing new"
    assert scheduler.watches.get(1).note == "Gold at 4,190." and scheduler.watches.get(1).checked

    await scheduler.heartbeat()
    assert a["channel"].said == ["👀 Claude has finished the bank project: merge it when you're ready."]
    assert [w.id for w in scheduler.watches.all()] == [1]  # the one that happened is off the list
    assert a["history"].load("111")[-2:] == [
        {"role": "user", "content": "(You checked what I asked you to watch.)"},
        {"role": "assistant", "content": "Claude has finished the bank project: merge it when you're ready."},
    ]
    assert scheduler.check_result["outcome"] == "told you" and a["ran"] == [1, 1]
    assert [step["tool"] for step in scheduler.check_result["steps"]] == ["watch"]  # what this check did


async def test_the_heartbeat_asks_before_tools_it_may_not_use_and_lets_go_of_old_watches(athena):  # noqa: F811
    a = athena([completion(None, [("publish", {})]), completion(NOTHING)], heartbeat_tools=[])
    scheduler = a["scheduler"]
    old = scheduler.watches.add("something long ago", days(1), by="you")
    scheduler.watches.add("the parcel", days(1), by="you")
    with scheduler.watches._lock, scheduler.watches._conn:
        scheduler.watches._conn.execute("UPDATE watches SET until = ? WHERE id = ?", (time.time() - 1, old.id))
    await scheduler.heartbeat()
    assert a["channel"].said == ["👀 I've stopped watching for this, as it was only until today: something long ago"]
    assert a["channel"].asked == [("publish", "While checking what you asked me to watch")]

    allowed = athena([completion(None, [("publish", {})]), completion(NOTHING)], heartbeat_tools=["publish"])
    allowed["scheduler"].watches.add("the parcel", days(1), by="you")
    await allowed["scheduler"].heartbeat()
    assert allowed["channel"].asked == [] and allowed["published"] == [{}]


async def test_the_heartbeat_runs_only_when_theres_something_to_watch_and_youre_not_chatting(athena):  # noqa: F811
    a = athena([completion(NOTHING)] * 3, heartbeat_minutes=30)
    scheduler = a["scheduler"]
    scheduler.last_check = time.time() - 31 * 60
    assert not scheduler._heartbeat_due()  # nothing to watch: no check, so it costs nothing
    scheduler.watches.add("the parcel", days(1), by="you")
    assert scheduler._heartbeat_due()
    scheduler.last_check = time.time()
    assert not scheduler._heartbeat_due()
    scheduler.check_now()
    assert scheduler._heartbeat_due()
    scheduler.cfg.heartbeat_minutes = 0
    assert not scheduler._heartbeat_due()
    scheduler.cfg.heartbeat_minutes = 1
    assert scheduler._heartbeat_minutes() == 10  # never more often than every 10 minutes

    scheduler.cfg.heartbeat_minutes = 30
    scheduler.start()
    try:
        async with asyncio.timeout(3):
            while scheduler.check_result is None:
                await asyncio.sleep(0.01)
        assert scheduler.check_result["outcome"] == "nothing new" and not scheduler._check_now
    finally:
        await scheduler.stop()
    watching = scheduler.watching()
    assert [i["what"] for i in watching["items"]] == ["the parcel"] and watching["every_minutes"] == 30
    assert watching["next"] == pytest.approx(scheduler.last_check + 1800)
