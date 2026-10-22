"""The memory store: entries and their embeddings in SQLite, searched with sqlite-vec by meaning and with FTS5
by words, and the suggestions you approve on the dashboard."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import sqlite_vec

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


Kind = Literal["fact", "document", "conversation"]
VECTOR_TABLES = {"fact": "memory_vectors", "document": "document_vectors", "conversation": "conversation_vectors"}
KINDS: tuple[Kind, ...] = ("fact", "document", "conversation")
INDEXED_TO = "conversations_indexed_to"  # memory_meta: the last message whose exchange is in memory
DOCUMENTS_HOME = "documents_home"  # memory_meta: the home folder on the Mac documents come from, to show paths with ~
WORDS_TABLE = "memory_words"  # the full-text index of every entry's text
# Words are matched without case, accents or endings (porter): "Café birthdays" finds "cafe birthday".
WORDS_TOKENIZER = "porter unicode61 remove_diacritics 2"


class MemoryStoreError(Exception):
    """Raised for memory store problems the user needs to act on."""


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
                    f"`athena embeddings switch {self.embedding_model}`, which re-embeds everything."
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
        """Every document in memory: those indexed from your Mac, and any added with `athena ingest`."""
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
