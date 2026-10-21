"""Long-term memory: an embeddings model plus a SQLite vector store (sqlite-vec).

Three kinds of entries:
  * facts         - short statements the assistant saves with the ``remember`` tool
  * documents     - chunks of your documents, indexed from folders on your Mac (documents.py),
                    or added with ``pi-assistant ingest``
  * conversations - each message you've sent and Athena's reply, added after it answers

Each kind has an index of its own. Facts are searched automatically for every message.
Documents and conversations are searched only when the model asks (``search_memory``):
a document is too long to add to every message, and an old answer could be mistaken for a
current one.

Searches are by meaning and by words together (``find``): every entry is also in a full-text
index (SQLite's FTS5), which finds exact names, numbers and references that meaning can miss,
and the two rankings are merged. Words still find things when the embeddings server can't be
reached. Facts recalled automatically are by meaning alone, as only close ones should be shown.

Facts Athena wants to remember while it works by itself, on a task, are suggestions until you
approve them on the dashboard: what it read could have put them there, and they'd be shown to
it in every conversation after.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import sqlite_vec
from openai import AsyncOpenAI

from pi_assistant.config import EmbeddingsConfig, MemoryConfig
from pi_assistant.tools import Tool, current_turn

if TYPE_CHECKING:
    from pi_assistant.history import ConversationStore, Exchange

log = logging.getLogger(__name__)

Kind = Literal["fact", "document", "conversation"]
VECTOR_TABLES = {"fact": "memory_vectors", "document": "document_vectors", "conversation": "conversation_vectors"}
KINDS: tuple[Kind, ...] = ("fact", "document", "conversation")
INDEXED_TO = "conversations_indexed_to"  # memory_meta: the last message whose exchange is in memory
DOCUMENTS_HOME = "documents_home"  # memory_meta: the home folder on the Mac documents come from, to show paths with ~
READ_CHARS = 5000  # how much of a document read_document returns at a time
ICLOUD_PATH, ICLOUD_DRIVE = "~/Library/Mobile Documents/com~apple~CloudDocs", "iCloud Drive"
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}
WORDS_TABLE = "memory_words"  # the full-text index of every entry's text
# Words are matched without case, accents or endings (porter): "Café birthdays" finds "cafe birthday".
WORDS_TOKENIZER = "porter unicode61 remove_diacritics 2"
FUSION_K = 60  # reciprocal rank fusion's constant: how much a top rank counts for over a lower one


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
    norm = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(norm) or norm == 0:
        # e.g. a model run in float16, which overflows: saving it would quietly break search.
        raise MemoryStoreError("The embeddings model returned an unusable vector (NaN, infinite or all zeros).")
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
class Found:
    """Something a search found: a fact, a past conversation, or a document by its best passage."""

    kind: Kind
    distance: float | None  # None: found by its words while meaning couldn't be searched
    text: str
    id: int | None = None  # facts and conversations
    path: str | None = None  # documents
    saved: str = ""  # the date it was saved, or for a document, the date it last changed
    words: bool = False  # it has the words searched for


@dataclass
class IndexedDocument:
    path: str  # its full path on your Mac
    folder: str  # the folder it was indexed from, as you chose it
    size: int
    modified: float  # when it last changed, as a Unix time
    chunks: int = 0
    indexed_at: str = ""
    note: str | None = None  # why it has no chunks, such as a scanned PDF without text


@dataclass
class MemoryHit:
    id: int
    kind: str
    source: str | None
    text: str
    created_at: str
    distance: float | None  # None: found by its words, without the query's embedding to measure it by


@dataclass
class Suggestion:
    """A fact Athena wanted to remember while working by itself, waiting for you to approve it."""

    id: int
    text: str
    source: str  # what it was doing, such as "your task “Morning briefing”"
    created_at: str


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
        self.words = True  # the full-text index works: False if this SQLite was built without FTS5
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
                -- Documents indexed from your Mac: what each was like when it was read, so indexing
                -- again only reads what's changed. Its chunks are memories with it as their source.
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY,
                    folder TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified REAL NOT NULL,
                    chunks INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL,
                    note TEXT
                );
                -- Facts Athena wanted to remember while working by itself, until you approve them.
                CREATE TABLE IF NOT EXISTS memory_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            for table in sorted(set(VECTOR_TABLES.values())):
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                    f"USING vec0(embedding float[{self.dimensions}] distance_metric=cosine)"
                )
            self._init_words()
            # Documents used to share facts' index: move any that are still there into their own.
            for (memory_id,) in self._conn.execute("SELECT id FROM memories WHERE kind = 'document'").fetchall():
                row = self._conn.execute(
                    "SELECT embedding FROM memory_vectors WHERE rowid = ?", (memory_id,)
                ).fetchone()
                if row:
                    self._conn.execute("DELETE FROM memory_vectors WHERE rowid = ?", (memory_id,))
                    self._conn.execute("DELETE FROM document_vectors WHERE rowid = ?", (memory_id,))
                    self._conn.execute(
                        "INSERT INTO document_vectors (rowid, embedding) VALUES (?, ?)", (memory_id, row[0])
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
                    f"'{self.embedding_model}'. To switch, run "
                    f"`bash scripts/switch-embeddings.sh {self.embedding_model}`, which re-embeds everything."
                )

    def _init_words(self) -> None:
        """The full-text index, kept in step with ``memories`` by triggers; built from it if it's new."""
        new = not self._conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (WORDS_TABLE,)).fetchone()
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {WORDS_TABLE} USING fts5(text, content='memories', "
                f"content_rowid='id', tokenize='{WORDS_TOKENIZER}')"
            )
        except sqlite3.OperationalError as exc:  # no FTS5: search by meaning alone, and keep saving memories
            log.warning("Searching memory by its words is off: this SQLite can't (%s)", exc)
            self.words = False
            for trigger in ("insert", "delete", "update"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS memories_words_{trigger}")
            return
        self._conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS memories_words_insert AFTER INSERT ON memories BEGIN
                INSERT INTO {WORDS_TABLE} (rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_words_delete AFTER DELETE ON memories BEGIN
                INSERT INTO {WORDS_TABLE} ({WORDS_TABLE}, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_words_update AFTER UPDATE OF text ON memories BEGIN
                INSERT INTO {WORDS_TABLE} ({WORDS_TABLE}, rowid, text) VALUES ('delete', old.id, old.text);
                INSERT INTO {WORDS_TABLE} (rowid, text) VALUES (new.id, new.text);
            END;
            """
        )
        if new:  # memories from before there was one
            self._conn.execute(f"INSERT INTO {WORDS_TABLE} ({WORDS_TABLE}) VALUES ('rebuild')")

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

    def replace_embeddings(self, vectors: Sequence[tuple[int, str, bytes]], model: str) -> None:
        """Swap in new (id, kind, serialised vector)s for every entry, and note the model, all at once."""
        with self._lock, self._conn:
            for memory_id, kind, blob in vectors:
                table = VECTOR_TABLES.get(kind, "memory_vectors")
                self._conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (memory_id,))
                self._conn.execute(f"INSERT INTO {table} (rowid, embedding) VALUES (?, ?)", (memory_id, blob))
            self._conn.execute(
                "INSERT INTO memory_meta VALUES ('embedding_model', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (model,),
            )
        self.embedding_model = model

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

    def search(self, embedding: Sequence[float], k: int, kinds: Sequence[Kind] = ("fact",)) -> list[MemoryHit]:
        """The ``k`` entries of these kinds nearest ``embedding``."""
        with self._lock:
            hits = [
                hit
                for table in sorted({VECTOR_TABLES[kind] for kind in kinds})
                for hit in self._nearest(table, embedding, k)
            ]
        return sorted((h for h in hits if h.kind in kinds), key=lambda h: h.distance)[:k]

    def search_words(
        self, words: str, k: int, kinds: Sequence[Kind], embedding: Sequence[float] | None = None
    ) -> list[MemoryHit]:
        """The ``k`` entries of these kinds that best match ``words``, an FTS5 query (see ``words_query``), best
        first. Each is measured against ``embedding`` too, if it's given, so it has a distance like any other."""
        if not self.words:
            return []
        marks = ", ".join("?" * len(kinds))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT m.id, m.kind, m.source, m.text, m.created_at FROM {WORDS_TABLE} AS w "
                f"JOIN memories AS m ON m.id = w.rowid WHERE {WORDS_TABLE} MATCH ? AND m.kind IN ({marks}) "
                f"ORDER BY bm25({WORDS_TABLE}) LIMIT ?",
                (words, *kinds, k),
            ).fetchall()
            hits = [MemoryHit(*row, distance=None) for row in rows]
            if embedding is not None:
                query = sqlite_vec.serialize_float32(list(embedding))
                for hit in hits:
                    row = self._conn.execute(
                        f"SELECT vec_distance_cosine(embedding, ?) FROM {VECTOR_TABLES[hit.kind]} "  # type: ignore[index]
                        "WHERE rowid = ?",
                        (query, hit.id),
                    ).fetchone()
                    hit.distance = row[0] if row else None
        return hits

    def get(self, memory_id: int) -> MemoryHit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, source, text, created_at, 0.0 FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return MemoryHit(*row) if row else None

    def find_text(self, kind: Kind, text: str) -> int | None:
        """The newest memory of this kind with exactly this text."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM memories WHERE kind = ? AND text = ? ORDER BY id DESC LIMIT 1", (kind, text)
            ).fetchone()
        return row[0] if row else None

    def delete(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            for table in set(VECTOR_TABLES.values()):
                self._conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (memory_id,))
        return cur.rowcount > 0

    def delete_source(self, source: str) -> int:
        with self._lock, self._conn:
            n = self._conn.execute("SELECT COUNT(*) FROM memories WHERE source = ?", (source,)).fetchone()[0]
            self._delete_source(source)
        return n

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
                    DROP TRIGGER IF EXISTS memories_words_insert;
                    DROP TRIGGER IF EXISTS memories_words_delete;
                    DROP TRIGGER IF EXISTS memories_words_update;
                    DROP TABLE IF EXISTS memory_words;
                    DROP TABLE IF EXISTS memory_vectors;
                    DROP TABLE IF EXISTS conversation_vectors;
                    DROP TABLE IF EXISTS document_vectors;
                    DELETE FROM memories;
                    DELETE FROM documents;
                    DELETE FROM memory_meta;
                    DELETE FROM memory_suggestions;
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

    # -- documents indexed from your Mac -----------------------------------------------------

    def documents(self) -> dict[str, IndexedDocument]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, folder, size, modified, chunks, indexed_at, note FROM documents"
            ).fetchall()
        return {row[0]: IndexedDocument(*row) for row in rows}

    def put_document(self, doc: IndexedDocument, chunks: Sequence[tuple[str, Sequence[float]]]) -> None:
        """Replace a document's chunks and its record, together, so a search never finds half of each."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._delete_source(doc.path)
            for text, embedding in chunks:
                self._insert(text, embedding, "document", doc.path, now)
            self._conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc.path, doc.folder, doc.size, doc.modified, len(chunks), now, doc.note),
            )

    def move_document(self, path: str, folder: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE documents SET folder = ? WHERE path = ?", (folder, path))

    def remove_documents(self, paths: Sequence[str]) -> None:
        with self._lock, self._conn:
            for path in paths:
                self._delete_source(path)
                self._conn.execute("DELETE FROM documents WHERE path = ?", (path,))

    def document_chunks(self, path: str) -> list[str]:
        """A document's chunks, in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT text FROM memories WHERE kind = 'document' AND source = ? ORDER BY id", (path,)
            ).fetchall()
        return [row[0] for row in rows]

    def document_sources(self) -> list[str]:
        """Every document in memory: those indexed from your Mac, and any added with `pi-assistant ingest`."""
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT source FROM memories WHERE kind = 'document'").fetchall()
        return [row[0] for row in rows if row[0]]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _delete_source(self, source: str) -> None:
        ids = [r[0] for r in self._conn.execute("SELECT id FROM memories WHERE source = ?", (source,))]
        for table in set(VECTOR_TABLES.values()):
            self._conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(i,) for i in ids])
        self._conn.execute("DELETE FROM memories WHERE source = ?", (source,))

    # -- suggestions, waiting for you to approve them ------------------------------------------

    def suggest(self, text: str, source: str) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO memory_suggestions (text, source, created_at) VALUES (?, ?, ?)", (text, source, now)
            )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def suggestions(self) -> list[Suggestion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, source, created_at FROM memory_suggestions ORDER BY id"
            ).fetchall()
        return [Suggestion(*row) for row in rows]

    def suggestion(self, suggestion_id: int) -> Suggestion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, text, source, created_at FROM memory_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
        return Suggestion(*row) if row else None

    def drop_suggestion(self, suggestion_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM memory_suggestions WHERE id = ?", (suggestion_id,)).rowcount > 0

    def count(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind").fetchall()
        return {kind: n for kind, n in rows}

    def close(self) -> None:
        self._conn.close()


# Words too common to tell anything apart, left out of a search by words.
STOPWORDS = frozenset(
    "a about all an and any are as at be been but by can could did do does for from had has have how i if in into "
    "is it its me my no not of on or our so some than that the their them then there these they this to us was we "
    "were what when where which who why will with would you your".split()
)
_TOKEN = re.compile(r"[^\W_]+")  # as the index's tokenizer splits text: letters and digits
_POSSESSIVE = re.compile(r"['’]s\b")
_PARTS = re.compile(r'"([^"]*)"|(\S+)')
MAX_WORDS = 12


def words_query(query: str) -> str | None:
    """An FTS5 query for searching ``query`` by its words: any of them, ranked by how many an entry has and how
    rare they are. Parts in double quotes are phrases an entry must have. None if there's nothing to search for.

    Only letters and digits get through, each word quoted, so nothing in ``query`` is read as FTS5's own syntax.
    """
    phrases: list[list[str]] = []
    words: list[list[str]] = []
    for quoted, bare in _PARTS.findall(query):
        if quoted:
            if tokens := _TOKEN.findall(quoted.lower()):
                phrases.append(tokens)
            continue
        tokens = _TOKEN.findall(_POSSESSIVE.sub("", bare).lower())
        if len(tokens) > 1:  # such as AAPL_US_EQ or an email address: its parts together
            words.append(tokens)
        elif tokens and tokens[0] not in STOPWORDS and (len(tokens[0]) > 1 or tokens[0].isdigit()):
            words.append(tokens)
    chosen, join = (phrases, " AND ") if phrases else (list({" ".join(w): w for w in words}.values()), " OR ")
    if not chosen:
        return None
    return join.join('"' + " ".join(tokens) + '"' for tokens in chosen[:MAX_WORDS])


def fuse(*rankings: Sequence[MemoryHit]) -> list[MemoryHit]:
    """Rankings of the same entries merged into one, by reciprocal rank fusion: an entry high in either, or fairly
    high in both, comes first. Each entry is kept as the first ranking with it has it."""
    scores: dict[int, float] = {}
    hits: dict[int, MemoryHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1 / (FUSION_K + rank + 1)
            hits.setdefault(hit.id, hit)
    return sorted(hits.values(), key=lambda h: -scores[h.id])


def _title(entry: MemoryHit) -> str:
    """The title an entry is embedded with (see EmbeddingsConfig.document_prefix)."""
    if entry.kind == "document" and entry.source:
        return Path(entry.source).stem
    return "conversation" if entry.kind == "conversation" else "none"


def relevance(found: Found) -> float | None:
    """How close in meaning something found is to what was searched for, from 1 down: None if it was found by its
    words alone."""
    return None if found.distance is None else round(1 - found.distance, 3)


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
