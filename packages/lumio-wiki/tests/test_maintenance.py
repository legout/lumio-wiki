"""Tests for the portable Maintainer workflows (ADR-0015).

Covers ``lumio_wiki.maintenance`` (lint, cross-link staging, Dream Cycle) and
the ``lumio-wiki lint|cross-link|dream`` CLI verbs. Everything is model-free
and proposal-first: lint/dream reflection is read-only, and staging produces
reviewable Ingest Proposals that never touch the Knowledge Base on disk.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.cli import main
from lumio_wiki.ingest import IngestStore
from lumio_wiki.publish import merge_compound_sources

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the categorized fixture into a writable Knowledge Base root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", root)
    return root


@pytest.fixture
def kb_with_candidate(kb_root: Path) -> Path:
    """A categorized KB whose overview page mentions Acme Corp UNLINKED."""
    overview = kb_root / "concepts" / "overview.md"
    text = overview.read_text(encoding="utf-8")
    overview.write_text(text.rstrip() + "\n\nAcme Corp is a launch customer.\n", encoding="utf-8")
    return kb_root


def _store(kb_root: Path) -> IngestStore:
    store_dir = kb_root / ".lumio" / "ingest"
    store_dir.mkdir(parents=True, exist_ok=True)
    return IngestStore(store_dir)


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def test_run_lint_reports_both_scopes(kb_root: Path):
    report = lw.run_lint(kb_root)
    assert report.is_valid
    assert report.page_count == 2
    assert report.canonical_structure.scope == "canonical"
    assert report.discovery_structure.scope == "discovery"
    assert "canonical" in report.scope_disclosure
    assert "discovery" in report.scope_disclosure


def test_run_lint_is_read_only(kb_root: Path):
    before = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    lw.run_lint(kb_root)
    after = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    assert before == after


# ---------------------------------------------------------------------------
# Cross-link repair helpers
# ---------------------------------------------------------------------------


def test_repair_mention_wraps_verbatim_text():
    md = "We use Beta for storage.\n"
    candidate = lw.find_link_candidates(
        [
            _compiled_page("Alpha", body="We use Beta for storage.\n", path="concepts/alpha.md"),
            _compiled_page("Beta", path="entities/beta.md"),
        ]
    )[0]
    repaired = lw.repair_mention(md, candidate)
    assert "[Beta](../entities/beta.md)" in repaired


def _compiled_page(title: str, *, body: str = "", path: str | None = None):
    from lumio_wiki.records import CompiledPage, Source

    return CompiledPage(
        path=path or f"{title.lower()}.md",
        title=title,
        aliases=[],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        body=body,
        body_start_line=1,
    )


def test_mark_compound_revision_restates_routing_fields():
    md = "---\ntitle: P\n---\n\nbody\n"
    marked = lw.mark_compound_revision(md, category="concepts", durability_rationale="kept")
    data, _body, _ = lw.parse_frontmatter(marked, Path("p.md"))
    assert data["compound_revision"] is True
    assert data["category"] == "concepts"
    assert data["durability_rationale"] == "kept"


def test_category_for_page_path(kb_root: Path):
    kb, _report = lw.load_knowledge_base(kb_root)
    assert lw.category_for_page_path(kb, "entities/acme.md") == "entities"
    assert lw.category_for_page_path(kb, "acme.md") is None
    assert lw.category_for_page_path(kb, "unknown/acme.md") is None


# ---------------------------------------------------------------------------
# Cross-link staging (compound revision through the Proposal Pipeline)
# ---------------------------------------------------------------------------


def test_stage_cross_link_proposal_is_publishable(kb_with_candidate: Path):
    """A staged repair validates clean against a categorized KB.

    Regression: compound revisions of pages with id-less sources were falsely
    blocked (merged sources came back empty), and categorized revisions were
    blocked for missing category / durability rationale routing fields.
    """
    kb_root = kb_with_candidate
    # Strip the source id so the page carries id-less provenance (valid).
    acme = kb_root / "entities" / "acme.md"
    acme.write_text(
        acme.read_text(encoding="utf-8").replace('  - id: "acme"\n', "  - "),
        encoding="utf-8",
    )
    kb, _report = lw.load_knowledge_base(kb_root)
    candidates = lw.find_link_candidates(kb.pages)
    assert len(candidates) == 1

    proposal = lw.stage_cross_link_proposal(kb_root, candidates[0], store=_store(kb_root))

    assert proposal.status == "staged"
    assert not proposal.blocked, [
        f"{i.file}: {i.field}: {i.message}"
        for i in proposal.validation_report.issues
        if i.severity == "error"
    ]
    # The Knowledge Base on disk is untouched (proposal-first).
    assert "[Acme Corp]" not in (kb_root / "concepts" / "overview.md").read_text(encoding="utf-8")


def test_stage_relationship_proposal(kb_root: Path):
    proposal = lw.stage_relationship_proposal(
        kb_root,
        "Lumio Overview",
        "Acme Corp",
        "references",
        store=_store(kb_root),
    )
    assert proposal.status == "staged"
    assert not proposal.blocked


# ---------------------------------------------------------------------------
# Dream Cycle
# ---------------------------------------------------------------------------


def test_run_dream_cycle_reflects_without_writing(kb_with_candidate: Path):
    before = {p: p.read_bytes() for p in kb_with_candidate.rglob("*.md")}
    report = lw.run_dream_cycle(kb_with_candidate)
    assert report.is_valid
    assert report.candidate_count == 1
    ranked = report.ranked_candidates[0]
    assert ranked.candidate.target_title == "Acme Corp"
    after = {p: p.read_bytes() for p in kb_with_candidate.rglob("*.md")}
    assert before == after


def test_stage_dream_repairs_stages_bounded_proposals(kb_with_candidate: Path):
    result = lw.stage_dream_repairs(kb_with_candidate, store=_store(kb_with_candidate), limit=1)
    assert len(result.staged) == 1
    assert not result.skipped
    assert result.staged[0].status == "staged"
    assert not result.staged[0].blocked


def test_stage_dream_repairs_rejects_bad_limit(kb_with_candidate: Path):
    with pytest.raises(lw.MaintenanceError):
        lw.stage_dream_repairs(kb_with_candidate, store=_store(kb_with_candidate), limit=0)


# ---------------------------------------------------------------------------
# merge_compound_sources regression: id-less sources are preserved
# ---------------------------------------------------------------------------


def test_merge_compound_sources_preserves_idless_sources():
    existing = textwrap.dedent(
        """\
        ---
        title: "P"
        sources:
          - title: "Medium post"
            url: "https://example.com/post"
          - title: "Spec repo"
            url: "https://example.com/spec"
        ---

        old body
        """
    )
    proposed = textwrap.dedent(
        """\
        ---
        title: "P"
        sources:
          - title: "Medium post"
            url: "https://example.com/post"
        ---

        new body
        """
    )
    merged = merge_compound_sources(proposed, existing)
    data, body, _ = lw.parse_frontmatter(merged, Path("p.md"))
    sources = lw.as_sources(data.get("sources"))
    titles = [s.title for s in sources]
    assert titles == ["Medium post", "Spec repo"], (
        "id-less existing sources are preserved, not dropped"
    )
    assert "new body" in body


# ---------------------------------------------------------------------------
# CLI verbs
# ---------------------------------------------------------------------------


def test_cli_lint_reports_and_exits_zero(kb_root: Path, capsys):
    rc = main(["lint", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "valid:               True" in out
    assert "scope_disclosure:" in out
    assert "structure_scope:     canonical" in out
    assert "structure_scope:     discovery" in out


def test_cli_lint_exit_one_on_invalid(tmp_path: Path, capsys):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "invalid", root)
    rc = main(["lint", str(root)])
    assert rc == 1


def test_cli_cross_link_lists_candidates(kb_with_candidate: Path, capsys):
    rc = main(["cross-link", str(kb_with_candidate)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "link_candidates:     1" in out
    assert "Lumio Overview -> Acme Corp" in out
    assert "--stage" in out


def test_cli_cross_link_stage(kb_with_candidate: Path, capsys):
    rc = main(["cross-link", str(kb_with_candidate), "--stage"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staged_proposals:    1" in out
    assert "[Acme Corp]" not in (kb_with_candidate / "concepts" / "overview.md").read_text(
        encoding="utf-8"
    )


def test_cli_dream_reflects(kb_with_candidate: Path, capsys):
    rc = main(["dream", str(kb_with_candidate)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Dream Cycle" in out
    assert "link_candidates:     1" in out


def test_cli_dream_stage(kb_with_candidate: Path, capsys):
    rc = main(["dream", str(kb_with_candidate), "--stage", "--limit", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staged_proposals:    1" in out
