"""Issue #135: publish explicit Page Removals with dependency repair.

These tests exercise the Page Removal mutation through the PUBLIC Proposal
Pipeline seam (assemble -> stage -> review/inspect -> validate -> publish ->
discard) and cover every acceptance criterion:

* AC1 — the same Proposal Pipeline as page creates/revisions.
* AC2 — a removal is explicit, never inferred from an omitted page.
* AC3 — page-level (sole-source-lost) invalidation proposes a removal.
* AC4 — inspect shows removed title, lost-support reason, diff, blast radius.
* AC5 — every canonical Relationship to the removed page is repaired;
  unresolved canonical dependencies block publication.
* AC6 — internal body links become location-bearing repair candidates, never
  silently redirected.
* AC7 — publication removes the page atomically, regenerates reserved
  artifacts, and records the Activity Log transition.
* AC9 — a blocked/failed publish leaves the Knowledge Base unchanged.
* AC10 — coverage of removal, partial removal that keeps the page, repair,
  body links, reserved artifacts, and rollback.

Per ADR-0014 the claim-level lineage design is deferred (#137), so
"material Claim / eligible support" (AC3) is exercised at the PAGE level via
the ``sole-source-lost`` classification produced by a source retirement.
"""

from __future__ import annotations

import re
from pathlib import Path

import lumio_wiki as lw
import msgspec
import pytest
from lumio_wiki.ingest import (
    IngestProposal,
    IngestStore,
    PageRemoval,
    SourceProvenance,
)
from lumio_wiki.knowledge_base import load_knowledge_base
from lumio_wiki.proposal_pipeline import ProposalBlockedError, ProposalPipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Entity-id slug: lowercase, non-alphanumerics collapsed to hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(
    title: str, *, body: str = "Body.", sources=None, relationships=None, aliases=None
) -> str:
    """Render a Compiled Page.

    Since ADR-0021 the title-based ``relationships`` frontmatter input is
    gone: a typed edge to another page is an accepted, evidence-bearing Claim
    whose object is the target page's Entity id ("entity:<slug-of-title>").
    Every page declares its Entity id and type so claim objects resolve.
    """
    src = (
        sources
        if sources is not None
        else [{"id": f"{title.lower()}-src", "title": f"{title} src"}]
    )
    als = aliases if aliases is not None else []
    src_lines = "\n".join(
        f'  - id: "{s.get("id", "")}"\n    title: "{s.get("title", "")}"' for s in src
    )
    claims_lines = "".join(
        f'  - id: "claim:{_slug(title)}-{_slug(rel["target"])}-{n}"\n'
        f'    predicate: "{rel["type"]}"\n'
        f'    object: "entity:{_slug(rel["target"])}"\n'
        f"    status: accepted\n"
        f'    evidence:\n      - section: "Evidence"\n'
        for n, rel in enumerate(relationships or [])
    )
    entity_lines = f'id: "entity:{_slug(title)}"\nentity_types:\n  - concept\n'
    if claims_lines:
        entity_lines += f"claims:\n{claims_lines}"
    body_text = (
        f"{body}\n\n## Evidence\n\nSupporting evidence for the edges.\n" if claims_lines else body
    )
    return (
        "---\n"
        f'title: "{title}"\n'
        f"aliases: {als}\n"
        'tags:\n  - "t"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        f"sources:\n{src_lines}\n"
        f"{entity_lines}"
        "synthetic: false\n"
        "---\n"
        f"# {title}\n\n{body_text}\n"
    )


def _categorized_kb(tmp_path: Path, *, hot_pins: list[str] | None = None) -> Path:
    """A categorized Knowledge Base root with a Control File."""
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    pins_block = ""
    if hot_pins:
        pin_lines = "\n".join(f'  - {{title: "{p}"}}' for p in hot_pins)
        pins_block = f"\nhot_index:\n{pin_lines}\n"
    _write(
        root,
        "lumio.yaml",
        (
            "version: 2\n"
            "mode: categorized\n"
            "categories:\n"
            "  - {name: concepts}\n"
            "ontology:\n"
            "  entity_types:\n"
            "    concept: {}\n"
            "  predicates:\n"
            "    depends-on:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            "    see:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            f"{pins_block}"
        ),
    )
    return root


def _pipeline(root: Path, tmp_path: Path) -> tuple[lw.KnowledgeBase, ProposalPipeline]:
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store=store)
    assert pipeline._store is not None  # populated by construction; narrows the type
    return kb, pipeline


# ---------------------------------------------------------------------------
# AC1 + AC2: explicit mutation through the public pipeline; never inferred
# ---------------------------------------------------------------------------


def test_page_removal_is_public_and_never_inferred(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal("Beta", reason="sole-source-lost")

    # AC1: persisted, reviewable, through the same pipeline.
    assert proposal.status == "staged"
    assert pipeline.review(proposal.id) is not None
    assert proposal.id in [p.id for p in pipeline.list()]
    assert proposal.removed_pages == [
        PageRemoval(title="Beta", lost_support_reason="sole-source-lost")
    ]
    # AC2: a removal is explicit. An ordinary revision proposal that simply
    # omits Beta carries NO removed_pages — Beta is never inferred removed.
    revision = pipeline.assemble(
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
        SourceProvenance(None, None, "text"),
        "alpha.md",
    )
    assert revision.removed_pages == []
    assert "Beta" in [p.title for p in kb.pages]  # untouched


def test_page_removal_unknown_title_is_refused(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(root, "concepts/alpha.md", _page("Alpha"))
    _kb, pipeline = _pipeline(root, tmp_path)
    with pytest.raises(Exception, match="no Compiled Page"):
        pipeline.propose_page_removal("Does Not Exist")


def test_page_removal_serializes_and_round_trips(tmp_path):
    """AC1: removed_pages + body_link_repairs persist through msgspec JSON."""
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", body="see [[Beta]]", relationships=[{"target": "Beta", "type": "see"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_page_removal("Beta", reason="lost")
    encoded = msgspec.json.encode(proposal)
    decoded = msgspec.json.decode(encoded, type=IngestProposal)
    assert decoded.removed_pages == proposal.removed_pages
    assert decoded.body_link_repairs == proposal.body_link_repairs


# ---------------------------------------------------------------------------
# AC5 + AC6: relationship repair + location-bearing body-link candidates
# ---------------------------------------------------------------------------


def test_relationship_repair_drops_edges_and_body_links_become_candidates(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page(
            "Alpha",
            body="Details in [[Beta]] and [plain](beta.md).",
            relationships=[{"target": "Beta", "type": "depends-on"}],
        ),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal("Beta")

    # AC5: Alpha gets ONE repair revision that drops the depends-on edge.
    assert [p.title for p in proposal.proposed_pages] == ["Alpha"]
    repair = proposal.proposed_pages[0]
    # The dropped edge surfaces in the blast radius (review disclosure).
    assert proposal.blast_radius is not None
    assert any("Beta" in c for c in proposal.blast_radius.relationship_changes)
    # The repair markdown no longer claims the removed page's Entity.
    _data, _body, _ = lw.parse_frontmatter(repair.markdown, Path("concepts/alpha.md"))
    assert all(c.get("object") != "entity:beta" for c in _data.get("claims", []))

    # AC6: the wikilink/body link becomes a location-bearing candidate, never
    # silently redirected. Alpha's body still mentions Beta verbatim.
    assert any(
        c.source_title == "Alpha" and c.target_title == "Beta" for c in proposal.body_link_repairs
    )
    assert "Beta" in proposal.proposed_pages[0].markdown  # body untouched


def test_unresolved_canonical_dependency_blocks_publication(tmp_path):
    """AC5: a relationship the proposal does NOT repair blocks publication.

    A Maintainer who strips the repair revision from the staged proposal leaves
    an unresolved Relationship target; candidate validation blocks publication
    and the Knowledge Base is left unchanged.
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_page_removal("Beta")

    # Sabotage: drop the repair so the removal would leave a dangling edge.
    sabotaged = msgspec.structs.replace(proposal, proposed_pages=[])
    assert pipeline._store is not None
    pipeline._store.save_proposal(sabotaged)

    with pytest.raises(ProposalBlockedError):
        pipeline.publish(proposal.id)
    # AC9: unchanged — Beta still on disk and resolvable.
    kb2, _ = load_knowledge_base(root)
    assert "Beta" in [p.title for p in kb2.pages]


# ---------------------------------------------------------------------------
# AC3: page-level (sole-source-lost) invalidation proposes a removal
# ---------------------------------------------------------------------------


def test_retirement_sole_source_lost_offers_page_removal(tmp_path):
    """AC3: a sole-source-lost page can be removed through the same pipeline.

    ADR-0014 maps "no material Claim retains eligible support" to the
    page-level ``sole-source-lost`` classification. Retiring the only source of
    a page classifies it sole-source-lost; the Maintainer then stages an
    explicit Page Removal from that signal.
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/policy.md",
        _page("Policy", sources=[{"id": "handbook", "title": "Handbook"}]),
    )
    kb, pipeline = _pipeline(root, tmp_path)

    pipeline.register_source("handbook", b"handbook-v1")
    retire = pipeline.retire_source("handbook")
    assert retire.source_change is not None
    impacts = {i.page_title: i.status for i in retire.source_change.impacts}
    assert impacts == {"Policy": "sole-source-lost"}

    # The Maintainer turns the sole-source-lost signal into an explicit removal.
    removal = pipeline.propose_page_removal(
        "Policy", reason="sole-source-lost: no active support remains"
    )
    assert removal.removed_pages[0].lost_support_reason.startswith("sole-source-lost")
    assert not removal.blocked
    pipeline.publish(removal.id)

    kb2, _ = load_knowledge_base(root)
    assert "Policy" not in [p.title for p in kb2.pages]


def test_partial_support_removal_keeps_the_page(tmp_path):
    """AC10: a page that retains other active support is NOT removed.

    Retiring ONE of two active sources leaves the page still-supported; no
    Page Removal is inferred. (Partial Claim removal that keeps the page.)
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/policy.md",
        _page(
            "Policy",
            sources=[
                {"id": "handbook", "title": "Handbook"},
                {"id": "guide", "title": "Guide"},
            ],
        ),
    )
    kb, pipeline = _pipeline(root, tmp_path)
    pipeline.register_source("handbook", b"h")
    pipeline.register_source("guide", b"g")

    retire = pipeline.retire_source("handbook")
    assert retire.source_change is not None
    impacts = {i.page_title: i.status for i in retire.source_change.impacts}
    assert impacts == {"Policy": "still-supported"}

    # Publishing the retirement does NOT remove the page.
    pipeline.publish(retire.id)
    kb2, _ = load_knowledge_base(root)
    assert "Policy" in [p.title for p in kb2.pages]


def test_unresolved_body_link_surfaces_as_nonblocking_diagnostic(tmp_path):
    """AC10: a body link to the removed page, left un-repaired, surfaces as a
    non-blocking broken-internal-link diagnostic after publish — never silent.

    Body links are NEVER silently redirected (AC6); a Maintainer who does not
    repair one still gets a visible, non-blocking warning in the new version
    rather than a silent break or a blocking failure.
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", body="Details in [[Beta]]."),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_page_removal("Beta")
    # No body link is repaired in this proposal (only the relationship edge
    # would be); Alpha's [[Beta]] wikilink is an un-repaired candidate.
    assert any(c.target_title == "Beta" for c in proposal.body_link_repairs)

    pipeline.publish(proposal.id)
    kb2, report2 = load_knowledge_base(root)
    # The published KB is VALID (body links are non-blocking)...
    assert report2.is_valid, report2
    # ...and the dangling link surfaces as a warning diagnostic, not silently.
    link_warnings = [
        i
        for i in report2.issues
        if i.severity == "warning" and "internal link" in i.message and "Beta" in i.message
    ]
    assert link_warnings, "expected a broken-internal-link warning for the dangling [[Beta]]"


# ---------------------------------------------------------------------------
# AC4: inspect shows removed title, reason, diff, blast radius
# ---------------------------------------------------------------------------


def test_inspect_carries_removed_title_reason_diff_and_blast_radius(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal(
        "Beta", reason="sole-source-lost", affected_claim_notes=["policy section unsupported"]
    )

    # Removed title + lost-support reason + affected notes (AC4).
    rm = proposal.removed_pages[0]
    assert rm.title == "Beta"
    assert rm.lost_support_reason == "sole-source-lost"
    assert rm.affected_claim_notes == ["policy section unsupported"]
    # Unified diff includes a deletion block for the removed page (AC4).
    assert "deleted file" in proposal.diff and "Beta" in proposal.diff
    # Blast radius discloses the relationship repair (AC4).
    assert proposal.blast_radius is not None
    assert proposal.blast_radius.relationship_changes


# ---------------------------------------------------------------------------
# AC7: atomic publication, reserved-artifact regeneration, activity log
# ---------------------------------------------------------------------------


def test_publish_removes_page_regenerates_artifacts_and_logs_activity(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_page_removal("Beta")

    published = pipeline.publish(proposal.id)
    assert published.status == "published"

    kb2, report2 = load_knowledge_base(root)
    assert report2.is_valid, report2
    assert "Beta" not in [p.title for p in kb2.pages]
    # The repaired Alpha no longer claims Beta.
    alpha = next(p for p in kb2.pages if p.title == "Alpha")
    assert all(claim.object != "entity:beta" for claim in alpha.claims)

    # AC7: Navigation Index regenerated and excludes Beta.
    index = (root / "index.md").read_text(encoding="utf-8")
    assert "Alpha" in index and "Beta" not in index

    # AC7: Activity Log records the page-removal transition, no Claim Lineage.
    log = (root / "log.md").read_text(encoding="utf-8")
    assert "page-removal" in log and "Beta" in log
    assert "claim" not in log.lower()


def test_publish_drops_a_pinned_hot_index_entry_atomically(tmp_path):
    """AC7: a Hot Index pin on the removed page is dropped in the same proposal."""
    root = _categorized_kb(tmp_path, hot_pins=["Beta"])
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal("Beta")
    assert proposal.control_file is not None  # pin-drop proposed atomically
    assert [p.title for p in proposal.control_file.hot_index] == []
    assert not proposal.blocked

    pipeline.publish(proposal.id)
    kb2, report2 = load_knowledge_base(root)
    assert report2.is_valid, report2  # no unresolved pin remains
    assert kb2.control is not None
    assert [p.title for p in kb2.control.hot_index] == []


def test_reserved_artifacts_are_regenerated_not_removed(tmp_path):
    """AC7/AC10: publishing a removal regenerates reserved artifacts; the removed
    page disappears from the Navigation Index while the artifacts stay present.

    Reserved artifacts (Navigation Index, Hot Index, Activity Log) carry no
    Canonical Title, so they are unreachable by a Page Removal (which targets a
    title). The basename guard in ``apply_proposed_pages`` is defense-in-depth;
    here we verify the public behavior: a removal publish regenerates the
    reserved artifacts and never deletes them.
    """
    root = _categorized_kb(tmp_path, hot_pins=["Alpha"])
    _write(root, "concepts/alpha.md", _page("Alpha"))
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)
    pipeline.publish(pipeline.propose_page_removal("Beta").id)

    # Reserved artifacts are present and regenerated.
    for name in ("index.md", "log.md"):
        assert (root / name).is_file(), f"{name} should be regenerated, not removed"
    index = (root / "index.md").read_text(encoding="utf-8")
    assert "Alpha" in index and "Beta" not in index

    # Defense-in-depth: the basename guard refuses a removal whose resolved
    # path is a reserved artifact (exercised directly at the apply step).
    from lumio_wiki.publish import _reserved_basenames

    assert {"index.md", "hot.md", "log.md"} <= _reserved_basenames()


# ---------------------------------------------------------------------------
# AC9: rollback / atomicity through the public seam
# ---------------------------------------------------------------------------


def test_blocked_removal_leaves_knowledge_base_unchanged(tmp_path):
    """AC9: a removal that fails candidate validation leaves the KB unchanged."""
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal("Beta")
    # Remove the repair so publication must block.
    sabotaged = msgspec.structs.replace(proposal, proposed_pages=[])
    assert pipeline._store is not None
    pipeline._store.save_proposal(sabotaged)
    snapshot_before = {p.title: (kb.root / p.path).read_text() for p in kb.pages}

    with pytest.raises(ProposalBlockedError):
        pipeline.publish(proposal.id)

    kb2, _ = load_knowledge_base(root)
    for title, text in snapshot_before.items():
        page = next(p for p in kb2.pages if p.title == title)
        assert (kb2.root / page.path).read_text() == text  # byte-unchanged


def test_discard_transitions_a_page_removal_proposal(tmp_path):
    """AC1: discard works through the same pipeline as creates/revisions."""
    root = _categorized_kb(tmp_path)
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_page_removal("Beta")

    discarded = pipeline.discard(proposal.id)
    assert discarded is not None and discarded.status == "discarded"
    # A discarded proposal cannot be published.
    from lumio_wiki.proposal_pipeline import ProposalPipelineError

    with pytest.raises(ProposalPipelineError):
        pipeline.publish(proposal.id)
    kb2, _ = load_knowledge_base(root)
    assert "Beta" in [p.title for p in kb2.pages]
