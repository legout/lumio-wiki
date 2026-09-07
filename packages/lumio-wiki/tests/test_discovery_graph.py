"""Tests for the Discovery Graph (issue #107, ADR-0011).

The Discovery Graph turns exactly-resolved internal links in Compiled Page
bodies into provenance-bearing Extracted References and exposes them beside
canonical Relationships through the public traversal seam (``related_pages``,
``shortest_path`` with ``scope="discovery"``).

These tests assert every acceptance criterion in the issue:

* Internal relative Markdown links and unambiguous wikilinks resolve
  deterministically to Compiled Pages and produce Extracted References with
  source page, target page, origin, source location, and extractor version.
* Canonical graph scope contains only reviewed Relationships; Discovery Graph
  scope contains canonical Relationships plus Extracted References.
* Incoming and outgoing traversal and bounded graph paths work over the
  Discovery Graph through the public seam.
* External URLs, escaping paths, broken targets, ambiguous targets,
  duplicates, unauthorized targets, and valid Reserved Artifacts do not become
  traversable references.
* Equivalent traversal edges are deduplicated deterministically while
  retaining enough source occurrence provenance for inspection.
* Extraction never edits Compiled Page bodies or frontmatter and never promotes
  an Extracted Reference into a typed Relationship.
* An Extracted Reference is never returned as Evidence and cannot alone make an
  unsupported query covered.

All tests are zero-index: no LanceDB is built.
"""

from __future__ import annotations

import re
from pathlib import Path

import msgspec
import pytest
from lumio_wiki.knowledge_base import (
    EXTRACTOR_VERSION,
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    GRAPH_EDGE_SCOPE_DISCOVERY,
    Claim,
    CompiledPage,
    Evidence,
    ExtractedReference,
    GraphEdge,
    Relationship,
    RetrievalResult,
    Source,
)

# ---------------------------------------------------------------------------
# Page builders.
# ---------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _claims_from(source_title: str, relationships: list[Relationship] | None) -> list[Claim]:
    """Convert title-level test edges to accepted entity-to-entity Claims.

    Since ADR-0021 a canonical edge is an accepted Claim; in-memory pages are
    not validated, so no evidence anchors are required.
    """
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
    *,
    body: str = "",
    aliases: list[str] | None = None,
    relationships: list[Relationship] | None = None,
    path: str | None = None,
    visibility: str = "public",
    body_start_line: int = 1,
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower().replace(' ', '-')}.md",
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
        body_start_line=body_start_line,
    )


def _kb(pages: list[CompiledPage]) -> KnowledgeBase:
    return KnowledgeBase(root=Path("."), pages=pages)


# ---------------------------------------------------------------------------
# 1. Relative Markdown links produce provenance-bearing Extracted References.
# ---------------------------------------------------------------------------


def test_relative_markdown_link_produces_extracted_reference():
    source = _page(
        "Alpha",
        body="Intro.\n\nSee [Beta page](beta-page.md) for more.\n",
    )
    target = _page("Beta Page")
    kb = _kb([source, target])

    refs = kb.extracted_references("Alpha")
    assert len(refs) == 1
    ref = refs[0]
    assert isinstance(ref, ExtractedReference)
    assert ref.source_title == "Alpha"
    assert ref.target_title == "Beta Page"
    assert ref.origin == "markdown-link"
    assert ref.source_path == source.path
    assert ref.extractor_version == EXTRACTOR_VERSION
    # 1-based line range covering the link line.
    assert ref.line_start == ref.line_end == 3
    assert ref.line_start >= 1


def test_nested_relative_link_resolves_via_source_directory():
    # A relative link ``[b](beta.md)`` in ``guide/alpha.md`` must resolve to
    # ``guide/beta.md`` (source-directory-relative), not a root-level page
    # whose path stem happens to be ``beta``.
    source = _page("Alpha", body="[b](beta.md)\n", path="guide/alpha.md")
    nested_target = _page("Beta Guide", path="guide/beta.md")
    root_decoy = _page("Beta Root", path="beta.md")
    kb = _kb([source, nested_target, root_decoy])

    refs = kb.extracted_references("Alpha")
    assert [r.target_title for r in refs] == ["Beta Guide"]


def test_root_relative_link_falls_back_to_global_stem():
    # A link from the root (source_dir is empty) with no directory prefix
    # resolves by the bare stem, matching a page at any depth by exact stem.
    source = _page("Alpha", body="[b](guide/beta.md)\n", path="alpha.md")
    target = _page("Beta", path="guide/beta.md")
    kb = _kb([source, target])

    refs = kb.extracted_references("Alpha")
    assert [r.target_title for r in refs] == ["Beta"]


def test_body_start_line_offsets_source_location():
    # body_start_line simulates the line where the body begins in the file.
    source = _page(
        "Alpha",
        body="First body line.\nLink [b](beta.md) here.\n",
        body_start_line=7,
    )
    target = _page("Beta", path="beta.md")
    kb = _kb([source, target])

    refs = kb.extracted_references("Alpha")
    assert len(refs) == 1
    # Link is on body line 2 (1-based); file line = 7 + (2 - 1) = 8.
    assert refs[0].line_start == 8
    assert refs[0].line_end == 8


# ---------------------------------------------------------------------------
# 2. Wikilinks resolve through canonical identity and aliases.
# ---------------------------------------------------------------------------


def test_wikilink_resolves_to_canonical_title():
    source = _page("Alpha", body="See [[Beta]] now.\n")
    target = _page("Beta")
    kb = _kb([source, target])

    refs = kb.extracted_references("Alpha")
    assert [r.target_title for r in refs] == ["Beta"]


def test_wikilink_resolves_via_alias_only():
    target = _page("Beta Canonical", aliases=["BB"])
    source = _page("Alpha", body="[[BB]]\n")
    kb = _kb([source, target])

    refs = kb.extracted_references("Alpha")
    assert [r.target_title for r in refs] == ["Beta Canonical"]


# ---------------------------------------------------------------------------
# 3. Canonical scope vs Discovery scope.
# ---------------------------------------------------------------------------


def test_canonical_scope_contains_only_reviewed_relationships():
    # Alpha -> Beta via a typed Relationship AND a body link.
    source = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
        body="[b](beta.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    # Gamma is reachable ONLY via a body link from Beta.
    beta_body = _page(
        "Beta Linker",
        body="[g](gamma.md)\n",
        path="beta-linker.md",
    )
    gamma2 = _page("Gamma", path="gamma.md")
    kb = _kb([source, beta, gamma, beta_body, gamma2])

    # Canonical scope: Alpha -> Beta only (the typed Relationship).
    canonical = kb.related_pages("Alpha", scope=GRAPH_SCOPE_CANONICAL)
    assert canonical == ["Beta"]


def test_discovery_scope_contains_relationships_plus_extracted_references():
    source = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
        body="[b](beta.md) and [g](gamma.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    kb = _kb([source, beta, gamma])

    canonical = kb.related_pages("Alpha", scope=GRAPH_SCOPE_CANONICAL)
    discovery = kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY)

    assert canonical == ["Beta"]
    assert discovery == ["Beta", "Gamma"]


# ---------------------------------------------------------------------------
# 4. Incoming + outgoing + bounded paths over the Discovery Graph.
# ---------------------------------------------------------------------------


def test_discovery_outgoing_neighbors_includes_extracted_edges():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([alpha, beta])

    assert kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY) == ["Beta"]


def test_discovery_incoming_neighbors_works():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([alpha, beta])

    assert kb.related_pages("Beta", direction="incoming", scope=GRAPH_SCOPE_DISCOVERY) == ["Alpha"]


def test_discovery_multi_hop_path_through_extracted_edges():
    # Alpha -link-> Beta -link-> Gamma (links only, no typed Relationships).
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", body="[g](gamma.md)\n", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    kb = _kb([alpha, beta, gamma])

    # No canonical path exists.
    assert kb.shortest_path("Alpha", "Gamma", scope=GRAPH_SCOPE_CANONICAL) is None
    # Discovery path exists via extracted edges.
    assert kb.shortest_path("Alpha", "Gamma", scope=GRAPH_SCOPE_DISCOVERY) == [
        "Alpha",
        "Beta",
        "Gamma",
    ]


def test_discovery_path_respects_max_depth():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", body="[g](gamma.md)\n", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    kb = _kb([alpha, beta, gamma])

    assert kb.shortest_path("Alpha", "Gamma", scope=GRAPH_SCOPE_DISCOVERY, max_depth=1) is None
    assert kb.shortest_path("Alpha", "Gamma", scope=GRAPH_SCOPE_DISCOVERY, max_depth=2) == [
        "Alpha",
        "Beta",
        "Gamma",
    ]


def test_discovery_respects_authorization_candidate_set():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([alpha, beta])

    # Gamma is not even loaded; candidate set excludes Beta too.
    assert (
        kb.related_pages(
            "Alpha",
            scope=GRAPH_SCOPE_DISCOVERY,
            candidate_titles={"Alpha"},
        )
        == []
    )


def test_discovery_authorization_blocks_endpoint():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    beta_link = _page(
        "Beta Linker",
        body="[g](gamma.md)\n",
        path="beta-linker.md",
    )
    kb = _kb([alpha, beta, gamma, beta_link])

    # Authorized set excludes Gamma; the Beta->Gamma extracted edge cannot
    # surface Gamma as an endpoint.
    assert (
        kb.related_pages(
            "Beta Linker",
            scope=GRAPH_SCOPE_DISCOVERY,
            candidate_titles={"Alpha", "Beta", "Beta Linker"},
        )
        == []
    )


# ---------------------------------------------------------------------------
# 5. Excluded targets: external, escaping, broken, ambiguous, duplicate,
#    reserved artifacts.
# ---------------------------------------------------------------------------


def test_external_urls_are_not_extracted():
    source = _page(
        "Alpha",
        body=(
            "[ext](https://example.com)\n"
            "[ext2](http://example.com)\n"
            "[mail](mailto:a@b.com)\n"
            "[ftp](ftp://h/x)\n"
            "[tel](tel:+123)\n"
            "[scheme](custom://x)\n"
            "[proto](//host/path)\n"
            "[anchor](#section)\n"
        ),
    )
    beta = _page("Beta")
    kb = _kb([source, beta])

    assert kb.extracted_references("Alpha") == []


def test_escaping_paths_are_not_extracted():
    source = _page(
        "Alpha",
        body="[up](../beta.md) and [up2](../../beta.md)\n",
    )
    beta = _page("Beta")
    kb = _kb([source, beta])

    assert kb.extracted_references("Alpha") == []


def test_broken_target_is_dropped_with_diagnostic():
    # Target does not exist as a Compiled Page.
    source = _page("Alpha", body="[b](no-such-page.md)\n")
    kb = _kb([source])

    assert kb.extracted_references("Alpha") == []
    diags = kb.extraction_diagnostics()
    broken = [d for d in diags if "no-such-page.md" in d.detail or "broken" in d.detail.lower()]
    assert broken, f"expected a broken-target diagnostic, got {diags}"


def test_ambiguous_target_is_dropped_with_diagnostic():
    # Two pages share the same path-derived identity (duplicate title).
    a = _page("Alpha", body="[b](beta.md)\n")
    b1 = _page("Beta", path="beta.md")
    b2 = _page("Beta", path="beta-other.md")  # duplicate canonical title
    kb = _kb([a, b1, b2])

    assert kb.extracted_references("Alpha") == []
    diags = kb.extraction_diagnostics()
    assert any("ambiguous" in d.detail.lower() for d in diags), (
        f"expected an ambiguous-target diagnostic, got {diags}"
    )


def test_ambiguous_alias_target_is_dropped_with_diagnostic():
    # Two pages declare the same alias; a wikilink to that alias is ambiguous.
    a = _page("Alpha", body="[[Shared]]\n")
    b1 = _page("Beta One", aliases=["Shared"])
    b2 = _page("Beta Two", aliases=["Shared"])
    kb = _kb([a, b1, b2])

    assert kb.extracted_references("Alpha") == []
    diags = kb.extraction_diagnostics()
    assert any("ambiguous" in d.detail.lower() for d in diags)


def test_reserved_artifact_targets_are_not_extracted():
    # index.md / hot.md / log.md basenames are reserved; even if a file with
    # such a basename existed it would be a derived artifact, not a page.
    source = _page(
        "Alpha",
        body="[i](index.md) [h](hot.md) [l](log.md)\n",
    )
    kb = _kb([source])

    assert kb.extracted_references("Alpha") == []


def test_duplicate_equivalent_edges_are_deduplicated():
    # Same source->target link appears twice in the body.
    source = _page(
        "Alpha",
        body="[b](beta.md) and again [b2](beta.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    refs = kb.extracted_references("Alpha")
    assert len(refs) == 1
    assert refs[0].target_title == "Beta"


def test_duplicate_edge_retains_first_occurrence_provenance():
    # Two occurrences on different lines; provenance keeps the FIRST line.
    source = _page(
        "Alpha",
        body="line1 [b](beta.md)\nline2\nline3 [b2](beta.md)\n",
        body_start_line=1,
    )
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    refs = kb.extracted_references("Alpha")
    assert len(refs) == 1
    # First occurrence is on body line 1 (file line 1).
    assert refs[0].line_start == 1


def test_extracted_reference_equivalent_to_relationship_dedups_in_traversal():
    # Alpha -> Beta via BOTH a typed Relationship and a body link.
    source = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
        body="[b](beta.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    # Discovery traversal returns Beta once, not twice.
    assert kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY) == ["Beta"]


def test_claim_and_reference_coexist_in_discovery_state():
    # The TRAVERSAL dedup above must not come from edge loss: the discovery
    # graph STATE retains BOTH edges — the accepted Claim with its identity
    # and the Extracted Reference with its provenance (issue #170). This is
    # the non-vacuous assertion behind the traversal-only test above.
    source = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
        body="[b](beta.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    state = kb.load_or_derive_graph(Path("does-not-exist"))
    edges = state.outgoing["entity:alpha"]
    assert len(edges) == 2
    claim_edge = next(e for e in edges if e.origin == GRAPH_EDGE_ORIGIN_CLAIM)
    ref_edge = next(e for e in edges if e.origin == GRAPH_EDGE_ORIGIN_EXTRACTED)
    assert claim_edge.predicate == "uses"
    assert claim_edge.claim_id == "claim:alpha-beta-0"
    assert claim_edge.scope == "canonical"
    assert ref_edge.source_path == "alpha.md"
    assert ref_edge.line_start == 1 and ref_edge.line_end == 1
    assert ref_edge.claim_id == "" and ref_edge.predicate == ""
    assert ref_edge.scope == "discovery"
    # The incoming mirror carries both edges too.
    assert len(state.incoming["entity:beta"]) == 2


# ---------------------------------------------------------------------------
# 6. No mutation, no promotion to Relationship.
# ---------------------------------------------------------------------------


def test_extraction_does_not_mutate_page_body_or_frontmatter():
    original_body = "See [b](beta.md) now.\n"
    source = _page("Alpha", body=original_body)
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    kb.extracted_references("Alpha")
    loaded = [p for p in kb.pages if p.title == "Alpha"][0]
    assert loaded.body == original_body
    assert loaded.claims == []


def test_extracted_reference_is_not_promoted_to_relationship():
    source = _page("Alpha", body="[b](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    # related_from returns derived views of accepted Claims only (ADR-0021).
    assert kb.related_from("Alpha") == []
    # graph_path (legacy canonical) does not see extracted edges.
    assert kb.graph_path("Alpha", "Beta") is None


# ---------------------------------------------------------------------------
# 7. Extracted References are never Evidence and cannot cover a query.
# ---------------------------------------------------------------------------


def test_extracted_references_are_not_evidence():
    alpha = _page(
        "Alpha",
        body="Alpha content about apples only.\n[b](beta.md)\n",
    )
    beta = _page(
        "Beta",
        body="Beta content about bananas only.\n",
        path="beta.md",
    )
    kb = _kb([alpha, beta])

    results = kb.retrieve("bananas", limit=5)
    assert results
    for r in results:
        assert isinstance(r, RetrievalResult)
        assert isinstance(r.evidence, Evidence)
        # Evidence comes from real Compiled Page text, never an Extracted Ref.
        assert r.evidence.page_title in {"Alpha", "Beta"}


def test_extracted_reference_cannot_alone_cover_unsupported_query():
    # Alpha links to Beta, but neither page mentions the query term.
    alpha = _page(
        "Alpha",
        body="Alpha is about apples.\n[b](beta.md)\n",
    )
    beta = _page(
        "Beta",
        body="Beta is about bananas.\n",
        path="beta.md",
    )
    kb = _kb([alpha, beta])

    # "zebra" is unsupported: no Evidence, and the extracted Alpha->Beta edge
    # cannot manufacture coverage.
    assert kb.retrieve("zebra", limit=5) == []


def test_retrieval_trace_does_not_fabricate_graph_stage_when_unused():
    # Zero-index retrieval is purely lexical; discovery graph does not feed it,
    # so the trace must not invent a graph-expansion stage.
    alpha = _page(
        "Alpha",
        body="Alpha apples content.\n[b](beta.md)\n",
    )
    beta = _page("Beta", body="Beta bananas.\n", path="beta.md")
    kb = _kb([alpha, beta])

    results = kb.retrieve("apples", limit=2)
    assert results
    stage_names = {s.name for r in results for s in r.trace.stages}
    # No graph stage is emitted because graph selection does not occur in the
    # zero-index lexical path. A truthful trace never invents stages.
    assert "graph" not in stage_names
    assert "search" in stage_names


# ---------------------------------------------------------------------------
# 8. Determinism: byte-identical extraction for unchanged pages; stable across
#    repeated loads.
# ---------------------------------------------------------------------------


def test_extraction_independent_of_page_insertion_order():
    alpha = _page("Alpha", body="[b](beta.md) and [g](gamma.md)\n")
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")

    kb_forward = _kb([alpha, beta, gamma])
    kb_reverse = _kb([gamma, beta, alpha])

    assert kb_forward.extracted_references("Alpha") == kb_reverse.extracted_references("Alpha")
    assert kb_forward.related_pages(
        "Alpha", scope=GRAPH_SCOPE_DISCOVERY
    ) == kb_reverse.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY)


# ---------------------------------------------------------------------------
# 9. Inspection: extracted_references(title) and full provenance.
# ---------------------------------------------------------------------------


def test_extracted_references_inspection_returns_all_for_source():
    alpha = _page("Alpha", body="[b](beta.md) and [g](gamma.md)\n")
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    kb = _kb([alpha, beta, gamma])

    refs = kb.extracted_references("Alpha")
    targets = sorted(r.target_title for r in refs)
    assert targets == ["Beta", "Gamma"]
    for ref in refs:
        assert ref.origin == "markdown-link"
        assert ref.extractor_version == EXTRACTOR_VERSION
        assert ref.source_path == alpha.path


def test_extracted_references_inspection_empty_for_unknown_title():
    kb = _kb([_page("Alpha")])
    assert kb.extracted_references("Nope") == []


# ---------------------------------------------------------------------------
# 10. Discovery scope is now a valid, working scope (flips the #106 reservation).
# ---------------------------------------------------------------------------


def test_unknown_scope_still_raises():
    kb = _kb([_page("Alpha")])
    with pytest.raises(ValueError):
        kb.related_pages("Alpha", scope="imaginary")
    with pytest.raises(ValueError):
        kb.shortest_path("Alpha", "Alpha", scope="imaginary")


# ---------------------------------------------------------------------------
# 11. Edge cases: inline code, reference-style links, mixed content.
# ---------------------------------------------------------------------------


def test_link_inside_inline_code_is_not_extracted():
    # A Markdown link inside backticks is code, not a navigable link.
    source = _page("Alpha", body="Run `[b](beta.md)` please.\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    assert kb.extracted_references("Alpha") == []


def test_multiple_links_on_same_line_each_extracted():
    source = _page("Alpha", body="[b](beta.md) [g](gamma.md) [d](delta.md)\n")
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma", path="gamma.md")
    delta = _page("Delta", path="delta.md")
    kb = _kb([source, beta, gamma, delta])

    refs = kb.extracted_references("Alpha")
    assert sorted(r.target_title for r in refs) == ["Beta", "Delta", "Gamma"]
    # All three share the same source line.
    assert len({r.line_start for r in refs}) == 1


def test_multiline_link_text_supported():
    # Standard Markdown allows link labels to span lines; we only require the
    # destination to resolve. The line range covers the label start.
    source = _page(
        "Alpha",
        body="[a multi\nline label](beta.md)\n",
    )
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    refs = kb.extracted_references("Alpha")
    assert len(refs) == 1
    assert refs[0].target_title == "Beta"
    assert refs[0].line_start == 1
    assert refs[0].line_end == 2


def test_self_link_dropped():
    # A page linking to itself produces no traversable extracted edge.
    alpha = _page("Alpha", body="[a](alpha.md)\n", path="alpha.md")
    kb = _kb([alpha])

    assert kb.extracted_references("Alpha") == []
    assert kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY) == []


def test_image_links_not_extracted_as_references():
    # Images use ![...]() syntax and are not navigational references.
    source = _page("Alpha", body="![alt](beta.md)\n")
    beta = _page("Beta", path="beta.md")
    kb = _kb([source, beta])

    assert kb.extracted_references("Alpha") == []


# ---------------------------------------------------------------------------
# Entity-ID graph identity (issue #170, ADR-0021).
#
# The canonical and Discovery Graphs are keyed by stable Entity IDs: a title
# or path change does not alter Entity identity or accepted traversal
# topology, and titles/aliases are resolved to Entity IDs at the module edge
# while human-readable titles remain the output surface.
# ---------------------------------------------------------------------------


def test_title_change_preserves_canonical_topology_and_identity():
    # AC1: renaming a page's title (same stable Entity ID) leaves the
    # Entity-ID adjacency and the accepted traversal topology unchanged.
    def build(beta_title: str) -> KnowledgeBase:
        alpha = _page("Alpha", relationships=[Relationship(target="Beta", type="uses")])
        beta = _page(beta_title)
        # Pin the Entity ID: identity is the id, never the title.
        beta = msgspec.structs.replace(beta, id="entity:beta")
        return _kb([alpha, beta])

    before, after = build("Beta"), build("Beta Renamed")
    before_index, after_index = before._knowledge_index(), after._knowledge_index()
    assert before_index.adjacency == after_index.adjacency
    assert before_index.incoming == after_index.incoming
    # Canonical traversal over the renamed page still reaches it (by title
    # surface), and direction/scope results are structurally identical.
    assert before.related_pages("Alpha") == ["Beta"]
    assert after.related_pages("Alpha") == ["Beta Renamed"]
    assert before.shortest_path("Alpha", "Beta") == ["Alpha", "Beta"]
    assert after.shortest_path("Alpha", "Beta Renamed") == ["Alpha", "Beta Renamed"]


def test_related_pages_resolves_alias_to_entity_id_seed():
    # Titles/aliases resolve to Entity IDs at the module edge (issue #170).
    alpha = _page("Alpha", relationships=[Relationship(target="Beta Canonical", type="uses")])
    beta = _page("Beta Canonical", aliases=["Beta Alias"])
    kb = _kb([alpha, beta])

    assert kb.related_pages("Beta Alias", direction="incoming") == ["Alpha"]
    assert kb.shortest_path("Beta Alias", "Alpha", direction="incoming") == [
        "Beta Canonical",
        "Alpha",
    ]


def test_only_accepted_entity_claims_enter_traversal():
    # AC2/AC4 (per #167): disputed, superseded, and literal Claims stay
    # inspectable but outside canonical AND discovery traversal.
    from lumio_wiki.records import CLAIM_STATUS_DISPUTED, CLAIM_STATUS_SUPERSEDED

    alpha = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    # Replace the auto-built claim set with a mixed-lifecycle set.
    claims = [
        Claim(
            id="claim:accepted",
            predicate="uses",
            object="entity:beta",
            status=CLAIM_STATUS_ACCEPTED,
        ),
        Claim(
            id="claim:disputed",
            predicate="uses",
            object="entity:gamma",
            status=CLAIM_STATUS_DISPUTED,
        ),
        Claim(
            id="claim:superseded",
            predicate="uses",
            object="entity:delta",
            status=CLAIM_STATUS_SUPERSEDED,
        ),
        Claim(
            id="claim:literal",
            predicate="launched-in",
            value=2026,
            value_type="number",
            status=CLAIM_STATUS_ACCEPTED,
        ),
    ]
    alpha = msgspec.structs.replace(alpha, claims=claims)
    beta, gamma, delta = _page("Beta"), _page("Gamma"), _page("Delta")
    kb = _kb([alpha, beta, gamma, delta])

    for scope in (GRAPH_SCOPE_CANONICAL, GRAPH_SCOPE_DISCOVERY):
        assert kb.related_pages("Alpha", scope=scope) == ["Beta"]
    index = kb._knowledge_index()
    assert index.adjacency == {
        "entity:alpha": [
            GraphEdge(
                endpoint="entity:beta",
                predicate="uses",
                claim_id="claim:accepted",
                origin=GRAPH_EDGE_ORIGIN_CLAIM,
            )
        ]
    }
    # The non-accepted Claims remain inspectable on the page itself.
    assert len(alpha.claims) == 4


def test_dangling_claim_object_produces_no_edge():
    # AC4: a Claim whose object Entity has no page (blocked by publication
    # validation) is defensively absent from the graph, never a crash.
    alpha = _page(
        "Alpha",
        relationships=[Relationship(target="Ghost", type="uses")],
    )
    beta = _page("Beta")
    kb = _kb([alpha, beta])  # no "Ghost" page

    assert kb.related_pages("Alpha") == []
    assert kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY).edge_count == 0


def test_legacy_pages_without_entity_id_fall_back_to_titles():
    # Legacy Flat Mode pages without an Entity ID keep working: the graph key
    # falls back to the Canonical Page Title (issue #170 keeps legacy
    # Knowledge Bases functional).
    alpha = CompiledPage(
        path="alpha.md",
        title="Alpha",
        tags=["test"],
        summary="Alpha",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id="src-alpha", title="Alpha")],
        body="[b](beta.md)\n",
    )
    beta = CompiledPage(
        path="beta.md",
        title="Beta",
        tags=["test"],
        summary="Beta",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id="src-beta", title="Beta")],
    )
    kb = _kb([alpha, beta])

    assert kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY) == ["Beta"]
    state = kb.load_or_derive_graph(Path("does-not-exist"))
    assert state.outgoing == {
        "Alpha": [
            GraphEdge(
                endpoint="Beta",
                origin=GRAPH_EDGE_ORIGIN_EXTRACTED,
                source_path="alpha.md",
                line_start=1,
                line_end=1,
                extractor_version=EXTRACTOR_VERSION,
                scope=GRAPH_EDGE_SCOPE_DISCOVERY,
            )
        ]
    }
