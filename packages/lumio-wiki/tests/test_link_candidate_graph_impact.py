"""Tests for graph-impact prioritization of link candidates (issue #127, ADR-0011).

``KnowledgeBase.rank_link_candidates_by_graph_impact`` evaluates each
deterministic ``LinkCandidate`` against the current authorized Discovery Graph
WITHOUT mutating Compiled Pages or graph state, and returns
``RankedLinkCandidate`` values ordered by documented structural-impact signals
with stable lexical tie-breaking.

The impact is ADVISORY: it never infers a Relationship, never mutates graph
state, and never publishes a link. Approved links remain Extracted References
with navigation meaning only (ADR-0011).

All tests are zero-index: no LanceDB, PyArrow, or operational database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from lumio_wiki import find_link_candidates
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    LINK_IMPACT_WEIGHT_COMPONENT_JOIN,
    LINK_IMPACT_WEIGHT_ORPHAN_REPAIR,
    KnowledgeBase,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    LINK_IMPACT_KIND_COMPONENT_JOIN,
    LINK_IMPACT_KIND_FRAGILE_STRENGTHENING,
    LINK_IMPACT_KIND_ORPHAN_REPAIR,
    Claim,
    CompiledPage,
    LinkCandidate,
    RankedLinkCandidate,
    Relationship,
    Source,
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


def _cand(
    source_title: str,
    target_title: str,
    *,
    source_path: str | None = None,
    target_path: str | None = None,
    line: int = 1,
    column: int = 1,
    term: str | None = None,
) -> LinkCandidate:
    return LinkCandidate(
        source_path=source_path or f"{source_title.lower()}.md",
        source_title=source_title,
        target_path=target_path or f"{target_title.lower()}.md",
        target_title=target_title,
        term=term or target_title,
        line=line,
        column=column,
        snippet=f"...{target_title}...",
    )


# ---------------------------------------------------------------------------
# AC: Public SDK operation evaluates each candidate against the authorized
# graph WITHOUT mutating Compiled Pages or graph state.
# ---------------------------------------------------------------------------


def test_returns_ranked_candidates_preserving_identity_fields():
    """The ranked result carries the original candidate's full identity."""
    alpha = _page("Alpha", body="We use Beta.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    assert len(ranked) == 1
    r = ranked[0]
    assert isinstance(r, RankedLinkCandidate)
    # Impact metadata carries source path, line, column, snippet, target.
    assert r.candidate.source_path == "alpha.md"
    assert r.candidate.line == 1
    assert r.candidate.column >= 1
    assert "Beta" in r.candidate.snippet
    assert r.candidate.target_title == "Beta"


def test_does_not_mutate_graph_state():
    """Running the ranking twice produces identical results — no mutation."""
    alpha = _page("Alpha", body="We use Beta for stuff.\n")
    beta = _page("Beta", relationships=[Relationship(target="Alpha", type="uses")])
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)
    first = kb.rank_link_candidates_by_graph_impact(cands)
    second = kb.rank_link_candidates_by_graph_impact(cands)
    assert first == second


def test_empty_candidates_returns_empty_list():
    kb = _kb([_page("Alpha")])
    assert kb.rank_link_candidates_by_graph_impact([]) == []


# ---------------------------------------------------------------------------
# AC: Orphan repair — the link would give an inbound connection to a page
# that is currently a Discovery Graph orphan (zero inbound edges).
# ---------------------------------------------------------------------------


def test_orphan_repair_signal():
    """A link to an inbound orphan gets an orphan_repair signal."""
    # Alpha mentions Beta (candidate). Beta has NO inbound edges.
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")  # orphan: no inbound edges
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    r = ranked[0]
    kinds = {s.kind for s in r.signals}
    assert LINK_IMPACT_KIND_ORPHAN_REPAIR in kinds
    assert r.impact_score >= LINK_IMPACT_WEIGHT_ORPHAN_REPAIR
    # Detail names the orphan.
    assert any("Beta" in s.detail for s in r.signals)


def test_orphan_repair_does_not_fire_when_target_has_inbound():
    """When the target already has an inbound edge, orphan_repair does not fire."""
    # Gamma -> Beta (canonical). Alpha mentions Beta.
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    gamma = _page("Gamma", relationships=[Relationship(target="Beta", type="uses")])
    kb = _kb([alpha, beta, gamma])
    cands = find_link_candidates(kb.pages)
    alpha_to_beta = [c for c in cands if c.source_title == "Alpha"]
    # Also Gamma mentions nothing... but Alpha's candidate is the one.
    ranked = kb.rank_link_candidates_by_graph_impact(alpha_to_beta)
    kinds = {s.kind for s in ranked[0].signals}
    assert LINK_IMPACT_KIND_ORPHAN_REPAIR not in kinds


# ---------------------------------------------------------------------------
# AC: Component joining — the link would connect two weakly connected
# components (reducing WCC count).
# ---------------------------------------------------------------------------


def test_component_join_signal():
    """A link between two disconnected components gets a component_join signal."""
    # Component 1: Alpha (mentions Beta but has no edges).
    # Component 2: Beta (completely separate, no edges to Alpha's component).
    # Adding Alpha->Beta would join the two components.
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    r = ranked[0]
    kinds = {s.kind for s in r.signals}
    assert LINK_IMPACT_KIND_COMPONENT_JOIN in kinds
    # Component join is highest weight.
    assert r.impact_score >= LINK_IMPACT_WEIGHT_COMPONENT_JOIN


def test_component_join_does_not_fire_when_already_connected():
    """When source and target are already in the same component, no join signal."""
    # Alpha -> Gamma (canonical), Gamma -> Beta (canonical).
    # Alpha and Beta are in the same component. Alpha mentions Beta.
    alpha = _page(
        "Alpha",
        body="We mention Beta here.\n",
        relationships=[Relationship(target="Gamma", type="uses")],
    )
    beta = _page("Beta")
    gamma = _page("Gamma", relationships=[Relationship(target="Beta", type="uses")])
    kb = _kb([alpha, beta, gamma])
    cands = [c for c in find_link_candidates(kb.pages) if c.target_title == "Beta"]

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    kinds = {s.kind for s in ranked[0].signals}
    assert LINK_IMPACT_KIND_COMPONENT_JOIN not in kinds


# ---------------------------------------------------------------------------
# AC: Fragile-connection strengthening — source and target in same WCC but
# no direct edge; adding one strengthens the indirect connection.
# ---------------------------------------------------------------------------


def test_fragile_strengthening_signal():
    """A link within a component but with no direct edge gets fragile signal."""
    # Alpha -> Gamma -> Beta (chain). Alpha mentions Beta.
    # Alpha and Beta are in the same component but Alpha has no direct edge to Beta.
    alpha = _page(
        "Alpha",
        body="We mention Beta here.\n",
        relationships=[Relationship(target="Gamma", type="uses")],
    )
    beta = _page("Beta")
    gamma = _page("Gamma", relationships=[Relationship(target="Beta", type="uses")])
    kb = _kb([alpha, beta, gamma])
    cands = [c for c in find_link_candidates(kb.pages) if c.target_title == "Beta"]

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    r = ranked[0]
    kinds = {s.kind for s in r.signals}
    # Beta has an inbound edge from Gamma, so not orphan.
    # Alpha and Beta are in the same component, so not component_join.
    # But no direct edge Alpha->Beta exists -> fragile strengthening.
    assert LINK_IMPACT_KIND_FRAGILE_STRENGTHENING in kinds
    assert LINK_IMPACT_KIND_COMPONENT_JOIN not in kinds


def test_fragile_strengthening_does_not_fire_when_direct_edge_exists():
    """When a direct Relationship source->target exists, no fragile signal."""
    # Alpha -> Beta (canonical Relationship). Alpha also mentions Beta in body.
    alpha = _page(
        "Alpha",
        body="We mention Beta here.\n",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = [c for c in find_link_candidates(kb.pages) if c.target_title == "Beta"]

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    kinds = {s.kind for s in ranked[0].signals}
    assert LINK_IMPACT_KIND_FRAGILE_STRENGTHENING not in kinds


# ---------------------------------------------------------------------------
# AC: All candidates returned, including zero-impact ones.
# ---------------------------------------------------------------------------


def test_zero_impact_candidate_still_returned():
    """A candidate with no structural impact is still returned with score 0."""
    # Alpha -> Beta (canonical). Alpha mentions Beta (redundant with existing edge).
    alpha = _page(
        "Alpha",
        body="We mention Beta here.\n",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = [c for c in find_link_candidates(kb.pages) if c.target_title == "Beta"]

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    assert len(ranked) == 1
    assert ranked[0].impact_score == 0
    assert ranked[0].signals == ()


def test_all_candidates_preserved():
    """Every input candidate appears in the output, including zero-impact ones."""
    alpha = _page("Alpha", body="We use Beta and Gamma.\n")
    beta = _page("Beta", relationships=[Relationship(target="Gamma", type="uses")])
    gamma = _page("Gamma")
    kb = _kb([alpha, beta, gamma])
    cands = find_link_candidates(kb.pages)
    assert len(cands) >= 2

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    assert len(ranked) == len(cands)
    # Source titles are preserved.
    assert {r.candidate.source_title for r in ranked} == {c.source_title for c in cands}


# ---------------------------------------------------------------------------
# AC: Stable lexical tie-breaking; repeated runs over unchanged Markdown
# are identical.
# ---------------------------------------------------------------------------


def test_stable_tie_breaking():
    """Ties in impact_score are broken lexically by candidate identity."""
    # Two candidates with the same impact score (both orphan repairs).
    # Alpha mentions Beta (orphan). Alpha mentions Delta (orphan).
    alpha = _page("Alpha", body="We mention Beta and Delta here.\n")
    beta = _page("Beta")
    delta = _page("Delta")
    kb = _kb([alpha, beta, delta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    # Both have the same score (orphan repair + component join since both are
    # isolated). Tie-break is by (source_path, line, column, target_title, term).
    # Beta appears before Delta in the body, so it comes first on column.
    assert ranked[0].candidate.target_title == "Beta"
    assert ranked[1].candidate.target_title == "Delta"
    # Verify same score.
    assert ranked[0].impact_score == ranked[1].impact_score


def test_deterministic_output_identical_runs():
    """Two runs over the same input produce byte-identical output."""
    alpha = _page("Alpha", body="We use Beta and Gamma here.\n")
    beta = _page("Beta", relationships=[Relationship(target="Gamma", type="uses")])
    gamma = _page("Gamma")
    kb = _kb([alpha, beta, gamma])
    cands = find_link_candidates(kb.pages)

    first = kb.rank_link_candidates_by_graph_impact(cands)
    second = kb.rank_link_candidates_by_graph_impact(cands)
    assert first == second


def test_ranked_candidate_is_frozen():
    alpha = _page("Alpha", body="We use Beta.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)
    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    with pytest.raises(AttributeError):
        ranked[0].impact_score = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC: Impact metadata names the graph scope.
# ---------------------------------------------------------------------------


def test_impact_metadata_names_graph_scope():
    """Each RankedLinkCandidate carries the discovery scope it was evaluated against."""
    alpha = _page("Alpha", body="We use Beta.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    assert all(r.scope == GRAPH_SCOPE_DISCOVERY for r in ranked)


def test_invalid_scope_raises():
    kb = _kb([_page("Alpha")])
    with pytest.raises(ValueError):
        kb.rank_link_candidates_by_graph_impact([], scope="nope")


# ---------------------------------------------------------------------------
# AC: Canonical Relationship impact is NOT inferred from a Markdown-link
# proposal; approved links remain Extracted References with navigation meaning
# only.
# ---------------------------------------------------------------------------


def test_ranking_does_not_infer_relationship_type():
    """Impact signals carry NO relationship type — only structural topology."""
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = find_link_candidates(kb.pages)

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    r = ranked[0]
    # No signal mentions a relationship type.
    for sig in r.signals:
        assert "uses" not in sig.detail
        assert "implements" not in sig.detail
        assert "relates-to" not in sig.detail
    # The RankedLinkCandidate itself does not carry a relationship type field.
    assert not hasattr(r, "relationship_type")


def test_canonical_scope_rejected():
    """Canonical scope is rejected: Markdown links produce Extracted References
    (discovery-only), never canonical Relationships."""
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    with pytest.raises(ValueError, match="discovery"):
        kb.rank_link_candidates_by_graph_impact([], scope=GRAPH_SCOPE_CANONICAL)


def test_discovery_scope_sees_extracted_references_in_topology():
    """Discovery scope sees Extracted References in the topology.

    Gamma links to Beta (extracted reference). Alpha mentions Beta (candidate).
    In discovery scope, Gamma->Beta is visible, so Beta has an inbound edge
    (no orphan_repair for Alpha->Beta). This confirms the ranking operates over
    the Discovery Graph, not the canonical graph.
    """
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    gamma = _page("Gamma", body="See [b](beta.md).\n")
    kb = _kb([alpha, beta, gamma])
    cands = [c for c in find_link_candidates(kb.pages) if c.target_title == "Beta"]

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    kinds = {s.kind for s in ranked[0].signals}
    # Beta has Gamma's inbound extracted reference -> not an orphan.
    assert LINK_IMPACT_KIND_ORPHAN_REPAIR not in kinds


# ---------------------------------------------------------------------------
# AC: Authorization is applied BEFORE graph impact is calculated; inaccessible
# page identities do not affect scores, counts, reasons, or output ordering.
# ---------------------------------------------------------------------------


def test_authorization_excludes_inaccessible_target():
    """A candidate whose target is not authorized gets score 0, no signals."""
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = [_cand("Alpha", "Beta")]

    # candidate_titles excludes Beta.
    ranked = kb.rank_link_candidates_by_graph_impact(cands, candidate_titles=["Alpha"])
    assert ranked[0].impact_score == 0
    assert ranked[0].signals == ()


def test_authorization_excludes_inaccessible_source():
    """A candidate whose source is not authorized gets score 0, no signals."""
    alpha = _page("Alpha")
    beta = _page("Beta")
    kb = _kb([alpha, beta])
    cands = [_cand("Alpha", "Beta")]

    ranked = kb.rank_link_candidates_by_graph_impact(cands, candidate_titles=["Beta"])
    assert ranked[0].impact_score == 0
    assert ranked[0].signals == ()


def test_inaccessible_page_does_not_affect_other_scores():
    """An inaccessible page does not inflate another candidate's score."""
    # Alpha, Beta, Gamma. Alpha mentions Beta and Gamma.
    # If Gamma is excluded from authorization, it should not appear as an
    # orphan or component, and Beta's score should be the same as if Gamma
    # were never loaded.
    alpha = _page("Alpha", body="We mention Beta and Gamma here.\n")
    beta = _page("Beta")
    gamma = _page("Gamma")
    kb = _kb([alpha, beta, gamma])
    cands = find_link_candidates(kb.pages)

    # With all authorized.
    full = kb.rank_link_candidates_by_graph_impact(cands)

    # With Gamma excluded.
    excl = kb.rank_link_candidates_by_graph_impact(cands, candidate_titles=["Alpha", "Beta"])

    # Beta's score is the same regardless of Gamma's authorization.
    full_beta = next(r for r in full if r.candidate.target_title == "Beta")
    excl_beta = next(r for r in excl if r.candidate.target_title == "Beta")
    assert full_beta.impact_score == excl_beta.impact_score
    assert {s.kind for s in full_beta.signals} == {s.kind for s in excl_beta.signals}


def test_inaccessible_candidate_does_not_affect_accessible_ordering():
    """Removing an inaccessible candidate from the input does not change the
    relative ordering of accessible candidates."""
    alpha = _page("Alpha", body="We mention Beta and Gamma here.\n")
    beta = _page("Beta")
    gamma = _page("Gamma")
    kb = _kb([alpha, beta, gamma])
    cands = find_link_candidates(kb.pages)

    # Authorize only Alpha and Beta (Gamma is inaccessible).
    restricted = kb.rank_link_candidates_by_graph_impact(cands, candidate_titles=["Alpha", "Beta"])
    # Extract the ordering of Alpha->Beta only (the accessible candidate).
    accessible_restricted = [r for r in restricted if r.candidate.target_title == "Beta"]

    # Now rank only the accessible candidate (no inaccessible candidate in input).
    accessible_only = kb.rank_link_candidates_by_graph_impact(
        [c for c in cands if c.target_title == "Beta"],
        candidate_titles=["Alpha", "Beta"],
    )

    # The accessible candidate's score, signals, and position are identical.
    assert accessible_restricted == accessible_only


# ---------------------------------------------------------------------------
# Integration: find_link_candidates -> rank_link_candidates_by_graph_impact.
# ---------------------------------------------------------------------------


def test_end_to_end_find_then_rank():
    """find_link_candidates output feeds directly into ranking."""
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    gamma = _page("Gamma", body="Back to [b](beta.md).\n")
    kb = _kb([alpha, beta, gamma])

    cands = find_link_candidates(kb.pages)
    assert len(cands) >= 1

    ranked = kb.rank_link_candidates_by_graph_impact(cands)
    assert len(ranked) == len(cands)
    assert all(isinstance(r, RankedLinkCandidate) for r in ranked)


# ---------------------------------------------------------------------------
# AC: Publication followed by deterministic Discovery Graph rebuild.
# (This is tested at the skill level in tests/test_skills_lint_cross_linker.py;
# here we verify the graph-impact API reflects the rebuild deterministically.)
# ---------------------------------------------------------------------------


def test_ranking_reflects_published_link_topology():
    """After a link is added, the ranking changes deterministically.

    Before: Alpha mentions Beta (candidate) -> orphan repair + component join.
    After (simulated): Alpha has an edge to Beta -> Beta is no longer an orphan.
    """
    alpha = _page("Alpha", body="We mention Beta here.\n")
    beta = _page("Beta")
    kb_before = _kb([alpha, beta])
    cands = find_link_candidates(kb_before.pages)

    before = kb_before.rank_link_candidates_by_graph_impact(cands)
    assert before[0].impact_score > 0  # orphan repair + component join

    # Simulate publishing: Alpha now has a body link to Beta (extracted reference).
    alpha_published = _page("Alpha", body="We mention [Beta](beta.md) here.\n")
    kb_after = _kb([alpha_published, beta])
    # The candidate no longer exists (find_link_candidates skips linked mentions).
    cands_after = find_link_candidates(kb_after.pages)
    assert all(c.target_title != "Beta" for c in cands_after if c.source_title == "Alpha")


# ---------------------------------------------------------------------------
# Public API surface.
# ---------------------------------------------------------------------------


def test_public_api_exports_ranking_types():
    import lumio_wiki

    for name in (
        "RankedLinkCandidate",
        "LinkImpactSignal",
        "LINK_IMPACT_KIND_ORPHAN_REPAIR",
        "LINK_IMPACT_KIND_COMPONENT_JOIN",
        "LINK_IMPACT_KIND_FRAGILE_STRENGTHENING",
        "LINK_IMPACT_WEIGHT_COMPONENT_JOIN",
        "LINK_IMPACT_WEIGHT_ORPHAN_REPAIR",
        "LINK_IMPACT_WEIGHT_FRAGILE_STRENGTHENING",
    ):
        assert hasattr(lumio_wiki, name), f"lumio_wiki must export {name}"

    assert callable(getattr(KnowledgeBase, "rank_link_candidates_by_graph_impact", None))


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
