"""Long-term memory: an embeddings model plus a SQLite vector store (sqlite-vec).

Three kinds of entries:
  * facts         - short statements the assistant saves with the ``remember`` tool
  * documents     - chunks of your own notes, added with ``pi-assistant ingest``
  * conversations - each message you've sent and Athena's reply, added after it answers

Facts and documents share one index, which is searched automatically for every message.
Conversations have an index of their own, searched only when the model asks
(``search_memory``), so an old answer isn't mistaken for a current one.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import sqlite_vec
from openai import AsyncOpenAI

from pi_assistant.config import EmbeddingsConfig, MemoryConfig
from pi_assistant.tools import Tool

if TYPE_CHECKING:
    from pi_assistant.history import ConversationStore, Exchange

log = logging.getLogger(__name__)

Kind = Literal["fact", "document", "conversation"]
VECTOR_TABLES = {"fact": "memory_vectors", "document": "memory_vectors", "conversation": "conversation_vectors"}
INDEXED_TO = "conversations_indexed_to"  # memory_meta: the last message whose exchange is in memory
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}
DUPLICATE_DISTANCE = 0.04  # facts closer than this to an existing fact are treated as duplicates


class MemoryStoreError(Exception):
    """Raised for memory store problems the user needs to act on."""


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def fit_dimensions(vector: Sequence[float], dims: int) -> list[float]:
    """Truncate a Matryoshka-style embedding to ``dims`` and re-normalise it."""
    if len(vector) < dims:
        raise MemoryStoreError(
            f"The embeddings model returned {len(vector)} dimensions but the config expects {dims}. "
            "Set embeddings.dimensions to match the model."
        )
    v = list(vector[:dims])
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class Embedder:
    def __init__(self, cfg: EmbeddingsConfig):
        self.cfg = cfg
        self._client = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "none", timeout=60, max_retries=1)

    async def embed(
        self, texts: Sequence[str], kind: Literal["query", "document"], titles: Sequence[str] | None = None
    ) -> list[list[float]]:
        if kind == "query":
            inputs = [self.cfg.query_prefix + t for t in texts]
        else:
            titles = titles or ["none"] * len(texts)
            inputs = [self.cfg.document_prefix.replace("{title}", ti) + t for t, ti in zip(texts, titles, strict=True)]
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), self.cfg.batch_size):
            batch = inputs[start : start + self.cfg.batch_size]
            resp = await self._client.embeddings.create(model=self.cfg.model, input=batch, encoding_format="float")
            for item in sorted(resp.data, key=lambda d: d.index):
                vectors.append(fit_dimensions(item.embedding, self.cfg.dimensions))
        return vectors

    async def embed_one(self, text: str, kind: Literal["query", "document"], title: str = "none") -> list[float]:
        return (await self.embed([text], kind, [title]))[0]

    async def close(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------


@dataclass
class MemoryHit:
    id: int
    kind: str
    source: str | None
    text: str
    created_at: str
    distance: float


def connect_db(path: Path, *, vectors: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if vectors:
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except AttributeError as exc:
            raise MemoryStoreError(
                "This Python's sqlite3 module can't load extensions, which sqlite-vec needs. "
                "Use the Python that ships with Raspberry Pi OS, or one installed by uv."
            ) from exc
    return conn


class MemoryStore:
    def __init__(self, db_path: Path, dimensions: int, embedding_model: str, *, allow_model_change: bool = False):
        self.dimensions = dimensions
        self.embedding_model = embedding_model
        self._allow_model_change = allow_model_change
        self._conn = connect_db(db_path, vectors=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    source TEXT,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_source ON memories(source);
                CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            for table in sorted(set(VECTOR_TABLES.values())):
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                    f"USING vec0(embedding float[{self.dimensions}] distance_metric=cosine)"
                )
            meta = dict(self._conn.execute("SELECT key, value FROM memory_meta").fetchall())
            current = {"embedding_model": self.embedding_model, "dimensions": str(self.dimensions)}
            if "dimensions" not in meta:  # a new index
                self._conn.executemany("INSERT OR REPLACE INTO memory_meta VALUES (?, ?)", current.items())
            elif meta.get("dimensions") != current["dimensions"]:
                raise MemoryStoreError(
                    f"The memory index was built with {meta.get('dimensions')}-dimension embeddings "
                    f"({meta.get('embedding_model')}) but the config now says {self.dimensions}. "
                    "Change it back, or move data/assistant.db aside to start a fresh index."
                )
            elif meta.get("embedding_model") != current["embedding_model"] and not self._allow_model_change:
                raise MemoryStoreError(
                    f"The memory index was built with '{meta.get('embedding_model')}' but the config now uses "
                    f"'{self.embedding_model}'. Run `pi-assistant reindex` to re-embed everything."
                )

    def set_embedding_model(self, model: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_meta VALUES ('embedding_model', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (model,),
            )
        self.embedding_model = model

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _insert(self, text: str, embedding: Sequence[float], kind: Kind, source: str | None, created_at: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO memories (kind, source, text, created_at) VALUES (?, ?, ?, ?)",
            (kind, source, text, created_at),
        )
        self._conn.execute(
            f"INSERT INTO {VECTOR_TABLES[kind]} (rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, sqlite_vec.serialize_float32(list(embedding))),
        )
        return int(cur.lastrowid)

    def add(self, text: str, embedding: Sequence[float], kind: Kind, source: str | None = None) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            return self._insert(text, embedding, kind, source, now)

    def add_conversations(self, entries: Sequence[tuple[str, Sequence[float], str]], indexed_to: int) -> None:
        """Add (text, embedding, created_at) for each exchange, and note the last message they cover, together."""
        with self._lock, self._conn:
            for text, embedding, created_at in entries:
                self._insert(text, embedding, "conversation", None, created_at)
            self._conn.execute(
                "INSERT INTO memory_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (INDEXED_TO, str(indexed_to)),
            )

    def replace_embedding(self, memory_id: int, embedding: Sequence[float]) -> None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT kind FROM memories WHERE id = ?", (memory_id,)).fetchone()
            table = VECTOR_TABLES.get(row[0] if row else "fact", "memory_vectors")
            self._conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (memory_id,))
            self._conn.execute(
                f"INSERT INTO {table} (rowid, embedding) VALUES (?, ?)",
                (memory_id, sqlite_vec.serialize_float32(list(embedding))),
            )

    def _nearest(self, table: str, embedding: Sequence[float], k: int) -> list[MemoryHit]:
        rows = self._conn.execute(
            f"""
            SELECT m.id, m.kind, m.source, m.text, m.created_at, v.distance
            FROM (
                SELECT rowid, distance FROM {table}
                WHERE embedding MATCH ? AND k = ?
            ) AS v
            JOIN memories AS m ON m.id = v.rowid
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(list(embedding)), k),
        ).fetchall()
        return [MemoryHit(*row) for row in rows]

    def search(
        self, embedding: Sequence[float], k: int, kind: Kind | None = None, *, conversations: bool = False
    ) -> list[MemoryHit]:
        """The ``k`` entries nearest ``embedding``: facts and documents, plus conversations if asked."""
        with self._lock:
            if kind == "conversation":
                return self._nearest("conversation_vectors", embedding, k)
            # Over-fetch when filtering by kind, since the KNN step can't filter.
            hits = self._nearest("memory_vectors", embedding, k * 4 if kind else k)
            if conversations and not kind:
                hits += self._nearest("conversation_vectors", embedding, k)
        if kind:
            hits = [h for h in hits if h.kind == kind]
        return sorted(hits, key=lambda h: h.distance)[:k]

    def get(self, memory_id: int) -> MemoryHit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, source, text, created_at, 0.0 FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return MemoryHit(*row) if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            for table in set(VECTOR_TABLES.values()):
                self._conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (memory_id,))
        return cur.rowcount > 0

    def delete_source(self, source: str) -> int:
        with self._lock, self._conn:
            ids = [r[0] for r in self._conn.execute("SELECT id FROM memories WHERE source = ?", (source,))]
            for table in set(VECTOR_TABLES.values()):
                self._conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(i,) for i in ids])
            self._conn.execute("DELETE FROM memories WHERE source = ?", (source,))
        return len(ids)

    def clear(self) -> None:
        """Delete every memory, and start a fresh index for the current embeddings model.

        What's deleted is overwritten on disk, not just unlinked, and the file is then
        rebuilt without it (best effort: if another connection is busy, that waits).
        """
        with self._lock:
            try:
                self._conn.executescript(
                    """
                    PRAGMA secure_delete = ON;
                    BEGIN IMMEDIATE;
                    DROP TABLE IF EXISTS memory_vectors;
                    DROP TABLE IF EXISTS conversation_vectors;
                    DELETE FROM memories;
                    DELETE FROM memory_meta;
                    COMMIT;
                    """
                )
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
        self._init_schema()
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                log.warning("Memories deleted, but the database file couldn't be compacted: %s", exc)

    def recent(self, limit: int = 10, kind: Kind | None = "fact") -> list[MemoryHit]:
        sql = "SELECT id, kind, source, text, created_at, 0.0 FROM memories"
        params: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind = ?"
            params = (kind,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [MemoryHit(*r) for r in rows]

    def all_entries(self) -> list[MemoryHit]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, source, text, created_at, 0.0 FROM memories ORDER BY id"
            ).fetchall()
        return [MemoryHit(*r) for r in rows]

    def count(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall()
        return {kind: n for kind, n in rows}

    def close(self) -> None:
        self._conn.close()


def _title(entry: MemoryHit) -> str:
    """The title an entry is embedded with (see EmbeddingsConfig.document_prefix)."""
    if entry.kind == "document" and entry.source:
        return Path(entry.source).stem
    return "conversation" if entry.kind == "conversation" else "none"


def conversation_text(exchange: Exchange, limit: int) -> str:
    """How an exchange is stored in memory: both sides, cut short if they're long."""
    text = f"User: {exchange.question.strip()}\nAssistant: {exchange.answer.strip()}"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Chunking for document ingestion
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """Split text into chunks of whole paragraphs, each at most ``max_chars`` long."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        # Long paragraph: split on sentence ends, then hard-wrap anything still too long.
        current = ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > max_chars:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if current and len(current) + 1 + len(sentence) > max_chars:
                pieces.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 2 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def iter_text_files(paths: Sequence[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            files.extend(
                sorted(
                    f
                    for f in p.rglob("*")
                    if f.is_file()
                    and f.suffix.lower() in TEXT_SUFFIXES
                    and not any(part.startswith(".") for part in f.parts)
                )
            )
        elif p.is_file():
            files.append(p)
        else:
            raise FileNotFoundError(p)
    return files


# ---------------------------------------------------------------------------
# High-level service + tools for the agent
# ---------------------------------------------------------------------------


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
        for hit in self.store.search(vector, 1, kind="fact"):
            if hit.distance < DUPLICATE_DISTANCE:
                return hit.id, False
        return self.store.add(text, vector, "fact", source), True

    async def search(
        self, query: str, k: int | None = None, kind: Kind | None = None, *, conversations: bool = True
    ) -> list[MemoryHit]:
        vector = await self.embedder.embed_one(query, "query")
        return self.store.search(vector, k or self.cfg.search_top_k, kind, conversations=conversations)

    async def recall(self, query: str) -> list[MemoryHit]:
        """Memories relevant enough to show the model automatically: facts and documents, not old conversations."""
        hits = await self.search(query, self.cfg.recall_top_k, conversations=False)
        return [h for h in hits if h.distance <= self.cfg.recall_max_distance]

    def forget(self, memory_id: int) -> bool:
        return self.store.delete(memory_id)

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

    async def reindex(self) -> int:
        entries = self.store.all_entries()
        for start in range(0, len(entries), 32):
            batch = entries[start : start + 32]
            titles = [_title(e) for e in batch]
            vectors = await self.embedder.embed([e.text for e in batch], "document", titles)
            for entry, vector in zip(batch, vectors, strict=True):
                self.store.replace_embedding(entry.id, vector)
        self.store.set_embedding_model(self.embedder.cfg.model)
        return len(entries)

    # -- tools exposed to the model -------------------------------------------------

    def tools(self) -> list[Tool]:
        async def remember(args: dict[str, Any]) -> str:
            memory_id, created = await self.remember(str(args.get("fact", "")))
            return f"Saved as memory #{memory_id}." if created else f"Already known (memory #{memory_id})."

        async def search_memory(args: dict[str, Any]) -> str:
            query = str(args.get("query", "")).strip()
            if not query:
                return "Error: query is required."
            limit = int(args.get("limit") or self.cfg.search_top_k)
            hits = await self.search(query, max(1, min(limit, 20)))
            if not hits:
                return "No memories found."
            return json.dumps(
                [
                    {
                        "id": h.id,
                        "kind": h.kind,
                        "text": h.text,
                        "source": h.source if h.kind == "document" else None,
                        "saved": h.created_at[:10],
                        "relevance": round(1 - h.distance, 3),
                    }
                    for h in hits
                ],
                ensure_ascii=False,
            )

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
                    "Search long-term memory: facts the user has told you, their notes and documents, and your "
                    "past conversations with them (kind 'conversation', dated when they were said)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for, in natural language."},
                        "limit": {"type": "integer", "description": "Maximum results (default 8)."},
                    },
                    "required": ["query"],
                },
                handler=search_memory,
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
