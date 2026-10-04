"""Read-only access to folders you choose on your Mac, for the assistant on the Pi.

An MCP server that runs on the Mac (scripts/mac/install.sh sets it up). It can search
the folders with Spotlight and read the files in them, and nothing else: it has no way
to change, move or delete anything. It can't see outside those folders, even through
a symlink, or hidden files and folders inside them, such as .env or .git.

    python -m pi_assistant.mac_files ~/Documents ~/Desktop
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import functools
import inspect
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

MAX_RESULTS = 50
MAX_ENTRIES = 200
DEFAULT_CHARS = 20_000
MAX_CHARS = 100_000
MAX_TEXT_BYTES = 20_000_000  # plain-text files bigger than this aren't read
MAX_PDF_CHARS = 2_000_000  # stop extracting a PDF's text after this much
COMMAND_TIMEOUT = 30.0
# Converted to text by textutil, which comes with macOS.
TEXTUTIL_SUFFIXES = {".doc", ".docx", ".rtf", ".odt", ".html", ".htm", ".webarchive", ".wordml"}
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class Folders:
    """The folders the server may use, and the checks that keep it inside them."""

    def __init__(self, roots: list[str]):
        self.roots: list[Path] = []
        for root in roots:
            path = Path(os.path.realpath(os.path.expanduser(root)))
            if path.is_dir():
                self.roots.append(path)
            else:
                print(f"mac_files: skipping {root}: not a folder", file=sys.stderr)
        if not self.roots:
            raise ValueError("none of the folders given exist")

    def visible(self, path: Path) -> bool:
        """Whether a resolved path is in one of the folders and not hidden."""
        for root in self.roots:
            if path == root:
                return True
            if path.is_relative_to(root):
                return not any(part.startswith(".") for part in path.relative_to(root).parts)
        return False

    def resolve(self, path: str) -> Path:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            raise ValueError("Use a full path, as given by search_files or list_folder.")
        real = Path(os.path.realpath(expanded))
        if not self.visible(real):
            raise PermissionError(f"{path} isn't in a folder you can see.")
        return real


def _friendly(fn):
    """Report expected problems, like a file that can't be read, to the model as a plain message."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except FileNotFoundError as exc:
            raise ToolError(f"There's nothing at {exc.filename or 'that path'}.") from None
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(str(exc)) from None

    return wrapper


def build_server(folders: Folders) -> MCPServer:
    server = MCPServer("mac-files", instructions="Read-only access to some folders on the user's Mac.")

    @server.tool(annotations=READ_ONLY)
    @_friendly
    async def search_files(query: str, folder: str = "", names_only: bool = False, limit: int = 20) -> str:
        """Search the Mac's folders with Spotlight for files whose contents or names contain the words in query,
        newest first. folder: a full path to search in (default: all). names_only: match file names only."""
        roots = [folders.resolve(folder)] if folder else folders.roots
        found: dict[Path, os.stat_result] = {}
        for root in roots:
            output = await _run([_tool("mdfind"), "-onlyin", str(root), *(["-name"] if names_only else []), query])
            for line in output.splitlines():
                real = Path(os.path.realpath(line))
                if line and real not in found and folders.visible(real):
                    try:
                        found[real] = real.stat()
                    except OSError:
                        continue
        if not found:
            return "Nothing found. Spotlight matches whole words: try fewer or different words."
        newest = sorted(found.items(), key=lambda item: item[1].st_mtime, reverse=True)
        limit = max(1, min(limit, MAX_RESULTS))
        lines = [_describe(path, st) for path, st in newest[:limit]]
        if len(newest) > limit:
            lines.append(f"...and {len(newest) - limit} more. Search with more words, or within a folder.")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY)
    @_friendly
    def list_folder(path: str = "") -> str:
        """List what's in a folder on the Mac, given its full path. With no path, lists the folders you can see."""
        if not path:
            return "\n".join(_describe(root, root.stat()) for root in folders.roots)
        folder = folders.resolve(path)
        if not folder.is_dir():
            raise ValueError(f"{path} isn't a folder.")
        found: dict[Path, os.stat_result] = {}  # by real path, so a symlink to something listed isn't listed twice
        for child in folder.iterdir():
            real = Path(os.path.realpath(child))
            if child.name.startswith(".") or real in found or not folders.visible(real):
                continue
            try:
                found[real] = real.stat()
            except OSError:
                continue
        entries = sorted(found.items(), key=lambda item: (not item[0].is_dir(), item[0].name.lower()))
        lines = [_describe(p, st) for p, st in entries[:MAX_ENTRIES]] or ["(empty)"]
        if len(entries) > MAX_ENTRIES:
            lines.append(f"...and {len(entries) - MAX_ENTRIES} more. Use search_files to find something specific.")
        return "\n".join(lines)

    @server.tool(annotations=READ_ONLY)
    @_friendly
    async def read_file(path: str, start: int = 0, max_chars: int = DEFAULT_CHARS) -> str:
        """Read the text of a file on the Mac, given its full path: plain text, PDF, Word, RTF or HTML.
        Long files come in parts: to continue, pass the start shown at the top."""
        file = folders.resolve(path)
        if file.is_dir():
            raise ValueError(f"{path} is a folder: use list_folder.")
        try:
            text = await _text_of(file)
        except PermissionError as exc:
            if exc.errno == errno.EPERM:  # macOS privacy controls, not file permissions
                raise PermissionError(
                    f"macOS hasn't allowed access to {path} yet. On the Mac, approve the prompt, or give "
                    "access in System Settings > Privacy & Security > Files and Folders."
                ) from None
            raise
        start = max(0, start)
        part = text[start : start + max(1, min(max_chars, MAX_CHARS))]
        end = start + len(part)
        more = f"; to continue, use start={end}" if end < len(text) else ""
        return f"[{file}, characters {start}-{end} of {len(text)}{more}]\n{part}"

    return server


def _describe(path: Path, st: os.stat_result) -> str:
    changed = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    if path.is_dir():
        return f"{path}/  (folder, changed {changed})"
    return f"{path}  ({_size(st.st_size)}, changed {changed})"


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024**2:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024**2:.1f} MB"


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} isn't available. This server needs macOS.")
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
        return await asyncio.to_thread(_pdf_text, file)
    if suffix in TEXTUTIL_SUFFIXES:
        return await _run([_tool("textutil"), "-convert", "txt", "-stdout", str(file)])
    if file.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError(f"{file.name} is too big to read as text.")
    data = file.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError(f"{file.name} isn't a text file, so there's no text to read.")
    return data.decode(errors="replace")


def _pdf_text(file: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("Reading PDFs needs pypdf. On the Mac, run scripts/mac/install.sh again.") from None
    reader = PdfReader(file)
    pages: list[str] = []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        lines = (line.rstrip() for line in (page.extract_text() or "").splitlines())  # PDFs pad lines with spaces
        text = f"--- page {number} ---\n" + "\n".join(lines)
        pages.append(text)
        total += len(text)
        if total > MAX_PDF_CHARS:
            pages.append(f"[stopped after page {number}: the PDF is very long]")
            break
    return "\n".join(pages)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m pi_assistant.mac_files", description=__doc__.splitlines()[0])
    parser.add_argument("folders", nargs="*", help="folders the assistant may search and read")
    parser.add_argument("--folders-file", help="a file listing more folders, one per line")
    args = parser.parse_args(argv)
    roots = list(args.folders)
    if args.folders_file:
        lines = Path(args.folders_file).read_text().splitlines()
        roots += [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    try:
        folders = Folders(roots)
    except ValueError as exc:
        parser.error(str(exc))
    build_server(folders).run("stdio")


if __name__ == "__main__":
    main()
