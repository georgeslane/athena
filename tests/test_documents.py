"""Indexing documents from your Mac, and finding them again: the documents server runs here, started the
way the Pi starts it on the Mac, only without SSH."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import FakeEmbedder

from athena.config import DocumentsConfig, MemoryConfig
from athena.documents import DocumentIndex, DocumentsError
from athena.memory import MemoryService, MemoryStore

DIMS = 256


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Your Mac's home folder, with documents in it."""
    home = Path(os.path.realpath(tmp_path)) / "home"
    docs = home / "Documents"
    (docs / "Taxes").mkdir(parents=True)
    (docs / "notes.md").write_text("Dentist appointment on Tuesday at nine.\n\nBring the insurance card.\n")
    (docs / "Taxes" / "2025 return.txt").write_text("Council tax band D for the flat on Elm Street.\n")
    (home / "Desktop").mkdir()
    (home / "Desktop" / "recipe.md").write_text("Sourdough: feed the starter twice a day.\n")
    monkeypatch.setenv("HOME", str(home))  # the server's ~
    return home


@pytest.fixture
def athena(tmp_path, mac):
    store = MemoryStore(tmp_path / "pi" / "assistant.db", DIMS, "fake-embed")
    memory = MemoryService(store, FakeEmbedder(), MemoryConfig(chunk_chars=50))  # notes.md is two passages
    cfg = DocumentsConfig(
        command=sys.executable, args=["-m", "athena.mac_documents", "--scope", str(mac)], folders=["~/Documents"]
    )
    index = DocumentIndex(cfg, memory, tmp_path / "pi" / "logs")
    yield index
    store.close()


async def indexed(index: DocumentIndex):
    run = index.start()
    await index._task
    return run


async def test_indexing_and_finding_documents(athena, mac):
    run = await indexed(athena)
    assert run.state == "done", run.error
    assert (run.to_read, run.read, run.added, run.removed) == (2, 2, 2, 0)
    assert run.mac  # the Mac's name, from the server

    summary = athena.summary()
    assert summary["folders"] == [{"folder": "~/Documents", "files": 2, "passages": 3, "no_text": 0}]
    assert (summary["files"], summary["running"]) == (2, False) and summary["indexed"]

    [found] = await athena.memory.find("council tax band", 1, "document")
    assert found.path == str(mac / "Documents" / "Taxes" / "2025 return.txt")
    assert athena.memory.show_path(found.path) == "~/Documents/Taxes/2025 return.txt"
    # Two passages of notes.md match: it's found once, by its better one.
    hits = await athena.memory.find("dentist insurance card appointment", 5, "document")
    assert [athena.memory.show_path(h.path) for h in hits].count("~/Documents/notes.md") == 1


async def test_indexing_again_reads_only_what_changed(athena, mac):
    await indexed(athena)
    again = await indexed(athena)
    assert (again.to_read, again.unchanged, again.added, again.updated) == (0, 2, 0, 0)

    docs = mac / "Documents"
    (docs / "notes.md").write_text("The dentist moved to Wednesday.\n")
    os.utime(docs / "notes.md", (1, 1))
    (docs / "Taxes" / "2025 return.txt").unlink()
    (docs / "new.txt").write_text("The boiler service is due in March.\n")
    changed = await indexed(athena)
    assert (changed.to_read, changed.updated, changed.added, changed.removed) == (2, 1, 1, 1)
    assert athena.memory.store.document_chunks(str(docs / "notes.md")) == ["The dentist moved to Wednesday."]
    assert str(docs / "Taxes" / "2025 return.txt") not in athena.memory.store.document_sources()


async def test_folders_are_added_and_removed(athena, mac):
    await indexed(athena)
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Documents", "~/Desktop"]}))
    added = await indexed(athena)
    assert added.added == 1 and added.unchanged == 2

    # Removing one forgets its documents at once, without reaching the Mac.
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Desktop"], "command": "/nonexistent"}))
    assert athena.memory.store.document_sources() == [str(mac / "Desktop" / "recipe.md")]
    assert athena.summary()["folders"] == [{"folder": "~/Desktop", "files": 1, "passages": 1, "no_text": 0}]


async def test_a_folder_it_cant_read_keeps_what_was_indexed(athena, mac):
    await indexed(athena)
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Documents", "~/.ssh"]}))
    (mac / "Documents").rename(mac / "Elsewhere")  # as if the folder can't be listed now
    (mac / "Documents").mkdir(mode=0)
    try:
        run = await indexed(athena)
    finally:
        (mac / "Documents").chmod(0o700)
    assert run.state == "done" and set(run.folder_errors) == {"~/Documents", "~/.ssh"}
    assert run.removed == 0 and len(athena.memory.store.documents()) == 2


async def test_what_couldnt_be_read_or_had_no_text(athena, mac):
    docs = mac / "Documents"
    (docs / "binary.txt").write_bytes(b"\x00\x01")
    (docs / "empty.md").write_text("   \n")
    run = await indexed(athena)
    assert run.failures == 1 and run.failed == [
        {"file": "~/Documents/binary.txt", "why": "binary.txt isn't a text file, so there's no text to read."}
    ]
    assert run.no_text == 1 and athena.summary()["folders"][0]["no_text"] == 1
    again = await indexed(athena)  # a document with no text isn't read again until it changes
    assert again.to_read == 1 and again.failures == 1  # the one it couldn't read is tried again


async def test_a_mac_it_cant_reach_says_why(athena):
    said = "ssh: connect to host athena-laptop port 22: Operation timed out"
    script = f"import sys; sys.stderr.write({said!r} + '\\n'); sys.exit(255)"  # what SSH does
    athena.configure(athena.cfg.model_copy(update={"args": ["-c", script]}))
    run = await indexed(athena)
    assert run.state == "failed" and "Couldn't reach your Mac" in run.error
    assert "Operation timed out" in run.error and "Remote Login" in run.error
    with pytest.raises(DocumentsError, match="Couldn't reach your Mac"):
        await athena.browse()


async def test_stopping_keeps_what_was_done(athena, mac):
    (mac / "Documents" / "more.txt").write_text("More notes.\n")
    gate = asyncio.Event()
    embed = athena.memory.embedder.embed
    count = 0

    async def slow(texts, kind, titles=None):
        nonlocal count
        count += 1
        if count > 1:
            await gate.wait()  # the second document waits until stopped
        return await embed(texts, kind, titles)

    athena.memory.embedder.embed = slow
    athena.start()
    with pytest.raises(DocumentsError, match="indexing already"):
        athena.start()
    while count < 2:
        await asyncio.sleep(0.01)
    await athena.stop()
    assert athena.run.state == "stopped" and not athena.running
    assert len(athena.memory.store.documents()) == 1
    athena.memory.embedder.embed = embed
    carried_on = await indexed(athena)
    assert carried_on.unchanged == 1 and carried_on.added == 2


async def test_nothing_to_index_until_you_choose_a_folder(athena):
    athena.configure(athena.cfg.model_copy(update={"folders": []}))
    with pytest.raises(DocumentsError, match="Choose a folder"):
        athena.start()


async def test_browsing_your_mac_for_folders(athena):
    start = await athena.browse()
    assert {"name": "Documents", "path": "~/Documents"} in start["folders"]
    taxes = await athena.browse("~/Documents")
    assert taxes["folders"] == [{"name": "Taxes", "path": "~/Documents/Taxes"}] and taxes["documents"] == 1


async def test_athena_finds_and_reads_documents(athena, mac):
    await indexed(athena)
    tools = {t.name: t for t in athena.memory.tools()}
    found = json.loads(
        await tools["search_memory"].handler({"query": "council tax band", "kind": "document", "limit": 1})
    )
    assert found == [
        {
            "kind": "document",
            "file": "~/Documents/Taxes/2025 return.txt",
            "changed": found[0]["changed"],
            "relevance": found[0]["relevance"],
            "has_the_words": True,
            "passage": "Council tax band D for the flat on Elm Street.",
        }
    ]
    read = tools["read_document"].handler
    text = await read({"file": "~/Documents/notes.md"})
    assert text.startswith("[~/Documents/notes.md, changed ")
    assert text.endswith("Dentist appointment on Tuesday at nine.\n\nBring the insurance card.")
    assert await read({"file": "2025 return.txt"}) == await read({"file": str(mac / "Documents/Taxes/2025 return.txt")})
    part = await read({"file": "notes.md", "start": 8})
    assert "characters 8-" in part and part.split("\n", 1)[1].startswith("appointment")
    assert "no document called" in await read({"file": "missing.pdf"})
    assert "kind must be one of" in await tools["search_memory"].handler({"query": "x", "kind": "files"})


async def test_doctor_checks_the_mac_and_whats_indexed(config, mac):
    from athena.doctor import FAIL, OK, WARN, check_documents

    server = ["-m", "athena.mac_documents", "--scope", str(mac)]
    config.documents = DocumentsConfig(command=sys.executable, args=server, folders=["~/Documents"])
    seen = []
    await check_documents(config, lambda mark, msg: seen.append((mark, msg)))
    assert seen[0][0] == OK and "answers; it lets Athena see ~" in seen[0][1]
    assert seen[1] == (WARN, "~/Documents: not indexed yet. Press Index now on the dashboard, under Memory.")

    store = MemoryStore(config.db_path, DIMS, "fake-embed")
    await indexed(DocumentIndex(config.documents, MemoryService(store, FakeEmbedder(), MemoryConfig())))
    store.close()
    seen.clear()
    await check_documents(config, lambda mark, msg: seen.append((mark, msg)))
    assert seen[1][0] == OK and seen[1][1].startswith("2 documents indexed from ~/Documents, last on ")

    config.documents.args = ["-c", "import sys; sys.exit(255)"]
    seen.clear()
    await check_documents(config, lambda mark, msg: seen.append((mark, msg)))
    assert seen[0][0] == FAIL and "Couldn't reach your Mac" in seen[0][1]


async def test_icloud_drive_goes_by_name(athena, mac):
    icloud = mac / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Notes"
    icloud.mkdir(parents=True)
    (icloud / "Gift ideas.md").write_text("Priya: a pottery class.\n")
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Library/Mobile Documents/com~apple~CloudDocs"]}))
    await indexed(athena)
    [found] = await athena.memory.find("pottery class", 1, "document")
    assert athena.memory.show_path(found.path) == "iCloud Drive/Notes/Gift ideas.md"
    read = {t.name: t for t in athena.memory.tools()}["read_document"].handler
    assert (await read({"file": "iCloud Drive/Notes/Gift ideas.md"})).startswith("[iCloud Drive/Notes/Gift ideas.md")


async def test_a_folder_removed_while_indexing_is_forgotten_when_it_finishes(athena, mac):
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Documents", "~/Desktop"]}))
    athena.start()
    athena.configure(athena.cfg.model_copy(update={"folders": ["~/Documents"]}))  # while it runs: kept for now
    await athena._task
    assert athena.run.state == "done"
    assert all(doc.folder == "~/Documents" for doc in athena.memory.store.documents().values())
