"""Direct journey test for the ``lumio-wiki`` ingestion public surface (issue #97).

Retains the existing-Knowledge-Base candidate-validation regression. The
standalone isolation and installed-wheel journeys exercise full ingest,
review, publish and discard behavior without optional dependencies.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import lumio_wiki as lw

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

RELATED_PAGE = """---
title: "Journey Related Page"
aliases: []
tags:
  - "journey"
summary: "A new page with a typed Relationship to an existing canonical title."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "jrn-related"
    title: "Journey related source"
synthetic: false
---

# Journey Related Page

Body authored by the host agent, linking to [[Lumio Overview]].
"""


def _kb(tmp_path: Path):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def test_plain_proposal_validates_against_existing_pages(tmp_path: Path):
    # Regression: a NEW page staged against an EXISTING KB was falsely
    # blocked because plain proposals validated proposed pages in isolation
    # (no KB overlay, no Control File). Staging must use the same candidate
    # gate as publish. (Typed relationship frontmatter input was removed by
    # ADR-0021; claim staging through proposals is issue #169.)
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    proposal = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store
    )
    errors = [
        issue.message for issue in proposal.validation_report.issues if issue.severity == "error"
    ]
    assert not any("unresolved relationship target" in m for m in errors), errors
    assert proposal.validation_report.is_valid
    assert not proposal.blocked
