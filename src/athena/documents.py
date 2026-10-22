"""Your documents, indexed from folders on your Mac so that Athena can search them by meaning.

The Mac runs a small read-only server (mac_documents.py, set up with athena mac install
--documents), which Athena starts over SSH when you press Index now on the dashboard, and
never on its own. It lists the documents in the folders you chose. Athena reads only those
that are new or have changed since last time, splits their text into passages, embeds them
and keeps them in memory, where search_memory finds them. Documents that have gone, or are in
folders you've removed, are taken out. Each document is stored as it's read, so stopping part
way keeps what's done, and the next run carries on from there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import anyio
from mcp import Client, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_types import Implementation
from openai import APIConnectionError

from athena import __version__
from athena.config import DocumentsConfig
from athena.mcp_manager import _describe, result_to_text
from athena.memory import DOCUMENTS_HOME, IndexedDocument, MemoryService, chunk_text

log = logging.getLogger(__name__)

DOCUMENTS_MAC = "documents_mac"  # memory_meta: the name of the Mac they come from
DOCUMENTS_INDEXED = "documents_indexed"  # memory_meta: when indexing last finished
BATCH_FILES = 16  # documents read from the Mac at a time
BATCH_BYTES = 20_000_000  # and at most this much of them
MAX_FAILURES = 50  # documents that couldn't be read, kept to show on the dashboard
NO_TEXT = "It has no text: a scan or a picture, perhaps."


class DocumentsError(Exception):
    """Indexing couldn't go on. The message says why, for the dashboard."""


@dataclass
class IndexRun:
    """One time indexing ran: how far it got, and what it found."""

    started: float = field(default_factory=time.time)
    state: str = "connecting"  # then listing, reading, and done, failed or stopped
    finished: float | None = None
    mac: str = ""
    to_read: int = 0  # new or changed documents
    read: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    no_text: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)  # {"file", "why"}, up to MAX_FAILURES
    failures: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # only in iCloud, too big, in a folder it couldn't open
    folder_errors: dict[str, str] = field(default_factory=dict)
    truncated: bool = False  # the Mac listed only so many documents
    error: str = ""


class DocumentIndex:
    def __init__(self, cfg: DocumentsConfig, memory: MemoryService, log_dir: Path | None = None):
        self.cfg = cfg
        self.memory = memory
        self.log_dir = log_dir  # the Mac server's stderr (and SSH's) goes to <log_dir>/documents.log
        self.run: IndexRun | None = None  # the latest
        self._task: asyncio.Task[None] | None = None

    @property
    def _log_path(self) -> Path | None:
        return self.log_dir / "documents.log" if self.log_dir else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def configure(self, cfg: DocumentsConfig) -> None:
        """New settings, from config.toml. Documents in folders that are no longer chosen go at once."""
        self.cfg = cfg
        if not self.running:
            self.forget_removed_folders()

    def forget_removed_folders(self) -> int:
        store = self.memory.store
        gone = [path for path, doc in store.documents().items() if doc.folder not in self.cfg.folders]
        store.remove_documents(gone)
        return len(gone)

    def start(self) -> IndexRun:
        if self.running:
            raise DocumentsError("Athena is indexing already.")
        if not self.cfg.folders:
            raise DocumentsError("Choose a folder to index first.")
        self.run = IndexRun()
        self._task = asyncio.create_task(self._index(self.run, list(self.cfg.folders)), name="index-documents")
        return self.run

    async def stop(self) -> None:
        if self.running:
            assert self._task
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def browse(self, path: str = "") -> dict[str, Any]:
        """The folders inside a folder on the Mac, to choose from."""
        async with connect_mac(self.cfg, self._log_path) as mac:
            return await mac.call("browse", path=path)

    def summary(self) -> dict[str, Any]:
        """What's indexed, folder by folder, and how indexing is going, for the dashboard."""
        store = self.memory.store
        indexed = store.documents().values()
        folders = []
        for folder in self.cfg.folders:
            mine = [d for d in indexed if d.folder == folder]
            folders.append(
                {
                    "folder": folder,
                    "files": sum(1 for d in mine if d.chunks),
                    "passages": sum(d.chunks for d in mine),
                    "no_text": sum(1 for d in mine if not d.chunks),
                }
            )
        ours = {d.path for d in indexed}
        return {
            "source": " ".join([self.cfg.command, *self.cfg.args]),
            "mac": store.get_meta(DOCUMENTS_MAC),
            "folders": folders,
            "files": sum(f["files"] for f in folders),
            "passages": sum(f["passages"] for f in folders),
            "ingested": sum(1 for s in store.document_sources() if s not in ours),  # with `athena ingest`
            "indexed": store.get_meta(DOCUMENTS_INDEXED),
            "running": self.running,
            "run": asdict(self.run) if self.run else None,
        }

    # -- indexing ---------------------------------------------------------------------------------

    async def _index(self, run: IndexRun, folders: list[str]) -> None:
        store = self.memory.store
        try:
            async with connect_mac(self.cfg, self._log_path) as mac:
                about = await mac.call("about")
                run.mac = str(about.get("host") or "")
                store.set_meta(DOCUMENTS_MAC, run.mac)
                store.set_meta(DOCUMENTS_HOME, str(about.get("home") or ""))

                run.state = "listing"
                listing = await mac.call("list_documents", folders=folders)
                seen, listed = self._listed(run, listing)
                indexed = store.documents()
                gone = [p for p, d in indexed.items() if d.folder not in folders]
                if not run.truncated:  # a listing that stopped short can't say what's gone
                    gone += [p for p, d in indexed.items() if d.folder in listed and p not in seen]
                store.remove_documents(gone)
                run.removed = len(gone)

                todo = []
                for path, (folder, size, modified) in seen.items():
                    doc = indexed.get(path)
                    if doc and (doc.size, doc.modified) == (size, modified):
                        run.unchanged += 1
                        if doc.folder != folder:
                            store.move_document(path, folder)
                    else:
                        todo.append(path)
                run.to_read = len(todo)
                run.state = "reading"
                for batch in _batches(todo, seen):
                    result = await mac.call("read_documents", paths=batch, max_chars=self.cfg.max_chars_per_file)
                    for item in result.get("documents", []):
                        path = item.get("path")
                        if path not in seen:
                            continue
                        run.read += 1
                        if "error" in item:
                            self._failed(run, path, str(item["error"]))
                            continue
                        await self._keep(run, IndexedDocument(path, *seen[path]), str(item.get("text", "")), indexed)
            run.state = "done"
            store.set_meta(DOCUMENTS_INDEXED, datetime.now(UTC).isoformat(timespec="seconds"))
        except asyncio.CancelledError:
            run.state = "stopped"
            raise
        except DocumentsError as exc:
            run.state, run.error = "failed", str(exc)
        except Exception as exc:
            log.exception("Indexing documents failed")
            run.state, run.error = "failed", f"Indexing stopped: {self._why(exc)}"
        finally:
            run.finished = time.time()
            self.forget_removed_folders()  # any taken out of config.toml while this ran
            log.info("Indexing documents: %s (%d read, %d removed)", run.state, run.read, run.removed)

    def _listed(self, run: IndexRun, listing: dict[str, Any]) -> tuple[dict[str, tuple[str, int, float]], set[str]]:
        """Each document listed, as path: (folder, size, modified), and the folders that could be listed."""
        seen: dict[str, tuple[str, int, float]] = {}
        listed: set[str] = set()
        for entry in listing.get("folders", []):
            folder = str(entry.get("folder"))
            if "error" in entry:
                run.folder_errors[folder] = str(entry["error"])
                continue
            listed.add(folder)
            for path, size, modified in entry.get("files", []):
                seen.setdefault(str(path), (folder, int(size), float(modified)))
        run.skipped = {k: int(v) for k, v in (listing.get("skipped") or {}).items() if v}
        run.truncated = bool(listing.get("truncated"))
        return seen, listed

    async def _keep(self, run: IndexRun, doc: IndexedDocument, text: str, indexed: dict[str, IndexedDocument]) -> None:
        chunks = chunk_text(text, self.memory.cfg.chunk_chars)
        vectors = (
            await self.memory.embedder.embed(chunks, "document", [Path(doc.path).stem] * len(chunks)) if chunks else []
        )
        if not chunks:
            doc.note = NO_TEXT
            run.no_text += 1
        self.memory.store.put_document(doc, list(zip(chunks, vectors, strict=True)))
        if doc.path in indexed:
            run.updated += 1
        else:
            run.added += 1

    def _why(self, exc: BaseException) -> str:
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        if isinstance(exc, APIConnectionError):
            return f"couldn't reach the embeddings server ({self.memory.embedder.cfg.base_url})."
        if isinstance(exc, (MCPError, anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
            return "the connection to your Mac closed. Did it go to sleep? Index again to carry on."
        return _describe(exc)

    def _failed(self, run: IndexRun, path: str, why: str) -> None:
        run.failures += 1
        if len(run.failed) < MAX_FAILURES:
            run.failed.append({"file": self.memory.show_path(path), "why": why})


@asynccontextmanager
async def connect_mac(cfg: DocumentsConfig, log_path: Path | None = None) -> AsyncIterator[_Mac]:
    """A connection to the documents server on your Mac: started over SSH, and stopped afterwards.
    Its stderr, and SSH's, goes to ``log_path``, and DocumentsError says why if it can't be reached."""
    command = " ".join([cfg.command, *cfg.args])
    async with AsyncExitStack() as stack:
        errlog: TextIO = sys.stderr
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            errlog = stack.enter_context(open(log_path, "a", buffering=1))
            errlog.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {command}\n")
        params = StdioServerParameters(command=cfg.command, args=cfg.args)
        info = Implementation(name="athena", version=__version__)
        client = Client(stdio_client(params, errlog=errlog), read_timeout_seconds=cfg.timeout_seconds, client_info=info)
        try:
            async with asyncio.timeout(60):
                await stack.enter_async_context(client)
        except Exception as exc:  # noqa: BLE001 - anyio may wrap it in an exception group
            said = _last_line(log_path) or _describe(exc)
            hint = "Is it awake, with Remote Login on? (docs/mac.md)"
            raise DocumentsError(f"Couldn't reach your Mac ({command}): {said}. {hint}") from None
        yield _Mac(client)


def _last_line(log_path: Path | None) -> str:
    """The last thing the server or SSH said, which is usually why it failed."""
    if not log_path or not log_path.exists():
        return ""
    lines = [line.strip() for line in log_path.read_text(errors="replace").splitlines()[-20:] if line.strip()]
    return next((line for line in reversed(lines) if not line.startswith("---")), "")


class _Mac:
    """The documents server on your Mac, over one connection."""

    def __init__(self, client: Client):
        self.client = client

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = await self.client.call_tool(tool, args)
        text = result_to_text(result)
        if result.is_error:
            raise DocumentsError(text.removeprefix("Error: "))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise DocumentsError(f"Your Mac's documents server said something unexpected: {text[:200]}") from None


def _batches(paths: list[str], seen: dict[str, tuple[str, int, float]]) -> list[list[str]]:
    """Documents to read together: a few at a time, so one big batch can't take too long."""
    batches: list[list[str]] = []
    size = 0
    for path in paths:
        if not batches or len(batches[-1]) >= BATCH_FILES or size + seen[path][1] > BATCH_BYTES:
            batches.append([])
            size = 0
        batches[-1].append(path)
        size += seen[path][1]
    return batches
