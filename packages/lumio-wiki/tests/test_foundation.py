from __future__ import annotations

import importlib
import importlib.util
import shutil
from pathlib import Path

import pytest
from lumio_wiki.records import RetrievalResult

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


def test_foundation_loads_validates_fingerprints_searches_reads_and_traverses():
    from lumio_wiki.knowledge_base import (
        fingerprint_sources,
        load_knowledge_base,
        validate,
    )

    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert report.is_valid
    assert validate(FIXTURES / "valid").is_valid
    assert kb.fingerprint().digest == fingerprint_sources(FIXTURES / "valid").digest
    assert kb.lookup_by_title("Architecture")[0].body
    assert kb.search_pages("LanceDB")
    # ADR-0021: canonical edges are accepted entity-to-entity Claims. The
    # legacy `valid` fixture carries none (its Relationship blocks were
    # removed with the input contract), so canonical traversal over it is
    # empty and title-level edges come from the categorized fixture's
    # accepted claim instead.
    assert kb.related_from("Lumio Overview") == []
    assert kb.graph_path("Lumio Overview", "Technology Stack") is None
    cat_kb, cat_report = load_knowledge_base(FIXTURES / "categorized_kb")
    assert cat_report.is_valid, [i.message for i in cat_report.issues]
    assert [(rel.target, rel.type) for rel in cat_kb.related_from("Lumio Overview")] == [
        ("Acme Corp", "uses")
    ]
    assert cat_kb.graph_path("Lumio Overview", "Acme Corp") == [
        "Lumio Overview",
        "Acme Corp",
    ]
    retrieved = kb.retrieve("Lumio uses LanceDB", limit=2)
    assert retrieved and all(isinstance(item, RetrievalResult) for item in retrieved)


def test_foundation_regenerates_portable_artifacts(tmp_path: Path):
    from lumio_wiki.knowledge_base import (
        fingerprint_sources,
        regenerate_reserved_artifacts,
    )

    target = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", target)
    before = fingerprint_sources(target).digest
    written = regenerate_reserved_artifacts(target)
    assert target / "index.md" in written
    assert target / "hot.md" in written
    assert (target / "index.md").is_file()
    assert (target / "hot.md").is_file()
    assert fingerprint_sources(target).digest == before


def _lumio_core_compat_available() -> bool:
    """ADR-0025: the temporary ``lumio.core`` compatibility surface lives in
    the private application repository; skip these alias checks when it (and
    therefore the application) is not installed."""
    return importlib.util.find_spec("lumio") is not None

@pytest.mark.skipif(
    not _lumio_core_compat_available(),
    reason="lumio.core lives in the private app repo (ADR-0025)",
)
def test_legacy_knowledge_base_module_aliases_the_new_owner():
    assert importlib.import_module("lumio.core.knowledge_base") is importlib.import_module(
        "lumio_wiki.knowledge_base"
    )
