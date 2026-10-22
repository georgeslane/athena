"""Files: reading and writing them in the workspace, Markdown and PDFs among them, and sending them to you.

  read_file    a file's text, a part at a time: a PDF's, page by page; or what's in a folder
  write_file   a file, from what the model writes: a .pdf is laid out from Markdown
  send_file    sends one to you with Athena's answer, as a file in Telegram

Files you send Athena in Telegram go into the workspace's inbox/ folder (telegram_bot.py), and the message
says where. Everything's within the workspace, which Athena opens without ever following a link
(workspace.py). Reading and making PDFs is done in the code's sandbox (pdfs.py), where a file made to
trouble a PDF reader can't do harm, with [code]'s limits; without bubblewrap, PDFs can't be read or made,
but other files can.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Self

from pydantic import Field

from athena import pdfs, workspace
from athena.builtin import FAIL, OK, WARN, Builtin, Report, Section
from athena.catalog import Integration
from athena.run_code import SandboxedPython
from athena.sandbox import Sandbox, SandboxError
from athena.tools import Tool, ToolError, current_turn

if TYPE_CHECKING:
    from athena.config import Config

READ_CHARS = 5000  # of a file's text, at a time
MAX_WRITE_CHARS = 500_000
TEXT = {".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json", ".html", ".xml", ".ics", ".log", ".py"}
SENT = "files"  # what send_file notes for the turn: {"path", "caption"} as JSON, for the channel to send


class FilesConfig(Section):
    # read_file, write_file and send_file: files in data/workspace, Markdown and PDFs among them (docs/tools.md).
    enabled: bool = True
    max_read_mb: int = Field(default=20, ge=1)  # the largest file read_file reads, or you can send Athena
    max_send_mb: int = Field(default=50, ge=1, le=50)  # the largest it sends you; Telegram takes up to 50 MB


class Files(Builtin):
    section = "workspace"  # not "files": a Pi set up before documents may have an MCP server of that name
    Settings = FilesConfig
    entry = Integration(
        "workspace",
        "Files",
        "Reads and writes files in Athena's own folder, PDFs included, and sends them to you. Files you send it go "
        "there too.",
        "workspace",
        builtin=True,
        asks="Never asks: files stay in Athena's folder unless they're sent to you.",
    )

    @classmethod
    def start(cls, cfg: Config) -> Self:
        sandbox = Sandbox.detect()
        python = SandboxedPython(cfg.code, sandbox, workspace.workspace_of(cfg)) if sandbox.bwrap else None
        return cls(cfg.workspace, workspace.workspace_of(cfg), python, cfg.code.workspace_mb)

    @classmethod
    async def check(cls, cfg: Config, report: Report) -> None:
        files = cls.start(cfg)
        if files.python is None:
            report(WARN, "PDFs can't be read or made without a sandbox, which needs Linux and bubblewrap")
            return
        name = ".athena-check/check.pdf"
        try:
            made = await files.write(name, "# Checking\n\nAthena can make PDFs.", "")
            said = await files.read(name, 0)
        except (ToolError, SandboxError) as exc:
            report(FAIL, f"PDFs: {exc}")
            return
        finally:
            workspace.remove(files.workspace, ".athena-check")
        fonts = "" if pdfs.DEJAVU.is_dir() else " (without DejaVu fonts: sudo apt install fonts-dejavu-core)"
        ok = "Athena can make PDFs" in said
        report(OK if ok else FAIL, f"PDFs can be made and read{fonts}" if ok else f"PDFs: {made}; {said[:200]}")

    def __init__(self, cfg: FilesConfig, folder: Path, python: SandboxedPython | None, workspace_mb: int = 500):
        self.cfg = cfg
        self.workspace = folder
        self.python = python  # for PDFs: None where there's no sandbox
        self.workspace_mb = workspace_mb  # [code]'s, which is the limit when there's no sandbox to say it

    def tools(self) -> list[Tool]:
        async def read_file(args: dict[str, Any]) -> str:
            return await self.read(str(args.get("path") or ""), int(args.get("start") or 0))

        async def write_file(args: dict[str, Any]) -> str:
            return await self.write(str(args.get("path") or ""), str(args.get("content") or ""),
                                    str(args.get("title") or ""))  # fmt: skip

        async def send_file(args: dict[str, Any]) -> str:
            return self.send(str(args.get("path") or ""), str(args.get("caption") or ""))

        path = {"type": "string", "description": "Within Athena's folder, like reports/march.pdf"}
        return [
            Tool(
                name="read_file",
                description=(
                    "Read a file in your folder, such as one the user sent (in inbox/) or one you made: its text, "
                    f"{READ_CHARS:,} characters at a time, and a PDF's page by page. Give a folder, or nothing, to "
                    "see what's in it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": path,
                        "start": {"type": "integer", "description": "Where to start reading (default 0)"},
                    },
                },
                handler=read_file,
            ),
            Tool(
                name="write_file",
                description=(
                    "Write a file in your folder, replacing one of that name: Markdown, text, CSV and the like as "
                    "they are, and a .pdf laid out from Markdown you give, with headings, lists and tables. Then "
                    "send it to the user with send_file if they want it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": path,
                        "content": {"type": "string", "description": "What's in it; Markdown for a .pdf"},
                        "title": {"type": "string", "description": "A PDF's title, for its properties"},
                    },
                    "required": ["path", "content"],
                },
                handler=write_file,
            ),
            Tool(
                name="send_file",
                description="Send the user a file from your folder, in Telegram, with your answer.",
                parameters={
                    "type": "object",
                    "properties": {"path": path, "caption": {"type": "string", "description": "A line about it"}},
                    "required": ["path"],
                },
                handler=send_file,
            ),
        ]

    # -- the work ------------------------------------------------------------------------------------------

    async def read(self, name: str, start: int) -> str:
        """What read_file says: a part of a file's text, or what's in a folder."""
        found = workspace.kind(self.workspace, name) if name.strip(" ./") else ("folder", 0)
        if found and found[0] == "folder":
            return self._listing(name)
        if found is None:
            raise ToolError(f"There's no file {name} in your folder. Give a folder, or nothing, to see what's there.")
        what, size = found
        rel = "/".join(workspace.parts(name))
        if what != "file":
            raise ToolError(f"{rel} isn't a file that can be read: it's a link or something else.")
        start = max(0, start)
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix == ".pdf":
            return await self._read_pdf(rel, start)
        if suffix not in TEXT and _binary(workspace.read(self.workspace, rel, 4096)):
            return f"[{rel}: {workspace.show_size(size)}, not text: run_python can look at it, if it's data]"
        text = workspace.read(self.workspace, rel, self.cfg.max_read_mb * 1024 * 1024).decode(errors="replace")
        return _part(rel, text, start, len(text))

    async def _read_pdf(self, rel: str, start: int) -> str:
        said = await self._in_sandbox(f"read_pdf({'/workspace/' + rel!r}, {start}, {READ_CHARS})", READ_CHARS * 2)
        if said.get("problem"):
            raise ToolError(f"{rel}: {said['problem']}")
        if said["empty"]:
            return f"[{rel}: {said['pages']} pages, with no text in them: it may be a scan, pictures of pages]"
        more = f", the first {said['read']} of {said['pages']} pages" if said["read"] < said["pages"] else ""
        return _part(f"{rel}, a PDF of {said['pages']} pages{more}", said["text"], start, said["length"], cut=True)

    async def write(self, name: str, content: str, title: str) -> str:
        """What write_file says: the file written."""
        rel = "/".join(workspace.parts(name))
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"That's more than {MAX_WRITE_CHARS:,} characters: write it in parts.")
        limit = self.workspace_mb
        if workspace.size(self.workspace) > limit * 1024 * 1024:
            raise ToolError(f"Your folder holds more than {limit} MB: delete what isn't needed before writing more.")
        if PurePosixPath(rel).suffix.lower() != ".pdf":
            workspace.write(self.workspace, rel, content.encode())
            return f"Wrote {rel} ({workspace.show_size(len(content.encode()))})."
        workspace.make_folders(self.workspace, rel)
        said = await self._in_sandbox(f"make_pdf({content!r}, {'/workspace/' + rel!r}, {title!r})", 1000)
        found = workspace.kind(self.workspace, rel)
        size = f", {workspace.show_size(found[1])}" if found else ""
        pages = said["pages"]
        return f"Made {rel} ({pages} page{'' if pages == 1 else 's'}{size})."

    def send(self, name: str, caption: str) -> str:
        """What send_file says: the file's noted, for the channel to send with the answer (telegram_bot.py)."""
        found = workspace.kind(self.workspace, name)
        rel = "/".join(workspace.parts(name))
        if found is None or found[0] != "file":
            raise ToolError(f"There's no file {rel} in your folder to send.")
        if found[1] > self.cfg.max_send_mb * 1024 * 1024:
            raise ToolError(f"{rel} is {workspace.show_size(found[1])}: files over {self.cfg.max_send_mb} MB can't "
                            "be sent.")  # fmt: skip
        current_turn().note(SENT, json.dumps({"path": rel, "caption": caption[:1000]}))
        return f"{rel} will be sent to the user with your answer."

    async def _in_sandbox(self, call: str, max_output: int) -> dict[str, Any]:
        """Run one of pdfs.py's functions in the sandbox, and what it returned."""
        if self.python is None:
            raise ToolError("PDFs can't be read or made here: that needs a sandbox, which needs Linux and bubblewrap.")
        code = f"{Path(pdfs.__file__).read_text()}\n\nimport json\nprint(json.dumps({call}))\n"
        ran = await self.python.run(code, max_output=max_output, max_code=len(code))  # it's ours, not the model's
        if ran.timed_out or ran.code != 0:
            last = ran.output.strip().splitlines()[-1:] or ["it stopped"]
            raise ToolError(f"That couldn't be done: {'it took too long' if ran.timed_out else last[0][:300]}")
        return json.loads(ran.output.strip().splitlines()[-1])

    def _listing(self, name: str) -> str:
        shown = "/".join(workspace.parts(name)) if name.strip(" ./") else "your folder"
        entries = workspace.listing(self.workspace, name)
        if not entries:
            return f"There's nothing in {shown}."
        lines = [f"{entry}/" if size is None else f"{entry} ({workspace.show_size(size)})" for entry, size in entries]
        return f"In {shown}:\n" + "\n".join(lines)


def _part(label: str, text: str, start: int, total: int, cut: bool = False) -> str:
    """A part of a file's text from ``start``, with where it's from. ``cut``: ``text`` is that part already."""
    part = text if cut else text[start : start + READ_CHARS]
    end = start + len(part)
    more = f"; to read on, start={end}" if end < total else ""
    return f"[{label}: characters {start}-{end} of {total}{more}]\n{part}"


def _binary(head: bytes) -> bool:
    """Whether a file's start looks like something other than text."""
    if b"\x00" in head:
        return True
    try:
        head.decode()
    except UnicodeDecodeError as exc:
        return exc.start < len(head) - 4  # not just a character cut in two at the end
    return False
