"""Embeddings, from an OpenAI-compatible server such as oMLX on the Mac."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from openai import AsyncOpenAI

from athena.config import EmbeddingsConfig
from athena.memory.store import MemoryStoreError

if TYPE_CHECKING:
    pass


log = logging.getLogger(__name__)


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
