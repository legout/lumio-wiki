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

from lumio_lancedb.graph import (
    ENTITY_TABLE_NAME,
    GRAPH_EDGE_TABLE_NAME,
    build_graph_tables,
    has_graph_tables,
    load_graph_state,
    search_entity_candidates,
)
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
from lumio_lancedb.location import (
    IndexLocation,
    LocalIndexLocation,
    RemoteIndexLocation,
)
from lumio_lancedb.publish import remote_publication_builder

__all__ = [
    "ENTITY_TABLE_NAME",
    "GRAPH_EDGE_TABLE_NAME",
    "IndexLocation",
    "LanceDBRetrievalAdapter",
    "LocalIndexLocation",
    "PAGE_TABLE_NAME",
    "RemoteIndexLocation",
    "TABLE_NAME",
    "VECTOR_TABLE_NAME",
    "build_graph_tables",
    "build_lancedb_index",
    "build_lexical_index",
    "build_semantic_index",
    "has_graph_tables",
    "has_semantic_index",
    "load_graph_state",
    "remote_publication_builder",
    "search_entity_candidates",
    "search_hybrid_index",
    "search_lexical_index",
    "search_pages",
    "search_semantic_index",
]

__version__ = "0.1.1"
