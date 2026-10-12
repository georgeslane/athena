import pytest

from pi_assistant.memory import MemoryStore, MemoryStoreError, chunk_text, fit_dimensions, iter_text_files


async def test_remember_and_search(memory):
    sid, created = await memory.remember("George's sister Anna has her birthday on 14 March.")
    await memory.remember("George prefers oat milk in coffee.")
    await memory.remember("The car is due for its MOT in November.")
    assert created

    hits = await memory.search("when is sister Anna birthday", 2)
    assert hits[0].id == sid
    assert hits[0].distance < hits[1].distance


async def test_duplicate_facts_are_not_saved_twice(memory):
    first, created1 = await memory.remember("George likes hiking.")
    second, created2 = await memory.remember("George likes hiking.")
    assert created1 and not created2
    assert first == second
    assert memory.store.count() == {"fact": 1}


async def test_forget(memory):
    mid, _ = await memory.remember("Temporary fact about lemons.")
    assert memory.forget(mid)
    assert not memory.forget(mid)
    assert await memory.search("lemons") == []


async def test_recall_respects_distance_cutoff(memory):
    await memory.remember("George plays the cello every Tuesday.")
    memory.cfg.recall_max_distance = 0.0001
    assert await memory.recall("cello Tuesday") == []
    memory.cfg.recall_max_distance = 0.95
    assert len(await memory.recall("cello Tuesday")) == 1


async def test_ingest_replaces_previous_chunks(memory, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    doc = notes / "garden.md"
    doc.write_text("# Garden\n\nTomatoes need watering daily.\n\nRoses get pruned in February.")
    (notes / ".hidden.md").write_text("ignore me")
    (notes / "photo.jpg").write_bytes(b"\xff")

    files = iter_text_files([notes])
    assert files == [doc]
    assert await memory.ingest_file(doc) == 1
    assert await memory.ingest_file(doc) == 1  # re-ingest replaces, not duplicates
    assert memory.store.count() == {"document": 1}

    hits = await memory.search("pruning roses", kind="document")
    assert hits and hits[0].source == str(doc)
    assert await memory.search("pruning roses", kind="fact") == []


def test_chunking_respects_limit_and_keeps_text():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(20)) + "\n\n" + "x" * 3000
    chunks = chunk_text(text, max_chars=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_fit_dimensions_truncates_and_normalises():
    v = fit_dimensions([3.0, 4.0, 12.0], 2)
    assert v == pytest.approx([0.6, 0.8])
    with pytest.raises(MemoryStoreError):
        fit_dimensions([1.0], 2)


@pytest.mark.parametrize("vector", [[float("nan"), 1.0], [float("inf"), 1.0], [0.0, 0.0]])
def test_broken_vectors_are_refused_rather_than_saved(vector):
    with pytest.raises(MemoryStoreError, match="unusable vector"):
        fit_dimensions(vector, 2)


def test_changing_embedding_model_is_detected(tmp_path):
    MemoryStore(tmp_path / "m.db", 8, "model-a").close()
    with pytest.raises(MemoryStoreError, match="switch-embeddings.sh model-b"):
        MemoryStore(tmp_path / "m.db", 8, "model-b")
    with pytest.raises(MemoryStoreError, match="dimension"):
        MemoryStore(tmp_path / "m.db", 16, "model-a")
    MemoryStore(tmp_path / "m.db", 8, "model-b", allow_model_change=True).close()


async def test_reindex_switches_model(memory):
    await memory.remember("George's bike is blue.")
    memory.embedder.cfg.model = "fake-embed-v2"
    assert await memory.reindex() == 1
    assert memory.store.embedding_model == "fake-embed-v2"
    assert (await memory.search("blue bike"))[0].text == "George's bike is blue."


async def test_memory_tools(memory):
    tools = {t.name: t for t in memory.tools()}
    assert tools["forget_memory"].needs_confirmation
    assert "Saved as memory #1" in await tools["remember"].handler({"fact": "George's dog is called Biscuit."})
    found = await tools["search_memory"].handler({"query": "dog name"})
    assert "Biscuit" in found
    assert "Forgot memory #1" in await tools["forget_memory"].handler({"id": 1})
    assert "No memory #1" in await tools["forget_memory"].handler({"id": "1"})
    assert "Error" in await tools["forget_memory"].handler({"id": "abc"})


# -- past conversations ---------------------------------------------------------------------------------


def chats(tmp_path, *exchanges):
    from pi_assistant.history import ConversationStore

    history = ConversationStore(tmp_path / "history.db")
    for question, answer in exchanges:
        history.append_exchange("chat", question, answer)
    return history


async def test_past_exchanges_can_be_searched_but_arent_recalled_automatically(memory, tmp_path):
    history = chats(
        tmp_path,
        ("Which restaurant did you suggest for Friday?", "Try Dishoom in King's Cross: book ahead."),
        ("What's the weather like?", "Sunny, 21 degrees."),
    )
    await memory.remember("George is vegetarian.")
    assert await memory.index_conversations(history) == 2
    assert await memory.index_conversations(history) == 0  # each is added once
    assert memory.store.count() == {"fact": 1, "conversation": 2}

    hits = await memory.search("restaurant Friday Dishoom")
    assert hits[0].kind == "conversation"
    assert (
        hits[0].text
        == "User: Which restaurant did you suggest for Friday?\nAssistant: Try Dishoom in King's Cross: book ahead."
    )
    assert all(h.kind != "conversation" for h in await memory.recall("restaurant Friday Dishoom"))
    only = await memory.search("restaurant Friday Dishoom", kind="conversation")
    assert [h.kind for h in only] == ["conversation", "conversation"] and "Dishoom" in only[0].text

    found = await {t.name: t for t in memory.tools()}["search_memory"].handler({"query": "Dishoom restaurant"})
    assert '"kind": "conversation"' in found and "Dishoom" in found
    history.close()


async def test_exchanges_said_while_embeddings_were_down_are_added_later(memory, tmp_path):
    history = chats(tmp_path, ("one", "first"), ("two", "second"))
    real = memory.embedder.embed

    async def down(*args, **kwargs):
        raise ConnectionError("Ollama isn't running")

    memory.embedder.embed = down
    with pytest.raises(ConnectionError):
        await memory.index_conversations(history)
    assert memory.store.count() == {}
    memory.embedder.embed = real
    history.append_exchange("chat", "three", "third")
    assert await memory.index_conversations(history) == 3
    history.close()


async def test_long_exchanges_are_cut_short(memory, tmp_path):
    history = chats(tmp_path, ("tell me everything", "word " * 1000))
    memory.cfg.chunk_chars = 200
    await memory.index_conversations(history)
    [entry] = memory.store.recent(kind="conversation")
    assert len(entry.text) == 200 and entry.text.endswith("…")
    history.close()


async def test_forgetting_everything(memory, tmp_path):
    history = chats(tmp_path, ("q", "a"))
    await memory.remember("George's bike is blue.")
    await memory.index_conversations(history)
    memory.store.clear()
    assert memory.store.count() == {}
    assert await memory.search("blue bike") == []
    assert memory.store.get_meta("dimensions") == str(memory.store.dimensions)
    # The index works afterwards, and old exchanges aren't added back from the history.
    await memory.remember("George's car is red.")
    assert (await memory.search("red car"))[0].text == "George's car is red."
    history.close()


async def test_reindexing_includes_past_exchanges(memory, tmp_path):
    history = chats(tmp_path, ("Where did I park?", "Level 3 of the station car park."))
    await memory.index_conversations(history)
    memory.embedder.cfg.model = "fake-embed-v2"
    assert await memory.reindex() == 1
    assert (await memory.search("park level station"))[0].kind == "conversation"
    history.close()


# -- documents: an index of their own ---------------------------------------------------------------


async def test_documents_are_searched_when_asked_not_recalled_with_every_message(memory, tmp_path):
    doc = tmp_path / "garden.md"
    doc.write_text("Roses get pruned in February.")
    await memory.ingest_file(doc)
    await memory.remember("George prunes the roses himself.")
    assert {h.kind for h in await memory.search("pruning roses February")} == {"fact", "document"}
    assert [h.kind for h in await memory.recall("pruning roses February")] == ["fact"]


def test_documents_move_out_of_the_facts_index(tmp_path):
    """Before documents had an index of their own, they shared the facts' one."""
    import sqlite_vec

    from pi_assistant.memory import connect_db

    store = MemoryStore(tmp_path / "m.db", 4, "model")
    store.add("A fact.", [1, 0, 0, 0], "fact")
    store.close()
    conn = connect_db(tmp_path / "m.db", vectors=True)
    with conn:  # what an older Athena wrote for an ingested note
        cur = conn.execute(
            "INSERT INTO memories (kind, source, text, created_at) VALUES ('document', 'n.md', 'A note.', 'x')"
        )
        conn.execute(
            "INSERT INTO memory_vectors (rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, sqlite_vec.serialize_float32([0, 1, 0, 0])),
        )
    conn.close()

    store = MemoryStore(tmp_path / "m.db", 4, "model")
    assert [h.text for h in store.search([0, 1, 0, 0], 5, ("document",))] == ["A note."]
    assert [h.text for h in store.search([0, 1, 0, 0], 5, ("fact",))] == ["A fact."]
    store.close()
    MemoryStore(tmp_path / "m.db", 4, "model").close()  # and again: nothing left to move


def test_a_document_is_replaced_whole(tmp_path):
    from pi_assistant.memory import IndexedDocument

    store = MemoryStore(tmp_path / "m.db", 4, "model")
    store.put_document(IndexedDocument("/Users/g/a.txt", "~", 10, 1.0), [("one", [1, 0, 0, 0]), ("two", [0, 1, 0, 0])])
    store.put_document(IndexedDocument("/Users/g/a.txt", "~", 12, 2.0), [("three", [0, 0, 1, 0])])
    assert store.document_chunks("/Users/g/a.txt") == ["three"]
    assert [h.text for h in store.search([1, 0, 0, 0], 5, ("document",))] == ["three"]  # no stale vectors
    doc = store.documents()["/Users/g/a.txt"]
    assert (doc.size, doc.modified, doc.chunks) == (12, 2.0, 1)

    store.clear()  # forgetting everything forgets documents too
    assert store.documents() == {} and store.document_sources() == []
    store.close()
