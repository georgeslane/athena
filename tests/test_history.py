import sqlite3

import pytest

from athena.history import ConversationStore


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


def test_a_summary_belongs_to_its_chat_and_never_outlives_a_reset_or_forgetting(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    for i in range(4):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    store.compact("chat", store.older("chat", keep=4), "Old news.")
    store.append_exchange("other", "hello", "hi")
    assert store.load("other") == [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    store.new_session()  # a session has a chat of its own: this one keeps its summary
    assert store.load("chat")[0]["content"].startswith("<summary>")

    store.reset("chat")
    store.append_exchange("chat", "after", "reset")
    assert store.load("chat")[0]["content"] == "after"

    # Compacting what was said before the chat moved on changes nothing.
    for i in range(3):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    for move_on in (lambda: store.reset("chat"), store.clear):
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


def test_reset_and_chats_are_separate(tmp_path):
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    store.append_exchange("a", "hello", "hi")
    store.append_exchange("b", "other", "chat")
    store.reset("a")
    assert store.load("a") == []
    assert len(store.load("b")) == 2
    store.append_exchange("a", "again", "yes")
    assert [m["content"] for m in store.load("a")] == ["again", "yes"]


def test_each_agent_is_a_conversation_and_the_master_agent_comes_first(tmp_path):
    changes = []
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    store.listeners.append(lambda: changes.append(1))
    master = store.master
    assert (master.id, master.name, master.master) == (1, "Master", True) and store.master_chat == "session:1"
    store.append_exchange(store.master_chat, "Plan a trip to   Rome", "Sure.", channel="Telegram")
    second = store.new_session()
    assert second.id == 2 and store.master.id == 1 and store.load("session:2") == []
    store.append_exchange(second.chat_id, "What's the weather?", "Sunny.", channel="Dashboard")
    assert store.load("session:1")[0]["content"] == "Plan a trip to   Rome"  # it's all still there

    # The master agent first, then the one with something said in it most recently; after a restart too.
    store.close()
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    assert store.master.id == 1 and store.sessions_started() == 2
    listed = store.sessions()
    assert [(s.id, s.title, s.messages, s.master) for s in listed] == [
        (1, "Master", 2, True),
        (2, "What's the weather?", 2, False),
    ]
    # A new one shows once something's said in it, or it has a name.
    third = store.new_session()
    assert [s.id for s in store.sessions()] == [1, 2]
    store.append_exchange(third.chat_id, "Hello", "Hi")
    assert [s.id for s in store.sessions()] == [1, 3, 2]
    assert len(changes) == 3  # each message and new agent (another store's aren't counted)


def test_a_sessions_messages_page_by_page_with_where_they_came_from(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    chat = store.master_chat
    for i in range(5):
        store.append_exchange(chat, f"q{i}", f"a{i}", tools=["fetch"] if i == 4 else None, channel="Telegram")
    last, more = store.messages(1, limit=4)
    assert more and [m.text for m in last] == ["q3", "a3", "q4", "a4"]
    assert (last[2].role, last[2].channel, last[3].role, last[3].tools) == ("user", "Telegram", "assistant", ["fetch"])
    earlier, more = store.messages(1, before=last[0].id, limit=10)
    assert not more and [m.text for m in earlier] == ["q0", "a0", "q1", "a1", "q2", "a2"]
    assert store.messages(2) == ([], False)


def test_tools_allowed_in_a_session(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    assert store.set_allowed_tools(1, ["fetch", "send_email", "fetch"]).allowed_tools == ("fetch", "send_email")
    assert store.master.allowed_tools == ("fetch", "send_email")
    other = store.new_session()
    assert other.allowed_tools == () and store.get_session(1).allowed_tools == ("fetch", "send_email")
    with pytest.raises(KeyError):
        store.set_allowed_tools(9, [])
    store.clear()  # forgetting everything forgets what you allowed too
    assert store.get_session(1).allowed_tools == () and store.master.allowed_tools == ()


def test_a_master_agent_is_made_in_a_database_from_before(tmp_path):
    db = sqlite3.connect(tmp_path / "h.db")
    db.executescript(
        """
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL,
            first_message INTEGER NOT NULL, last_used REAL, allowed_tools TEXT NOT NULL DEFAULT '[]',
            name TEXT NOT NULL DEFAULT '', muted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO sessions (started, first_message, name) VALUES (1, 0, 'Old');
        """
    )
    db.commit()
    db.close()
    store = ConversationStore(tmp_path / "h.db")
    assert (store.master.id, store.master.name) == (2, "Master")
    assert ConversationStore(tmp_path / "h.db").master.id == 2  # just the one


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


def test_clearing_deletes_every_message_and_keeps_the_agents(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    other = store.new_session("Jobs")
    store.append_exchange("a", "secret plans", "noted")
    last = store.exchanges_after(0)[-1].id
    store.clear()
    assert store.exchanges_after(0) == [] and store.master.id == 1 and store.get_session(other.id)
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

    # Only what the model still sees: not from before a reset.
    store.reset("chat")
    assert store.take_last("chat") is None
    assert [e.question for e in store.exchanges_after(0)] == ["first", "elsewhere"]


def test_deleting_a_session_deletes_what_was_said_in_it_and_only_its_own_photos(tmp_path):
    from athena.photos import Photo, red_square

    store = ConversationStore(tmp_path / "h.db")
    first = store.new_session("Here")
    shared, own = red_square(), Photo(b"\xff\xd8\xff" + b"own", "image/jpeg")
    store.append_exchange(first.chat_id, "[2 photos] Look", "Red.", photos=[shared, own])
    second = store.new_session("Elsewhere")
    store.append_exchange(second.chat_id, "[Photo] Again", "Still red.", photos=[shared])

    [exchange] = store.delete_session(first.id)
    assert (exchange.question, exchange.answer) == ("[2 photos] Look", "Red.")
    assert store.photos.load(shared.name) and store.photos.load(own.name) is None
    assert store.get_session(first.id) is None and store.load(first.chat_id) == []
    with pytest.raises(KeyError):
        store.delete_session(first.id)
    with pytest.raises(ValueError, match="can't be deleted"):
        store.delete_session(store.master.id)
    store.delete_session(second.id)
    assert [s.id for s in store.sessions()] == [store.master.id]


def test_sessions_can_be_named_and_muted_and_a_named_one_is_listed(tmp_path):
    store = ConversationStore(tmp_path / "h.db")
    store.rename(1, "  Maths   research ")
    store.set_muted(1, True)
    assert (store.master.name, store.master.muted) == ("Maths research", True)
    later = store.new_session("Business")
    assert [s.id for s in store.sessions()] == [1, later.id]
    assert [s.title for s in store.sessions()] == ["Maths research", "Business"]
    store.append_exchange(f"session:{later.id}", "Plan it.", "Planned.")
    store.rename(later.id, "")
    assert store.title(later.id) == "Plan it."  # after your first message in it
