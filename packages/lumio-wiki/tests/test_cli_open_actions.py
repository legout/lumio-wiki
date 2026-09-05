"""Issue #177: CLI rendering exposes labelled, openable citation actions.

The ``search``, evidence-mode ``search``, and ``page`` outputs gain additive,
labelled open-action lines:

- ``open:``  the copyable ``lumio-wiki page "<title>"`` command;
- ``web:``   the optional Reader browser URL — present ONLY when a valid
  ``LUMIO_READER_BASE_URL`` is configured;
- ``source-url:`` / ``source-artifact:`` clearly distinct labels for the
  authored external Source URL and the explicit private-Source inspect
  command (never an implicit signed/public artifact URL, ADR-0020).

Existing grounding output (path/entity/score/snippet/source lines) stays
byte-stable, so agents parsing the prior contract keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki.citation_actions import (
    citation_open_actions,
    render_open_actions,
)
from lumio_wiki.cli import main

FIXTURES = Path(__file__).parents[3] / "tests" / "fixtures"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the valid fixture into a writable Knowledge Base root."""
    import shutil

    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    return root


# ---------------------------------------------------------------------------
# search (page results)
# ---------------------------------------------------------------------------


def test_search_prints_copyable_open_command(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["search", str(kb_root), "Lumio", "--limit", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'open:            lumio-wiki page "Lumio Overview"' in out


def test_search_hides_browser_links_without_a_configured_base_url(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("LUMIO_READER_BASE_URL", raising=False)
    rc = main(["search", str(kb_root), "Lumio", "--limit", "3"])
    assert rc == 0
    assert "web:" not in capsys.readouterr().out


def test_search_prints_reader_url_when_base_url_configured(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LUMIO_READER_BASE_URL", "https://lumio.example.com/")
    rc = main(["search", str(kb_root), "Lumio", "--limit", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "web:             https://lumio.example.com/kb/page/Lumio%20Overview" in out


def test_search_rejects_an_invalid_reader_base_url(
    kb_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    # An object-store URI is private storage, never a public browser URL.
    monkeypatch.setenv("LUMIO_READER_BASE_URL", "s3://bucket/kb")
    rc = main(["search", str(kb_root), "Lumio", "--limit", "3"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_READER_BASE_URL" in err
    assert "http" in err


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def test_page_prints_open_command_and_reader_url(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LUMIO_READER_BASE_URL", "https://lumio.example.com")
    rc = main(["page", str(kb_root), "Lumio Overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'open:            lumio-wiki page "Lumio Overview"' in out
    assert "web:             https://lumio.example.com/kb/page/Lumio%20Overview" in out


def test_page_labels_authored_source_url_and_private_source_action(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("LUMIO_READER_BASE_URL", raising=False)
    rc = main(["page", str(kb_root), "Lumio Overview"])
    assert rc == 0
    out = capsys.readouterr().out
    # Authored external Source URL is a distinct, labelled line.
    assert "source-url:      https://example.com/lumio" in out
    # The private Source action is the explicit inspect command, never a URL.
    assert (
        "source-artifact: lumio-wiki source inspect --source-id lumio-overview" in out
    )
    assert "signed" not in out.lower()


def test_page_keeps_existing_grounding_lines_stable(
    kb_root: Path, capsys: pytest.CaptureFixture[str]
):
    """Grounding/provenance fields stay backward-compatible (issue #177 AC)."""
    rc = main(["page", str(kb_root), "Lumio Overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "path:        overview.md" in out
    assert "source:      lumio-overview — Lumio public landing page" in out


# ---------------------------------------------------------------------------
# evidence results (semantic/hybrid search renderer)
# ---------------------------------------------------------------------------


def _evidence_result(page_title: str, page_path: str, source_id: str | None):
    from lumio_wiki.evidence import retrieval_result_from_evidence
    from lumio_wiki.records import Evidence, RetrievalTrace

    body = "Supporting passage for the citation."
    evidence = Evidence(
        id=f"{page_path}#L3-3",
        source_type="compiled_markdown",
        page_path=page_path,
        page_title=page_title,
        line_start=3,
        line_end=3,
        text=body,
    )
    return retrieval_result_from_evidence(
        evidence,
        source=source_id,
        score=1.0,
        reason="test",
        trace=RetrievalTrace(),
    )


def test_evidence_renderer_prints_labelled_open_actions(
    kb_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    from lumio_wiki.cli import _print_evidence_results
    from lumio_wiki.knowledge_base import load_knowledge_base

    monkeypatch.setenv("LUMIO_READER_BASE_URL", "https://lumio.example.com")
    kb, _ = load_knowledge_base(kb_root)
    result = _evidence_result("Lumio Overview", "overview.md", "lumio-overview")
    _print_evidence_results([result], kb=kb, reader_base_url="https://lumio.example.com")
    out = capsys.readouterr().out
    assert 'open:            lumio-wiki page "Lumio Overview"' in out
    assert "web:             https://lumio.example.com/kb/page/Lumio%20Overview" in out
    assert "source-url:      https://example.com/lumio" in out
    assert (
        "source-artifact: lumio-wiki source inspect --source-id lumio-overview" in out
    )


def test_evidence_renderer_without_kb_or_base_url_stays_concise(
    capsys: pytest.CaptureFixture[str]
):
    from lumio_wiki.cli import _print_evidence_results

    result = _evidence_result("Lumio Overview", "overview.md", None)
    _print_evidence_results([result])
    out = capsys.readouterr().out
    assert 'open:            lumio-wiki page "Lumio Overview"' in out
    assert "web:" not in out
    assert "source-url:" not in out
    assert "source-artifact:" not in out


# ---------------------------------------------------------------------------
# rendered actions never leak object-store URLs (issue #177 AC)
# ---------------------------------------------------------------------------


def test_rendered_open_actions_never_contain_object_store_urls():
    actions = citation_open_actions(
        page_title="T",
        page_path="t.md",
        source_id="s",
        source_url="https://example.com/doc.pdf",
    )
    lines = render_open_actions(actions)
    assert not any(
        scheme in line for line in lines for scheme in ("s3://", "gs://", "az://")
    )
