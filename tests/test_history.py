from pi_assistant.history import ConversationStore


def test_history_window_drops_half_at_once(tmp_path):
    store = ConversationStore(tmp_path / "h.db", max_messages=8)
    for i in range(4):
        store.append_exchange("chat", f"q{i}", f"a{i}")
    assert len(store.load("chat")) == 8

    store.append_exchange("chat", "q4", "a4")  # 10 messages > 8: trim to the newest 4
    msgs = store.load("chat")
    assert [m["content"] for m in msgs] == ["q3", "a3", "q4", "a4"]
    assert msgs[0]["role"] == "user"

    # The window then stays put (stable prompt prefix) until it fills up again.
    store.append_exchange("chat", "q5", "a5")
    assert [m["content"] for m in store.load("chat")][:2] == ["q3", "a3"]


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
