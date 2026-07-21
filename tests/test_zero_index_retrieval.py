from pathlib import Path

import pytest

from lumio_wiki.embeddings import EmbeddingError
from lumio_wiki.fingerprint_store import load_stored_fingerprint
from lumio_wiki.knowledge_base import fingerprint_sources, load_knowledge_base
from lumio_wiki.records import Citation, Evidence, RetrievalResult, RetrievalTrace
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


def test_zero_index_adapter_build_writes_fingerprint(tmp_path):
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    index_dir = tmp_path / "idx"
    fp = fingerprint_sources(kb.root)
    ZeroIndexRetrieval().build_index(kb.pages, index_dir, fingerprint=fp)
    stored = load_stored_fingerprint(index_dir)
    assert stored is not None
    assert stored.digest == fp.digest


def test_kb_zero_index_retrieve_without_index_dir():
    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert report.is_valid
    results = kb.retrieve("Lumio uses LanceDB", limit=5)
    assert results
    assert isinstance(results[0], RetrievalResult)


def test_kb_zero_index_build_index_roundtrip_freshness(tmp_path):
    from lumio_wiki.knowledge_base import is_fresh

    kb, _ = load_knowledge_base(FIXTURES / "valid")
    indexed = kb.build_index(tmp_path / "idx")  # default zero-index
    assert indexed.retrieval is not None or indexed.stored_fingerprint() is not None
    assert is_fresh(indexed.stored_fingerprint(), fingerprint_sources(kb.root))
