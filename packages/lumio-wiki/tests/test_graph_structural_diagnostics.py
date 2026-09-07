"""Tests for scope-aware structural graph diagnostics (issue #126, ADR-0011).

``KnowledgeBase.graph_diagnostics`` returns a deterministic, read-only
``StructuralGraphReport`` over the in-memory graph for EITHER the canonical
graph (reviewed typed Relationships only) or the Discovery Graph
(Relationships PLUS Extracted References). This is STRUCTURAL topology
analysis, distinct from the artifact/runtime observability in
``GraphHealthReport`` (graph_fresh, materialization, latency).

All tests are zero-index: no LanceDB, PyArrow, or operational database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    Claim,
    CompiledPage,
    GraphHub,
    Relationship,
    Source,
    StructuralGraphReport,
    UnresolvedReferenceGroup,
    UnresolvedReferenceSample,
)

# ---------------------------------------------------------------------------
# Page + Knowledge Base builders.
# ---------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _claims_from(source_title: str, relationships: list[Relationship] | None) -> list[Claim]:
    """Convert title-level test edges to accepted entity-to-entity Claims."""
    return [
        Claim(
            id=f"claim:{_slug(source_title)}-{_slug(rel.target)}-{n}",
            predicate=rel.type,
            object=f"entity:{_slug(rel.target)}",
            status=CLAIM_STATUS_ACCEPTED,
        )
        for n, rel in enumerate(relationships or [])
    ]


def _page(
    title: str,
    body: str = "",
    *,
    path: str | None = None,
    aliases: list[str] | None = None,
    relationships: list[Relationship] | None = None,
    visibility: str = "public",
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower()}.md",
        title=title,
        id=f"entity:{_slug(title)}",
        entity_types=["concept"],
        aliases=aliases or [],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility=visibility,
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        claims=_claims_from(title, relationships),
        body=body,
    )


def _kb(pages: list[CompiledPage], *, root: Path | None = None) -> KnowledgeBase:
    return KnowledgeBase(root=root or Path("."), pages=pages)


def _hub_kb() -> list[CompiledPage]:
    """A canonical-only topology with a clear outbound and inbound hub.

    Canonical Relationships only (no body links):

        Hub --uses--> A --uses--> Sink
        Hub --uses--> B --uses--> Sink
        Hub --uses--> C
        D (isolated)
    """
    hub = _page(
        "Hub",
        relationships=[
            Relationship(target="A", type="uses"),
            Relationship(target="B", type="uses"),
            Relationship(target="C", type="uses"),
        ],
    )
    a = _page("A", relationships=[Relationship(target="Sink", type="uses")])
    b = _page("B", relationships=[Relationship(target="Sink", type="uses")])
    c = _page("C")
    sink = _page("Sink")
    d = _page("D")
    return [hub, a, b, c, sink, d]


# ---------------------------------------------------------------------------
# Report shape: separate from artifact/runtime observability.
# ---------------------------------------------------------------------------


def test_report_never_embeds_compiled_page_bodies():
    kb = _kb(_hub_kb())
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    assert isinstance(report, StructuralGraphReport)
    # No body text leaks anywhere in the report payload.
    blob = repr(report)
    for page in _hub_kb():
        assert (page.summary or "") not in blob
    assert "body" not in {f for f in dir(report)}


def test_every_result_identifies_scope():
    kb = _kb(_hub_kb())
    for scope in (GRAPH_SCOPE_CANONICAL, GRAPH_SCOPE_DISCOVERY):
        report = kb.graph_diagnostics(scope=scope)
        assert report.scope == scope


def test_invalid_scope_raises():
    kb = _kb(_hub_kb())
    with pytest.raises(ValueError):
        kb.graph_diagnostics(scope="nope")


# ---------------------------------------------------------------------------
# Core structural counts + directionality (canonical, no body links).
# ---------------------------------------------------------------------------


def test_canonical_counts_and_directionality():
    kb = _kb(_hub_kb())
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)

    assert report.page_count == 6
    assert report.edge_count == 5  # Hub->A,B,C + A->Sink + B->Sink

    # inbound orphan = no page points here: Hub (in=0), D (in=0).
    assert report.inbound_orphan_count == 2
    # outbound orphan = points to nobody: C (out=0), Sink (out=0), D (out=0).
    assert report.outbound_orphan_count == 3

    # Hub is the dominant outbound hub (3 outgoing edges).
    assert report.top_outbound_hubs[0] == GraphHub(
        title="Hub", edge_count=3, entity_id="entity:hub"
    )
    # Sink is the dominant inbound hub (2 incoming edges).
    assert report.top_inbound_hubs[0] == GraphHub(
        title="Sink", edge_count=2, entity_id="entity:sink"
    )
    # Directionality: Hub has zero inbound, so it never appears as inbound hub.
    assert all(h.title != "Hub" for h in report.top_inbound_hubs)
    # Sink has zero outbound, so it never appears as outbound hub.
    assert all(h.title != "Sink" for h in report.top_outbound_hubs)


def test_weakly_connected_components_and_coverage():
    kb = _kb(_hub_kb())
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    # {Hub,A,B,C,Sink} connected + {D} isolated.
    assert report.weakly_connected_component_count == 2
    assert report.largest_component_coverage == pytest.approx(5 / 6)
    # Components use an undirected projection of directed edges.
    assert report.undirected_projection_used is True


# ---------------------------------------------------------------------------
# Cycles are handled (no infinite traversal, correct component).
# ---------------------------------------------------------------------------


def test_cycles_handled():
    pages = [
        _page("A", relationships=[Relationship(target="B", type="uses")]),
        _page("B", relationships=[Relationship(target="C", type="uses")]),
        _page("C", relationships=[Relationship(target="A", type="uses")]),
    ]
    kb = _kb(pages)
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    assert report.edge_count == 3
    assert report.weakly_connected_component_count == 1
    assert report.largest_component_coverage == pytest.approx(1.0)
    # No page is an orphan in a full cycle.
    assert report.inbound_orphan_count == 0
    assert report.outbound_orphan_count == 0


# ---------------------------------------------------------------------------
# Both scopes differ when Extracted References exist.
# ---------------------------------------------------------------------------


def _linked_kb_pages() -> list[CompiledPage]:
    """Canonical Alpha->Beta plus extracted Alpha->Gamma, Gamma->Beta."""
    alpha = _page(
        "Alpha",
        body="See [g](gamma.md).\n",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    beta = _page("Beta")
    gamma = _page("Gamma", body="Back to [b](beta.md).\n")
    return [alpha, beta, gamma]


def test_canonical_scope_excludes_extracted_edges():
    kb = _kb(_linked_kb_pages())
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    assert report.edge_count == 1  # only Alpha->Beta
    # Gamma is isolated in the canonical graph.
    assert report.weakly_connected_component_count == 2
    assert report.largest_component_coverage == pytest.approx(2 / 3)


def test_discovery_scope_includes_extracted_edges():
    kb = _kb(_linked_kb_pages())
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY)
    assert report.edge_count == 3  # Alpha->Beta, Alpha->Gamma, Gamma->Beta
    assert report.weakly_connected_component_count == 1
    assert report.largest_component_coverage == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bounded samples with deterministic ordering.
# ---------------------------------------------------------------------------


def test_hub_samples_are_bounded():
    # Build many pages each with one outgoing edge to a shared sink so the
    # outbound-hub list would be large and tied.
    sink = _page("Sink")
    pages = [sink]
    for i in range(40):
        pages.append(_page(f"P{i:02d}", relationships=[Relationship(target="Sink", type="uses")]))
    kb = _kb(pages)
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, max_hub_sample=5)
    assert len(report.top_outbound_hubs) == 5
    # All outbound hubs have degree 1 (tied) -> deterministic title order.
    titles = [h.title for h in report.top_outbound_hubs]
    assert titles == sorted(titles)
    # The single inbound hub (Sink, degree 40) is bounded too.
    assert len(report.top_inbound_hubs) == 1
    assert report.top_inbound_hubs[0] == GraphHub(
        title="Sink", edge_count=40, entity_id="entity:sink"
    )


def test_orphan_samples_are_bounded_and_deterministic():
    # 30 isolated pages -> all are both inbound and outbound orphans.
    pages = [_page(f"Iso{i:02d}") for i in range(30)]
    kb = _kb(pages)
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, max_orphan_sample=10)
    assert report.inbound_orphan_count == 30
    assert report.outbound_orphan_count == 30
    assert len(report.inbound_orphan_sample_titles) == 10
    assert len(report.outbound_orphan_sample_titles) == 10
    # Deterministic (lexicographic) ordering of the representative sample.
    assert report.inbound_orphan_sample_titles == tuple(sorted(report.inbound_orphan_sample_titles))
    assert report.outbound_orphan_sample_titles == tuple(
        sorted(report.outbound_orphan_sample_titles)
    )


def test_negative_sample_bounds_rejected():
    kb = _kb(_hub_kb())
    with pytest.raises(ValueError):
        kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, max_hub_sample=-1)
    with pytest.raises(ValueError):
        kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, max_orphan_sample=-1)
    with pytest.raises(ValueError):
        kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, max_unresolved_sample=-1)


# ---------------------------------------------------------------------------
# Visibility-filtered candidate sets.
# ---------------------------------------------------------------------------


def test_candidate_set_restricts_nodes_and_edges():
    # Discovery graph: Alpha->Beta->Gamma->Delta.
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", body="[g](gamma.md)\n")
    gamma = _page("Gamma", body="[d](delta.md)\n")
    delta = _page("Delta")
    kb = _kb([alpha, beta, gamma, delta])

    full = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY)
    assert full.page_count == 4
    assert full.edge_count == 3

    # Exclude Delta: the Gamma->Delta edge drops, Gamma becomes an outbound
    # orphan, and only Alpha->Beta, Beta->Gamma remain.
    filtered = kb.graph_diagnostics(
        scope=GRAPH_SCOPE_DISCOVERY,
        candidate_titles=["Alpha", "Beta", "Gamma"],
    )
    assert filtered.page_count == 3
    assert filtered.edge_count == 2
    assert "Delta" not in filtered.outbound_orphan_sample_titles
    assert "Gamma" in filtered.outbound_orphan_sample_titles
    assert filtered.weakly_connected_component_count == 1


def test_candidate_set_excluding_source_isolates_target():
    alpha = _page("Alpha", relationships=[Relationship(target="Beta", type="uses")])
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    # Only Beta authorized: the Alpha->Beta edge drops (Alpha not authorized).
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL, candidate_titles=["Beta"])
    assert report.page_count == 1
    assert report.edge_count == 0
    assert report.outbound_orphan_count == 1
    assert report.inbound_orphan_count == 1


# ---------------------------------------------------------------------------
# Unresolved references aggregated by outcome and target.
# ---------------------------------------------------------------------------


def test_unresolved_references_aggregated_by_outcome_and_target():
    # Two pages link to the SAME missing target -> one group, count 2.
    # A third page links to a different missing target -> second group.
    a = _page("A", body="[x](missing.md)\n")
    b = _page("B", body="[y](missing.md)\n")
    c = _page("C", body="[z](other-missing.md)\n")
    kb = _kb([a, b, c])

    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY)
    groups = report.unresolved_references
    assert len(groups) == 2

    by_target = {g.target: g for g in groups}
    assert isinstance(by_target["missing.md"], UnresolvedReferenceGroup)
    assert by_target["missing.md"].outcome == "broken"
    assert by_target["missing.md"].count == 2
    # Bounded representative samples carry actionable source locations.
    samples = by_target["missing.md"].samples
    assert all(isinstance(s, UnresolvedReferenceSample) for s in samples)
    sample_paths = {s.source_path for s in samples}
    assert {"a.md", "b.md"} == sample_paths
    assert by_target["other-missing.md"].count == 1


def test_unresolved_samples_bounded():
    # Many pages link to the same missing target; samples are bounded.
    pages = [_page(f"P{i:02d}", body="[x](missing.md)\n") for i in range(12)]
    kb = _kb(pages)
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY, max_unresolved_sample=3)
    assert len(report.unresolved_references) == 1
    group = report.unresolved_references[0]
    assert group.count == 12
    assert len(group.samples) == 3
    # Deterministic ordering of the representative subset.
    sample_keys = [(s.source_path, s.line_start) for s in group.samples]
    assert sample_keys == sorted(sample_keys)


def test_unresolved_references_empty_in_canonical_scope():
    # Extracted-reference outcomes are a Discovery Graph concept; the
    # canonical scope has no extracted edges, so no unresolved groups.
    a = _page("A", body="[x](missing.md)\n")
    kb = _kb([a])
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    assert report.unresolved_references == ()


def test_unresolved_references_filtered_by_candidate_visibility():
    # When candidate_titles excludes a source page, its diagnostics drop.
    a = _page("A", body="[x](missing.md)\n")
    b = _page("B", body="[y](missing.md)\n")
    kb = _kb([a, b])
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY, candidate_titles=["A"])
    groups = {g.target: g for g in report.unresolved_references}
    assert groups["missing.md"].count == 1


def test_external_and_escaping_links_are_not_unresolved():
    # External URLs and escaping paths are expected exclusions, never
    # unresolved-reference repair signals.
    a = _page("A", body="[ext](https://example.com) and [up](../x.md)\n")
    kb = _kb([a])
    report = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY)
    assert report.unresolved_references == ()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
