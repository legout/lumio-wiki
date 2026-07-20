"""Issue #97: the standalone ingestion -> distill -> propose -> validate ->
review -> publish -> discard journey runs from ``lumio_wiki`` alone.

Proven in a fresh subprocess that installs a meta-path finder blocking the
heavyweight optional dependencies forbidden to the base wheel by ADR-0010
(LanceDB, PyArrow, Stario, Piccolo, OpenAI, LiteParse, MarkItDown) AND the
full ``lumio`` application package (equivalent to an environment with only
``lumio-wiki`` installed), then drives the full model-free text/Markdown
ingestion journey through the package's public interfaces. Asserts the journey
succeeds AND that none of those libraries nor ``lumio`` were ever imported.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

# Authored Compiled Page Markdown the host coding agent would produce. Defined
# here in the parent (no escaping issues) and passed to the child via a temp
# file.
MARKDOWN = """---
title: "Isolation Page"
aliases: []
tags:
  - "isolation"
summary: "Authored without any model or document converter."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "iso"
    title: "Isolation source"
relationships: []
synthetic: false
---

# Isolation Page

Body authored by the host agent.
"""

_CHILD = textwrap.dedent(
    """
    import importlib.abc, shutil, sys, tempfile
    from pathlib import Path

    FIXTURES = Path(sys.argv[1])
    MARKDOWN_PATH = Path(sys.argv[2])

    class _Blocker(importlib.abc.MetaPathFinder):
        # Heavyweight optional dependencies the base wheel must NOT need, plus
        # the full ``lumio`` application: ``lumio-wiki`` stands alone. The full
        # forbidden set is dictated by ADR-0010 (LanceDB, PyArrow, Stario,
        # Piccolo, OpenAI, LiteParse, MarkItDown) plus the ``lumio`` app.
        _BLOCKED = {
            "lancedb",
            "pyarrow",
            "stario",
            "piccolo",
            "openai",
            "liteparse",
            "markitdown",
            "lumio",
        }

        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in self._BLOCKED:
                raise ImportError(f"blocked for isolation test: {name}")
            return None

    sys.meta_path.insert(0, _Blocker())

    from lumio_wiki import load_knowledge_base
    from lumio_wiki.ingest import IngestStore, create_proposal_without_provider
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    root = Path(tempfile.mkdtemp()) / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, report

    markdown = MARKDOWN_PATH.read_bytes()
    store = IngestStore(Path(tempfile.mkdtemp()) / "ingest")

    # Full journey: ingest (Source Processor) -> distill (Distiller) ->
    # propose/validate (Proposal Pipeline assemble) -> stage for review.
    proposal = create_proposal_without_provider(
        markdown, "text/markdown", "iso.md", kb, store=store
    )
    assert proposal.status == "staged"
    assert proposal.validation_report.is_valid
    assert proposal.provenance.converted_by == "markdown"
    assert proposal.affected_pages == ["Isolation Page"]
    assert proposal.blast_radius is not None

    # Review surface: list + review by id.
    assert [p.id for p in ProposalPipeline(kb, store=store).list()] == [proposal.id]
    assert ProposalPipeline(kb, store=store).review(proposal.id).id == proposal.id

    # Publish through the pipeline so the page is written to the Knowledge Base
    # root and the proposal is marked terminal.
    pipeline = ProposalPipeline(kb, store=store)
    published = pipeline.publish(proposal.id)
    assert published.status == "published"
    assert (kb.root / "isolation_page.md").exists()

    # Discard journey is exercised on a second proposal: stage then discard.
    discardable = create_proposal_without_provider(
        markdown, "text/markdown", "iso2.md", kb, store=store
    )
    discarded = pipeline.discard(discardable.id)
    assert discarded.status == "discarded"
    assert pipeline.discard(discardable.id) is None

    for blocked in (
        "lancedb",
        "pyarrow",
        "stario",
        "piccolo",
        "openai",
        "liteparse",
        "markitdown",
        "lumio",
    ):
        assert blocked not in sys.modules, f"{blocked} was imported"

    print("ISOLATION_OK")
    """
)


def test_standalone_ingest_journey_runs_from_lumio_wiki_without_optionals(tmp_path):
    markdown_path = tmp_path / "iso.md"
    markdown_path.write_text(MARKDOWN, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(FIXTURES), str(markdown_path)],
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert result.returncode == 0, (
        f"child failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "ISOLATION_OK" in result.stdout
    assert "ImportError" not in result.stderr
