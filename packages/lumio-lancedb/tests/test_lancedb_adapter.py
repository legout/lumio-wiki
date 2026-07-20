"""Shared retrieval contract smoke tests for the lumio-lancedb adapter wheel.

Run from an isolated wheel install (ADR-0010): builds a tiny Knowledge Base
inline and exercises build + lexical + freshness, returning a non-zero exit on
failure. No repo fixtures required.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lumio_lancedb import (
    LanceDBRetrievalAdapter,
    build_lexical_index,
    has_semantic_index,
    search_lexical_index,
)
from lumio_wiki.records import CompiledPage, Relationship


def _pages() -> list[CompiledPage]:
    return [
        CompiledPage(
            path="overview.md",
            title="Overview",
            body="Lumio uses LanceDB for derived evidence retrieval.\n",
            relationships=[Relationship(target="tech.md", type="depends-on")],
        ),
        CompiledPage(
            path="tech.md",
            title="Technology",
            body="Retrieval is backed by LanceDB full-text search.\n",
        ),
    ]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp)
        build_lexical_index(_pages(), index_dir)
        results = search_lexical_index(index_dir, "LanceDB", limit=5)
        assert results, "lexical search must hit the fixture query"
        first = results[0]
        assert first.evidence.source_type == "compiled_markdown"
        assert first.citation.page_title
        assert first.snippet
        assert first.trace.stages
        assert has_semantic_index(index_dir) is False
    assert LanceDBRetrievalAdapter().name == "lancedb"
    print("lumio-lancedb isolated wheel smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
