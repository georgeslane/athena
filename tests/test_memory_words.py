"""Searching memory by its words as well as by meaning: the full-text index, the queries made for it, and how
the two searches are merged."""

import json
import sqlite3

import pytest
from conftest import DIMS, FakeEmbedder

from athena.config import MemoryConfig
from athena.memory import IndexedDocument, MemoryService, MemoryStore, words_query
from athena.memory import store as memory_store
from athena.memory.search import MAX_WORDS


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("When is Anna's birthday?", '"anna" OR "birthday"'),  # common words and possessives left out
        ("Anna’s ANNA anna", '"anna"'),  # once each
        ("the ticker AAPL_US_EQ", '"ticker" OR "aapl us eq"'),  # its parts together, as the index has them
        ("george@example.com", '"george example com"'),
        ("invoice 2026 a 5", '"invoice" OR "2026" OR "5"'),  # single digits count, single letters don't
        ('notes on "Project Nimbus" and "budget"', '"project nimbus" AND "budget"'),  # quoted: must be there
        ("café", '"café"'),  # the index's tokenizer takes the accent off, for the query as for the text
        ("what is it", None),
        ("", None),
        ('""', None),
    ],
)
def test_a_search_by_words_is_for_the_words_that_tell_things_apart(query, expected):
    assert words_query(query) == expected


def test_a_long_query_is_cut_to_the_most_words():
    assert words_query(" ".join(f"word{i}" for i in range(30))).count(" OR ") == MAX_WORDS - 1


@pytest.mark.parametrize(
    "query",
    [
        'text:secret OR NEAR(anna birthday) "unbalanced',
        "anna* ^birthday -march +x",
        "{text} : ( ) AND NOT OR NEAR",
        "'; DROP TABLE memories; --",
        '"" "a" """',
        "\x00\x1f‮",
    ],
)
async def test_nothing_in_a_query_is_read_as_fts5s_own_syntax(memory, query):
    await memory.remember("George's sister Anna has her birthday in March.")
    words = words_query(query)
    assert words is None or all(c.isalnum() or c in ' "' for c in words.replace(" OR ", "").replace(" AND ", ""))
    if words:
        memory.store.search_words(words, 5, ("fact",))  # doesn't raise
    await memory.find(query)
    assert memory.store.count() == {"fact": 1}


# -- the index ------------------------------------------------------------------------------------


async def test_the_index_follows_every_change_to_memory(memory):
    def found(words, kinds=("fact",)):
        return [h.text for h in memory.store.search_words(words_query(words), 5, kinds)]

    anna, _ = await memory.remember("George's sister Anna has her birthday on 14 March.")
    await memory.remember("The boiler is serviced every March.")
    assert found("birthdays") == ["George's sister Anna has her birthday on 14 March."]  # endings don't matter
    assert set(found("march")) == {"George's sister Anna has her birthday on 14 March.",
                                   "The boiler is serviced every March."}  # fmt: skip
    memory.forget(anna)
    assert found("birthday") == [] and indexed(memory.store, "birthday") == 0  # gone from the index itself

    store = memory.store
    store.put_document(IndexedDocument("/u/a.txt", "~", 1, 1.0), [("Council tax band D", [1.0] + [0.0] * (DIMS - 1))])
    assert found("council", ("document",)) == ["Council tax band D"] and found("council") == []  # by kind
    store.put_document(IndexedDocument("/u/a.txt", "~", 2, 2.0), [("Water rates", [1.0] + [0.0] * (DIMS - 1))])
    assert found("council", ("document",)) == [] and found("water", ("document",)) == ["Water rates"]
    assert indexed(store, "council") == 0
    store.remove_documents(["/u/a.txt"])
    assert found("water", ("document",)) == [] and indexed(store, "water") == 0


def indexed(store, word):
    """How many entries the index itself has ``word`` for, whether or not they're still in memory."""
    return store._conn.execute(
        "SELECT COUNT(*) FROM memory_words WHERE memory_words MATCH ?", (f'"{word}"',)
    ).fetchone()[0]


async def test_forgetting_everything_leaves_no_word_behind(memory, tmp_path):
    await memory.remember("George's locker code is zanzibarquux.")
    await memory.ingest_file(_file(tmp_path, "notes.md", "Meeting about qwertyplugh on Friday."))
    memory.store.clear()
    for word in ("zanzibarquux", "qwertyplugh"):
        assert memory.store.search_words(f'"{word}"', 5, ("fact", "document")) == []
    on_disk = b"".join(p.read_bytes() for p in tmp_path.glob("test.db*"))
    assert b"zanzibarquux" not in on_disk and b"qwertyplugh" not in on_disk
    await memory.remember("George's car is red.")  # it works afterwards
    assert [h.text for h in memory.store.search_words('"red"', 5, ("fact",))] == ["George's car is red."]


async def test_memories_from_before_the_index_are_added_to_it(tmp_path):
    store = MemoryStore(tmp_path / "m.db", DIMS, "fake-embed")
    service = MemoryService(store, FakeEmbedder(), MemoryConfig())
    await service.remember("The spare key is under the blue pot.")
    store.close()
    conn = sqlite3.connect(tmp_path / "m.db")  # as a database from before had it: no index, no triggers
    conn.executescript(
        "DROP TRIGGER memories_words_insert; DROP TRIGGER memories_words_delete; DROP TRIGGER memories_words_update;"
        "DROP TABLE memory_words;"
    )
    conn.close()

    store = MemoryStore(tmp_path / "m.db", DIMS, "fake-embed")
    assert [h.text for h in store.search_words('"key"', 5, ("fact",))] == ["The spare key is under the blue pot."]
    store.close()


async def test_without_fts5_memory_is_searched_by_meaning_alone(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(memory_store, "WORDS_TOKENIZER", "no_such_tokenizer")  # creating the index fails
    store = MemoryStore(tmp_path / "m.db", DIMS, "fake-embed")
    assert not store.words and "Searching memory by its words is off" in caplog.text
    service = MemoryService(store, FakeEmbedder(), MemoryConfig())
    await service.remember("The spare key is under the blue pot.")  # saving still works
    assert store.search_words('"key"', 5, ("fact",)) == []
    [found] = await service.find("spare key")
    assert found.text == "The spare key is under the blue pot." and not found.words
    store.close()


# -- searching by both ----------------------------------------------------------------------------


class MissesTheMeaning(FakeEmbedder):
    """Every query means oat milk to it, as an embeddings model can miss a name or a number it doesn't know."""

    async def embed(self, texts, kind, titles=None):
        return await super().embed(["oat milk coffee"] * len(texts) if kind == "query" else texts, kind, titles)


class Down(FakeEmbedder):
    async def embed(self, texts, kind, titles=None):
        if kind == "query":
            raise ConnectionError("embeddings server unreachable")
        return await super().embed(texts, kind, titles)


async def service_with(tmp_path, embedder, facts):
    service = MemoryService(MemoryStore(tmp_path / "m.db", DIMS, "fake-embed"), FakeEmbedder(), MemoryConfig())
    for fact in facts:
        await service.remember(fact)
    service.embedder = embedder
    return service


FACTS = [
    "George prefers oat milk in his coffee.",
    "Project Nimbus is due at the end of November.",
    "The car's MOT is in March.",
    "George's dentist is on Tuesday.",
]


async def test_what_has_the_words_is_found_when_meaning_misses_it(tmp_path):
    service = await service_with(tmp_path, MissesTheMeaning(), FACTS)
    found = await service.find("when's Nimbus due?", 3)
    assert found[0].text == "Project Nimbus is due at the end of November." and found[0].words
    assert found[0].distance is not None  # measured by meaning too, like everything else
    assert found[1].text == "George prefers oat milk in his coffee." and not found[1].words
    service.store.close()


async def test_when_meaning_cant_be_searched_words_still_are(tmp_path, caplog):
    service = await service_with(tmp_path, Down(), FACTS)
    [found] = await service.find("Nimbus")
    assert found.text.startswith("Project Nimbus") and found.distance is None and found.words
    assert "Searching memory by its words alone" in caplog.text
    tool = {t.name: t for t in service.tools()}["search_memory"]
    [described] = json.loads(await tool.handler({"query": "Nimbus"}))
    assert described["relevance"] is None and described["has_the_words"] is True
    with pytest.raises(ConnectionError):  # nothing to search by words either: it's an error, as before
        await service.find("what is it")
    service.store.close()


async def test_the_model_is_told_which_results_have_the_words_and_how_to_ask_for_exact_ones(tmp_path):
    service = await service_with(tmp_path, FakeEmbedder(), FACTS)
    tool = {t.name: t for t in service.tools()}["search_memory"]
    assert "double quotes" in tool.description
    results = json.loads(await tool.handler({"query": '"oat milk"'}))
    assert results[0]["text"] == "George prefers oat milk in his coffee." and results[0]["has_the_words"] is True
    assert all("has_the_words" not in r for r in results[1:]) and isinstance(results[0]["relevance"], float)
    service.store.close()


def _file(folder, name, text):
    path = folder / name
    path.write_text(text)
    return path
