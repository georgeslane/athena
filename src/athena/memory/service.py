"""Memory for the agent: remembering, recalling and searching, indexing conversations, and the model's tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec

from athena.config import MemoryConfig
from athena.memory.embeddings import Embedder
from athena.memory.search import fuse, relevance, words_query
from athena.memory.store import DOCUMENTS_HOME, INDEXED_TO, KINDS, Found, Kind, MemoryHit, MemoryStore
from athena.memory.text import chunk_text, conversation_text
from athena.tools import Tool, current_turn

if TYPE_CHECKING:
    from athena.history import ConversationStore, Exchange


log = logging.getLogger(__name__)

READ_CHARS = 5000  # how much of a document read_document returns at a time
ICLOUD_PATH, ICLOUD_DRIVE = "~/Library/Mobile Documents/com~apple~CloudDocs", "iCloud Drive"


class MemoryService:
    def __init__(self, store: MemoryStore, embedder: Embedder, cfg: MemoryConfig):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg

    async def remember(self, text: str, source: str = "assistant") -> tuple[int, bool]:
        """Save a fact. Returns (id, created) - created is False if a near-identical fact exists."""
        text = text.strip()
        if not text:
            raise ValueError("nothing to remember")
        vector = await self.embedder.embed_one(text, "document")
        for hit in self.store.search(vector, 1, ("fact",)):
            if hit.distance < self.cfg.duplicate_distance:
                return hit.id, False
        return self.store.add(text, vector, "fact", source), True

    async def suggest(self, text: str, source: str) -> tuple[str, int]:
        """Keep a fact for you to approve. Returns what became of it, and its id: ("suggested", the
        suggestion's), ("known", the memory's) if it's remembered already, or ("pending", the
        suggestion's) if it's waiting for you already."""
        text = text.strip()
        if not text:
            raise ValueError("nothing to remember")
        vector = await self.embedder.embed_one(text, "document")
        for hit in self.store.search(vector, 1, ("fact",)):
            if hit.distance < self.cfg.duplicate_distance:
                return "known", hit.id
        for waiting in self.store.suggestions():
            if waiting.text.casefold() == text.casefold():
                return "pending", waiting.id
        return "suggested", self.store.suggest(text, source)

    async def approve(self, suggestion_id: int, text: str | None = None) -> tuple[int, bool]:
        """Remember a suggested fact, as it is or as you've changed it. Returns what remember() does."""
        suggestion = self.store.suggestion(suggestion_id)
        if suggestion is None:
            raise KeyError(suggestion_id)
        result = await self.remember(text if text and text.strip() else suggestion.text)
        self.store.drop_suggestion(suggestion_id)
        return result

    async def search(self, query: str, k: int | None = None, kind: Kind | None = None) -> list[MemoryHit]:
        """The entries nearest the query: of one kind, or of every kind."""
        vector = await self.embedder.embed_one(query, "query")
        return self.store.search(vector, k or self.cfg.search_top_k, (kind,) if kind else KINDS)

    async def recall(self, query: str) -> list[MemoryHit]:
        """Memories relevant enough to show the model automatically: facts, not documents or old conversations."""
        hits = await self.search(query, self.cfg.recall_top_k, "fact")
        return [h for h in hits if h.distance <= self.cfg.recall_max_distance]

    async def find(self, query: str, limit: int | None = None, kind: Kind | None = None) -> list[Found]:
        """What a search by meaning and by words finds, for the model and the dashboard: each document once, by its
        best passage. If meaning can't be searched (the embeddings server is down), what has the words is found."""
        limit = limit or self.cfg.search_top_k
        candidates = limit * 5  # a document's passages are folded into one below
        kinds = (kind,) if kind else KINDS
        words = words_query(query) if self.store.words else None
        try:
            vector: list[float] | None = await self.embedder.embed_one(query, "query")
        except Exception as exc:
            if not words:
                raise
            log.warning("Searching memory by its words alone: couldn't by meaning (%s)", exc)
            vector = None
        by_meaning = self.store.search(vector, candidates, kinds) if vector is not None else []
        by_words = self.store.search_words(words, candidates, kinds, vector) if words else []
        matched = {hit.id for hit in by_words}
        indexed = self.store.documents()
        found: list[Found] = []
        files: set[str] = set()
        for hit in fuse(by_meaning, by_words):
            has = hit.id in matched
            if hit.kind != "document":
                found.append(Found(hit.kind, hit.distance, hit.text, id=hit.id, saved=hit.created_at[:10], words=has))
            elif hit.source and hit.source not in files:
                files.add(hit.source)
                doc = indexed.get(hit.source)
                changed = datetime.fromtimestamp(doc.modified).date().isoformat() if doc else hit.created_at[:10]
                found.append(Found("document", hit.distance, hit.text, path=hit.source, saved=changed, words=has))
        return found[:limit]

    def show_path(self, path: str, home: str | None = None) -> str:
        """A document's path as you'd write it on its Mac: with ~ for your home folder, and iCloud Drive by name."""
        home = (home if home is not None else self.store.get_meta(DOCUMENTS_HOME) or "").rstrip("/")
        if not home or not path.startswith(home + "/"):
            return path
        shown = "~/" + path[len(home) + 1 :]
        return ICLOUD_DRIVE + shown[len(ICLOUD_PATH) :] if shown.startswith(ICLOUD_PATH + "/") else shown

    def find_document(self, name: str) -> str:
        """The document a path or a file name means. ValueError, saying why, if none or several do."""
        name = name.strip()
        sources = self.store.document_sources()
        home = self.store.get_meta(DOCUMENTS_HOME) or ""
        exact = [s for s in sources if name in (s, self.show_path(s, home))]
        if exact:
            return exact[0]
        named = [s for s in sources if Path(s).name.lower() == Path(name).name.lower()]
        if len(named) == 1:
            return named[0]
        if named:
            options = ", ".join(self.show_path(s, home) for s in named[:5])
            raise ValueError(f"Several documents are called {Path(name).name}: {options}. Give the whole path.")
        raise ValueError(f"There's no document called {name} in memory. Use search_memory to find one.")

    def _describe(self, found: Found) -> dict[str, Any]:
        # How close in meaning (None if that couldn't be searched), and whether it has the words searched for.
        scores: dict[str, Any] = {"relevance": relevance(found), **({"has_the_words": True} if found.words else {})}
        if found.kind == "document":
            assert found.path
            passage = found.text if len(found.text) <= 600 else found.text[:599].rstrip() + "…"
            return {"kind": "document", "file": self.show_path(found.path), "changed": found.saved, **scores,
                    "passage": passage}  # fmt: skip
        text = found.text if len(found.text) <= 800 else found.text[:799].rstrip() + "…"
        return {"kind": found.kind, "id": found.id, "text": text, "saved": found.saved, **scores}

    def forget(self, memory_id: int) -> bool:
        return self.store.delete(memory_id)

    def forget_conversation(self, exchange: Exchange) -> bool:
        """Take an exchange out of memory, if it's been added, as when /retry answers it again."""
        found = self.store.find_text("conversation", conversation_text(exchange, self.cfg.chunk_chars))
        return found is not None and self.store.delete(found)

    async def index_conversations(self, history: ConversationStore, batch: int = 16) -> int:
        """Add exchanges that aren't in memory yet, so they can be searched later. Returns how many were added.

        Each batch is added together with a note of where it got to, so if the embeddings
        server is down, the next call carries on from there.
        """
        added = 0
        while exchanges := history.exchanges_after(int(self.store.get_meta(INDEXED_TO) or 0), batch):
            texts = [conversation_text(e, self.cfg.chunk_chars) for e in exchanges]
            vectors = await self.embedder.embed(texts, "document", ["conversation"] * len(texts))
            entries = [(t, v, e.created_at) for t, v, e in zip(texts, vectors, exchanges, strict=True)]
            self.store.add_conversations(entries, indexed_to=exchanges[-1].id)
            added += len(entries)
        return added

    async def ingest_file(self, path: Path) -> int:
        text = path.read_text(errors="replace")
        source = str(path)
        self.store.delete_source(source)
        chunks = chunk_text(text, self.cfg.chunk_chars)
        if not chunks:
            return 0
        vectors = await self.embedder.embed(chunks, "document", [path.stem] * len(chunks))
        for chunk, vector in zip(chunks, vectors, strict=True):
            self.store.add(chunk, vector, "document", source)
        return len(chunks)

    async def reindex(self, progress: Callable[[int, int], None] | None = None) -> int:
        """Re-embed every entry with the configured model. Nothing changes until all of them are done,
        so if the embeddings server fails part way, the old index is still whole."""
        entries = self.store.all_entries()
        done: list[tuple[int, str, bytes]] = []
        for start in range(0, len(entries), 32):
            batch = entries[start : start + 32]
            titles = [_title(e) for e in batch]
            vectors = await self.embedder.embed([e.text for e in batch], "document", titles)
            done += [(e.id, e.kind, sqlite_vec.serialize_float32(v)) for e, v in zip(batch, vectors, strict=True)]
            if progress:
                progress(len(done), len(entries))
        self.store.replace_embeddings(done, self.embedder.cfg.model)
        return len(entries)

    # -- tools exposed to the model -------------------------------------------------

    def tools(self) -> list[Tool]:
        async def search_memory(args: dict[str, Any]) -> str:
            query = str(args.get("query", "")).strip()
            if not query:
                return "Error: query is required."
            kind = args.get("kind") or None
            if kind not in (None, *KINDS):
                return f"Error: kind must be one of {', '.join(KINDS)}."
            limit = int(args.get("limit") or self.cfg.search_top_k)
            found = await self.find(query, max(1, min(limit, 20)), kind)
            if not found:
                return "Nothing found."
            return json.dumps([self._describe(f) for f in found], ensure_ascii=False)

        async def read_document(args: dict[str, Any]) -> str:
            try:
                path = self.find_document(str(args.get("file", "")))
            except ValueError as exc:
                return f"Error: {exc}"
            text = "\n\n".join(self.store.document_chunks(path))
            doc = self.store.documents().get(path)
            if not text:
                return f"[{self.show_path(path)}] It has no text" + (f": {doc.note}" if doc and doc.note else ".")
            start = max(0, int(args.get("start") or 0))
            part = text[start : start + READ_CHARS]
            end = start + len(part)
            more = f"; to continue, use start={end}" if end < len(text) else ""
            changed = f", changed {datetime.fromtimestamp(doc.modified):%Y-%m-%d}" if doc else ""
            return f"[{self.show_path(path)}{changed}: characters {start}-{end} of {len(text)}{more}]\n{part}"

        async def remember(args: dict[str, Any]) -> str:
            turn = current_turn()
            if turn.alone:  # working by itself: you approve it first
                became, ident = await self.suggest(str(args.get("fact", "")), turn.alone)
                if became == "known":
                    return f"Already known (memory #{ident})."
                if became == "suggested":
                    turn.note("suggested", "memory")
                return (
                    "Kept for the user to approve: what you remember while working by yourself is saved "
                    "once they've approved it on the dashboard."
                )
            memory_id, created = await self.remember(str(args.get("fact", "")))
            return f"Saved as memory #{memory_id}." if created else f"Already known (memory #{memory_id})."

        async def forget_memory(args: dict[str, Any]) -> str:
            try:
                memory_id = int(args.get("id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return "Error: id must be a memory number."
            hit = self.store.get(memory_id)
            if not hit:
                return f"No memory #{memory_id}."
            self.forget(memory_id)
            return f"Forgot memory #{memory_id}: {hit.text}"

        return [
            Tool(
                name="remember",
                description=(
                    "Save a durable fact about the user (their people, preferences, plans, routines) "
                    "to long-term memory. One self-contained fact per call, in the third person."
                ),
                parameters={
                    "type": "object",
                    "properties": {"fact": {"type": "string", "description": "The fact to save."}},
                    "required": ["fact"],
                },
                handler=remember,
            ),
            Tool(
                name="search_memory",
                description=(
                    "Search long-term memory by meaning and by words: facts the user has told you, your past "
                    "conversations with them (dated when they were said), and their documents (files indexed from "
                    "their Mac, each with its path and best passage). Put a name, number or phrase that must be "
                    'there exactly in double quotes, like "Project Nimbus".'
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for, in natural language."},
                        "kind": {"type": "string", "enum": list(KINDS), "description": "Only this kind."},
                        "limit": {"type": "integer", "description": "Maximum results (default 8)."},
                    },
                    "required": ["query"],
                },
                handler=search_memory,
            ),
            Tool(
                name="read_document",
                description=(
                    "Read a document that search_memory found, to answer from it or summarise it. "
                    "Long ones come in parts: to continue, pass the start shown at the top."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "Its path, as search_memory gave it."},
                        "start": {"type": "integer", "description": "Where to start reading (default 0)."},
                    },
                    "required": ["file"],
                },
                handler=read_document,
            ),
            Tool(
                name="forget_memory",
                description="Delete a memory by its id (from search_memory). Use when a fact is wrong or outdated.",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "integer", "description": "The memory id."}},
                    "required": ["id"],
                },
                handler=forget_memory,
                needs_confirmation=True,
            ),
        ]


def _title(entry: MemoryHit) -> str:
    """The title an entry is embedded with (see EmbeddingsConfig.document_prefix)."""
    if entry.kind == "document" and entry.source:
        return Path(entry.source).stem
    return "conversation" if entry.kind == "conversation" else "none"
