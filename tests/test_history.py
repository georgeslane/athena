from pi_assistant.history import ConversationStore


def test_a_history_that_couldnt_be_compacted_drops_half_at_twice_the_limit(tmp_path):
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    for i in range(8):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    assert len(store.load("chat")) == 16  # compacting should have happened by now, but nothing's dropped yet

    store.append_exchange("chat", "q8", "a8")  # 18 messages > 16: trim to the newest 4
    msgs = store.load("chat")
    assert [m["content"] for m in msgs] == ["q7", "a7", "q8", "a8"]
    assert msgs[0]["role"] == "user"

    # The window then stays put (stable prompt prefix) until it fills up again.
    store.append_exchange("chat", "q9", "a9")
    assert [m["content"] for m in store.load("chat")][:2] == ["q7", "a7"]


def test_compacting_replaces_the_older_messages_with_a_summary(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    assert store.older("chat", keep=4) is None
    for i in range(2):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    assert store.older("chat", keep=4) is None  # only the last few: nothing to compact
    for i in range(2, 5):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    store.append_exchange("other", "elsewhere", "yes")

    older = store.older("chat", keep=4)
    assert older.summary is None
    assert older.messages == [("user", "q0"), ("assistant", "a0"), ("user", "q1"), ("assistant", "a1"),
                              ("user", "q2"), ("assistant", "a2")]  # fmt: skip
    assert store.compact("chat", older, "George asked q0 to q2.")
    msgs = store.load("chat")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == (
        "<summary>\nWhat we said earlier in this conversation, summarised:\nGeorge asked q0 to q2.\n</summary>\n\nq3"
    )
    assert [m["content"] for m in msgs[1:]] == ["a3", "q4", "a4"]
    assert store.length("chat") == 4 and [m["content"] for m in store.load("other")] == ["elsewhere", "yes"]

    # The next compaction summarises the summary and what came after it.
    store.append_exchange("chat", "q5", "a5")
    again = store.older("chat", keep=4)
    assert again.summary == "George asked q0 to q2." and again.messages == [("user", "q3"), ("assistant", "a3")]
    assert store.compact("chat", again, "q0 to q3.")
    assert store.load("chat")[0]["content"].endswith("q0 to q3.\n</summary>\n\nq4")

    store.close()
    reopened = ConversationStore(tmp_path / "h.db")  # it's kept
    assert reopened.load("chat")[0]["content"].endswith("q0 to q3.\n</summary>\n\nq4")


def test_a_summary_never_outlives_its_session_a_reset_or_forgetting(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    for i in range(4):
        store.append_exchange("chat", f"q{i}", f"a{i}")

    store.compact("chat", store.older("chat", keep=4), "Old news.")
    store.new_session()
    store.append_exchange("chat", "hello", "hi")
    assert store.load("chat") == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

    for i in range(3):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    store.compact("chat", store.older("chat", keep=4), "This session.")
    store.reset("chat")
    store.append_exchange("chat", "after", "reset")
    assert store.load("chat")[0]["content"] == "after"

    # Compacting what was said before the chat moved on changes nothing.
    for i in range(3):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    for move_on in (store.new_session, lambda: store.reset("chat"), store.clear):
        store.append_exchange("chat", "x", "y")
        store.append_exchange("chat", "z", "w")
        store.append_exchange("chat", "u", "v")
        older = store.older("chat", keep=4)
        move_on()
        assert not store.compact("chat", older, "What was said before.")
        assert "before" not in str(store.load("chat"))


def test_compacting_a_chat_for_the_first_time_after_forgetting_everything_brings_nothing_back(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    for i in range(3):
        store.append_exchange("chat", f"secret {i}", "ok")
    older = store.older("chat", keep=4)  # no state kept for the chat yet: its window starts at 0
    store.clear()  # as forgetting everything does, which starts a new session
    for i in range(3):
        store.append_exchange("chat", f"new {i}", "ok")
    assert not store.compact("chat", older, "secret 0 and secret 1")
    assert "secret" not in str(store.load("chat"))


def test_a_history_from_before_compacting_gets_it(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE chat_state (chat_id TEXT PRIMARY KEY, window_start INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO chat_state VALUES ('chat', 0);"
    )
    conn.commit()
    conn.close()
    store = ConversationStore(db)
    for i in range(3):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    assert store.compact("chat", store.older("chat", keep=4), "Summary.")
    assert store.load("chat")[0]["content"].endswith("Summary.\n</summary>\n\nq1")


def test_reset_and_chats_are_separate(tmp_path):
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    store.append_exchange("a", "hello", "hi")
    store.append_exchange("b", "other", "chat")
    store.reset("a")
    assert store.load("a") == []
    assert len(store.load("b")) == 2
    store.append_exchange("a", "again", "yes")
    assert [m["content"] for m in store.load("a")] == ["again", "yes"]


def test_a_new_session_clears_every_chat_and_lasts_after_a_restart(tmp_path):
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    assert store.session.id == 1 and store.sessions_started() == 1
    store.append_exchange("telegram", "hello", "hi")
    store.append_exchange("cli", "other", "chat")
    session = store.new_session()
    assert session.id == 2 and store.load("telegram") == [] and store.load("cli") == []
    store.append_exchange("telegram", "again", "yes")
    store.close()

    reopened = ConversationStore(tmp_path / "h.db", max_messages=8)
    assert reopened.session == session  # the same session, after a restart
    assert [m["content"] for m in reopened.load("telegram")] == ["again", "yes"]
    assert reopened.sessions_started() == 2
    reopened.close()


def test_exchanges_pair_each_message_with_its_reply(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    store.append_exchange("a", "q1", "a1")
    store.append_exchange("b", "q2", "a2")
    store.append_exchange("a", "q3", "a3")
    exchanges = store.exchanges_after(0)
    assert [(e.chat_id, e.question, e.answer) for e in exchanges] == [
        ("a", "q1", "a1"),
        ("b", "q2", "a2"),
        ("a", "q3", "a3"),
    ]
    assert [e.question for e in store.exchanges_after(exchanges[0].id, limit=1)] == ["q2"]
    assert store.exchanges_after(exchanges[-1].id) == []


def test_clearing_deletes_every_message_and_starts_a_session(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    store.append_exchange("a", "secret plans", "noted")
    last = store.exchanges_after(0)[-1].id
    session = store.clear()
    assert session.id == 2 and store.exchanges_after(0) == []
    store.append_exchange("a", "new", "start")
    assert store.exchanges_after(0)[0].id > last  # ids aren't reused, so nothing old can come back
    assert [m["content"] for m in store.load("a")] == ["new", "start"]
    store.close()


def test_taking_back_the_last_exchange_to_answer_it_again(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    store.append_exchange("chat", "first", "one")
    store.append_exchange("chat", "second", "two")
    store.append_exchange("other", "elsewhere", "there")

    taken = store.take_last("chat")
    assert (taken.chat_id, taken.question, taken.answer) == ("chat", "second", "two")
    assert [m["content"] for m in store.load("chat")] == ["first", "one"]
    assert [e.question for e in store.exchanges_after(0)] == ["first", "elsewhere"]  # gone, not just hidden
    assert len(store.load("other")) == 2

    # Only what the model still sees: not from before a new session.
    store.new_session()
    assert store.take_last("chat") is None
    assert [e.question for e in store.exchanges_after(0)] == ["first", "elsewhere"]
