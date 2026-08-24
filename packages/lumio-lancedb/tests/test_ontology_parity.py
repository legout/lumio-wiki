"""Behavioural parity over one ontology corpus (issue #173, ADR-0021).

The shared corpus in ``eval/ontology_corpus`` exercises every ontology
phenomenon in one place: entity and literal Claims, an inverse Predicate
pair, disputed/superseded lifecycle, Extracted References (including
discovery-only neighbours), an Entity Merge redirect, visibility classes,
and ontology validation failures.

Every traversal/retrieval assertion in this module runs identically against
the zero-index MessagePack projection and the LanceDB ``entities`` /
``graph_edges`` projections of the same corpus — the parity #173 requires.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lumio_wiki.knowledge_base import (
    EXTRACTOR_VERSION,
    GRAPH_DIRECTION_BOTH,
    GRAPH_DIRECTION_INCOMING,
    GRAPH_DIRECTION_OUTGOING,
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    fingerprint_sources,
    load_knowledge_base,
)
from lumio_wiki.graph_state import (
    build_graph_state,
    load_graph_artifact,
)
from lumio_wiki.records import CLAIM_STATUS_ACCEPTED

from lumio_lancedb.graph import (
    ENTITY_TABLE_NAME,
    GRAPH_EDGE_TABLE_NAME,
    build_graph_tables,
    load_graph_state,
)

CORPUS = Path(__file__).resolve().parents[3] / "eval" / "ontology_corpus"


@pytest.fixture(scope="module")
def corpus_kb():
    kb, report = load_knowledge_base(CORPUS)
    assert report.is_valid, [i.message for i in report.issues]
    return kb


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory):
    """A writable copy of the corpus, so tests can inject faults."""
    target = tmp_path_factory.mktemp("ontology-parity") / "corpus"
    shutil.copytree(CORPUS, target)
    return target


PUBLIC_TITLES = frozenset({"Lumio", "LanceDB", "Knowledge Graph"})
ALL_TITLES = PUBLIC_TITLES | {"obstore", "Sage Wiki"}


# ---------------------------------------------------------------------------
# One corpus, three projections: in-memory, MessagePack artifact, LanceDB.
# ---------------------------------------------------------------------------


def _zero_index_state(kb, tmp_path):
    """The zero-index projection: derived in memory, round-tripped via MessagePack."""
    fingerprint = fingerprint_sources(kb.root)
    state = build_graph_state(
        kb._knowledge_index(),  # noqa: SLF001 — parity harness, not a public seam
        fingerprint=fingerprint,
        extractor_version=EXTRACTOR_VERSION,
    )
    kb.materialize_graph(tmp_path / "derived")
    loaded = load_graph_artifact(
        tmp_path / "derived", fingerprint=fingerprint, extractor_version=EXTRACTOR_VERSION
    )
    assert loaded is not None, "freshly materialized artifact must load"
    assert loaded == state, "MessagePack artifact must round-trip the derived state"
    return state


def test_lancedb_projection_matches_zero_index_graph_state(corpus_kb, tmp_path):
    """The #173 headline: identical accepted traversal topology, both engines."""
    zero = _zero_index_state(corpus_kb, tmp_path)
    fingerprint = fingerprint_sources(corpus_kb.root)
    location = tmp_path / "lance"
    build_graph_tables(corpus_kb, location, fingerprint)
    projected = load_graph_state(location, fingerprint)
    assert projected is not None
    assert projected == zero


def test_projection_contains_every_corpus_phenomenon(corpus_kb, tmp_path):
    """The corpus really exercises inverse, lifecycle, redirect, and scopes."""
    zero = _zero_index_state(corpus_kb, tmp_path)
    out = zero.outgoing["entity:lumio"]

    # Inverse pair: both directions exist as authored, accepted Claims.
    assert {e.predicate for e in zero.outgoing["entity:lumio"] if e.claim_id} == {"uses"}
    assert {e.predicate for e in zero.outgoing["entity:lancedb"] if e.claim_id} == {"used-by"}

    # Accepted entity endpoints; superseded claim collapses with its successor.
    assert {e.endpoint for e in out if e.scope == "canonical"} == {
        "entity:lancedb",
        "entity:obstore",
    }
    # Disputed `replaces` claim never enters adjacency; literal claim is not an edge.
    assert all(e.predicate != "replaces" for edge_list in zero.outgoing.values() for e in edge_list)
    assert all("first-released" != e.predicate for e in out)

    # Discovery scope adds Extracted-Reference-only neighbours.
    discovery_endpoints = {e.endpoint for e in out}
    assert discovery_endpoints == {
        "entity:lancedb",
        "entity:obstore",
        "entity:sage-wiki",
        "entity:knowledge-graph",
    }

    # The Entity Merge redirect keeps the retired ID resolvable.
    resolution = corpus_kb.resolve_entity("entity:legacy-wiki")
    assert resolution.entity is not None
    assert resolution.entity.id == "entity:sage-wiki"


# ---------------------------------------------------------------------------
# Identical traversal/retrieval assertions against both projections.
# ---------------------------------------------------------------------------

TRAVERSAL_CASES = [
    # (source, scope, direction, relationship_type, expected titles)
    ("Lumio", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_OUTGOING, None, ["LanceDB", "obstore"]),
    ("Lumio", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_OUTGOING, "uses", ["LanceDB", "obstore"]),
    ("Lumio", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_OUTGOING, "replaces", []),
    ("Lumio", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_INCOMING, None, ["LanceDB"]),
    ("Lumio", GRAPH_SCOPE_DISCOVERY, GRAPH_DIRECTION_OUTGOING, None,
     ["Knowledge Graph", "LanceDB", "Sage Wiki", "obstore"]),
    ("Lumio", GRAPH_SCOPE_DISCOVERY, GRAPH_DIRECTION_BOTH, None,
     ["Knowledge Graph", "LanceDB", "Sage Wiki", "obstore"]),
    ("LanceDB", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_OUTGOING, "used-by", ["Lumio"]),
    ("LanceDB", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_INCOMING, "uses", ["Lumio"]),
    ("obstore", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_OUTGOING, None, []),
    ("obstore", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_INCOMING, None, ["Lumio"]),
    ("Sage Wiki", GRAPH_SCOPE_CANONICAL, GRAPH_DIRECTION_BOTH, None, []),
    ("Sage Wiki", GRAPH_SCOPE_DISCOVERY, GRAPH_DIRECTION_OUTGOING, None, ["Lumio"]),
    ("Knowledge Graph", GRAPH_SCOPE_DISCOVERY, GRAPH_DIRECTION_INCOMING, None, ["Lumio"]),
]


def _projections(corpus_kb, tmp_path):
    """Yield (label, kb) pairs: the base KB and the LanceDB-bound KB."""
    from lumio_lancedb.index import build_lancedb_index

    yield "zero-index", corpus_kb
    yield "lancedb", build_lancedb_index(corpus_kb, tmp_path / "lance")


@pytest.mark.parametrize(
    "source,scope,direction,relationship_type,expected",
    TRAVERSAL_CASES,
    ids=[c[0] + "-" + c[1] + "-" + c[2] for c in TRAVERSAL_CASES],
)
def test_identical_traversal_across_projections(
    corpus_kb, tmp_path, source, scope, direction, relationship_type, expected
):
    """Same bounded traversal, both projections, identical results."""
    for _label, kb in _projections(corpus_kb, tmp_path):
        assert kb.related_pages(
            source, scope=scope, direction=direction, relationship_type=relationship_type
        ) == expected


def test_identical_shortest_paths_across_projections(corpus_kb, tmp_path):
    for _label, kb in _projections(corpus_kb, tmp_path):
        both = {"direction": GRAPH_DIRECTION_BOTH}
        # Canonical: obstore -> LanceDB travels through Lumio.
        assert kb.shortest_path("obstore", "LanceDB", **both) == ["obstore", "Lumio", "LanceDB"]
        # Discovery-only: Sage Wiki reaches Knowledge Graph through Lumio's body link.
        assert kb.shortest_path(
            "Sage Wiki", "Knowledge Graph", scope=GRAPH_SCOPE_DISCOVERY, **both
        ) == ["Sage Wiki", "Lumio", "Knowledge Graph"]
        # No canonical path exists through a disputed edge.
        assert kb.shortest_path("Lumio", "Sage Wiki", **both) is None


def test_identical_authorization_before_expansion(corpus_kb, tmp_path):
    """Visibility filtering is applied before expansion on both projections."""
    for _label, kb in _projections(corpus_kb, tmp_path):
        public = kb.related_pages("Lumio", candidate_titles=PUBLIC_TITLES)
        assert public == ["LanceDB"], "internal obstore/Sage Wiki must be filtered before expansion"
        # An internal seed under a public-only authorization set expands to nothing.
        assert kb.related_pages("Sage Wiki", candidate_titles=PUBLIC_TITLES) == []


def test_identical_retrieval_eligibility_across_projections(corpus_kb, tmp_path):
    """Citation-ready retrieval selects the same eligible pages on both engines."""
    kwargs = {
        "graph_seed_titles": ["Lumio"],
        "graph_scope": GRAPH_SCOPE_DISCOVERY,
        "graph_direction": GRAPH_DIRECTION_OUTGOING,
        "graph_max_depth": 1,
    }
    results = {}
    for label, kb in _projections(corpus_kb, tmp_path):
        retrieved = kb.retrieve("deployable chat platform", limit=10, **kwargs)
        results[label] = {r.evidence.page_title for r in retrieved}
        assert retrieved, f"{label}: graph-expanded retrieval must return Evidence"
        # Every result carries citation-ready Evidence (claims are not Evidence).
        for r in retrieved:
            assert r.evidence.page_title in ALL_TITLES
    assert results["zero-index"] == results["lancedb"]


def test_entity_resolution_surfaces(corpus_kb):
    """Exact ID / title / alias / redirect resolution is deterministic."""
    cases = [
        ("entity:lumio", "entity:lumio", "entity-id"),
        ("Lumio", "entity:lumio", "canonical-title"),
        ("Lumio Knowledge Platform", "entity:lumio", "alias"),
        ("entity:legacy-wiki", "entity:sage-wiki", "redirect"),
        ("Lance Columnar Store", "entity:lancedb", "alias"),
        ("entity:no-such-entity", None, ""),
    ]
    for name, expected_id, expected_match in cases:
        resolution = corpus_kb.resolve_entity(name)
        got = resolution.entity.id if resolution.entity is not None else None
        assert got == expected_id, name
        assert resolution.matched_by == expected_match, name


# ---------------------------------------------------------------------------
# Ontology failures block validation and publication (corpus fault injection).
# ---------------------------------------------------------------------------


def _load_expecting_invalid(root: Path) -> list[str]:
    _kb, report = load_knowledge_base(root)
    assert not report.is_valid
    return [i.message for i in report.issues if i.severity == "error"]


def test_unknown_predicate_in_corpus_blocks_validation(corpus_dir):
    page = corpus_dir / "entities" / "lancedb.md"
    original = page.read_text()
    page.write_text(original.replace('predicate: "used-by"', 'predicate: "consumes"'))
    try:
        messages = _load_expecting_invalid(corpus_dir)
        assert any("unknown predicate: consumes" in m for m in messages)
    finally:
        page.write_text(original)


def test_domain_violation_in_corpus_blocks_validation(corpus_dir):
    # `uses` requires a software-system subject; obstore is a library.
    page = corpus_dir / "entities" / "obstore.md"
    original = page.read_text()
    faulty = original.replace(
        "---\n\n# obstore",
        "claims:\n  - id: \"claim:obstore-uses-lumio\"\n"
        "    predicate: \"uses\"\n"
        "    object: \"entity:lumio\"\n"
        "    status: \"accepted\"\n"
        "    evidence:\n      - section: \"Overview\"\n"
        "---\n\n# obstore",
    )
    page.write_text(faulty)
    try:
        messages = _load_expecting_invalid(corpus_dir)
        assert any("domain violation" in m for m in messages)
    finally:
        page.write_text(original)


def test_redirect_cycle_in_corpus_blocks_validation(corpus_dir):
    control = corpus_dir / "lumio.yaml"
    original = control.read_text()
    control.write_text(
        original.replace(
            "    entity:legacy-wiki: entity:sage-wiki",
            "    entity:legacy-wiki: entity:sage-wiki\n"
            "    entity:sage-wiki: entity:legacy-wiki",
        )
    )
    try:
        messages = _load_expecting_invalid(corpus_dir)
        # sage-wiki is a live entity: it may not redirect; the pair also cycles.
        assert any("redirect" in m for m in messages)
    finally:
        control.write_text(original)


def test_missing_evidence_section_blocks_validation(corpus_dir):
    page = corpus_dir / "entities" / "lancedb.md"
    original = page.read_text()
    page.write_text(original.replace('section: "Adoption"', 'section: "Nowhere"'))
    try:
        messages = _load_expecting_invalid(corpus_dir)
        assert any("unknown evidence section" in m for m in messages)
    finally:
        page.write_text(original)


# ---------------------------------------------------------------------------
# Publication faults: graph tables missing / stale / fingerprint-mismatched.
# ---------------------------------------------------------------------------


def test_local_projection_falls_back_truthfully_on_faults(corpus_kb, tmp_path):
    """Missing, partial, stale, and fingerprint-mismatched tables never load."""
    fingerprint = fingerprint_sources(corpus_kb.root)

    # Missing tables entirely.
    assert load_graph_state(tmp_path / "lance", fingerprint) is None

    # Partial build: entities present, graph_edges missing.
    build_graph_tables(corpus_kb, tmp_path / "lance", fingerprint)
    import shutil as _shutil

    _shutil.rmtree(tmp_path / "lance" / f"{GRAPH_EDGE_TABLE_NAME}.lance")
    assert load_graph_state(tmp_path / "lance", fingerprint) is None

    # Fingerprint-mismatched: tables built for a different content fingerprint.
    from lumio_wiki.records import SourceFingerprint

    wrong = SourceFingerprint(digest="0" * 64, sources=list(fingerprint.sources))
    build_graph_tables(corpus_kb, tmp_path / "lance2", wrong)
    assert load_graph_state(tmp_path / "lance2", fingerprint) is None

    # A fresh rebuild recovers.
    build_graph_tables(corpus_kb, tmp_path / "lance3", fingerprint)
    assert load_graph_state(tmp_path / "lance3", fingerprint) is not None


def test_accepted_edge_rows_carry_lifecycle_and_evidence(corpus_kb, tmp_path):
    """The projection discloses lifecycle + evidence (citation-readiness)."""
    import lancedb

    fingerprint = fingerprint_sources(corpus_kb.root)
    build_graph_tables(corpus_kb, tmp_path / "lance", fingerprint)
    db = lancedb.connect(tmp_path / "lance")
    assert set(db.list_tables().tables) >= {ENTITY_TABLE_NAME, GRAPH_EDGE_TABLE_NAME}

    edges = db.open_table(GRAPH_EDGE_TABLE_NAME).to_arrow().to_pylist()
    by_id = {row["edge_id"]: row for row in edges}
    accepted = by_id["claim:lumio-uses-lancedb"]
    assert accepted["status"] == CLAIM_STATUS_ACCEPTED
    assert accepted["subject"] == "entity:lumio"
    assert accepted["endpoint"] == "entity:lancedb"
    assert "Retrieval" in accepted["evidence"]
    # Superseded stays inspectable with empty traversal scope; disputed too.
    assert by_id["claim:lumio-uses-obstore-v1"]["status"] == "superseded"
    assert by_id["claim:lumio-uses-obstore-v1"]["scope"] == ""
    assert by_id["claim:lumio-replaces-sage-wiki"]["status"] == "disputed"
    # The literal claim is inspectable and is not an edge.
    literal = by_id["claim:lumio-first-released"]
    assert literal["value"] == "2025"  # literal values are serialized as text
    assert literal["endpoint"] == ""
