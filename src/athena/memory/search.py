"""Searching by words, and merging that with searching by meaning."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from athena.memory.store import Found, MemoryHit

if TYPE_CHECKING:
    pass


log = logging.getLogger(__name__)

FUSION_K = 60  # reciprocal rank fusion's constant: how much a top rank counts for over a lower one


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


def relevance(found: Found) -> float | None:
    """How close in meaning something found is to what was searched for, from 1 down: None if it was found by its
    words alone."""
    return None if found.distance is None else round(1 - found.distance, 3)
