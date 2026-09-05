"""TDD spec for Dream duplicate-identity candidates (t_3327f75e).

Read-only, bounded, stably ranked possible-duplicate Entity candidates on the
Dream report. Deterministic signals only (exact surfaces, token-overlap,
same-type); advisory always — the explicit Entity Merge proposal stays the
only mutation path (ADR-0021: automatic merging is forbidden).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.cli import main

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

DUP_PAGE = """---
id: "{id}"
title: "{title}"
entity_types:
  - {entity_type}
tags:
  - "entity"
summary: "A near-duplicate page for the Dream duplicate-candidate tests."
lifecycle: "approved"
visibility: "public"
type: "entity"
sources:
  - id: "{source_id}"
    title: "Duplicate-flavored registry"
---

# {title}

A duplicate-flavored page used by the Dream duplicate-candidate tests.
"""


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


def _add_page(kb_root: Path, name: str, entity_id: str, title: str) -> Path:
    path = kb_root / "entities" / name
    path.write_text(
        DUP_PAGE.format(
            id=entity_id, title=title, entity_type="organization", source_id=entity_id
        ),
        encoding="utf-8",
    )
    return path


def _add_alias(kb_root: Path, page_name: str, alias: str) -> None:
    path = kb_root / "entities" / page_name
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'type: "entity"', f'aliases:\n  - "{alias}"\ntype: "entity"', 1
    )
    path.write_text(text, encoding="utf-8")


def _pairs(report):
    return {
        tuple(sorted((c.retired_entity_id, c.surviving_entity_id)))
        for c in report.duplicate_candidates
    }


# ---------------------------------------------------------------------------
# Candidate discovery (SDK, read-only)
# ---------------------------------------------------------------------------


def test_alias_signal_between_two_entities(kb_root: Path):
    _add_page(kb_root, "acme-dup.md", "entity:acme-dup", "Acme Corporation")
    _add_alias(kb_root, "acme-dup.md", "Acme Corp")
    _add_alias(kb_root, "acme.md", "Acme Corp")
    report = lw.run_dream_cycle(kb_root)
    assert _pairs(report) == {("entity:acme-corp", "entity:acme-dup")}
    candidate = report.duplicate_candidates[0]
    assert "shared-alias: Acme Corp" in candidate.signals
    assert candidate.signals_rank == 2


def test_no_candidates_in_clean_kb(kb_root: Path):
    report = lw.run_dream_cycle(kb_root)
    assert report.duplicate_candidates == ()
    assert report.duplicate_count == 0


def test_token_overlap_signal_without_alias(kb_root: Path):
    _add_page(kb_root, "acme-near.md", "entity:acme-near", "Acme Corp Group")
    report = lw.run_dream_cycle(kb_root)
    candidate = next(
        c
        for c in report.duplicate_candidates
        if {c.retired_entity_id, c.surviving_entity_id}
        == {"entity:acme-corp", "entity:acme-near"}
    )
    assert candidate.signals_rank == 1
    assert candidate.signals == ("token-overlap: acme corp",)


def test_token_overlap_never_crosses_entity_type(kb_root: Path):
    _add_page(kb_root, "acme-org.md", "entity:acme-org", "Acme Corp Group")
    path = kb_root / "entities" / "acme-org.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("entity_type: organization", "x", 1).replace(
            "  - organization", "  - concept", 1
        ),
        encoding="utf-8",
    )
    report = lw.run_dream_cycle(kb_root)
    assert _pairs(report) == set()


def test_candidates_stably_ranked_strongest_first(kb_root: Path):
    """Rank desc, then Entity IDs asc — a stable, deterministic order."""
    _add_page(kb_root, "acme-a.md", "entity:acme-a", "Acme Corporation")
    _add_page(kb_root, "acme-b.md", "entity:acme-b", "Acme Corp Group")
    _add_alias(kb_root, "acme-b.md", "Acme Corporation")
    report = lw.run_dream_cycle(kb_root)
    candidates = report.duplicate_candidates
    assert len(candidates) >= 2
    ranks = [c.signals_rank for c in candidates]
    assert ranks == sorted(ranks, reverse=True)
    keys = [(c.retired_entity_id, c.surviving_entity_id) for c in candidates]
    assert keys == sorted(keys)


def test_candidates_bounded_by_limit(kb_root: Path):
    for n in range(lw.DUPLICATE_CANDIDATE_LIMIT + 2):
        _add_page(kb_root, f"acme-{n}.md", f"entity:acme-{n}", "Acme Corp")
    report = lw.run_dream_cycle(kb_root)
    assert report.duplicate_count == lw.DUPLICATE_CANDIDATE_LIMIT


def test_duplicate_entity_ids_are_not_self_candidates(kb_root: Path):
    """Invalid shared Entity IDs are a validation error, never a candidate."""
    _add_page(kb_root, "acme-copy.md", "entity:acme-corp", "Acme Copy")
    report = lw.run_dream_cycle(kb_root)
    assert _pairs(report) == set()


def test_legacy_pages_without_entity_id_excluded(kb_root: Path):
    path = _add_page(kb_root, "acme-near.md", "entity:acme-near", "Acme Corp Group")
    text = path.read_text(encoding="utf-8").replace('id: "entity:acme-near"', "id: ''")
    path.write_text(text, encoding="utf-8")
    report = lw.run_dream_cycle(kb_root)
    assert _pairs(report) == set()


def test_synthetic_pages_excluded(kb_root: Path):
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_duplicate_entity_candidates

    _add_page(kb_root, "acme-dup.md", "entity:acme-dup", "Acme Corporation")
    kb, _ = load_knowledge_base(kb_root)
    assert find_duplicate_entity_candidates(kb.pages)
    for page in kb.pages:
        object.__setattr__(page, "synthetic", True)
    assert find_duplicate_entity_candidates(kb.pages) == []


def test_finder_is_pure_over_given_pages(kb_root: Path):
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.maintenance import find_duplicate_entity_candidates

    _add_page(kb_root, "acme-dup.md", "entity:acme-dup", "Acme Corporation")
    kb, _ = load_knowledge_base(kb_root)
    first = find_duplicate_entity_candidates(kb.pages)
    second = find_duplicate_entity_candidates(kb.pages)
    assert first == second
    assert [c.signals for c in first] == [c.signals for c in second]


# ---------------------------------------------------------------------------
# Read-only guarantee: the explicit merge proposal stays the only mutation
# ---------------------------------------------------------------------------


def test_discovery_never_writes_and_merge_path_still_works(kb_root: Path):
    _add_page(kb_root, "acme-dup.md", "entity:acme-dup", "Acme Corporation")
    before = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    report = lw.run_dream_cycle(kb_root)
    assert report.duplicate_count == 1
    assert {p: p.read_bytes() for p in kb_root.rglob("*.md")} == before
    # The candidate feeds the EXISTING explicit preview/proposal path (no new
    # staging seam): proposing a merge is unchanged and reviewable.
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    kb, _ = load_knowledge_base(kb_root)
    proposal = ProposalPipeline(kb, store=_store(kb_root)).propose_entity_merge(
        "entity:acme-dup", "entity:acme-corp", reason="reviewed duplicate candidate"
    )
    assert proposal.status == "staged"
    assert not proposal.blocked


def test_dream_report_field_defaults_to_empty():
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
    assert report.duplicate_candidates == ()
    assert report.duplicate_count == 0


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_dream_prints_duplicate_candidates(kb_root: Path, capsys):
    _add_page(kb_root, "acme-dup.md", "entity:acme-dup", "Acme Corporation")
    assert main(["dream", str(kb_root)]) == 0
    out = capsys.readouterr().out
    assert "duplicate_candidates:    1" in out
    assert "entity:acme-corp <- entity:acme-dup" in out
    assert "token-overlap: acme" in out
    assert "merge-entity" in out


def test_cli_dream_clean_kb_mentions_nothing(kb_root: Path, capsys):
    assert main(["dream", str(kb_root)]) == 0
    out = capsys.readouterr().out
    assert "duplicate_candidates:    0" in out
    assert "merge-entity" not in out


# ---------------------------------------------------------------------------
# Skill routing contract: reviewed candidates are routed to merge-entity
# ---------------------------------------------------------------------------


def test_skill_routes_duplicate_candidates_to_merge_entity():
    from lumio_wiki.skill import resolve_skill_path

    text = resolve_skill_path().read_text(encoding="utf-8")
    assert "duplicate-identity candidates" in text
    assert "merge-entity" in text
    # The never-mutates guardrail must sit beside the routing instruction.
    assert "nothing merges or stages automatically" in text
