"""Usage statistics: counts and timings per session and in total, and never what was said."""

import sqlite3

from pi_assistant.stats import DECLINED, FAILED, RAN, QueryRecord, UsageStats


def test_counts_per_session_and_in_total(tmp_path):
    stats = UsageStats(tmp_path / "a.db")
    stats.record(
        QueryRecord(
            session=1,
            channel="Telegram",
            started=100.0,
            seconds=4.0,
            model_calls=2,
            prompt_tokens=900,
            completion_tokens=50,
            context_tokens=950,
            tools=[("search_memory", RAN), ("fetch", DECLINED)],
        )
    )
    stats.record(QueryRecord(session=1, channel="Siri", started=200.0, seconds=2.0, tools=[("fetch", RAN)]))
    stats.record(
        QueryRecord(
            session=2, started=300.0, seconds=6.0, error="Couldn't reach the model server", tools=[("fetch", FAILED)]
        )
    )

    one = stats.summary(1)
    assert (one["queries"], one["failed"], one["average_seconds"], one["first"]) == (2, 0, 3.0, 100.0)
    assert one["tool_calls"] == 3 and one["largest_prompt"] == 900
    assert one["tools"] == [
        {"name": "fetch", "calls": 2, "failed": 0, "declined": 1},
        {"name": "search_memory", "calls": 1, "failed": 0, "declined": 0},
    ]
    assert one["context"] == {"tokens": 950, "measured": 104.0}

    total = stats.summary()
    assert (total["queries"], total["failed"], total["average_seconds"], total["tool_calls"]) == (3, 1, 4.0, 4)
    assert total["tools"][0] == {"name": "fetch", "calls": 3, "failed": 1, "declined": 1}
    assert total["context"] is None  # only a session has a context

    assert stats.summary(3) | {"tools": None} == {
        "queries": 0,
        "failed": 0,
        "average_seconds": 0,
        "first": None,
        "largest_prompt": 0,
        "tool_calls": 0,
        "tools": None,
        "context": None,
    }
    stats.close()


def test_the_latest_context_measurement_wins(tmp_path):
    stats = UsageStats(tmp_path / "a.db")
    stats.note_context(1, 5000, measured=200.0)
    stats.note_context(1, 4000, measured=100.0)  # older: ignored
    assert stats.summary(1)["context"] == {"tokens": 5000, "measured": 200.0}
    stats.note_context(1, 1200, measured=300.0)  # e.g. after a new session's warm-up
    assert stats.summary(1)["context"]["tokens"] == 1200
    stats.close()


def test_nothing_that_was_said_is_kept(tmp_path):
    stats = UsageStats(tmp_path / "a.db")
    stats.record(QueryRecord(session=1, tools=[("remember", RAN)]))
    conn = sqlite3.connect(tmp_path / "a.db")
    columns = {
        row[1]
        for table in ("stats_queries", "stats_tool_calls", "stats_context")
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    assert columns == {
        "id",
        "session",
        "channel",
        "started",
        "seconds",
        "model_calls",
        "prompt_tokens",
        "completion_tokens",
        "context_tokens",
        "error",
        "query",
        "tool",
        "outcome",
        "tokens",
        "measured",
    }
    conn.close()
    stats.close()
