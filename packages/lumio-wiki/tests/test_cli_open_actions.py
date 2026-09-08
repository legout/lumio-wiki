"""Issue #177: CLI rendering exposes labelled, openable citation actions.

The ``search``, evidence-mode ``search``, and ``page`` outputs gain additive,
labelled open-action lines:

- ``open:``  the copyable ``lumio-wiki page "<title>"`` command;
- ``web:``   the optional Reader browser URL — present ONLY when a valid
  ``LUMIO_READER_BASE_URL`` is configured;
- ``source-url:`` / ``source-artifact:`` clearly distinct labels for the
  authored external Source URL and the explicit private-Source inspect
  command (never an implicit signed/public artifact URL, ADR-0020).

Installed-wheel journeys cover successful reads and browser/open actions;
this file retains invalid-base and private-source separation checks.
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
        f'source-artifact: lumio-wiki source inspect "{kb_root.resolve()}" '
        "--source-id lumio-overview" in out
    )
    assert "signed" not in out.lower()


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
    assert not any(scheme in line for line in lines for scheme in ("s3://", "gs://", "az://"))
