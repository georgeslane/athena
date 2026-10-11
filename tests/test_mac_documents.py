"""The documents server that runs on your Mac, driven through the assistant's own MCP client."""

import contextlib
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pi_assistant import mac_documents
from pi_assistant.config import MCPServerConfig
from pi_assistant.mac_documents import Scope
from pi_assistant.mcp_manager import MCPManager


@contextlib.asynccontextmanager
async def documents_server(home: Path, *scope: Path):
    args = ["-m", "pi_assistant.mac_documents", *[a for root in scope for a in ("--scope", str(root))]]
    server = MCPServerConfig(command=sys.executable, args=args, env={"HOME": str(home)}, confirm=[])
    manager = MCPManager({"documents": server})
    await manager.start()
    try:
        status = manager.status()[0]
        assert status.connected, status.error
        tools = {t.name: t for t in manager.tools()}

        async def call(name, **args):
            text = await tools[name].handler(args)
            return text if text.startswith("Error") else json.loads(text)

        call.tools = set(tools)
        yield call
    finally:
        await manager.stop()


@pytest.fixture
def home(tmp_path):
    """A home folder with documents, hidden things, ~/Library with iCloud Drive in it, and a way out."""
    home = Path(os.path.realpath(tmp_path)) / "home"  # macOS's temporary folder is behind a symlink
    docs = home / "Documents"
    (docs / "Taxes").mkdir(parents=True)
    (docs / ".git").mkdir()
    (docs / "notes.md").write_text("# Dentist\nTuesday at 9.\n")
    (docs / "Taxes" / "2025.txt").write_text("Council tax band D.\n")
    (docs / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (docs / ".env").write_text("API_KEY=very-secret\n")
    (docs / ".git" / "notes.txt").write_text("secret\n")
    (docs / "Tool.app" / "Contents").mkdir(parents=True)
    (docs / "Tool.app" / "Contents" / "readme.txt").write_text("inside a bundle\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "notes.txt").write_text("secret\n")
    (home / "Library" / "Mail").mkdir(parents=True)
    (home / "Library" / "Mail" / "inbox.txt").write_text("secret\n")
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Recipes"
    icloud.mkdir(parents=True)
    (icloud / "bread.md").write_text("500 g flour\n")
    outside = home.parent / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("very-secret diary\n")
    (docs / "escape").symlink_to(outside)  # a way out of the home folder
    (docs / "escape.txt").symlink_to(outside / "private.txt")
    return home


def paths(listing, folder=0):
    return [Path(f[0]).name for f in listing["folders"][folder]["files"]]


async def test_it_can_only_look(home):
    async with documents_server(home, home) as call:
        assert call.tools == {"about", "browse", "list_documents", "read_documents"}
        about = await call("about")
    assert about["home"] == str(home) and about["scope"] == ["~"] and ".pdf" in about["kinds"]


async def test_lists_documents_and_nothing_hidden_or_private(home):
    async with documents_server(home, home) as call:
        listing = await call(
            "list_documents", folders=["~/Documents", "~/Library/Mobile Documents/com~apple~CloudDocs"]
        )
        whole = await call("list_documents", folders=["~"])
    assert paths(listing, 0) == ["notes.md", "2025.txt"]  # not .env, .git, the bundle, the photo, or the way out
    assert paths(listing, 1) == ["bread.md"]  # iCloud Drive is the one part of ~/Library it may see
    notes = listing["folders"][0]["files"][0]
    assert notes[0] == str(home / "Documents" / "notes.md") and notes[1] == len("# Dentist\nTuesday at 9.\n")
    assert isinstance(notes[2], float)
    assert paths(whole) == ["notes.md", "2025.txt"]  # all of ~ skips ~/Library, so iCloud Drive needs adding itself


async def test_overlapping_folders_list_each_document_once(home):
    async with documents_server(home, home) as call:
        listing = await call("list_documents", folders=["~/Documents", "~/Documents/Taxes"])
    assert paths(listing, 0) == ["notes.md", "2025.txt"] and paths(listing, 1) == []


@pytest.mark.parametrize("folder", ["~/.ssh", "~/Library/Mail", "~/Documents/escape", "/etc", "Documents"])
async def test_refuses_folders_it_mayn_t_look_in(home, folder):
    async with documents_server(home, home) as call:
        listing = await call("list_documents", folders=[folder])
    [entry] = listing["folders"]
    assert "files" not in entry and entry["error"]


async def test_the_scope_is_decided_on_the_mac(home):
    async with documents_server(home, home / "Documents" / "Taxes") as call:
        listing = await call("list_documents", folders=["~/Documents", "~/Documents/Taxes"])
        about = await call("about")
    assert "error" in listing["folders"][0] and paths(listing, 1) == ["2025.txt"]
    assert about["scope"] == ["~/Documents/Taxes"]


async def test_reads_text_and_says_what_it_couldn_t(home):
    docs = home / "Documents"
    (docs / "statement.pdf").write_bytes(_pdf("The fund returned 7.4 percent"))
    (docs / "scan.pdf").write_bytes(_pdf(""))  # a scan: a page, but no text on it
    (docs / "binary.txt").write_bytes(b"\x00\x01binary")
    async with documents_server(home, home) as call:
        result = await call(
            "read_documents",
            paths=[
                str(docs / "notes.md"),
                "~/Documents/statement.pdf",
                "~/Documents/scan.pdf",
                "~/.ssh/notes.txt",
                "~/Documents/escape.txt",
                "~/Documents/photo.jpg",
                "~/Documents/binary.txt",
                "~/Documents/gone.txt",
            ],
        )
        cut = await call("read_documents", paths=["~/Documents/notes.md"], max_chars=9)
    got = {Path(d["path"]).name: d["text"] if "text" in d else "ERROR: " + d["error"] for d in result["documents"]}
    assert got["notes.md"] == "# Dentist\nTuesday at 9.\n"
    assert "--- page 1 ---\nThe fund returned 7.4 percent" in got["statement.pdf"]
    assert got["scan.pdf"] == ""  # no text at all, so Athena notes it has none, rather than indexing page markers
    assert got["notes.txt"].startswith("ERROR") and got["escape.txt"].startswith("ERROR")
    assert "not a kind of document" in got["photo.jpg"] and "isn't a text file" in got["binary.txt"]
    assert "nothing at" in got["gone.txt"]
    assert cut["documents"][0]["text"] == "# Dentist"


@pytest.mark.skipif(shutil.which("textutil") is None, reason="textutil comes with macOS")
async def test_reads_word_documents(home):
    docs = home / "Documents"
    (docs / "plan.txt").write_text("Quarterly plan\n")
    subprocess.run(["textutil", "-convert", "docx", str(docs / "plan.txt"), "-output", str(docs / "plan.docx")])
    async with documents_server(home, home) as call:
        result = await call("read_documents", paths=["~/Documents/plan.docx"])
    assert "Quarterly plan" in result["documents"][0]["text"]


async def test_browsing_for_folders(home):
    async with documents_server(home, home) as call:
        start = await call("browse")
        documents = await call("browse", path="~/Documents")
        refused = await call("browse", path="~/.ssh")
    assert start["folders"] == [
        {"name": "Documents", "path": "~/Documents"},
        {"name": "iCloud Drive", "path": "~/Library/Mobile Documents/com~apple~CloudDocs"},
    ]
    # Not .git, the bundle, or the way out; and the two documents directly inside.
    assert documents == {
        "path": "~/Documents",
        "folders": [{"name": "Taxes", "path": "~/Documents/Taxes"}],
        "documents": 2,
    }
    assert refused.startswith("Error") and "isn't somewhere Athena may look" in refused


def test_files_only_in_icloud_or_too_big_are_skipped(home, monkeypatch):
    docs = home / "Documents"
    (docs / "big.txt").write_text("x" * 100)
    monkeypatch.setattr(mac_documents, "MAX_FILE_BYTES", 50)
    if hasattr(os, "chflags"):
        # A flag anyone can set, standing in for SF_DATALESS, which only iCloud can.
        monkeypatch.setattr(mac_documents, "SF_DATALESS", stat.UF_NODUMP)
        os.chflags(docs / "notes.md", stat.UF_NODUMP)
    skipped = {"cloud": 0, "too_big": 0}
    files = mac_documents._walk(Scope([str(home)], home), docs, set(), skipped, 100)
    assert skipped["too_big"] == 1 and "big.txt" not in [Path(f[0]).name for f in files]
    if hasattr(os, "chflags"):
        assert skipped["cloud"] == 1 and [Path(f[0]).name for f in files] == ["2025.txt"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_folder_inside_doesnt_stop_the_rest(home):
    locked = home / "Documents" / "Locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        skipped = {"cloud": 0, "too_big": 0}
        files = mac_documents._walk(Scope([str(home)], home), home / "Documents", set(), skipped, 100)
    finally:
        locked.chmod(0o700)
    assert [Path(f[0]).name for f in files] == ["notes.md", "2025.txt"] and skipped["unreadable"] == 1


def test_macos_privacy_controls_are_explained():
    message = mac_documents._describe(PermissionError(errno.EPERM, "Operation not permitted"), "~/Documents")
    assert "macOS hasn't allowed access to ~/Documents yet" in message and "Files and Folders" in message


def test_the_scope_file_and_folders_that_dont_exist(tmp_path, capsys):
    scope = tmp_path / "scope"
    scope.write_text(f"# what the Pi may see\n{tmp_path}\n{tmp_path / 'missing'}\n")
    assert Scope([str(tmp_path), str(tmp_path / "missing")], tmp_path).roots == [Path(os.path.realpath(tmp_path))]
    assert "skipping" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        mac_documents.main(["--scope", str(tmp_path / "missing")])


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
