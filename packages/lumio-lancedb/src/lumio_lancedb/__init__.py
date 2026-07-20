"""LanceDB retrieval adapter for the Lumio Knowledge Base (``lumio-lancedb``).

Installing ``lumio-lancedb`` beside ``lumio-wiki`` adds BM25, vector/semantic,
and hybrid/RRF ranking without changing the Knowledge Base format, retrieval
interface, Evidence, citations, or client behavior. ``lumio-wiki`` never
imports this package; callers inject :class:`LanceDBRetrievalAdapter` into a
:class:`lumio_wiki.KnowledgeBase` explicitly (see :func:`build_lancedb_index`).

The local embedding extra (``lumio-lancedb[embeddings]``) pulls in
``sentence-transformers`` and Torch; Torch stays out of the base adapter and
out of ``lumio-wiki`` (ADR-0010).
"""

from __future__ import annotations

from lumio_lancedb.index import (
    PAGE_TABLE_NAME,
    TABLE_NAME,
    VECTOR_TABLE_NAME,
    LanceDBRetrievalAdapter,
    build_lancedb_index,
    build_lexical_index,
    build_semantic_index,
    has_semantic_index,
    search_hybrid_index,
    search_lexical_index,
    search_pages,
    search_semantic_index,
)

__all__ = [
    "LanceDBRetrievalAdapter",
    "PAGE_TABLE_NAME",
    "TABLE_NAME",
    "VECTOR_TABLE_NAME",
    "build_lancedb_index",
    "build_lexical_index",
    "build_semantic_index",
    "has_semantic_index",
    "search_hybrid_index",
    "search_lexical_index",
    "search_pages",
    "search_semantic_index",
]

__version__ = "0.1.1"
