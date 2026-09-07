"""TDD spec for Dream synthesis candidates and transitive impact (t_c13aea62).

Two bounded read-only Dream diagnostics. Synthesis candidates rank page pairs
whose ACCEPTED Claims justify a synthetic Compiled Page (canonical support
only); transitive impact reports the bounded canonical (optionally discovery)
reach of one selected page/Entity. Advisory always: nothing is authored,
staged, or rewritten automatically (ADR-0021, ADR-0015). Discovery edges may
only select pages to inspect, never support a conclusion.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.cli import main

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

SYN_PAGE = """---
id: "{id}"
title: "{title}"
entity_types:
  - {entity_type}
tags:
  - "entity"
summary: "A synthesis-flavored page for the Dream synthesis-candidate tests."
lifecycle: "approved"
visibility: "public"
type: "entity"
sources:
  - id: "{source_id}"
    title: "Synthesis-flavored registry"
{claims}---
# {title}

A synthesis-flavored page used by the Dream synthesis-candidate tests.
"""

CLAIM = """claims:
  - id: "claim:{short}"
    predicate: {predicate}
    object: "{object_id}"
    status: {status}
    evidence:
      - section: "{title}"
"""


def _claims(
    short: str, predicate: str, object_id: str, title: str, status: str = "accepted"
) -> str:
    return CLAIM.format(
        short=short, predicate=predicate, object_id=object_id, title=title, status=status
    )


def _store(kb_root: Path) -> lw.IngestStore:
    ingest_dir = kb_root / ".ingest"
    ingest_dir.mkdir(parents=True, exist_ok=True)
    return lw.IngestStore(ingest_dir)


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the categorized fixture into a writable Knowledge Base root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", root)
    return root


def _add_page(
    kb_root: Path,
    name: str,
    entity_id: str,
    title: str,
    *,
    entity_type: str = "concept",
    claims: str = "",
) -> Path:
    path = kb_root / "entities" / name
    path.write_text(
        SYN_PAGE.format(
            id=entity_id,
            title=title,
            entity_type=entity_type,
            source_id=entity_id,
            claims=claims,
        ),
        encoding="utf-8",
    )
    return path


def _pairs(report):
    return {
        (c.entity_a_id, c.entity_b_id)
        for c in report.synthesis_candidates
    }


# ---------------------------------------------------------------------------
# Synthesis candidate discovery (SDK, read-only, canonical support only)
# ---------------------------------------------------------------------------


def test_fixture_one_way_claim_is_candidate(kb_root: Path):
    """The categorized fixture ships overview --uses--> acme: rank 2 pair."""
    report = lw.run_dream_cycle(kb_root)
    assert ("entity:acme-corp", "entity:lumio-overview") in _pairs(report)
    candidate = next(
        c
        for c in report.synthesis_candidates
        if {c.entity_a_id, c.entity_b_id} == {"entity:acme-corp", "entity:lumio-overview"}
    )
    assert candidate.signals_rank == 2
    assert "one-way-claim: uses" in candidate.signals


def test_mutual_claims_rank_strongest(kb_root: Path):
    path = kb_root / "entities" / "acme.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "type: \"entity\"",
            "type: \"entity\"\nclaims:\n"
            "  - id: \"claim:acme-used-by-overview\"\n"
            "    predicate: used-by\n"
            "    object: \"entity:lumio-overview\"\n"
            "    status: accepted\n"
            "    evidence:\n"
            "      - section: \"Acme Corp\"",
            1,
        ),
        encoding="utf-8",
    )
    report = lw.run_dream_cycle(kb_root)
    candidate = next(
        c
        for c in report.synthesis_candidates
        if {c.entity_a_id, c.entity_b_id} == {"entity:acme-corp", "entity:lumio-overview"}
    )
    assert candidate.signals_rank == 3
    assert any(s.startswith("mutual-claims: ") for s in candidate.signals)


def test_shared_object_rank_weakest(kb_root: Path):
    _add_page(
        kb_root,
        "other.md",
        "entity:other-concept",
        "Other Concept",
        claims=_claims("other-uses-acme", "uses", "entity:acme-corp", "Other Concept"),
    )
    report = lw.run_dream_cycle(kb_root)
    candidate = next(
        c
        for c in report.synthesis_candidates
        if {c.entity_a_id, c.entity_b_id} == {"entity:lumio-overview", "entity:other-concept"}
    )
    assert candidate.signals_rank == 1
    assert "shared-object: entity:acme-corp" in candidate.signals


def test_disputed_claims_never_justify(kb_root: Path):
    _add_page(
        kb_root,
        "disputed-src.md",
        "entity:disputed-src",
        "Disputed Source",
        claims=_claims(
            "disputed-uses", "uses", "entity:acme-corp", "Disputed Source", status="disputed"
        ),
    )
    report = lw.run_dream_cycle(kb_root)
    assert all(
        "entity:disputed-src" not in (c.entity_a_id, c.entity_b_id)
        for c in report.synthesis_candidates
    )


def test_dangling_claim_object_never_justifies(kb_root: Path):
    _add_page(
        kb_root,
        "dangling.md",
        "entity:dangling",
        "Dangling Page",
        claims=_claims("dangling-uses", "uses", "entity:missing", "Dangling Page"),
    )
    report = lw.run_dream_cycle(kb_root)
    assert all(
        "entity:dangling" not in (c.entity_a_id, c.entity_b_id)
        for c in report.synthesis_candidates
    )


def test_candidates_stably_ranked_strongest_first(kb_root: Path):
    _add_page(
        kb_root,
        "a.md",
        "entity:alpha",
        "Alpha",
        claims=_claims("a-uses-acme", "uses", "entity:acme-corp", "Alpha"),
    )
    _add_page(
        kb_root,
        "b.md",
        "entity:beta",
        "Beta",
        claims=_claims("b-uses-acme", "uses", "entity:acme-corp", "Beta"),
    )
    report = lw.run_dream_cycle(kb_root)
    candidates = report.synthesis_candidates
    assert len(candidates) >= 3
    ranks = [c.signals_rank for c in candidates]
    assert ranks == sorted(ranks, reverse=True)
    keys = [(c.entity_a_id, c.entity_b_id) for c in candidates]
    assert keys == sorted(keys)


def test_candidates_bounded_by_limit(kb_root: Path):
    for n in range(lw.SYNTHESIS_CANDIDATE_LIMIT + 2):
        _add_page(
            kb_root,
            f"s-{n}.md",
            f"entity:s-{n}",
            f"Syn Page {n}",
            claims=_claims(f"s-{n}-uses-acme", "uses", "entity:acme-corp", f"Syn Page {n}"),
        )
    report = lw.run_dream_cycle(kb_root)
    assert report.synthesis_count == lw.SYNTHESIS_CANDIDATE_LIMIT


def test_finder_is_pure_over_given_pages(kb_root: Path):
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_synthesis_candidates

    first = find_synthesis_candidates(load_knowledge_base(kb_root)[0].pages)
    second = find_synthesis_candidates(load_knowledge_base(kb_root)[0].pages)
    assert first == second


def test_synthetic_and_legacy_pages_excluded(kb_root: Path):
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_synthesis_candidates

    assert find_synthesis_candidates(load_knowledge_base(kb_root)[0].pages)
    kb, _ = load_knowledge_base(kb_root)
    for page in kb.pages:
        object.__setattr__(page, "synthetic", True)
    assert find_synthesis_candidates(kb.pages) == []


# ---------------------------------------------------------------------------
# Transitive impact (SDK, read-only, bounded)
# ---------------------------------------------------------------------------


def _add_chain_page(kb_root: Path) -> None:
    """acme --used-by--> sub, extending the fixture chain one hop."""
    _add_page(kb_root, "sub.md", "entity:acme-sub", "Acme Sub")
    path = kb_root / "entities" / "acme.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "type: \"entity\"",
            "type: \"entity\"\nclaims:\n"
            "  - id: \"claim:acme-used-by-sub\"\n"
            "    predicate: used-by\n"
            "    object: \"entity:acme-sub\"\n"
            "    status: accepted\n"
            "    evidence:\n"
            "      - section: \"Acme Corp\"",
            1,
        ),
        encoding="utf-8",
    )


def test_impact_direct_edges_and_depths(kb_root: Path):
    _add_chain_page(kb_root)
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_transitive_impact

    kb, _ = load_knowledge_base(kb_root)
    impact = find_transitive_impact(kb, "Lumio Overview", max_depth=2)
    assert impact.found
    assert impact.entity_id == "entity:lumio-overview"
    assert impact.direct_out == ("Acme Corp",)
    assert impact.direct_in == ()
    assert impact.pages_by_depth[0] == ("Acme Corp",)
    assert impact.pages_by_depth[1] == ("Acme Sub",)
    assert impact.reachable == 2


def test_impact_depth_cutoff_and_unknown_name(kb_root: Path):
    _add_chain_page(kb_root)
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_transitive_impact

    kb, _ = load_knowledge_base(kb_root)
    shallow = find_transitive_impact(kb, "Lumio Overview", max_depth=1)
    assert shallow.reachable == 1
    assert shallow.pages_by_depth == (("Acme Corp",),)

    missing = find_transitive_impact(kb, "No Such Page")
    assert not missing.found
    assert missing.reachable == 0
    assert missing.pages_by_depth == ()


def test_impact_discovery_scope_discloses_select_only(kb_root: Path):
    from lumio_wiki.knowledge_base import (
        GRAPH_SCOPE_DISCOVERY,
        load_knowledge_base,
    )
    from lumio_wiki.maintenance import find_transitive_impact

    kb, _ = load_knowledge_base(kb_root)
    impact = find_transitive_impact(kb, "Lumio Overview", scope=GRAPH_SCOPE_DISCOVERY)
    assert impact.scope == GRAPH_SCOPE_DISCOVERY


def test_impact_by_entity_id(kb_root: Path):
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_transitive_impact

    kb, _ = load_knowledge_base(kb_root)
    impact = find_transitive_impact(kb, "entity:acme-corp")
    assert impact.found
    assert impact.entity_id == "entity:acme-corp"
    assert impact.direct_in == ("Lumio Overview",)


# ---------------------------------------------------------------------------
# Dream report composition and read-only guarantee
# ---------------------------------------------------------------------------


def test_dream_report_fields_default_empty():
    from lumio_wiki.maintenance import LintReport
    from lumio_wiki.records import GraphHealthReport, ValidationReport

    report = lw.DreamReport(
        lint=LintReport(
            kb_path="x",
            validation_report=ValidationReport(),
            graph_health=GraphHealthReport(),
            extracted_references=(),
            canonical_relationships=(),
            canonical_scope="canonical",
            discovery_scope="discovery",
            scope_disclosure="",
            canonical_structure=None,  # type: ignore[arg-type]
            discovery_structure=None,  # type: ignore[arg-type]
            page_count=0,
        ),
        ranked_candidates=(),
    )
    assert report.synthesis_candidates == ()
    assert report.synthesis_count == 0
    assert report.impact is None


def test_dream_never_writes(kb_root: Path):
    _add_page(
        kb_root,
        "other.md",
        "entity:other-concept",
        "Other Concept",
        claims=_claims("other-uses-acme", "uses", "entity:acme-corp", "Other Concept"),
    )
    before = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    report = lw.run_dream_cycle(kb_root, impact_target="Lumio Overview")
    # overview+acme (one-way), other+acme (one-way), overview+other (shared-object).
    assert report.synthesis_count == 3
    assert report.impact is not None and report.impact.found
    assert {p: p.read_bytes() for p in kb_root.rglob("*.md")} == before


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_dream_prints_synthesis_candidates(kb_root: Path, capsys):
    assert main(["dream", str(kb_root)]) == 0
    out = capsys.readouterr().out
    assert "synthesis_candidates:    1" in out
    assert "entity:acme-corp + entity:lumio-overview" in out
    assert "one-way-claim: uses" in out
    assert "nothing is authored or staged automatically" in out


def test_cli_dream_impact_block(kb_root: Path, capsys):
    _add_chain_page(kb_root)
    assert main(["dream", str(kb_root), "--impact", "Lumio Overview"]) == 0
    out = capsys.readouterr().out
    assert (
        "transitive_impact: Lumio Overview (entity entity:lumio-overview, scope=canonical)"
        in out
    )
    assert "direct_out: 1" in out
    assert "reachable_within_depth: 2" in out
    assert "depth 2: Acme Sub" in out
    # canonical scope must NOT print the discovery-only disclosure
    assert "never Evidence" not in out


def test_cli_dream_impact_discovery_scope_disclosure(kb_root: Path, capsys):
    assert (
        main(
            [
                "dream",
                str(kb_root),
                "--impact",
                "Lumio Overview",
                "--impact-scope",
                "discovery",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "scope=discovery" in out
    assert "they are never Evidence" in out


def test_cli_dream_impact_unknown_name(kb_root: Path, capsys):
    assert main(["dream", str(kb_root), "--impact", "No Such Page"]) == 0
    out = capsys.readouterr().out
    assert "transitive_impact: not resolved (No Such Page)" in out


# ---------------------------------------------------------------------------
# Skill routing contract
# ---------------------------------------------------------------------------


def test_skill_documents_synthesis_and_impact_workflow():
    from lumio_wiki.skill import resolve_skill_path

    text = resolve_skill_path().read_text(encoding="utf-8")
    assert "synthesis_candidates" in text
    assert "--impact" in text
    assert "authored or staged automatically" in text
    # The discovery-scope guardrail must sit beside the inspection workflow.
    assert "select pages to inspect" in text
