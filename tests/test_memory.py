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


def test_changing_embedding_model_is_detected(tmp_path):
    MemoryStore(tmp_path / "m.db", 8, "model-a").close()
    with pytest.raises(MemoryStoreError, match="reindex"):
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
