from pathlib import Path

import pytest
from lumio_wiki.embeddings import EmbeddingError
from lumio_wiki.knowledge_base import (
    KnowledgeBaseError,
    fingerprint_sources,
    load_knowledge_base,
)
from lumio_wiki.records import Citation, CompiledPage, Evidence, RetrievalResult, RetrievalTrace
from lumio_wiki.retrieval import ZeroIndexRetrieval

FIXTURES = Path(__file__).parent / "fixtures"


def test_zero_index_adapter_retrieve_returns_cited_results():
    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert report.is_valid
    results = ZeroIndexRetrieval().retrieve(kb.pages, "Lumio uses LanceDB", limit=5)
    assert results
    result = results[0]
    assert isinstance(result, RetrievalResult)
    assert isinstance(result.evidence, Evidence)
    assert isinstance(result.citation, Citation)
    assert isinstance(result.trace, RetrievalTrace)
    assert result.citation.relative_path and result.citation.page_title
    assert result.score >= 0 and result.reason
    assert any("LanceDB" in r.snippet for r in results)
    names = {s.name for r in results for s in r.trace.stages}
    assert names >= {"search", "rank"}
    assert all("LanceDB BM25" not in s.detail for r in results for s in r.trace.stages)


def test_zero_index_adapter_unknown_and_limit_and_semantic():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    adapter = ZeroIndexRetrieval()
    assert adapter.retrieve(kb.pages, "banana", limit=5) == []
    assert len(adapter.retrieve(kb.pages, "Lumio", limit=1)) <= 1
    with pytest.raises(EmbeddingError, match="LanceDB|adapter|semantic"):
        adapter.retrieve(kb.pages, "Lumio", mode="semantic")


def test_nested_section_retrieval_cites_specific_passage():
    page = CompiledPage(
        path="nested.md",
        title="Nested",
        body=(
            "Introduction before headings.\n"
            "# Parent\n"
            "Parent context.\n"
            "## Child\n"
            "Child context.\n"
            "#### Deep Target\n"
            "The needle is only in this nested passage.\n"
        ),
        body_start_line=10,
    )
    results = ZeroIndexRetrieval().retrieve([page], "needle", limit=1)
    assert len(results) == 1
    result = results[0]
    assert result.citation.section_title == "Deep Target"
    assert result.citation.line_start == 15
    assert result.citation.line_end == 16
    assert "needle" in result.snippet
    assert result.trace.results_returned == 1
    assert result.trace.results_dropped >= 2


def test_fenced_code_headings_are_not_retrievable_sections():
    page = CompiledPage(
        path="fenced.md",
        title="Fenced",
        body=(
            "# Actual\n"
            "```markdown\n"
            "```python\n"
            "## Fake\n"
            "needle in an example\n"
            "```\n"
            "## Real\n"
            "needle in published content\n"
        ),
    )
    result = ZeroIndexRetrieval().retrieve([page], "needle", limit=1)[0]
    assert result.citation.section_title == "Real"




def test_bounded_page_reads_share_the_fence_aware_section_parser(tmp_path):
    """One ATX section parser serves Evidence extraction AND bounded reads:
    ``KnowledgeBase.read_page(section=...)`` cannot select a heading inside a
    fenced code block that retrieval correctly ignores."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / "fenced.md").write_text(
        "---\n"
        'title: "Fenced"\n'
        "aliases: []\n"
        "tags:\n  - fenced\n"
        'summary: "Fence-aware bounded reads."\n'
        'lifecycle: "draft"\n'
        'visibility: "public"\n'
        "sources:\n  - id: fenced\n    title: Fenced source\n"
        "synthetic: false\n"
        "---\n"
        "# Actual\n"
        "```markdown\n"
        "```python\n"
        "## Fake\n"
        "needle in an example\n"
        "```\n"
        "## Real\n"
        "needle in published content\n",
        encoding="utf-8",
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    with pytest.raises(KnowledgeBaseError, match="section not found"):
        kb.read_page("Fenced", section="Fake")
    read = kb.read_page("Fenced", section="Real")
    assert "needle in published content" in read.content
    assert "## Fake" not in read.content


def test_loaded_view_cannot_stamp_old_pages_fresh(tmp_path):
    import shutil

    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    old_digest = kb.fingerprint().digest
    index_dir = tmp_path / "idx"
    kb.build_index(index_dir)
    kb.materialize_graph(index_dir)

    (root / "overview.md").write_text(
        (root / "overview.md").read_text(encoding="utf-8") + "\nchanged on disk\n",
        encoding="utf-8",
    )
    assert fingerprint_sources(root).digest != old_digest
    with pytest.raises(KnowledgeBaseError, match="changed after this view was loaded"):
        kb.build_index(index_dir)
    with pytest.raises(KnowledgeBaseError, match="changed after this view was loaded"):
        kb.retrieve(
            "LanceDB",
            index_dir=index_dir,
            graph_seed_titles=["Lumio Overview"],
        )
    # An immutable old view can still inspect its graph, but it remains bound
    # to the captured digest rather than stamping the newer live tree.
    state = kb.load_or_derive_graph(index_dir)
    assert state.fingerprint_digest == old_digest


def test_kb_zero_index_build_index_roundtrip_freshness(tmp_path):
    from lumio_wiki.knowledge_base import is_fresh

    kb, _ = load_knowledge_base(FIXTURES / "valid")
    indexed = kb.build_index(tmp_path / "idx")  # default zero-index
    stored = indexed.stored_fingerprint()
    assert indexed.retrieval is not None or stored is not None
    assert stored is not None
    assert is_fresh(stored, fingerprint_sources(kb.root))
