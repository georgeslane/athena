"""Read-only access to the documents on a Mac, for the assistant on the Pi to index.

An MCP server that runs on the Mac whose documents you want to search (athena mac install
--documents sets it up). The Pi starts it over SSH when you index from the dashboard, which is
where you choose the folders. It can only list folders and read the text of documents in them:
plain text and Markdown, PDF, Word, RTF and HTML. It has no way to change, move or delete
anything.

What the Pi can see is decided here, not on the Pi: only what's inside the scope you gave
(your home folder, unless you narrowed it), never hidden files or folders (such as .ssh or
.env), and nothing in ~/Library apart from iCloud Drive. Files that are only in iCloud, not
downloaded to this Mac, are skipped rather than downloaded.

    python -m athena.mac_documents --scope ~
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

TEXT_SUFFIXES = {".txt", ".text", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".tsv"}
# Converted to text by textutil, which comes with macOS.
TEXTUTIL_SUFFIXES = {".doc", ".docx", ".rtf", ".odt", ".html", ".htm", ".webarchive", ".wordml"}
SUFFIXES = TEXT_SUFFIXES | TEXTUTIL_SUFFIXES | {".pdf"}
# Folders that are really files (bundles), or that only hold code's own files: not looked inside.
SKIPPED_SUFFIXES = (".app", ".bundle", ".framework", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".xcodeproj")
SKIPPED_NAMES = {"node_modules", "__pycache__", "site-packages"}
ICLOUD_DRIVE = Path("Library") / "Mobile Documents" / "com~apple~CloudDocs"
SF_DATALESS = 0x40000000  # st_flags: the file's contents are in iCloud, not on this Mac
MAX_FILE_BYTES = 200_000_000  # bigger files aren't read
MAX_FILES = 50_000  # per listing
MAX_PDF_CHARS = 2_000_000
COMMAND_TIMEOUT = 60.0
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class Scope:
    """What the Pi may see on this Mac, and the checks that keep it there."""

    def __init__(self, roots: list[str], home: Path | None = None):
        self.home = Path(os.path.realpath(home or Path.home()))
        self.roots: list[Path] = []
        for root in roots:
            path = Path(os.path.realpath(os.path.expanduser(root)))
            if path.is_dir():
                self.roots.append(path)
            else:
                print(f"mac_documents: skipping {root}: not a folder", file=sys.stderr)
        if not self.roots:
            raise ValueError("none of the folders given exist")

    def allowed(self, real: Path) -> bool:
        """Whether a resolved path is inside the scope, not hidden, and not in ~/Library (but iCloud Drive)."""
        if not any(real == root or real.is_relative_to(root) for root in self.roots):
            return False
        if real.is_relative_to(self.home):
            relative = real.relative_to(self.home)
            if relative.parts[:1] == ("Library",) and not relative.is_relative_to(ICLOUD_DRIVE):
                return False
            parts = relative.parts
        else:
            parts = next(real.relative_to(root).parts for root in self.roots if real.is_relative_to(root))
        return not any(part.startswith(".") for part in parts)

    def resolve(self, path: str) -> Path:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            raise ValueError(f"Use a full path, or one starting with ~/: {path}")
        real = Path(os.path.realpath(expanded))
        if not self.allowed(real):
            raise PermissionError(f"{path} isn't somewhere Athena may look on this Mac.")
        return real

    def show(self, path: Path) -> str:
        """A path as you'd write it, with ~ for your home folder."""
        if path == self.home:
            return "~"
        if path.is_relative_to(self.home):
            return f"~/{path.relative_to(self.home)}"
        return str(path)


def build_server(scope: Scope) -> MCPServer:
    server = MCPServer(
        "athena-documents", instructions="Read-only access to documents on the user's Mac, for indexing."
    )

    @server.tool(annotations=READ_ONLY)
    def about() -> str:
        """Which Mac this is, its home folder, and what Athena may see on it."""
        return _json(
            {
                "host": _computer_name(),
                "home": str(scope.home),
                "scope": [scope.show(root) for root in scope.roots],
                "kinds": sorted(SUFFIXES),
            }
        )

    @server.tool(annotations=READ_ONLY)
    def browse(path: str = "") -> str:
        """The folders inside a folder, for choosing what to index. With no path: places to start."""
        if not path:
            starts = [scope.home / name for name in ("Documents", "Desktop", "Downloads")] + [scope.home / ICLOUD_DRIVE]
            places = [p for p in starts if p.is_dir() and scope.allowed(Path(os.path.realpath(p)))]
            places += [root for root in scope.roots if root not in places and root != scope.home]
            return _json({"path": "", "folders": [_place(scope, p) for p in places], "documents": 0})
        folder = _guard(lambda: scope.resolve(path))
        if not folder.is_dir():
            raise ToolError(f"{path} isn't a folder.")
        folders, documents = [], 0
        for child in _guard(lambda: sorted(folder.iterdir(), key=lambda p: p.name.lower())):
            if child.name.startswith("."):
                continue
            real = Path(os.path.realpath(child))
            if child.is_dir() and _looked_inside(child) and scope.allowed(real):
                folders.append(_place(scope, child))
            elif child.suffix.lower() in SUFFIXES and child.is_file():
                documents += 1
        return _json({"path": scope.show(folder), "folders": folders, "documents": documents})

    @server.tool(annotations=READ_ONLY)
    def list_documents(folders: list[str], max_files: int = MAX_FILES) -> str:
        """Every document in these folders and the folders inside them, with its size and when it changed."""
        listed, seen = [], set()
        skipped = {"cloud": 0, "too_big": 0}
        budget = max(1, min(max_files, MAX_FILES))
        for given in folders:
            try:
                root = scope.resolve(given)
                if not root.is_dir():
                    raise ValueError(f"{given} isn't a folder.")
                files = _walk(scope, root, seen, skipped, budget)
            except (OSError, ValueError) as exc:
                listed.append({"folder": given, "error": _describe(exc, given)})
                continue
            budget -= len(files)
            listed.append({"folder": given, "files": files})
        return _json({"folders": listed, "skipped": skipped, "truncated": budget <= 0})

    @server.tool(annotations=READ_ONLY)
    async def read_documents(paths: list[str], max_chars: int = 300_000) -> str:
        """The text of these documents, each cut to max_chars, or why it couldn't be read."""
        out: list[dict[str, Any]] = []
        for path in paths:
            try:
                file = scope.resolve(path)
                if not file.exists():
                    raise FileNotFoundError(errno.ENOENT, "No such file", path)
                if file.suffix.lower() not in SUFFIXES or not file.is_file():
                    raise ValueError("That's not a kind of document Athena can read.")
                text = await _text_of(file)
                out.append({"path": path, "text": text[: max(0, max_chars)]})
            except (OSError, ValueError, RuntimeError) as exc:
                out.append({"path": path, "error": _describe(exc, path)})
        return _json({"documents": out})

    return server


def _walk(scope: Scope, root: Path, seen: set[Path], skipped: dict[str, int], budget: int) -> list[list[Any]]:
    """[path, size, modified] for each document under root that Athena may read, in a stable order."""
    files: list[list[Any]] = []

    def unreadable(exc: OSError) -> None:
        if Path(exc.filename or "") == root:
            raise exc  # the folder itself: say why
        skipped["unreadable"] = skipped.get("unreadable", 0) + 1  # one inside it: carry on without it

    for dirpath, dirnames, filenames in os.walk(root, onerror=unreadable):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and _looked_inside(here / d) and scope.allowed(here / d)
        )
        for name in sorted(filenames):
            if name.startswith(".") or Path(name).suffix.lower() not in SUFFIXES:
                continue
            real = Path(os.path.realpath(here / name))
            if real in seen or not scope.allowed(real):
                continue  # listed already, or a link to somewhere Athena mayn't look
            try:
                st = real.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            seen.add(real)
            if getattr(st, "st_flags", 0) & SF_DATALESS:
                skipped["cloud"] += 1
            elif st.st_size > MAX_FILE_BYTES:
                skipped["too_big"] += 1
            else:
                files.append([str(real), st.st_size, st.st_mtime])
                if len(files) >= budget:
                    return files
    return files


def _looked_inside(folder: Path) -> bool:
    return folder.name not in SKIPPED_NAMES and not folder.name.lower().endswith(SKIPPED_SUFFIXES)


def _place(scope: Scope, path: Path) -> dict[str, str]:
    name = "iCloud Drive" if path == scope.home / ICLOUD_DRIVE else path.name
    return {"name": name, "path": scope.show(path)}


def _guard(fn):
    try:
        return fn()
    except (OSError, ValueError) as exc:
        raise ToolError(_describe(exc, str(getattr(exc, "filename", "") or ""))) from None


def _describe(exc: BaseException, path: str) -> str:
    if isinstance(exc, PermissionError) and exc.errno == errno.EPERM:
        # macOS's privacy controls, not the file's permissions
        return (
            f"macOS hasn't allowed access to {path} yet. On the Mac, allow it when asked (for python3), "
            "or in System Settings > Privacy & Security > Files and Folders."
        )
    if isinstance(exc, FileNotFoundError):
        return f"There's nothing at {path}."
    return str(exc) or type(exc).__name__


def _computer_name() -> str:
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name.strip() or socket.gethostname()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} isn't available. Reading this kind of document needs macOS.")
    return path


async def _run(args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), COMMAND_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{Path(args[0]).name} took more than {COMMAND_TIMEOUT:.0f}s") from None
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip() or f"{Path(args[0]).name} failed")
    return out.decode(errors="replace")


async def _text_of(file: Path) -> str:
    suffix = file.suffix.lower()
    if suffix == ".pdf":
        return await asyncio.wait_for(asyncio.to_thread(_pdf_text, file), COMMAND_TIMEOUT)
    if suffix in TEXTUTIL_SUFFIXES:
        return await _run([_tool("textutil"), "-convert", "txt", "-stdout", str(file)])
    data = file.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError(f"{file.name} isn't a text file, so there's no text to read.")
    return data.decode(errors="replace")


def _pdf_text(file: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("Reading PDFs needs pypdf. On the Mac, run athena mac install again.") from None
    reader = PdfReader(file)
    pages: list[str] = []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        lines = [line.rstrip() for line in (page.extract_text() or "").splitlines()]  # PDFs pad lines with spaces
        if not any(lines):
            continue  # a page with no text, such as a scan, isn't worth a marker: a document of them has no text
        text = f"--- page {number} ---\n" + "\n".join(lines)
        pages.append(text)
        total += len(text)
        if total > MAX_PDF_CHARS:
            pages.append(f"[stopped after page {number}: the PDF is very long]")
            break
    return "\n\n".join(pages)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m athena.mac_documents", description=__doc__.splitlines()[0])
    parser.add_argument("--scope", action="append", default=[], help="what the Pi may see (default: your home)")
    parser.add_argument("--scope-file", help="a file listing more of it, one folder per line")
    args = parser.parse_args(argv)
    roots = list(args.scope)
    if args.scope_file:
        lines = Path(args.scope_file).read_text().splitlines()
        roots += [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    try:
        scope = Scope(roots or [str(Path.home())])
    except ValueError as exc:
        parser.error(str(exc))
    build_server(scope).run("stdio")


if __name__ == "__main__":
    main()
