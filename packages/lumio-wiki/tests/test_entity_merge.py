"""Issue #169: publish reviewed Claim mutations and Entity Merges.

Covers the acceptance criteria through the PUBLIC Proposal Pipeline seam
(stage -> review/inspect -> validate -> publish -> discard):

* Claim-level before/after disclosure (additions, edits, dispute/
  supersession lifecycle transitions, removals, evidence changes) in blast
  radius and ``proposal inspect`` — without dumping full page bodies.
* Reviewed Entity Merge: one atomic candidate KB (Claim retargeting, claim
  migration, exactly-resolved link repair, ontology redirect, retired page
  removal) with a stable surviving Entity ID.
* Merge collision (duplicate Claim ID after migration), ambiguous link
  repair, self/unknown entities, and Legacy Flat Mode refusals; a blocked
  or failed publication leaves the working Knowledge Base unchanged.
* Proposed/rejected assertions stay in proposal state; active Published
  Versions contain only accepted, disputed, or superseded Claims.
* Page Removal cannot leave a dangling Claim.
* CLI and SDK expose the same proposal behavior (``merge-entity``,
  ``proposal inspect``, ``publish``).
"""

from __future__ import annotations

import re
from pathlib import Path

import lumio_wiki as lw
import msgspec
import pytest
from lumio_wiki.cli import main
from lumio_wiki.ingest import (
    CLAIM_CHANGE_ADDED,
    CLAIM_CHANGE_EDITED,
    CLAIM_CHANGE_REMOVED,
    CLAIM_CHANGE_STATUS,
    IngestProposal,
    IngestStore,
    SourceProvenance,
)
from lumio_wiki.knowledge_base import load_knowledge_base
from lumio_wiki.proposal_pipeline import (
    ProposalBlockedError,
    ProposalPipeline,
    ProposalPipelineError,
)

# ---------------------------------------------------------------------------
# Fixtures (mirroring test_page_removal.py conventions)
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Entity-id slug: lowercase, non-alphanumerics collapsed to hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(
    title: str,
    *,
    body: str = "Body.",
    sources=None,
    relationships=None,
    aliases=None,
    claim_overrides=None,
) -> str:
    """Render a Compiled Page.

    Typed edges are accepted, evidence-bearing Claims whose object is the
    target page's Entity id. ``claim_overrides`` replaces the generated
    claims block with literal YAML lines (for hand-crafted Claim IDs).
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
    if claim_overrides is not None:
        claims_lines = claim_overrides
    else:
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


def _categorized_kb(
    tmp_path: Path,
    *,
    hot_pins: list[str] | None = None,
    redirects: str = "",
    extra_predicates: str = "",
) -> Path:
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
            f"{extra_predicates}"
            "  redirects:"
            f"{redirects}\n"
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


def _merge_kb(tmp_path: Path) -> Path:
    """The canonical three-page merge Knowledge Base.

    Alpha depends-on Beta and links to it; Beta depends-on Alpha; Gamma sees
    Alpha. The default merge under test retires ``entity:beta`` into
    ``entity:gamma``.
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page(
            "Alpha",
            body="See [[Beta]].",
            relationships=[{"target": "Beta", "type": "depends-on"}],
        ),
    )
    _write(
        root,
        "concepts/beta.md",
        _page("Beta", relationships=[{"target": "Alpha", "type": "depends-on"}]),
    )
    _write(
        root,
        "concepts/gamma.md",
        _page("Gamma", relationships=[{"target": "Alpha", "type": "see"}]),
    )
    return root


def _claim_objects(root: Path) -> set[tuple[str, str, str]]:
    """All (page title, claim id, object entity id) triples in a KB."""
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    return {
        (page.title, claim.id, claim.object)
        for page in kb.pages
        for claim in page.claims
        if claim.object is not None
    }


# ---------------------------------------------------------------------------
# Entity Merge: the reviewed, atomic mutation
# ---------------------------------------------------------------------------


def test_entity_merge_is_explicit_and_never_inferred(tmp_path):
    root = _merge_kb(tmp_path)
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")

    assert proposal.status == "staged"
    assert proposal.provenance.origin == "lumio:entity-merge"
    assert proposal.entity_merges == [
        lw.EntityMerge(
            retired_entity_id="entity:beta",
            surviving_entity_id="entity:gamma",
            retired_title="Beta",
            surviving_title="Gamma",
        )
    ]
    assert [removal.title for removal in proposal.removed_pages] == ["Beta"]
    assert pipeline.review(proposal.id) is not None
    # An ordinary revision proposal never carries a merge — merging is never
    # inferred from page content (ADR-0021 forbids automatic merging).
    revision = pipeline.assemble(
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
        SourceProvenance(None, None, "text"),
        "alpha.md",
    )
    assert revision.entity_merges == []
    assert revision.removed_pages == []


def test_entity_merge_publishes_one_atomic_candidate(tmp_path):
    """Retarget, migrate, repair links, redirect, remove — ONE proposal."""
    root = _merge_kb(tmp_path)
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    assert not proposal.blocked, proposal.validation_report
    assert proposal.blast_radius is not None  # populated by assemble; narrows the type
    # Blast radius discloses the Claim-level changes without page bodies.
    changed = {(c.claim_id, c.change) for c in proposal.blast_radius.claim_changes}
    assert ("claim:alpha-beta-0", CLAIM_CHANGE_EDITED) in changed
    assert ("claim:beta-alpha-0", CLAIM_CHANGE_ADDED) in changed  # migrated to Gamma
    assert "# Beta" not in proposal.diff.split("deleted file:")[0]

    published = pipeline.publish(proposal.id)

    assert published.status == "published"
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    titles = {page.title for page in kb.pages}
    assert titles == {"Alpha", "Gamma"}  # retired page excluded atomically
    objects = _claim_objects(root)
    # Alpha's claim retargeted to the surviving Entity ID.
    assert ("Alpha", "claim:alpha-beta-0", "entity:gamma") in objects
    assert not any(obj == "entity:beta" for _t, _c, obj in objects)
    # Beta's claim migrated onto the surviving page; Gamma keeps its own.
    assert ("Gamma", "claim:beta-alpha-0", "entity:alpha") in objects
    assert ("Gamma", "claim:gamma-alpha-0", "entity:alpha") in objects
    # Stable surviving ID: Gamma's page keeps its Entity ID and path.
    gamma = next(page for page in kb.pages if page.title == "Gamma")
    assert gamma.id == "entity:gamma"
    assert gamma.path == "concepts/gamma.md"
    # Exactly-resolved body link repaired to the survivor.
    alpha_markdown = (root / "concepts/alpha.md").read_text(encoding="utf-8")
    assert "[[Gamma]]" in alpha_markdown and "[[Beta]]" not in alpha_markdown
    # Retired Entity ID recorded as an ontology redirect.
    control_yaml = (root / "lumio.yaml").read_text(encoding="utf-8")
    assert "entity:beta: entity:gamma" in control_yaml
    # Activity Log records the entity-merge transition.
    activity = (root / "log.md").read_text(encoding="utf-8") if (root / "log.md").exists() else ""
    assert "entity-merge" in activity
    assert "entity:beta -> entity:gamma" in activity


def test_entity_merge_composes_with_existing_redirects(tmp_path):
    """A redirect chain (old -> retired -> survivor) stays resolvable."""
    root = _categorized_kb(tmp_path, redirects="\n    entity:old: entity:beta")
    _write(root, "concepts/alpha.md", _page("Alpha"))
    _write(root, "concepts/beta.md", _page("Beta"))
    _write(root, "concepts/gamma.md", _page("Gamma"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    assert not proposal.blocked, proposal.validation_report
    pipeline.publish(proposal.id)

    _kb2, report = load_knowledge_base(root)
    assert report.is_valid, report  # chain old -> beta -> gamma resolves


def test_entity_merge_collision_blocks_publication(tmp_path):
    """A migrated Claim whose subject violates the predicate domain blocks.

    From a valid base every Claim ID is Knowledge-Base-wide unique, so the
    reachable merge collision is semantic: the retired TOOL page's
    ``built-with`` Claim (subject_types: [tool]) migrates onto the CONCEPT
    survivor, whose types no longer satisfy the predicate domain.
    Publication blocks; the working Knowledge Base is unchanged.
    """
    root = tmp_path / "kb"
    root.mkdir(parents=True)
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
            "    tool: {}\n"
            "  predicates:\n"
            "    depends-on:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            "    see:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            "    built-with:\n"
            "      subject_types: [tool]\n"
            "      object_types: [concept]\n"
            "  redirects:\n"
        ),
    )
    _write(root, "concepts/alpha.md", _page("Alpha"))
    # The retired TOOL page holds a domain-valid built-with Claim.
    hammer = _page(
        "Hammer",
        claim_overrides=(
            '  - id: "claim:hammer-alpha-0"\n'
            '    predicate: "built-with"\n'
            '    object: "entity:alpha"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
        ),
    ).replace("  - concept\n", "  - tool\n", 1)
    _write(root, "concepts/hammer.md", hammer)
    _write(root, "concepts/anvil.md", _page("Anvil"))
    before = _claim_objects(root)
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:hammer", "entity:anvil")

    assert proposal.blocked
    assert any(
        "domain violation" in issue.message for issue in proposal.validation_report.issues
    ), proposal.validation_report
    with pytest.raises(ProposalBlockedError):
        pipeline.publish(proposal.id)
    # Failed publication leaves the prior state active and unchanged.
    assert _claim_objects(root) == before
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    assert {page.title for page in kb.pages} == {"Alpha", "Hammer", "Anvil"}


def test_entity_merge_ambiguous_link_repair_refuses_to_stage(tmp_path):
    """A link matching the retired page AND another page is never guessed.

    The retired page ``Beta`` and a page titled ``BETA`` casefold onto one
    stem, so a ``[[Beta]]`` link matches two pages through the SAME
    resolution key (Canonical Title) — exactly the collision the shared
    resolver drops as ambiguous. The repair must refuse rather than guess.
    (A unique Canonical-Title match wins over aliases — mirroring the
    resolver — so a mere alias collision with the retired TITLE is not
    ambiguity and repairs by title.)
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", body="See [[Beta]]."),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    # ``BETA`` is not an exact duplicate title (validation keys exact titles)
    # but casefolds onto the same stem, so link resolution is ambiguous.
    # Its Entity ID is hand-set so it does not slug-collide with ``entity:beta``.
    _write(
        root,
        "concepts/zeta.md",
        _page("BETA").replace('id: "entity:beta"', 'id: "entity:beta-shadow"'),
    )
    _write(root, "concepts/gamma.md", _page("Gamma"))
    _kb, pipeline = _pipeline(root, tmp_path)

    with pytest.raises(ProposalPipelineError, match="ambiguous link repair"):
        pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    assert pipeline.list() == []  # nothing staged, nothing half-repaired


def test_entity_merge_repairs_alias_links_and_prefers_title_over_alias(tmp_path):
    """Exactly-resolved links repair even when another page shares an alias.

    ``[[Beta]]`` resolves by the retired page's unique Canonical Title (the
    resolver's first key), so Zeta's alias "Beta" cannot make it ambiguous:
    the link repairs to the surviving title.
    """
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page("Alpha", body="See [[Beta]]."),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _write(root, "concepts/zeta.md", _page("Zeta", aliases=["Beta"]))
    _write(root, "concepts/gamma.md", _page("Gamma"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    assert not proposal.blocked, proposal.validation_report
    pipeline.publish(proposal.id)

    alpha_markdown = (root / "concepts/alpha.md").read_text(encoding="utf-8")
    assert "[[Gamma]]" in alpha_markdown and "[[Beta]]" not in alpha_markdown


def test_entity_merge_invalid_resulting_ontology_blocks_publication(tmp_path):
    """A retargeted Claim that violates the predicate range blocks."""
    root = tmp_path / "kb"
    root.mkdir(parents=True)
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
            "    tool: {}\n"
            "  predicates:\n"
            "    depends-on:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            "    see:\n"
            "      subject_types: [concept]\n"
            "      object_types: [concept]\n"
            "    built-with:\n"
            "      subject_types: [concept]\n"
            "      object_types: [tool]\n"
            "  redirects:\n"
        ),
    )
    _write(
        root,
        "concepts/alpha.md",
        _page(
            "Alpha",
            claim_overrides=(
                '  - id: "claim:alpha-tool-0"\n'
                '    predicate: "built-with"\n'
                '    object: "entity:hammer"\n'
                "    status: accepted\n"
                '    evidence:\n      - section: "Evidence"\n'
            ),
        ),
    )
    # A tool-typed page (hand-authored: different entity_types).
    hammer = _page("Hammer").replace("  - concept\n", "  - tool\n")
    _write(root, "concepts/hammer.md", hammer)
    _write(root, "concepts/anvil.md", _page("Anvil"))
    _kb, pipeline = _pipeline(root, tmp_path)

    # Merging the TOOL entity into a CONCEPT page retargets Alpha's
    # built-with claim at a concept object: a range violation.
    proposal = pipeline.propose_entity_merge("entity:hammer", "entity:anvil")

    assert proposal.blocked
    assert any(
        "range violation" in issue.message for issue in proposal.validation_report.issues
    ), proposal.validation_report
    with pytest.raises(ProposalBlockedError):
        pipeline.publish(proposal.id)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report  # working KB untouched


def test_entity_merge_self_unknown_and_flat_refusals(tmp_path):
    root = _merge_kb(tmp_path)
    _kb, pipeline = _pipeline(root, tmp_path)
    with pytest.raises(ProposalPipelineError, match="cannot merge an entity into itself"):
        pipeline.propose_entity_merge("entity:beta", "entity:beta")
    with pytest.raises(ProposalPipelineError, match="no Compiled Page found for retired"):
        pipeline.propose_entity_merge("entity:nope", "entity:gamma")
    with pytest.raises(ProposalPipelineError, match="no Compiled Page found for surviving"):
        pipeline.propose_entity_merge("entity:beta", "entity:nope")
    # Legacy Flat Mode has no ontology redirects.
    flat = tmp_path / "flat"
    flat.mkdir()
    _write(
        flat,
        "alpha.md",
        (
            "---\n"
            'title: "Alpha"\n'
            "aliases: []\n"
            'tags:\n  - "t"\n'
            'summary: "Alpha summary."\n'
            'lifecycle: "approved"\n'
            'visibility: "public"\n'
            "sources:\n"
            '  - id: "alpha-src"\n'
            '    title: "Alpha src"\n'
            "synthetic: false\n"
            "---\n"
            "# Alpha\n\n"
            "Body.\n"
        ),
    )
    flat_kb, report = load_knowledge_base(flat)
    assert report.is_valid, report
    flat_pipeline = ProposalPipeline(flat_kb, store=IngestStore(tmp_path / "flat-ingest"))
    with pytest.raises(ProposalPipelineError, match="version-2 Knowledge Base with a Control File"):
        flat_pipeline.propose_entity_merge("entity:a", "entity:b")


def test_entity_merge_discard_leaves_kb_unchanged(tmp_path):
    root = _merge_kb(tmp_path)
    before_control = (root / "lumio.yaml").read_text(encoding="utf-8")
    before_claims = _claim_objects(root)
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    discarded = pipeline.discard(proposal.id)

    assert discarded is not None and discarded.status == "discarded"
    assert (root / "lumio.yaml").read_text(encoding="utf-8") == before_control
    assert _claim_objects(root) == before_claims
    assert "concepts/beta.md" in [str(p.relative_to(root)) for p in root.rglob("*.md")]


def test_entity_merge_drops_a_pinned_hot_index_entry_atomically(tmp_path):
    root = _categorized_kb(tmp_path, hot_pins=["Beta"])
    _write(root, "concepts/alpha.md", _page("Alpha"))
    _write(root, "concepts/beta.md", _page("Beta"))
    _write(root, "concepts/gamma.md", _page("Gamma"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    assert not proposal.blocked, proposal.validation_report
    pipeline.publish(proposal.id)

    control_yaml = (root / "lumio.yaml").read_text(encoding="utf-8")
    assert "hot_index" not in control_yaml  # pin dropped atomically
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report  # no dangling pin remains


def test_entity_merge_proposal_round_trips_msgspec(tmp_path):
    root = _merge_kb(tmp_path)
    _kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.propose_entity_merge("entity:beta", "entity:gamma")
    decoded = msgspec.json.decode(msgspec.json.encode(proposal), type=IngestProposal)
    assert decoded.blast_radius is not None and proposal.blast_radius is not None
    assert decoded.entity_merges == proposal.entity_merges
    assert decoded.blast_radius.claim_changes == proposal.blast_radius.claim_changes
    assert decoded.control_file is not None
    assert decoded.control_file.ontology is not None
    assert decoded.control_file.ontology.redirects == [
        lw.EntityRedirect(from_id="entity:beta", to_id="entity:gamma")
    ]


# ---------------------------------------------------------------------------
# Claim-level before/after disclosure (inspection + blast radius)
# ---------------------------------------------------------------------------


def _alpha_with_claims(status_a1: str, evidence_count: int = 1) -> str:
    evidence = "".join(
        '      - section: "Evidence"\n' for _ in range(evidence_count)
    )
    return _page(
        "Alpha",
        claim_overrides=(
            '  - id: "claim:a1"\n'
            '    predicate: "depends-on"\n'
            '    object: "entity:beta"\n'
            f"    status: {status_a1}\n"
            f"    evidence:\n{evidence}"
        ),
    )


def test_claim_changes_report_additions_edits_lifecycle_and_removals(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(root, "concepts/beta.md", _page("Beta"))
    _write(root, "concepts/gamma.md", _page("Gamma"))
    # Existing Alpha: two claims (a1 disputed later, a2 removed later).
    existing = _page(
        "Alpha",
        claim_overrides=(
            '  - id: "claim:a1"\n'
            '    predicate: "depends-on"\n'
            '    object: "entity:beta"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
            '  - id: "claim:a2"\n'
            '    predicate: "see"\n'
            '    object: "entity:gamma"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
            '  - id: "claim:a5"\n'
            '    predicate: "see"\n'
            '    object: "entity:beta"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
            '      - section: "Evidence"\n'
        ),
    )
    _write(root, "concepts/alpha.md", existing)
    _kb, pipeline = _pipeline(root, tmp_path)

    # Proposed Alpha: a1 disputed (lifecycle), a2 gone (removed), a3 added
    # (accepted), a4 added (proposed — stays in proposal state), a5 same
    # status but one more evidence anchor (edited).
    revised = _page(
        "Alpha",
        claim_overrides=(
            '  - id: "claim:a1"\n'
            '    predicate: "depends-on"\n'
            '    object: "entity:beta"\n'
            "    status: disputed\n"
            '    evidence:\n      - section: "Evidence"\n'
            '  - id: "claim:a3"\n'
            '    predicate: "see"\n'
            '    object: "entity:gamma"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
            '  - id: "claim:a4"\n'
            '    predicate: "see"\n'
            '    object: "entity:beta"\n'
            "    status: proposed\n"
            '    evidence:\n      - section: "Evidence"\n'
            '  - id: "claim:a5"\n'
            '    predicate: "see"\n'
            '    object: "entity:beta"\n'
            "    status: accepted\n"
            '    evidence:\n      - section: "Evidence"\n'
            '      - section: "Evidence"\n'
            '      - section: "Evidence"\n'
        ),
    )
    proposal = pipeline.assemble(revised, SourceProvenance(None, None, "text"), "alpha.md")

    assert proposal.blast_radius is not None  # populated by assemble; narrows the type
    changes = {c.claim_id: c for c in proposal.blast_radius.claim_changes}
    assert set(changes) == {"claim:a1", "claim:a2", "claim:a3", "claim:a4", "claim:a5"}
    assert changes["claim:a1"].change == CLAIM_CHANGE_STATUS
    assert "status=accepted" in changes["claim:a1"].before
    assert "status=disputed" in changes["claim:a1"].after
    assert changes["claim:a2"].change == CLAIM_CHANGE_REMOVED
    assert changes["claim:a3"].change == CLAIM_CHANGE_ADDED
    assert "status=accepted" in changes["claim:a3"].after
    assert changes["claim:a4"].change == CLAIM_CHANGE_ADDED
    assert "status=proposed" in changes["claim:a4"].after  # proposal-state lifecycle
    assert changes["claim:a5"].change == CLAIM_CHANGE_EDITED
    assert "evidence=2" in changes["claim:a5"].before
    assert "evidence=3" in changes["claim:a5"].after

    # Proposed/rejected assertions stay in proposal state: the proposal carries
    # them, but publication is blocked (active pages only hold accepted,
    # disputed, or superseded Claims — ADR-0021).
    staged = pipeline.stage(proposal)
    assert staged.blocked
    assert any("claim:a4" in page.markdown for page in staged.proposed_pages)
    with pytest.raises(ProposalBlockedError):
        pipeline.publish(staged.id)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    alpha = next(page for page in kb.pages if page.title == "Alpha")
    assert [claim.id for claim in alpha.claims] == ["claim:a1", "claim:a2", "claim:a5"]


def test_claim_changes_for_a_new_page_report_all_additions(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(root, "concepts/beta.md", _page("Beta"))
    _kb, pipeline = _pipeline(root, tmp_path)
    proposal = pipeline.assemble(
        _page("Alpha", relationships=[{"target": "Beta", "type": "depends-on"}]),
        SourceProvenance(None, None, "text"),
        "alpha.md",
    )
    assert proposal.blast_radius is not None  # populated by assemble; narrows the type
    assert [c.change for c in proposal.blast_radius.claim_changes] == [CLAIM_CHANGE_ADDED]


# ---------------------------------------------------------------------------
# Page Removal cannot leave a dangling Claim
# ---------------------------------------------------------------------------


def test_page_removal_never_leaves_a_dangling_claim(tmp_path):
    root = _categorized_kb(tmp_path)
    _write(
        root,
        "concepts/alpha.md",
        _page(
            "Alpha",
            relationships=[
                {"target": "Beta", "type": "depends-on"},
                {"target": "Gamma", "type": "see"},
            ],
        ),
    )
    _write(root, "concepts/beta.md", _page("Beta"))
    _write(root, "concepts/gamma.md", _page("Gamma"))
    _kb, pipeline = _pipeline(root, tmp_path)

    proposal = pipeline.propose_page_removal("Beta", reason="lost")
    assert not proposal.blocked, proposal.validation_report
    pipeline.publish(proposal.id)

    objects = _claim_objects(root)
    assert ("Alpha", "claim:alpha-gamma-1", "entity:gamma") in objects
    assert not any(obj == "entity:beta" for _t, _c, obj in objects)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report  # no dangling Claim object survives removal
    # The removal is disclosed as a Claim removal in the blast radius.
    assert proposal.blast_radius is not None  # populated by assemble; narrows the type
    removed = [c for c in proposal.blast_radius.claim_changes if c.change == CLAIM_CHANGE_REMOVED]
    assert any(c.claim_id == "claim:alpha-beta-0" for c in removed)


# ---------------------------------------------------------------------------
# CLI and SDK expose the same proposal behavior
# ---------------------------------------------------------------------------


def test_merge_entity_cli_stages_inspects_and_publishes(tmp_path, capsys):
    root = _merge_kb(tmp_path)
    ingest_dir = tmp_path / "cli-ingest"

    exit_code = main(
        ["merge-entity", str(root), "entity:beta", "entity:gamma", "--ingest-dir", str(ingest_dir)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "Staged proposal" in out
    assert "entity_merge:   entity:beta -> entity:gamma" in out

    proposal_id = out.split("Staged proposal ")[1].splitlines()[0].strip()
    exit_code = main(
        ["proposal", "inspect", str(root), proposal_id, "--ingest-dir", str(ingest_dir)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "claim_changes:" in out
    assert "before:" in out and "after:" in out
    # Disclosure carries no full page bodies (the Diff section is a diff,
    # not a body dump).
    disclosure = out.split("Diff:")[0]
    assert "Supporting evidence for the edges." not in disclosure

    exit_code = main(["publish", str(root), proposal_id, "--ingest-dir", str(ingest_dir)])
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert "Published proposal" in out

    kb, report = load_knowledge_base(root)
    assert report.is_valid, report
    assert {page.title for page in kb.pages} == {"Alpha", "Gamma"}


def test_merge_entity_cli_blocked_merge_is_an_error(tmp_path, capsys):
    # The domain-violation merge (tool page retired into a concept survivor)
    # stages BLOCKED; the CLI reports the block.
    root = _merge_kb(tmp_path)
    exit_code = main(
        [
            "merge-entity",
            str(root),
            "entity:beta",
            "entity:nope",
            "--ingest-dir",
            str(tmp_path / "i"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Staged proposal" not in captured.out
    assert "error:" in captured.err


def test_proposal_inspect_json_carries_claim_changes_and_merges(tmp_path, capsys):
    root = _merge_kb(tmp_path)
    ingest_dir = tmp_path / "json-ingest"
    assert main(
        ["merge-entity", str(root), "entity:beta", "entity:gamma", "--ingest-dir", str(ingest_dir)]
    ) == 0
    proposal_id = (
        capsys.readouterr().out.split("Staged proposal ")[1].splitlines()[0].strip()
    )
    assert main(
        [
            "proposal",
            "inspect",
            str(root),
            proposal_id,
            "--json",
            "--ingest-dir",
            str(ingest_dir),
        ]
    ) == 0
    payload = capsys.readouterr().out
    assert '"entity_merges"' in payload
    assert '"claim_changes"' in payload
