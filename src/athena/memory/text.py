"""The text that goes into memory: exchanges as they're kept, and documents in chunks."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athena.history import Exchange

log = logging.getLogger(__name__)


TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}


def conversation_text(exchange: Exchange, limit: int) -> str:
    """How an exchange is stored in memory: both sides, cut short if they're long."""
    text = f"User: {exchange.question.strip()}\nAssistant: {exchange.answer.strip()}"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


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
