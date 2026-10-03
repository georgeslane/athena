"""Long-term memory: an embeddings model plus a SQLite vector store (sqlite-vec).

Two kinds of entries share one index:
  * facts     - short statements the assistant saves with the ``remember`` tool
  * documents - chunks of your own notes, added with ``pi-assistant ingest``
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import sqlite_vec
from openai import AsyncOpenAI

from pi_assistant.config import EmbeddingsConfig, MemoryConfig
from pi_assistant.tools import Tool

Kind = Literal["fact", "document"]
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
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors "
                f"USING vec0(embedding float[{self.dimensions}] distance_metric=cosine)"
            )
            meta = dict(self._conn.execute("SELECT key, value FROM memory_meta").fetchall())
            current = {"embedding_model": self.embedding_model, "dimensions": str(self.dimensions)}
            if not meta:
                self._conn.executemany("INSERT INTO memory_meta VALUES (?, ?)", current.items())
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

    def add(self, text: str, embedding: Sequence[float], kind: Kind, source: str | None = None) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO memories (kind, source, text, created_at) VALUES (?, ?, ?, ?)",
                (kind, source, text, now),
            )
            row_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO memory_vectors (rowid, embedding) VALUES (?, ?)",
                (row_id, sqlite_vec.serialize_float32(list(embedding))),
            )
        return int(row_id)

    def replace_embedding(self, memory_id: int, embedding: Sequence[float]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (memory_id,))
            self._conn.execute(
                "INSERT INTO memory_vectors (rowid, embedding) VALUES (?, ?)",
                (memory_id, sqlite_vec.serialize_float32(list(embedding))),
            )

    def search(self, embedding: Sequence[float], k: int, kind: Kind | None = None) -> list[MemoryHit]:
        # Over-fetch when filtering by kind, since the KNN step can't filter.
        fetch = k * 4 if kind else k
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.id, m.kind, m.source, m.text, m.created_at, v.distance
                FROM (
                    SELECT rowid, distance FROM memory_vectors
                    WHERE embedding MATCH ? AND k = ?
                ) AS v
                JOIN memories AS m ON m.id = v.rowid
                ORDER BY v.distance
                """,
                (sqlite_vec.serialize_float32(list(embedding)), fetch),
            ).fetchall()
        hits = [MemoryHit(*row) for row in rows]
        if kind:
            hits = [h for h in hits if h.kind == kind]
        return hits[:k]

    def get(self, memory_id: int) -> MemoryHit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, source, text, created_at, 0.0 FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return MemoryHit(*row) if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (memory_id,))
        return cur.rowcount > 0

    def delete_source(self, source: str) -> int:
        with self._lock, self._conn:
            ids = [r[0] for r in self._conn.execute("SELECT id FROM memories WHERE source = ?", (source,))]
            self._conn.executemany("DELETE FROM memory_vectors WHERE rowid = ?", [(i,) for i in ids])
            self._conn.execute("DELETE FROM memories WHERE source = ?", (source,))
        return len(ids)

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

    async def search(self, query: str, k: int | None = None, kind: Kind | None = None) -> list[MemoryHit]:
        vector = await self.embedder.embed_one(query, "query")
        return self.store.search(vector, k or self.cfg.search_top_k, kind)

    async def recall(self, query: str) -> list[MemoryHit]:
        """Memories relevant enough to show the model automatically."""
        hits = await self.search(query, self.cfg.recall_top_k)
        return [h for h in hits if h.distance <= self.cfg.recall_max_distance]

    def forget(self, memory_id: int) -> bool:
        return self.store.delete(memory_id)

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
            titles = [Path(e.source).stem if e.kind == "document" and e.source else "none" for e in batch]
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
                    "Search long-term memory: facts the user has told you and their ingested notes and documents."
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
