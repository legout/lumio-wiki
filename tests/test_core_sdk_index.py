import shutil
import sys
import tempfile
from pathlib import Path
from subprocess import run

from lumio.core import (
    Citation,
    Evidence,
    RetrievalResult,
    RetrievalTrace,
    SourceFingerprint,
    fingerprint_sources,
    is_fresh,
    load_knowledge_base,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_fingerprint_sources_is_deterministic():
    fp1 = fingerprint_sources(FIXTURES / "valid")
    fp2 = fingerprint_sources(FIXTURES / "valid")
    assert isinstance(fp1, SourceFingerprint)
    assert fp1.digest == fp2.digest


def test_freshness_reports_stale_after_source_change():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "valid"
        shutil.copytree(FIXTURES / "valid", source)

        fp1 = fingerprint_sources(source)
        overview = source / "overview.md"
        overview.write_text(overview.read_text() + "\n")
        fp2 = fingerprint_sources(source)

        assert not is_fresh(fp1, fp2)


def test_rebuilding_restores_freshness():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "valid"
        index_dir = Path(tmp) / "index"
        shutil.copytree(FIXTURES / "valid", source)

        kb, _ = load_knowledge_base(source)
        kb = kb.build_index(index_dir)
        assert is_fresh(kb.stored_fingerprint(), fingerprint_sources(source))

        overview = source / "overview.md"
        overview.write_text(overview.read_text() + "\n")
        assert not is_fresh(kb.stored_fingerprint(), fingerprint_sources(source))

        kb = kb.build_index(index_dir)
        assert is_fresh(kb.stored_fingerprint(), fingerprint_sources(source))


def test_rebuilding_from_source_reproduces_index():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "valid"
        index_dir = Path(tmp) / "index"
        shutil.copytree(FIXTURES / "valid", source)

        kb, _ = load_knowledge_base(source)
        kb = kb.build_index(index_dir)
        before = kb.retrieve("Lumio uses LanceDB", limit=5)
        assert before

        shutil.rmtree(index_dir)
        kb = kb.build_index(index_dir)
        after = kb.retrieve("Lumio uses LanceDB", limit=5)
        assert after

        assert [r.evidence.id for r in before] == [r.evidence.id for r in after]
        assert [r.snippet for r in before] == [r.snippet for r in after]


def test_lookup_by_title():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    pages = kb.lookup_by_title("Architecture")
    assert len(pages) == 1
    assert pages[0].title == "Architecture"


def test_lookup_by_alias():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    pages = kb.lookup_by_alias("Stack")
    assert len(pages) == 1
    assert pages[0].title == "Technology Stack"


def test_lookup_by_tag():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    pages = kb.lookup_by_tag("architecture")
    assert len(pages) == 1
    assert pages[0].title == "Architecture"


def test_lookup_by_source():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    pages = kb.lookup_by_source("architecture-doc")
    assert len(pages) == 1
    assert pages[0].title == "Architecture"


def test_lookup_by_lifecycle():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    pages = kb.lookup_by_lifecycle("approved")
    assert len(pages) == 3
    assert {p.title for p in pages} == {
        "Lumio Overview",
        "Architecture",
        "Technology Stack",
    }


def test_related_from():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    rels = kb.related_from("Lumio Overview")
    assert any(r.target == "Architecture" and r.type == "relates-to" for r in rels)


def test_graph_path_two_hops():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    path = kb.graph_path("Lumio Overview", "Technology Stack")
    assert path == ["Lumio Overview", "Architecture", "Technology Stack"]


def test_graph_path_unreachable_returns_none():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    assert kb.graph_path("Lumio Overview", "Missing Page") is None


def test_retrieve_lexical_returns_cited_results():
    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert report.is_valid

    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp)
        kb = kb.build_index(index_dir)
        results = kb.retrieve("Lumio uses LanceDB", limit=5)

    assert len(results) >= 1
    result = results[0]
    assert isinstance(result, RetrievalResult)
    assert isinstance(result.evidence, Evidence)
    assert isinstance(result.citation, Citation)
    assert isinstance(result.trace, RetrievalTrace)
    assert result.citation.relative_path
    assert result.citation.page_title
    assert result.score >= 0
    assert result.reason
    assert any("LanceDB" in r.snippet for r in results)


def test_retrieve_unknown_fact_returns_no_results():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp)
        kb = kb.build_index(index_dir)
        results = kb.retrieve("banana", limit=5)
    assert results == []


def test_retrieval_result_source_type_is_free_string():
    kb, _ = load_knowledge_base(FIXTURES / "valid")
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp)
        kb = kb.build_index(index_dir)
        results = kb.retrieve("Lumio uses LanceDB", limit=5)
    assert results
    assert all(isinstance(r.evidence.source_type, str) for r in results)
    assert all(r.evidence.source_type == "compiled_markdown" for r in results)


def test_cli_retrieve_returns_cited_results():
    with tempfile.TemporaryDirectory() as tmp:
        index_dir = Path(tmp)
        result = run(
            [
                sys.executable,
                "-m",
                "lumio.cli",
                "retrieve",
                str(FIXTURES / "valid"),
                "Lumio uses LanceDB",
                "--index-dir",
                str(index_dir),
            ],
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0
    assert "Architecture" in result.stdout
    assert "Lumio uses LanceDB" in result.stdout
    assert "architecture.md" in result.stdout
