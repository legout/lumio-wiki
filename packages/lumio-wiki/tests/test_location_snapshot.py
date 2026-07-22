"""Tests for the public Knowledge Base Location + immutable Snapshot seam.

Issue #119, ADR-0013: expand the Core SDK around the public Knowledge Base
Location and immutable Knowledge Base Snapshot seam. A Location resolves an
immutable Published Version (a Snapshot); the filesystem implementation is the
verified reference. These tests prove the seam is a real public contract and
that every observable filesystem behavior — validation, fingerprinting,
zero-index retrieval, Discovery Graph traversal, Citations, and Retrieval
Traces — is byte-for-byte identical when a Knowledge Base is opened through the
new seam instead of the path-based loader.

S3 (parent epic #118) is intentionally out of scope: only the filesystem
implementation is exercised here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    fingerprint_sources,
    load_knowledge_base,
)
from lumio_wiki.location import (
    FILESYSTEM_LOCATION_KIND,
    FilesystemLocation,
    KnowledgeBaseLocation,
    KnowledgeBaseSnapshot,
    open_filesystem_knowledge_base,
    open_knowledge_base,
)
from lumio_wiki.records import (
    RetrievalResult,
    SourceFingerprint,
    ValidationReport,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

# Every equivalence assertion runs over both shipped filesystem fixtures: the
# legacy-flat ``valid`` KB (root-level pages with Relationships) and the
# categorized ``categorized_kb`` (Control File + directory categories).
LOCATION_FIXTURES = [FIXTURES / "valid", FIXTURES / "categorized_kb"]


# ---------------------------------------------------------------------------
# 1. The Location is a public, runtime-checkable contract.
# ---------------------------------------------------------------------------


def test_filesystem_location_satisfies_the_location_contract():
    location = FilesystemLocation(FIXTURES / "valid")
    assert isinstance(location, KnowledgeBaseLocation)
    assert location.kind == FILESYSTEM_LOCATION_KIND
    # describe() never leaks credentials (filesystem has none) and is stable.
    desc = location.describe()
    assert isinstance(desc, str) and desc


def test_filesystem_location_describe_names_the_path():
    location = FilesystemLocation(FIXTURES / "valid")
    assert str((FIXTURES / "valid").resolve()) in location.describe()


# ---------------------------------------------------------------------------
# 2. resolve() opens a filesystem Knowledge Base as an immutable Snapshot.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_resolve_returns_an_immutable_snapshot(fixture):
    snapshot = FilesystemLocation(fixture).resolve()
    assert isinstance(snapshot, KnowledgeBaseSnapshot)
    assert isinstance(snapshot.knowledge_base, type(load_knowledge_base(fixture)[0]))
    assert isinstance(snapshot.validation_report, ValidationReport)
    assert isinstance(snapshot.fingerprint, SourceFingerprint)
    assert isinstance(snapshot.location, KnowledgeBaseLocation)
    # The Snapshot is immutable: its captured state cannot be reassigned.
    with pytest.raises((AttributeError, TypeError)):
        snapshot.knowledge_base = snapshot.knowledge_base  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        snapshot.fingerprint = snapshot.fingerprint  # type: ignore[misc]
    # The page collection is an immutable view: mutating it cannot affect the
    # Snapshot, and the backing list is never exposed through the seam surface.
    immutable_pages = snapshot.pages
    assert isinstance(immutable_pages, tuple)
    with pytest.raises((AttributeError, TypeError)):
        immutable_pages.append(immutable_pages[0])  # type: ignore[attr-defined]


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_validation_report_matches_the_path_based_loader(fixture):
    direct_report = load_knowledge_base(fixture)[1]
    snapshot = FilesystemLocation(fixture).resolve()
    assert snapshot.validation_report == direct_report


# ---------------------------------------------------------------------------
# 3. Equivalence: the seam produces identical observable results to the
#    existing path-based filesystem behavior (validation, fingerprinting,
#    pages, control file).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_pages_match_the_path_based_loader(fixture):
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    # pages is an immutable tuple view; compare element-wise against the list.
    assert list(snapshot.pages) == direct_kb.pages
    assert [p.title for p in snapshot.pages] == [p.title for p in direct_kb.pages]


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_control_file_matches_the_path_based_loader(fixture):
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    assert snapshot.control == direct_kb.control


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_fingerprint_matches_fingerprint_sources(fixture):
    snapshot = FilesystemLocation(fixture).resolve()
    # The Published Version fingerprint is captured at resolve time and is the
    # immutable digest of this version; it equals the live source digest.
    assert snapshot.fingerprint == fingerprint_sources(fixture)


# ---------------------------------------------------------------------------
# 4. Equivalence: zero-index retrieval, Citations, and Retrieval Traces.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_zero_index_retrieval_matches_the_path_based_loader(fixture):
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    query = "Lumio LanceDB architecture"
    direct_results = direct_kb.retrieve(query, limit=5)
    seam_results = snapshot.retrieve(query, limit=5)
    assert len(seam_results) == len(direct_results)
    for seam, direct in zip(seam_results, direct_results, strict=True):
        assert isinstance(seam, RetrievalResult)
        # Citation identity, snippet, score, and the full Retrieval Trace must
        # match exactly — the seam changes no observable retrieval behavior.
        assert seam.evidence == direct.evidence
        assert seam.citation == direct.citation
        assert seam.snippet == direct.snippet
        assert seam.score == direct.score
        assert seam.reason == direct.reason
        assert seam.trace == direct.trace


def test_snapshot_retrieval_returns_citation_ready_results():
    snapshot = FilesystemLocation(FIXTURES / "valid").resolve()
    results = snapshot.retrieve("LanceDB retrieval", limit=3)
    assert results
    for result in results:
        # A Citation lets an answer point back to its source content.
        assert result.citation.page_title
        assert result.citation.relative_path
        assert result.trace.stages


def test_snapshot_graph_seeded_retrieval_matches_the_path_based_loader():
    """Graph-seeded retrieval through the seam matches the path-based loader,
    including the shared default ``discovery`` scope (ADR-0011, #112)."""
    from lumio_wiki.knowledge_base import GRAPH_SCOPE_DISCOVERY

    fixture = FIXTURES / "valid"
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    seeds = ["Lumio Overview"]
    # Default scope (no explicit graph_scope) must match the path-based loader.
    direct_default = direct_kb.retrieve("Lumio", limit=5, graph_seed_titles=seeds)
    seam_default = snapshot.retrieve("Lumio", limit=5, graph_seed_titles=seeds)
    assert _results_equal(seam_default, direct_default)
    # Explicit discovery scope must also match.
    direct_disc = direct_kb.retrieve(
        "Lumio", limit=5, graph_seed_titles=seeds, graph_scope=GRAPH_SCOPE_DISCOVERY
    )
    seam_disc = snapshot.retrieve(
        "Lumio", limit=5, graph_seed_titles=seeds, graph_scope=GRAPH_SCOPE_DISCOVERY
    )
    assert _results_equal(seam_disc, direct_disc)


def _results_equal(seam: list[RetrievalResult], direct: list[RetrievalResult]) -> bool:
    if len(seam) != len(direct):
        return False
    for s, d in zip(seam, direct, strict=True):
        if (
            s.evidence != d.evidence
            or s.citation != d.citation
            or s.snippet != d.snippet
            or s.score != d.score
            or s.reason != d.reason
            or s.trace != d.trace
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# 5. Equivalence: search and registry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_search_matches_the_path_based_loader(fixture):
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    assert snapshot.search_pages("Lumio") == direct_kb.search_pages("Lumio")


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_snapshot_registry_matches_the_path_based_loader(fixture):
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()
    assert snapshot.registry() == direct_kb.registry()


def test_snapshot_graph_traversal_matches_the_path_based_loader():
    fixture = FIXTURES / "valid"
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()

    titles = [page.title for page in direct_kb.pages]
    # Canonical relationships.
    assert snapshot.related_from("Lumio Overview") == direct_kb.related_from(
        "Lumio Overview"
    )
    assert snapshot.graph_path("Lumio Overview", "Technology Stack") == direct_kb.graph_path(
        "Lumio Overview", "Technology Stack"
    )
    # Public traversal seam — canonical scope.
    assert snapshot.related_pages("Lumio Overview", candidate_titles=titles) == (
        direct_kb.related_pages("Lumio Overview", candidate_titles=titles)
    )
    assert snapshot.shortest_path(
        "Lumio Overview", "Technology Stack", candidate_titles=titles
    ) == direct_kb.shortest_path("Lumio Overview", "Technology Stack", candidate_titles=titles)
    # Discovery scope (canonical + extracted references).
    assert snapshot.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_DISCOVERY, candidate_titles=titles
    ) == direct_kb.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_DISCOVERY, candidate_titles=titles
    )
    assert snapshot.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_CANONICAL, candidate_titles=titles
    ) == direct_kb.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_CANONICAL, candidate_titles=titles
    )
    # Extracted-reference inspection.
    assert snapshot.extracted_references("Lumio Overview") == direct_kb.extracted_references(
        "Lumio Overview"
    )
    assert snapshot.extraction_diagnostics() == direct_kb.extraction_diagnostics()


# ---------------------------------------------------------------------------
# 7. The path-based entrypoint is retained and compatible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_path_based_entrypoint_still_works(fixture):
    kb, report = load_knowledge_base(fixture)
    assert isinstance(kb.pages, list)
    assert isinstance(report, ValidationReport)
    # The path-based loader and the Location seam agree on the loaded pages.
    snapshot = FilesystemLocation(fixture).resolve()
    assert [p.title for p in kb.pages] == [p.title for p in snapshot.pages]


def test_open_knowledge_base_resolves_any_location():
    location = FilesystemLocation(FIXTURES / "valid")
    snapshot = open_knowledge_base(location)
    assert isinstance(snapshot, KnowledgeBaseSnapshot)
    assert snapshot.location is location


def test_open_filesystem_knowledge_base_is_a_path_shorthand():
    snapshot = open_filesystem_knowledge_base(FIXTURES / "valid")
    assert isinstance(snapshot, KnowledgeBaseSnapshot)
    assert snapshot.location.kind == FILESYSTEM_LOCATION_KIND


# ---------------------------------------------------------------------------
# 8. The Location contract surfaces filesystem errors through public behavior.
# ---------------------------------------------------------------------------


def test_resolve_a_missing_path_raises_knowledge_base_error():
    from lumio_wiki.knowledge_base import KnowledgeBaseError

    location = FilesystemLocation(FIXTURES / "does-not-exist")
    with pytest.raises(KnowledgeBaseError):
        location.resolve()


def test_resolve_a_file_path_raises_knowledge_base_error():
    from lumio_wiki.knowledge_base import KnowledgeBaseError

    not_a_dir = FIXTURES / "valid" / "overview.md"
    location = FilesystemLocation(not_a_dir)
    with pytest.raises(KnowledgeBaseError):
        location.resolve()


def test_snapshot_validation_of_invalid_kb_matches_the_path_based_loader():
    """An invalid Knowledge Base surfaces the SAME validation issues through the
    seam as the path-based loader — the seam changes no validation behavior."""
    fixture = FIXTURES / "invalid"
    direct_report = load_knowledge_base(fixture)[1]
    snapshot = FilesystemLocation(fixture).resolve()
    assert snapshot.validation_report == direct_report
    assert snapshot.validation_report.is_valid == direct_report.is_valid


# ---------------------------------------------------------------------------
# 9. The snapshot read surface round-trips lookups identically.
# ---------------------------------------------------------------------------


def test_snapshot_lookups_match_the_path_based_loader():
    fixture = FIXTURES / "valid"
    direct_kb, _ = load_knowledge_base(fixture)
    snapshot = FilesystemLocation(fixture).resolve()

    assert snapshot.lookup_by_title("Architecture") == direct_kb.lookup_by_title("Architecture")
    assert snapshot.lookup_by_alias("Stack") == direct_kb.lookup_by_alias("Stack")
    assert snapshot.lookup_by_tag("lumio") == direct_kb.lookup_by_tag("lumio")
    assert snapshot.lookup_by_source("lumio-overview") == direct_kb.lookup_by_source(
        "lumio-overview"
    )
    assert snapshot.lookup_by_lifecycle("approved") == direct_kb.lookup_by_lifecycle("approved")
    assert snapshot.public_pages() == direct_kb.public_pages()
