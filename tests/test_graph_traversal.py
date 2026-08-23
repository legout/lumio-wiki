"""Tests for the expanded public Knowledge Base graph traversal seam (issue #106).

These tests exercise graph behavior ONLY through the public ``KnowledgeBase``
interface (``related_pages`` and ``shortest_path``), never private adjacency
structures. They cover ADR-0011 canonical-scope traversal: bounded neighbors,
incoming/outgoing/both direction, directed shortest paths, authorization over a
caller-supplied candidate page set, cycle-safety, deterministic ordering, and
deterministic truncation. They also assert the legacy ``graph_path`` and
``related_from`` behavior stays compatible.

The graph below is intentionally cyclic and carries a restricted-visibility
page so authorization and cycle-safety are exercised together::

    Alpha --uses--> Beta --uses--> Gamma --implements--> Alpha   (cycle)
                   Beta --uses--> Delta (restricted)

All tests are zero-index: no LanceDB is built, proving #106 works without it.
"""

from pathlib import Path

import pytest

from lumio_wiki import KnowledgeBase, load_knowledge_base
from lumio_wiki.records import CompiledPage, Relationship, Source

from tests._entity_fixtures import claim_for, entity_id_for


def _page(
    title: str,
    relationships: list[Relationship] | None = None,
    visibility: str = "public",
) -> CompiledPage:
    return CompiledPage(
        path=f"{title.lower()}.md",
        title=title,
        id=entity_id_for(title),
        aliases=[],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility=visibility,
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        # Canonical edges are accepted entity-to-entity Claims (ADR-0021);
        # call sites still express them as title-level Relationship records.
        claims=[claim_for(title, rel.target, rel.type) for rel in relationships or []],
        body=f"# {title}\n",
    )


# Canonical test graph: Alpha -> Beta -> Gamma -> Alpha (cycle), Beta -> Delta.
def _graph_pages() -> list[CompiledPage]:
    return [
        _page("Alpha", [Relationship(target="Beta", type="uses")]),
        _page(
            "Beta",
            [
                Relationship(target="Gamma", type="uses"),
                Relationship(target="Delta", type="uses"),
            ],
        ),
        _page("Gamma", [Relationship(target="Alpha", type="implements")]),
        _page("Delta", visibility="restricted"),
    ]


def _kb(pages: list[CompiledPage] | None = None, tmp: str | Path = ".") -> KnowledgeBase:
    return KnowledgeBase(root=Path(tmp), pages=pages if pages is not None else _graph_pages())


def _write_kb_to_disk(pages: list[CompiledPage], kb_dir: Path) -> None:
    """Write pages as minimal valid Compiled Page Markdown for load_knowledge_base."""
    kb_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        # Claims need a Control File ontology to be declarable on disk
        # (ADR-0021); this writer exercises plain-page load stability only.
        (kb_dir / page.path).write_text(
            "---\n"
            f'title: "{page.title}"\n'
            'tags: ["test"]\n'
            f'summary: "{page.summary}"\n'
            'lifecycle: "approved"\n'
            f'visibility: "{page.visibility}"\n'
            "sources:\n"
            f'  - id: "{page.sources[0].id}"\n'
            f'    title: "{page.sources[0].title}"\n'
            "---\n\n# {page.title}\n"
        )


# ---------------------------------------------------------------------------
# Outgoing neighbors (existing direction, now bounded + authorized).
# ---------------------------------------------------------------------------


def test_outgoing_neighbors_within_authorized_set():
    kb = _kb()
    assert kb.related_pages("Beta") == ["Delta", "Gamma"]


def test_outgoing_neighbors_multi_hop_respects_max_depth():
    kb = _kb()
    assert kb.related_pages("Alpha", max_depth=1) == ["Beta"]
    assert kb.related_pages("Alpha", max_depth=2) == ["Beta", "Delta", "Gamma"]


# ---------------------------------------------------------------------------
# Incoming neighbors (NEW capability added by #106).
# ---------------------------------------------------------------------------


def test_incoming_neighbors_is_new_capability():
    kb = _kb()
    assert kb.related_pages("Alpha", direction="incoming") == ["Gamma"]
    assert kb.related_pages("Beta", direction="incoming") == ["Alpha"]
    assert kb.related_pages("Delta", direction="incoming") == ["Beta"]


def test_both_direction_deduplicates_neighbors():
    kb = _kb()
    assert kb.related_pages("Alpha", direction="both", max_depth=1) == ["Beta", "Gamma"]


def test_relationship_type_filter_applies_in_all_directions():
    kb = _kb()
    assert kb.related_pages("Beta", direction="outgoing", relationship_type="uses") == [
        "Delta",
        "Gamma",
    ]
    assert kb.related_pages("Beta", direction="outgoing", relationship_type="implements") == []
    assert kb.related_pages("Beta", direction="incoming", relationship_type="uses") == ["Alpha"]


# ---------------------------------------------------------------------------
# Authorization: traversal operates ONLY over the supplied candidate page set.
# ---------------------------------------------------------------------------


def test_unauthorized_adjacent_page_is_never_returned():
    kb = _kb()
    authorized = {"Alpha", "Beta", "Gamma"}
    assert kb.related_pages("Beta", candidate_titles=authorized) == ["Gamma"]


def test_unauthorized_page_is_never_traversed_through():
    kb = _kb()
    # With only {Alpha, Gamma} authorized, the outgoing bridge Beta is withheld;
    # only the direct Gamma->Alpha incoming edge can connect them.
    authorized = {"Alpha", "Gamma"}
    assert kb.related_pages("Alpha", direction="both", candidate_titles=authorized) == ["Gamma"]


def test_shortest_path_never_returns_unauthorized_endpoint():
    kb = _kb()
    authorized = {"Alpha", "Beta", "Gamma"}
    assert kb.shortest_path("Alpha", "Delta", candidate_titles=authorized) is None


def test_source_outside_candidate_set_returns_empty_neighbors():
    kb = _kb()
    assert kb.related_pages("Beta", candidate_titles={"Alpha", "Gamma"}) == []


def test_candidate_titles_none_means_all_loaded_titles_authorized():
    kb = _kb()
    assert kb.related_pages("Beta") == ["Delta", "Gamma"]


# ---------------------------------------------------------------------------
# Directed shortest paths with cycle-safe bounded depth.
# ---------------------------------------------------------------------------


def test_directed_shortest_path_outgoing():
    kb = _kb()
    assert kb.shortest_path("Alpha", "Delta") == ["Alpha", "Beta", "Delta"]


def test_directed_shortest_path_unreachable_returns_none():
    kb = _kb()
    assert kb.shortest_path("Delta", "Alpha") is None


def test_directed_shortest_path_follows_incoming_edges_when_requested():
    kb = _kb()
    assert kb.shortest_path("Delta", "Alpha", direction="incoming") == [
        "Delta",
        "Beta",
        "Alpha",
    ]


def test_shortest_path_source_equals_target():
    kb = _kb()
    assert kb.shortest_path("Alpha", "Alpha") == ["Alpha"]


def test_shortest_path_respects_max_depth():
    kb = _kb()
    assert kb.shortest_path("Alpha", "Delta", max_depth=1) is None
    assert kb.shortest_path("Alpha", "Delta", max_depth=2) == ["Alpha", "Beta", "Delta"]


def test_shortest_path_both_direction_finds_undirected_route():
    kb = _kb()
    assert kb.shortest_path("Delta", "Alpha", direction="both") == [
        "Delta",
        "Beta",
        "Alpha",
    ]


# ---------------------------------------------------------------------------
# Cycle-safety.
# ---------------------------------------------------------------------------


def test_cycle_does_not_loop_forever_and_excludes_self():
    kb = _kb()
    result = kb.related_pages("Alpha", max_depth=10)
    assert "Alpha" not in result
    assert result == ["Beta", "Delta", "Gamma"]


def test_shortest_path_in_cyclic_graph_terminates():
    kb = _kb()
    assert kb.shortest_path("Alpha", "Gamma") == ["Alpha", "Beta", "Gamma"]


# ---------------------------------------------------------------------------
# Deterministic ordering and tie-breaking across reloads.
# ---------------------------------------------------------------------------


def test_results_independent_of_page_insertion_order():
    kb_forward = _kb(_graph_pages())
    kb_reverse = _kb(list(reversed(_graph_pages())))
    for title in ("Alpha", "Beta", "Gamma", "Delta"):
        assert kb_forward.related_pages(title, direction="both", max_depth=3) == (
            kb_reverse.related_pages(title, direction="both", max_depth=3)
        )
    assert kb_forward.shortest_path("Alpha", "Delta", direction="both") == (
        kb_reverse.shortest_path("Alpha", "Delta", direction="both")
    )


def test_results_stable_across_repeated_loads(tmp_path):
    kb_dir = tmp_path / "kb"
    _write_kb_to_disk(_graph_pages(), kb_dir)
    kb_a, report_a = load_knowledge_base(kb_dir)
    kb_b, report_b = load_knowledge_base(kb_dir)
    assert report_a.is_valid and report_b.is_valid
    assert kb_a.related_pages("Alpha", direction="both", max_depth=3) == (
        kb_b.related_pages("Alpha", direction="both", max_depth=3)
    )
    assert kb_a.shortest_path("Alpha", "Delta") == kb_b.shortest_path("Alpha", "Delta")


# ---------------------------------------------------------------------------
# Deterministic limit truncation.
# ---------------------------------------------------------------------------


def test_result_limit_truncates_deterministically():
    pages = [
        _page(
            "Hub",
            [
                Relationship(target="Bravo", type="uses"),
                Relationship(target="Charlie", type="uses"),
                Relationship(target="Apple", type="uses"),
            ],
        ),
        _page("Bravo"),
        _page("Charlie"),
        _page("Apple"),
    ]
    kb = _kb(pages)
    assert kb.related_pages("Hub") == ["Apple", "Bravo", "Charlie"]
    assert kb.related_pages("Hub", max_results=2) == ["Apple", "Bravo"]
    assert kb.related_pages("Hub", max_results=1) == ["Apple"]
    assert kb.related_pages("Hub", max_results=0) == []


def test_edge_limit_stops_expansion_deterministically():
    kb = _kb()
    # max_edges=1 expands only the first (lexicographically-first) neighbor.
    assert kb.related_pages("Beta", max_edges=1) == ["Delta"]


# ---------------------------------------------------------------------------
# Discovery scope (issue #107): now IMPLEMENTED, no longer reserved.
#
# The canonical test graph has no body links, so discovery scope traverses
# exactly the same reviewed Relationships as canonical scope. The point of
# these assertions is that ``scope="discovery"`` is now a valid, working value
# that no longer raises ``NotImplementedError`` — flipping the #106 reservation.
# Comprehensive Extracted-Reference behavior is covered in
# ``packages/lumio-wiki/tests/test_discovery_graph.py``.
# ---------------------------------------------------------------------------


def test_discovery_scope_is_now_implemented_and_works():
    kb = _kb()
    # ``discovery`` no longer raises; over a Relationship-only graph it
    # traverses the same edges as canonical scope.
    assert kb.related_pages("Beta", scope="discovery") == ["Delta", "Gamma"]
    assert kb.shortest_path("Alpha", "Delta", scope="discovery") == [
        "Alpha",
        "Beta",
        "Delta",
    ]
    # ``shortest_path`` source == target still returns the singleton path.
    assert kb.shortest_path("Alpha", "Alpha", scope="discovery") == ["Alpha"]


def test_invalid_direction_raises():
    kb = _kb()
    with pytest.raises(ValueError):
        kb.related_pages("Beta", direction="sideways")
    with pytest.raises(ValueError):
        kb.shortest_path("Alpha", "Beta", direction="sideways")


def test_unknown_scope_name_raises():
    kb = _kb()
    with pytest.raises(ValueError):
        kb.related_pages("Beta", scope="imaginary")


# ---------------------------------------------------------------------------
# Legacy compatibility: graph_path and related_from are unchanged.
# ---------------------------------------------------------------------------


def test_legacy_graph_path_still_works():
    kb = _kb()
    assert kb.graph_path("Alpha", "Alpha") == ["Alpha"]
    assert kb.graph_path("Alpha", "Delta") == ["Alpha", "Beta", "Delta"]
    assert kb.graph_path("Delta", "Alpha") is None


def test_legacy_related_from_still_returns_relationships():
    kb = _kb()
    rels = kb.related_from("Beta")
    assert sorted(r.target for r in rels) == ["Delta", "Gamma"]
    assert sorted(r.target for r in kb.related_from("Beta", "uses")) == [
        "Delta",
        "Gamma",
    ]


# ---------------------------------------------------------------------------
# Edge-budget semantics (ADR-0011): max_edges bounds Relationship EDGES
# inspected, not post-processed unique endpoints. Parallel edges to the same
# endpoint each consume the budget; a high-degree hub is capped at max_edges
# eligible endpoints; max_edges=0 inspects nothing.
# ---------------------------------------------------------------------------


def _parallel_edge_pages() -> list[CompiledPage]:
    """A -> B with 50 PARALLEL edges (distinct types), plus A -> C.

    Sorted adjacency for A is ``[(B, t00), (B, t01), ..., (B, t49), (C, uses)]``
    so the parallel B-edges are inspected before the C-edge. A strict edge
    budget consumed by parallel B-edges must prevent C from being reached.
    """
    return [
        _page(
            "A",
            [Relationship(target="B", type=f"t{i:02d}") for i in range(50)]
            + [Relationship(target="C", type="uses")],
        ),
        _page("B"),
        _page("C"),
    ]


def test_parallel_edges_each_consume_edge_budget():
    kb = _kb(_parallel_edge_pages())
    # 1 edge budget: only the first parallel B-edge is inspected -> B found.
    assert kb.related_pages("A", max_edges=1) == ["B"]
    # 2 edge budget: consumed by two parallel B-edges, C is NOT reached.
    # (Old endpoint-counting code returned ["B", "C"] here because the 50
    # parallel B-edges collapsed to a single endpoint before counting.)
    assert kb.related_pages("A", max_edges=2) == ["B"]
    # Unbounded traversal reaches both distinct endpoints.
    assert kb.related_pages("A") == ["B", "C"]


def test_high_degree_hub_is_capped_at_max_edges():
    # Hub -> 10 distinct leaves; max_edges=3 returns the 3 lexicographically
    # smallest eligible endpoints, proving the bound caps expansion.
    pages = [
        _page(
            "Hub",
            [Relationship(target=f"Leaf{i}", type="uses") for i in range(10)],
        ),
        *[_page(f"Leaf{i}") for i in range(10)],
    ]
    kb = _kb(pages)
    assert kb.related_pages("Hub", max_edges=3) == ["Leaf0", "Leaf1", "Leaf2"]
    assert kb.related_pages("Hub") == [f"Leaf{i}" for i in range(10)]


def test_max_edges_zero_inspects_nothing():
    kb = _kb()
    assert kb.related_pages("Beta", max_edges=0) == []
    assert kb.shortest_path("Alpha", "Delta", max_edges=0) is None


def test_edge_budget_respects_relationship_type_filter_without_counting():
    # A -> B (50 parallel edges of types t00..t49) plus A -> B (one "uses").
    # Filtering to "uses": only the single matching edge is eligible, so it is
    # the only edge that consumes budget; the 49 non-matching edges are skipped
    # without consuming the budget.
    pages = [
        _page(
            "A",
            [Relationship(target="B", type=f"t{i:02d}") for i in range(50)]
            + [Relationship(target="B", type="uses")],
        ),
        _page("B"),
    ]
    kb = _kb(pages)
    assert kb.related_pages("A", relationship_type="uses", max_edges=1) == ["B"]


# ---------------------------------------------------------------------------
# Negative limit rejection (ADR-0011): max_depth / max_edges / max_results
# must be non-negative; negative values raise ValueError instead of silently
# meaning "unlimited".
# ---------------------------------------------------------------------------


def test_related_pages_rejects_negative_limits():
    kb = _kb()
    with pytest.raises(ValueError):
        kb.related_pages("Beta", max_results=-1)
    with pytest.raises(ValueError):
        kb.related_pages("Beta", max_edges=-1)
    with pytest.raises(ValueError):
        kb.related_pages("Beta", max_depth=-1)


def test_shortest_path_rejects_negative_limits():
    kb = _kb()
    with pytest.raises(ValueError):
        kb.shortest_path("Alpha", "Delta", max_depth=-1)
    with pytest.raises(ValueError):
        kb.shortest_path("Alpha", "Delta", max_edges=-1)


def test_max_results_zero_is_valid_and_returns_empty():
    kb = _kb()
    assert kb.related_pages("Beta", max_results=0) == []
