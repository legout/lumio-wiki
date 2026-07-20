"""Direct journey test for the ``lumio-wiki`` ingestion public surface (issue #97).

Exercises the full ingestion -> distill -> propose -> validate -> review ->
publish -> discard journey through the package's top-level exports, locking in
the public wiring (``__init__`` re-exports) without the subprocess overhead of
the isolation test. The subprocess test remains the authoritative proof that
the journey runs without LiteParse / MarkItDown / OpenAI / the full ``lumio``
app; this test documents the intended public usage and catches wiring
regressions fast.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lumio_wiki as lw

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

PAGE = """---
title: "Journey Page"
aliases: []
tags:
  - "journey"
summary: "Authored Markdown staged through the lumio-wiki journey."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "jrn"
    title: "Journey source"
relationships: []
synthetic: false
---

# Journey Page

Body authored by the host agent.
"""


def _kb(tmp_path: Path):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def test_public_surface_exposes_the_journey_interfaces():
    # The three ingestion interfaces are part of the package public surface.
    for name in (
        "TextMarkdownSourceProcessor",
        "PassthroughMarkdownDistiller",
        "ProposalPipeline",
        "IngestStore",
        "IngestProposal",
        "ProposedPage",
        "SourceProvenance",
        "create_proposal_without_provider",
        "apply_proposed_pages",
        "validate_candidate_knowledge_base",
        "is_reviewable_proposal",
    ):
        assert hasattr(lw, name), f"lumio_wiki must export {name}"


def test_full_journey_runs_end_to_end_through_lumio_wiki(tmp_path: Path):
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")

    # ingest (Source Processor) -> distill (Distiller) -> propose + validate.
    proposal = lw.create_proposal_without_provider(
        PAGE.encode("utf-8"), "text/markdown", "journey.md", kb, store=store
    )
    assert proposal.status == "staged"
    assert proposal.validation_report.is_valid
    assert proposal.provenance.converted_by == "markdown"
    assert proposal.affected_pages == ["Journey Page"]
    assert isinstance(proposal, lw.IngestProposal)
    assert isinstance(proposal.proposed_pages[0], lw.ProposedPage)
    assert isinstance(proposal.provenance, lw.SourceProvenance)

    # review surface: list + review by id.
    pipeline = lw.ProposalPipeline(kb, store=store)
    assert [p.id for p in pipeline.list()] == [proposal.id]
    assert pipeline.review(proposal.id).id == proposal.id

    # publish writes the page and marks the proposal terminal.
    published = pipeline.publish(proposal.id)
    assert published.status == "published"
    assert (kb.root / "journey_page.md").exists()

    # discard transitions a second proposal to terminal.
    discardable = lw.create_proposal_without_provider(
        PAGE.encode("utf-8"), "text/markdown", "journey2.md", kb, store=store
    )
    assert lw.is_reviewable_proposal(discardable)
    discarded = pipeline.discard(discardable.id)
    assert discarded.status == "discarded"
    assert not lw.is_reviewable_proposal(discarded)
