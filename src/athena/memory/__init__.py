"""Long-term memory: an embeddings model plus a SQLite vector store (sqlite-vec).

Three kinds of entries:
  * facts         - short statements the assistant saves with the ``remember`` tool
  * documents     - chunks of your documents, indexed from folders on your Mac (documents.py),
                    or added with ``athena ingest``
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

from athena.memory.embeddings import Embedder, fit_dimensions
from athena.memory.search import fuse, relevance, words_query
from athena.memory.service import MemoryService
from athena.memory.store import (
    DOCUMENTS_HOME,
    KINDS,
    Found,
    IndexedDocument,
    Kind,
    MemoryHit,
    MemoryStore,
    MemoryStoreError,
    Suggestion,
    connect_db,
)
from athena.memory.text import chunk_text, conversation_text, iter_text_files

__all__ = [
    "DOCUMENTS_HOME",
    "KINDS",
    "Embedder",
    "Found",
    "IndexedDocument",
    "Kind",
    "MemoryHit",
    "MemoryService",
    "MemoryStore",
    "MemoryStoreError",
    "Suggestion",
    "chunk_text",
    "connect_db",
    "conversation_text",
    "fit_dimensions",
    "fuse",
    "iter_text_files",
    "relevance",
    "words_query",
]
