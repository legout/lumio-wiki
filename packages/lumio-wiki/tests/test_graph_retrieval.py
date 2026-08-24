"""Graph expansion into retrieval over the in-memory Discovery Graph (#112).

These tests prove the Knowledge Base can select eligible page identities via
Discovery Graph expansion (canonical Relationships + Extracted References) and
hand ONLY those pages to the retrieval adapter for Evidence ranking. The graph
selects page identities; the adapter ranks citation-ready Evidence from them.

All tests are zero-index: no LanceDB is built, proving the in-memory Discovery
Graph and zero-index agent workflow remain usable without LanceDB (#112
criterion: removing ``lumio-lancedb`` requires no migration).

Acceptance criteria covered:

* Graph expansion restricts ranked Evidence to eligible pages.
* An Extracted Reference is never returned as Evidence.
* Retrieval Traces carry a truthful ``graph-expansion`` stage when expansion
  runs and never fabricate one when it does not.
* ``retrieve`` with no graph params behaves exactly as before.
* An empty eligible set returns ``[]`` without falling back to all pages.
* Expansion respects ``max_depth`` and authorization (loaded pages only).
"""

from __future__ import annotations

import re
from pathlib import Path

from lumio_wiki.knowledge_base import (
    GRAPH_DIRECTION_BOTH,
    GRAPH_DIRECTION_OUTGOING,
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    Claim,
    CompiledPage,
    Evidence,
    Relationship,
    RetrievalResult,
    RetrievalTrace,
    Source,
)

COMMON = "Lumio common term"


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
    body: str,
    *,
    path: str | None = None,
    relationships: list[Relationship] | None = None,
    extra_claims: list[Claim] | None = None,
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower()}.md",
        title=title,
        id=f"entity:{_slug(title)}",
        entity_types=["concept"],
        aliases=[],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        claims=[*_claims_from(title, relationships), *(extra_claims or [])],
        body=body,
        body_start_line=1,
    )


def _kb(pages: list[CompiledPage]) -> KnowledgeBase:
    return KnowledgeBase(root=Path("."), pages=pages)


def _graph_kb() -> KnowledgeBase:
    """A four-page Knowledge Base with discovery adjacency A -> B -> C and D isolated.

    Every page's body contains ``COMMON`` so an unexpanded query matches all
    four; graph expansion from seed ``A`` is the only thing that narrows it.
    """
    a = _page(
        "A",
        f"{COMMON}. Alpha details.\nSee [b](beta.md).\n",
        relationships=[Relationship(target="B", type="relates-to")],
        path="alpha.md",
    )
    b = _page("B", f"{COMMON}. Beta details.\nSee [g](gamma.md).\n", path="beta.md")
    c = _page("C", f"{COMMON}. Gamma details.\n", path="gamma.md")
    d = _page("D", f"{COMMON}. Delta details.\n", path="delta.md")
    return _kb([a, b, c, d])


def _titles(results: list[RetrievalResult]) -> set[str]:
    return {r.evidence.page_title for r in results}


# ---------------------------------------------------------------------------
# 1. Graph expansion restricts ranked Evidence to eligible pages.
# ---------------------------------------------------------------------------


def test_graph_expansion_restricts_results_to_eligible_pages():
    kb = _graph_kb()
    # Without graph expansion, the common term matches all four pages.
    unexpanded = kb.retrieve(COMMON, limit=10)
    assert _titles(unexpanded) == {"A", "B", "C", "D"}

    # With graph expansion from seed A at depth 1, only A and its discovery
    # neighbors (B) are eligible. C and D must never appear.
    expanded = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_direction=GRAPH_DIRECTION_OUTGOING,
        graph_max_depth=1,
    )
    assert _titles(expanded) <= {"A", "B"}
    assert _titles(expanded) == {"A", "B"}


def test_graph_expansion_respects_max_depth():
    kb = _graph_kb()
    # A -> B (depth 1), B -> C (depth 2). D is never reachable from A.
    depth1 = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    assert _titles(depth1) == {"A", "B"}

    depth2 = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert _titles(depth2) == {"A", "B", "C"}
    assert "D" not in _titles(depth2)


def test_graph_expansion_includes_seeds_in_eligible_set():
    kb = _graph_kb()
    # Seed C has no outgoing discovery neighbors; it must still be eligible
    # for its own Evidence to be ranked.
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["C"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert _titles(results) == {"C"}


def test_graph_expansion_preserves_configured_limit():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=2,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    # Three eligible pages (A, B, C) but limit caps the returned Evidence.
    assert len(results) <= 2
    assert _titles(results) <= {"A", "B", "C"}


def test_graph_expansion_authorization_excludes_unloaded_pages():
    # Only A and B are loaded; C and D exist on disk (via links) but are not
    # loaded Compiled Pages. Expansion must never surface an unloaded page.
    a = _page(
        "A",
        f"{COMMON}. See [b](beta.md) and [g](gamma.md).\n",
        relationships=[Relationship(target="B", type="relates-to")],
        path="alpha.md",
    )
    b = _page("B", f"{COMMON}.\n", path="beta.md")
    kb = _kb([a, b])
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=3,
    )
    assert _titles(results) <= {"A", "B"}
    # The broken/unloaded link target produces a diagnostic, not Evidence.
    assert "C" not in _titles(results)


# ---------------------------------------------------------------------------
# 2. An Extracted Reference is never Evidence.
# ---------------------------------------------------------------------------


def test_extracted_reference_never_becomes_evidence():
    kb = _graph_kb()
    # C is reachable from A only through B's body link (an Extracted Reference).
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert results, "expected eligible Evidence"
    for r in results:
        assert isinstance(r.evidence, Evidence)
        # Evidence is always derived from a Compiled Page, never from an edge.
        assert r.evidence.source_type == "compiled_markdown"
        assert r.evidence.page_title in {"A", "B", "C"}


def test_extracted_reference_alone_cannot_cover_unsupported_query():
    # Every page is graph-reachable from A, but none mention "zebra".
    kb = _graph_kb()
    results = kb.retrieve(
        "zebra",
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert results == []


# ---------------------------------------------------------------------------
# 3. Truthful Retrieval Traces.
# ---------------------------------------------------------------------------


def test_trace_carries_graph_expansion_stage_when_expanded():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_direction=GRAPH_DIRECTION_BOTH,
        graph_max_depth=1,
    )
    assert results
    for r in results:
        assert isinstance(r.trace, RetrievalTrace)
        names = [s.name for s in r.trace.stages]
        assert "graph-expansion" in names, names
        stage = next(s for s in r.trace.stages if s.name == "graph-expansion")
        assert "discovery" in stage.detail
        assert "eligible" in stage.detail


def test_trace_does_not_fabricate_graph_stage_when_unused():
    kb = _graph_kb()
    results = kb.retrieve(COMMON, limit=5)
    assert results
    for r in results:
        names = {s.name for s in r.trace.stages}
        assert "graph-expansion" not in names
        assert "search" in names


def test_trace_graph_stage_reports_seed_and_page_counts():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A", "C"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    assert results
    stage = next(s for r in results for s in r.trace.stages if s.name == "graph-expansion")
    # Two seeds; A->B plus C yields eligible pages {A, B, C} = 3.
    assert "2" in stage.detail  # seed count
    assert "3" in stage.detail  # eligible page count


# ---------------------------------------------------------------------------
# 4. Backward compatibility: no graph params == current behavior.
# ---------------------------------------------------------------------------


def test_no_graph_params_behaves_exactly_as_before():
    kb = _graph_kb()
    plain = kb.retrieve(COMMON, limit=10)
    # Passing graph_seed_titles=None must be identical to not using the feature.
    nominally_off = kb.retrieve(COMMON, limit=10, graph_seed_titles=None)
    assert [r.evidence.id for r in plain] == [r.evidence.id for r in nominally_off]
    # No graph stage is fabricated.
    for r in plain:
        assert all(s.name != "graph-expansion" for s in r.trace.stages)


# ---------------------------------------------------------------------------
# 5. Empty eligible set returns [] without falling back to all pages.
# ---------------------------------------------------------------------------


def test_empty_eligible_set_returns_empty_no_fallback():
    kb = _graph_kb()
    # An unknown seed expands to nothing; retrieve must not fall back to all
    # pages even though every page matches the common query.
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["Nonexistent"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert results == []


def test_explicit_empty_seed_list_returns_empty():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=[],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert results == []


# ---------------------------------------------------------------------------
# 6. Zero-index + graph works without LanceDB (removal requires no migration).
# ---------------------------------------------------------------------------


def test_zero_index_graph_retrieval_works_without_lancedb():
    # Constructed directly with the default zero-index adapter (no build_index,
    # no LanceDB). Graph expansion + zero-index Evidence ranking must succeed.
    kb = _graph_kb()
    assert kb._retrieval_adapter().name == "zero-index"
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    assert _titles(results) == {"A", "B"}


def test_graph_expansion_deterministic_across_calls():
    kb = _graph_kb()
    first = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    second = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    assert [r.evidence.id for r in first] == [r.evidence.id for r in second]


# ---------------------------------------------------------------------------
# 7. Claim-aware trace metadata (issue #172, ADR-0021).
# ---------------------------------------------------------------------------


def _expansion_stage(results: list[RetrievalResult]):
    return next(
        s for r in results for s in r.trace.stages if s.name == "graph-expansion"
    )


def test_trace_carries_resolved_seed_entity_ids():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    seeds_stage = next(
        s for r in results for s in r.trace.stages if s.name == "graph-seeds"
    )
    # The seed resolved to its stable Entity ID, not just a title.
    assert "entity:a" in seeds_stage.detail


def test_trace_distinguishes_claim_and_extracted_origins():
    kb = _graph_kb()
    discovery = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=2,
    )
    stage = _expansion_stage(discovery)
    assert "claim" in stage.detail
    assert "extracted-reference" in stage.detail

    canonical = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_CANONICAL,
        graph_max_depth=1,
    )
    # Canonical scope follows only the accepted claim edge A -> B; the
    # extracted-reference-only neighbor C stays ineligible.
    assert _titles(canonical) == {"A", "B"}


def test_trace_discloses_traversed_claim_ids_and_predicates():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    stage = _expansion_stage(results)
    # The accepted claim edge A -relates-to-> B was traversed.
    assert "claim:a-b-0" in stage.detail
    assert "relates-to" in stage.detail


def test_trace_discloses_lifecycle_filter_and_excludes_disputed_claims():
    disputed = Claim(
        id="claim:a-d-disputed",
        predicate="relates-to",
        object="entity:d",
        status="disputed",
    )
    a = _page(
        "A",
        f"{COMMON}. Alpha details.\n",
        relationships=[Relationship(target="B", type="relates-to")],
        path="alpha.md",
        extra_claims=[disputed],
    )
    b = _page("B", f"{COMMON}. Beta details.\n", path="beta.md")
    d = _page("D", f"{COMMON}. Delta details.\n", path="delta.md")
    kb = _kb([a, b, d])
    results = kb.retrieve(
        COMMON,
        limit=10,
        graph_seed_titles=["A"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    stage = _expansion_stage(results)
    assert "lifecycle=accepted" in stage.detail
    # The disputed Claim never traversed and never enters the trace.
    assert "claim:a-d-disputed" not in stage.detail
    assert "D" not in _titles(results)


def test_trace_discloses_artifact_source_and_unresolved_seeds():
    kb = _graph_kb()
    results = kb.retrieve(
        COMMON,
        limit=5,
        graph_seed_titles=["A", "Nonexistent"],
        graph_scope=GRAPH_SCOPE_DISCOVERY,
        graph_max_depth=1,
    )
    stage = _expansion_stage(results)
    assert "in-memory" in stage.detail
    seeds_stage = next(
        s for r in results for s in r.trace.stages if s.name == "graph-seeds"
    )
    assert "Nonexistent" in seeds_stage.detail
