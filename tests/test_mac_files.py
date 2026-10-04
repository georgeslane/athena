"""The read-only files server that runs on the Mac, driven through the assistant's own MCP client."""

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pi_assistant.config import MCPServerConfig
from pi_assistant.mac_files import Folders
from pi_assistant.mcp_manager import MCPManager


@contextlib.asynccontextmanager
async def files_server(*roots, env=None):
    server = MCPServerConfig(
        command=sys.executable, args=["-m", "pi_assistant.mac_files", *map(str, roots)], env=env or {}, confirm=[]
    )
    manager = MCPManager({"files": server})
    await manager.start()
    try:
        status = manager.status()[0]
        assert status.connected, status.error
        tools = {t.name: t for t in manager.tools()}

        async def call(name, **args):
            return await tools[name].handler(args)

        call.tools = set(tools)
        yield call
    finally:
        await manager.stop()


@pytest.fixture
def home(tmp_path):
    """A folder to share, with hidden files and symlinks in it, and a private folder beside it."""
    root = Path(os.path.realpath(tmp_path))  # macOS's temporary folder is behind a symlink
    docs, outside = root / "docs", root / "outside"
    (docs / "Taxes").mkdir(parents=True)
    (docs / ".git").mkdir()
    outside.mkdir()
    (docs / "notes.txt").write_text("Dentist on Tuesday at 9.\n")
    (docs / "Taxes" / "2025.txt").write_text("Council tax band D.\n")
    (docs / ".env").write_text("API_KEY=very-secret\n")
    (docs / ".git" / "config").write_text("[secret]\n")
    (outside / "private.txt").write_text("very-secret diary\n")
    (docs / "escape").symlink_to(outside)  # a way out of the folder
    (docs / "taxes-link").symlink_to(docs / "Taxes")  # a way back into it
    return docs


async def test_it_can_only_read(home):
    async with files_server(home) as call:
        assert call.tools == {"search_files", "list_folder", "read_file"}


async def test_lists_and_reads_what_is_in_the_folder(home):
    async with files_server(home) as call:
        assert (await call("list_folder")).startswith(f"{home}/  (folder")

        listing = await call("list_folder", path=str(home))
        lines = listing.splitlines()
        assert lines[0].startswith(f"{home}/Taxes/  (folder")  # folders first, and only once
        assert lines[1].startswith(f"{home}/notes.txt  (25 bytes")
        assert len(lines) == 2  # no .env, .git or the way out

        text = await call("read_file", path=str(home / "notes.txt"))
        assert text == f"[{home}/notes.txt, characters 0-25 of 25]\nDentist on Tuesday at 9.\n"
        assert "Council tax" in await call("read_file", path=str(home / "taxes-link" / "2025.txt"))


@pytest.mark.parametrize(
    "path",
    [".env", ".git/config", "escape/private.txt", "../outside/private.txt", "Taxes/../../outside/private.txt"],
)
async def test_refuses_hidden_files_and_anything_outside(home, path):
    async with files_server(home) as call:
        result = await call("read_file", path=f"{home}/{path}")
        listing = await call("list_folder", path=f"{home}/{path.rsplit('/', 1)[0]}") if "/" in path else ""
    assert result.startswith("Error") and "isn't in a folder you can see" in result
    assert "secret" not in result + listing


async def test_needs_full_paths(home):
    async with files_server(home) as call:
        result = await call("read_file", path="notes.txt")
    assert "Use a full path" in result


async def test_reads_long_files_in_parts(home):
    (home / "long.txt").write_text("abcdefghij" * 3)
    async with files_server(home) as call:
        part = await call("read_file", path=str(home / "long.txt"), start=5, max_chars=10)
    assert part == f"[{home}/long.txt, characters 5-15 of 30; to continue, use start=15]\nfghijabcde"


async def test_reads_pdfs(home):
    (home / "statement.pdf").write_bytes(_pdf("The fund returned 7.4 percent"))
    async with files_server(home) as call:
        text = await call("read_file", path=str(home / "statement.pdf"))
    assert "--- page 1 ---\nThe fund returned 7.4 percent" in text


@pytest.mark.skipif(shutil.which("textutil") is None, reason="textutil comes with macOS")
async def test_reads_word_documents(home):
    (home / "plan.txt").write_text("Quarterly plan\n")
    subprocess.run(["textutil", "-convert", "docx", str(home / "plan.txt"), "-output", str(home / "plan.docx")])
    async with files_server(home) as call:
        assert "Quarterly plan" in await call("read_file", path=str(home / "plan.docx"))


async def test_binary_files_have_no_text(home):
    (home / "photo.jpg").write_bytes(b"\xff\xd8\xff\x00\x10JFIF")
    async with files_server(home) as call:
        result = await call("read_file", path=str(home / "photo.jpg"))
    assert result.startswith("Error") and "isn't a text file" in result


async def test_search_uses_spotlight_and_keeps_to_the_folders(home, tmp_path):
    # A stand-in for Spotlight's mdfind, which also finds things the server mustn't show.
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log, results = tmp_path / "mdfind.log", tmp_path / "mdfind.out"
    (stubs / "mdfind").write_text('#!/bin/sh\necho "$@" >> "$MDFIND_LOG"\ncat "$MDFIND_RESULTS"\n')
    (stubs / "mdfind").chmod(0o755)
    found = ["notes.txt", "Taxes/2025.txt", ".env", "escape/private.txt", "../outside/private.txt", "gone.txt"]
    results.write_text("".join(f"{home}/{name}\n" for name in found))
    os.utime(home / "notes.txt", (1_000_000_000, 1_000_000_000))  # older
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "MDFIND_LOG": str(log), "MDFIND_RESULTS": str(results)}

    async with files_server(home, env=env) as call:
        everything = await call("search_files", query="council tax")
        first = await call("search_files", query="council tax", limit=1)
        await call("search_files", query="2025", names_only=True, folder=str(home / "Taxes"))

    assert [line.split("  (")[0] for line in everything.splitlines()] == [f"{home}/Taxes/2025.txt", f"{home}/notes.txt"]
    assert first.splitlines()[1] == "...and 1 more. Search with more words, or within a folder."
    assert log.read_text().splitlines()[-1] == f"-onlyin {home}/Taxes -name 2025"


def test_skips_folders_that_do_not_exist(tmp_path):
    assert Folders([str(tmp_path), str(tmp_path / "missing")]).roots == [Path(os.path.realpath(tmp_path))]
    with pytest.raises(ValueError):
        Folders([str(tmp_path / "missing")])


def _pdf(text: str) -> bytes:
    """A one-page PDF with a line of text in it."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return pdf + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
