"""Shared test helper: build a LanceDB-backed Knowledge Base.

Importing the full LanceDB retrieval behavior in tests keeps the canonical
``KnowledgeBase`` zero-index by default (so LanceDB/pyarrow stay optional at
the core level) while letting enhanced-retrieval tests opt into BM25 +
semantic/hybrid with one call.
"""

from lumio.core.index import build_lancedb_index


def build_lancedb_kb(kb, index_dir, *, embedder=None):
    """Build a LanceDB-derived index on ``kb`` and return the bound KB."""
    return build_lancedb_index(kb, index_dir, embedder=embedder)
