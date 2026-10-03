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
