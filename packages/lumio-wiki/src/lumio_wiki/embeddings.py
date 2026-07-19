"""Framework-independent embedding contract and ranking math for KB retrieval.

This module is part of the Core SDK and deliberately depends only on the
standard library and ``lumio.core`` records. It owns:

- the minimal structural ``Embedder`` seam the retrieval path calls,
- deterministic vector normalization / cosine / reciprocal-rank-fusion helpers,
- persistence of embedding-model metadata alongside the derived index.

It never imports an embedding framework (Torch, Sentence Transformers, ...).
Concrete providers live in ``lumio.providers`` and are *structurally*
compatible with ``Embedder`` (interface segregation): the Core SDK accepts any
object that exposes ``embed`` and ``model_info`` and never imports the provider
classes, so lexical-only deployments stay framework-free and there is no import
cycle (providers already import ``lumio.core``).

This is fresh retrieval over Compiled Pages / Knowledge Base Evidence. It is
distinct from Conversation Recall (#59): no persisted Reader questions, no chat
context, and no shared recall records are involved here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal, Protocol

import msgspec

from lumio_wiki.records import EmbeddingModelInfo

# Retrieval is scoped to Knowledge Base Evidence only. The literal excludes any
# recall/chat notion, keeping the #59 boundary explicit at the type level (#75).
RetrievalMode = Literal["lexical", "semantic", "hybrid"]

EMBEDDING_MODEL_FILE = "embedding_model.json"

# Default cosine-similarity floor below which a semantic candidate is treated as
# unrelated and dropped, so unsupported questions can still surface
# "not covered by this knowledge base" instead of a weak nearest-neighbour hit.
DEFAULT_SEMANTIC_THRESHOLD = 0.3

# Reciprocal-rank-fusion constant. A larger value smooths rank differences; 60
# is the canonical value from the original RRF paper and is deterministic.
RRF_K = 60


class Embedder(Protocol):
    """Minimal structural contract for anything that embeds text.

    The Core SDK calls only ``embed`` and reads ``model_info``; it does not
    import concrete provider classes. ``lumio.providers.EmbeddingProvider`` is
    a richer abstraction that is structurally compatible with this seam.
    """

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def model_info(self) -> EmbeddingModelInfo: ...


class EmbeddingError(ValueError):
    """Raised when an embedding provider is required but not available/usable."""


class EmbeddingNotBuiltError(EmbeddingError):
    """Raised when semantic/hybrid retrieval is requested without a built vector index."""


class EmbeddingDimensionMismatch(EmbeddingError):
    """Raised when a provider returns vectors whose dimension disagrees with its declared model."""


# ---------------------------------------------------------------------------
# Vector math (deterministic, framework-free).
# ---------------------------------------------------------------------------


def normalize_vector(vector: list[float]) -> list[float]:
    """Return the L2-normalized vector (unchanged for the zero vector)."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length vectors."""
    if len(a) != len(b):
        raise EmbeddingDimensionMismatch(
            f"cosine_similarity dimension mismatch: {len(a)} != {len(b)}"
        )
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def reciprocal_rank_fusion(
    ranked_id_lists: list[list[str]],
    *,
    k: int = RRF_K,
) -> dict[str, float]:
    """Fuse multiple ranked id lists into a deterministic id -> score map.

    Each inner list is ordered best-first (rank 0 is strongest). The fused
    score for an id is ``sum 1 / (k + rank)`` over the lists that contain it.
    Ties are broken by the caller (typically by id) for stable ordering.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_id_lists:
        for rank, evidence_id in enumerate(ranked):
            if evidence_id is None:
                continue
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (k + rank)
    return scores


# ---------------------------------------------------------------------------
# Embedding-model metadata persistence.
# ---------------------------------------------------------------------------


def save_embedding_model(index_dir: Path, info: EmbeddingModelInfo) -> None:
    """Persist embedding-model metadata alongside the derived index."""
    path = Path(index_dir) / EMBEDDING_MODEL_FILE
    path.write_bytes(msgspec.json.encode(info))


def load_embedding_model(index_dir: Path) -> EmbeddingModelInfo | None:
    """Load the embedding-model metadata stored with the index, if any."""
    path = Path(index_dir) / EMBEDDING_MODEL_FILE
    if not path.exists():
        return None
    return msgspec.json.decode(path.read_bytes(), type=EmbeddingModelInfo)


def is_semantic_index_stale(index_dir: Path, info: EmbeddingModelInfo) -> bool:
    """Return True if no vector metadata exists or it was built by a different model."""
    stored = load_embedding_model(index_dir)
    return stored is None or stored != info
