"""Testing embeddings models and switching between them: `pi-assistant embeddings test` and `use`."""

import asyncio
import sqlite3

import pytest
from conftest import DIMS

from pi_assistant import cli
from pi_assistant.config import EmbeddingsConfig, MemoryConfig, load_config
from pi_assistant.memory import Embedder, MemoryService, MemoryStore
from pi_assistant.search_eval import DIFFERENT, DUPLICATES, QUESTIONS, UNRELATED, SearchReport, run_search_test

CONFIG = """\
# Athena's settings
[llm]
base_url = "http://127.0.0.1:9/v1"
model = "test-model"

[embeddings]
base_url = "{url}"
model = "{model}"        # the embeddings model
dimensions = {dims}

[memory]
recall_max_distance = 0.55   # tuned for the old model
"""


def _cfg(url: str, model: str = "fake-a") -> EmbeddingsConfig:
    return EmbeddingsConfig(base_url=url, model=model, dimensions=DIMS)


# -- the search test -------------------------------------------------------------------------------------


async def test_the_search_test_measures_a_model(embeddings_server):
    report = await run_search_test(_cfg(embeddings_server.url))
    assert not report.error
    assert report.questions == len(QUESTIONS) and len(report.unrelated) == len(UNRELATED)
    assert (len(report.duplicates), len(report.different)) == (len(DUPLICATES), len(DIFFERENT))
    assert report.top1 >= 10  # word overlap finds plenty of them
    assert report.per_text_ms > 0
    # Queries and memories go in with EmbeddingGemma's prompts, as Athena sends them.
    inputs = [text for request in embeddings_server.requests for text in request["input"]]
    assert "task: search result | query: When is Priya's birthday?" in inputs
    assert "title: sourdough | text: Feed the starter" in "\n".join(inputs)


async def test_a_model_that_ignores_meaning_does_worse(embeddings_server):
    good = await run_search_test(_cfg(embeddings_server.url), "fake-a")
    noise = await run_search_test(_cfg(embeddings_server.url), "fake-noise")
    assert noise.top1 < good.top1 and noise.misses


@pytest.mark.parametrize(
    ("model", "error"),
    [
        ("fake-nan", "unusable vector"),
        ("missing-model", "ollama pull missing-model"),
    ],
)
async def test_a_broken_or_missing_model_is_reported(embeddings_server, model, error):
    report = await run_search_test(_cfg(embeddings_server.url), model)
    assert error in report.error


async def test_an_unreachable_server_is_reported():
    report = await run_search_test(_cfg("http://127.0.0.1:9/v1"))
    assert report.error == "couldn't reach the embeddings server"


def test_the_suggested_cut_offs():
    r = SearchReport("m", right=[0.10, 0.20, 0.25], unrelated=[0.35, 0.50])
    assert r.recall(0.30) == (3, 0) and r.recall(0.40) == (3, 1)
    assert 0.25 <= r.best_recall_cutoff() < 0.35  # between the furthest right memory and the nearest unrelated one
    r = SearchReport("m", duplicates=[0.01, 0.02], different=[0.05, 0.2])
    assert r.duplicates_at(0.04) == (2, 0) and r.best_duplicate_cutoff() == 0.03  # a third of the way into the gap
    r = SearchReport("m", duplicates=[0.06], different=[0.05])
    assert r.best_duplicate_cutoff() is None  # nothing separates them


# -- re-embedding ---------------------------------------------------------------------------------------


async def test_reembedding_changes_nothing_unless_it_finishes(tmp_path, embeddings_server):
    store = MemoryStore(tmp_path / "m.db", DIMS, "fake-a")
    memory = MemoryService(store, Embedder(_cfg(embeddings_server.url)), MemoryConfig())
    for i in range(40):  # more than one batch
        await memory.remember(f"Fact number {i} is about topic{i}.")
    before = await memory.search("topic7", 1)

    # The server fails part way: the second batch of 32.
    class Flaky(Embedder):
        calls = 0

        async def embed(self, texts, kind, titles=None):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("the embeddings server fell over")
            return await super().embed(texts, kind, titles)

    flaky = MemoryService(store, Flaky(_cfg(embeddings_server.url, "fake-b")), MemoryConfig())
    with pytest.raises(RuntimeError):
        await flaky.reindex()
    assert store.get_meta("embedding_model") == "fake-a"
    assert await memory.search("topic7", 1) == before  # every vector is still the old model's

    seen = []
    switched = MemoryService(store, Embedder(_cfg(embeddings_server.url, "fake-b")), MemoryConfig())
    n = len(store.all_entries())  # a few may have been taken as duplicates
    assert n > 32
    assert await switched.reindex(lambda done, total: seen.append((done, total))) == n
    assert seen == [(32, n), (n, n)]
    assert store.get_meta("embedding_model") == "fake-b"
    assert (await switched.search("topic7", 1))[0].text == "Fact number 7 is about topic7."
    store.close()
    await memory.embedder.close()
    await switched.embedder.close()


# -- switching: pi-assistant embeddings use -------------------------------------------------------------


@pytest.fixture
def athena(tmp_path, embeddings_server, monkeypatch):
    """A config.toml and a memory database made with fake-a, and Athena not running."""
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(url=embeddings_server.url, model="fake-a", dims=DIMS))
    path.chmod(0o600)
    (tmp_path / ".env").write_text("")
    monkeypatch.setattr(cli, "_athena_running", lambda: False)
    return path


async def _remember(path, *facts):
    cfg = load_config(path)
    store = MemoryStore(cfg.db_path, DIMS, cfg.embeddings.model)
    memory = MemoryService(store, Embedder(cfg.embeddings), cfg.memory)
    for fact in facts:
        await memory.remember(fact)
    store.close()
    await memory.embedder.close()


async def _search(path, query):
    cfg = load_config(path)
    store = MemoryStore(cfg.db_path, DIMS, cfg.embeddings.model)
    memory = MemoryService(store, Embedder(cfg.embeddings), cfg.memory)
    try:
        return (await memory.search(query, 1))[0].text
    finally:
        store.close()
        await memory.embedder.close()


def _run(path, *args):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["-c", str(path), "embeddings", *args])
    return exit_.value.code


def test_switching_models(athena, capsys):
    asyncio.run(_remember(athena, "Sam is allergic to peanuts.", "The boiler is due a service in March."))
    original = athena.read_text()

    assert _run(athena, "use", "fake-b", "--yes") == 0
    out = capsys.readouterr().out
    assert "change embeddings.model from fake-a to fake-b" in out and "re-embed 2 memories" in out

    cfg = load_config(athena)
    assert cfg.embeddings.model == "fake-b"
    assert cfg.memory.recall_max_distance != 0.55  # set to what suits fake-b
    text = athena.read_text()
    assert "# the embeddings model" in text and "# Athena's settings" in text  # comments kept
    # (fake-b can't tell "locker 42" from "locker 24", so no duplicate cut-off suits it and that's left alone.)
    assert "duplicate_distance" not in text
    assert (athena.parent / "config.toml.bak").read_text() == original
    assert athena.stat().st_mode & 0o777 == 0o600

    # Athena starts with it, and finds memories with it.
    found = asyncio.run(_search(athena, "Who is allergic to peanuts?"))
    assert found == "Sam is allergic to peanuts."

    assert _run(athena, "use", "fake-b") == 0
    assert "already uses fake-b" in capsys.readouterr().out


def test_nothing_changes_if_you_say_no(athena, monkeypatch, capsys):
    asyncio.run(_remember(athena, "Sam is allergic to peanuts."))
    original = athena.read_text()
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert _run(athena, "use", "fake-b") == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert athena.read_text() == original and not (athena.parent / "config.toml.bak").exists()


def test_nothing_changes_if_reembedding_fails(athena, monkeypatch, capsys):
    asyncio.run(_remember(athena, "Sam is allergic to peanuts."))
    original = athena.read_text()

    async def fail(cfg):
        raise RuntimeError("the embeddings server fell over")

    monkeypatch.setattr(cli, "_reembed", fail)
    assert _run(athena, "use", "fake-b", "--yes") == 1
    assert "config.toml is as it was" in capsys.readouterr().out
    assert athena.read_text() == original


def test_it_wont_switch_while_athena_runs(athena, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_athena_running", lambda: True)
    original = athena.read_text()
    assert _run(athena, "use", "fake-b", "--yes") == 1
    assert "sudo systemctl stop pi-assistant" in capsys.readouterr().out
    assert athena.read_text() == original


@pytest.mark.parametrize("model", ["fake-nan", "missing-model"])
def test_a_model_that_doesnt_work_isnt_switched_to(athena, model, capsys):
    original = athena.read_text()
    assert _run(athena, "use", model, "--yes") == 1
    assert f"Nothing was changed: {model} didn't work" in capsys.readouterr().out
    assert athena.read_text() == original


def test_a_worse_model_needs_you_to_say_so(athena, monkeypatch, capsys):
    asyncio.run(_remember(athena, "Sam is allergic to peanuts."))
    assert _run(athena, "use", "fake-noise", "--yes") == 1
    assert "found the right memory for fewer questions" in capsys.readouterr().out
    assert load_config(athena).embeddings.model == "fake-a"

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert _run(athena, "use", "fake-noise") == 0
    assert load_config(athena).embeddings.model == "fake-noise"


def test_switching_after_changing_config_toml_by_hand(athena, embeddings_server, capsys):
    asyncio.run(_remember(athena, "Sam is allergic to peanuts."))
    athena.write_text(CONFIG.format(url=embeddings_server.url, model="fake-b", dims=DIMS))
    assert _run(athena, "use", "fake-b", "--yes") == 0  # the memories are still fake-a's, so it re-embeds
    assert "re-embed 1 memories with fake-b" in capsys.readouterr().out
    conn = sqlite3.connect(load_config(athena).db_path)
    assert conn.execute("SELECT value FROM memory_meta WHERE key = 'embedding_model'").fetchone() == ("fake-b",)
    conn.close()


def test_testing_models_from_the_command_line(athena, capsys):
    assert _run(athena, "test", "-m", "fake-a", "-m", "fake-b") == 0
    out = capsys.readouterr().out
    assert "fake-a\n" in out and "fake-b\n" in out and "auto-recall:  at your cut-off of 0.55" in out
    assert _run(athena, "test", "-m", "missing-model") == 1
