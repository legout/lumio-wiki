"""Compiled Page freshness via ``review_after`` (ADR-0023, PRD-0004, issue #191).

Covers the schema decode (valid / absent / malformed), the due boundary
(``today >= review_after``), the advisory warnings in ``validate``/``lint``,
the Dream Cycle's due-page ranking, the ``status`` due count, and the OKF
Profile 2 round-trip tests live beside the other Profile 2 tests.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import lumio_wiki
from lumio_wiki.cli import main
from lumio_wiki.knowledge_base import load_knowledge_base

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

PAST = "2020-01-15"
FUTURE = "2999-12-31"


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", root)
    return root


def _set_review_after(kb_root: Path, relative: str, value: str | None) -> None:
    """Insert (or drop) a review_after line in a fixture page's frontmatter."""
    target = kb_root / relative
    text = target.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    assert end != -1, relative
    line = "" if value is None else f"review_after: {value}\n"
    target.write_text(text[:end] + "\n" + line + text[end + 1 :], encoding="utf-8")


# ---------------------------------------------------------------------------
# Schema + decode.
# ---------------------------------------------------------------------------


def test_review_after_absent_is_no_opinion(tmp_path: Path):
    kb, report = load_knowledge_base(_kb(tmp_path))
    assert all(page.review_after is None for page in kb.pages)
    assert report.is_valid
    assert not any("review" in issue.field for issue in report.issues)


def test_review_after_valid_date_decodes(tmp_path: Path):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    kb, report = load_knowledge_base(root)
    page = kb.lookup_by_title("Lumio Overview")[0]
    assert page.review_after == PAST


def test_review_after_malformed_string_is_error(tmp_path: Path):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", "not-a-date")
    _kb_report = load_knowledge_base(root)[1]
    assert not _kb_report.is_valid
    issue = next(i for i in _kb_report.issues if i.field == "review_after")
    assert issue.severity == "error"
    assert "ISO 8601" in issue.message


def test_review_after_malformed_type_is_error(tmp_path: Path):
    root = _kb(tmp_path)
    # An integer frontmatter value decodes as int, not a date.
    _set_review_after(root, "concepts/overview.md", "20270115")
    report = load_knowledge_base(root)[1]
    assert not report.is_valid
    assert any(i.field == "review_after" and i.severity == "error" for i in report.issues)


def test_review_after_datetime_is_rejected(tmp_path: Path):
    root = _kb(tmp_path)
    # A full timestamp is not an ISO 8601 *date*; YAML decodes it as datetime.
    _set_review_after(root, "concepts/overview.md", "2027-01-15T10:00:00Z")
    report = load_knowledge_base(root)[1]
    assert not report.is_valid
    assert any(i.field == "review_after" for i in report.issues)


def test_review_after_quoted_iso_string_decodes(tmp_path: Path):
    """A quoted ISO date (as OKF import renders) decodes and stays advisory.

    Regression for the Spec review finding: ``_render_canonical_page_markdown``
    double-quotes every scalar, so an imported ``review_after`` arrives as a
    YAML string. The canonical loader must accept it, not block it.
    """
    from lumio_wiki.knowledge_base import _load_page

    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", '"2020-01-15"')
    kb, report = load_knowledge_base(root)
    page = kb.lookup_by_title("Lumio Overview")[0]
    assert page.review_after == "2020-01-15"
    # Due (past date) is still only a warning, never blocking.
    assert report.is_valid
    assert any(
        i.file == "concepts/overview.md"
        and i.field == "review_after"
        and i.severity == "warning"
        for i in report.issues
    )
    # The proposed-markdown path OKF import produces loads identically.
    proposed, _data = _load_page(
        '---\ntitle: "X"\ntags: [t]\nlifecycle: draft\nvisibility: internal\n'
        'review_after: "2027-01-15"\nsynthetic: true\n---\n\n# X\n',
        "x.md",
    )
    assert proposed.review_after == "2027-01-15"


# ---------------------------------------------------------------------------
# Boundary: due = today >= review_after.
# ---------------------------------------------------------------------------


def test_due_boundary_exactly_at_date():
    from lumio_wiki.knowledge_base import is_due_for_review

    assert is_due_for_review("2027-01-15", today=date(2027, 1, 15))
    assert not is_due_for_review("2027-01-15", today=date(2027, 1, 14))
    assert is_due_for_review("2027-01-15", today=date(2027, 1, 16))
    assert not is_due_for_review(None, today=date(2027, 1, 15))


def test_due_pages_ranked_most_overdue_first(tmp_path: Path):
    from lumio_wiki.knowledge_base import due_review_pages

    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    _set_review_after(root, "entities/acme.md", "2021-06-01")
    kb, _report = load_knowledge_base(root)
    due = due_review_pages(kb.pages)
    assert [page.title for page in due] == ["Lumio Overview", "Acme Corp"]


# ---------------------------------------------------------------------------
# validate / lint: due pages are warnings, never failures.
# ---------------------------------------------------------------------------


def test_due_page_is_warning_not_failure(tmp_path: Path):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    _set_review_after(root, "entities/acme.md", FUTURE)
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    due = [i for i in report.issues if i.field == "review_after"]
    assert len(due) == 1
    issue = due[0]
    assert issue.severity == "warning"
    assert issue.file == "concepts/overview.md"
    assert PAST in issue.message
    # The fresh page carries no warning.
    assert not any(i.file == "entities/acme.md" for i in report.issues)


def test_lint_gold_reports_due_page_and_date(tmp_path: Path, capsys):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    _set_review_after(root, "entities/acme.md", FUTURE)
    assert main(["lint", str(root)]) == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "concepts/overview.md: review_after" in out and PAST in out


# ---------------------------------------------------------------------------
# Dream Cycle: due pages ranked alongside Link Candidates.
# ---------------------------------------------------------------------------


def test_dream_cycle_reports_due_pages(tmp_path: Path):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    _set_review_after(root, "entities/acme.md", FUTURE)
    acme_before = (root / "entities" / "acme.md").read_text(encoding="utf-8")
    report = lumio_wiki.run_dream_cycle(root)
    assert [page.title for page in report.due_pages] == ["Lumio Overview"]
    # Read-only: the reflection never writes.
    assert (root / "entities" / "acme.md").read_text(encoding="utf-8") == acme_before


def test_dream_cli_lists_due_for_review(tmp_path: Path, capsys):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    assert main(["dream", str(root)]) == 0
    out = capsys.readouterr().out
    assert "due_for_review:     1" in out
    assert "concepts/overview.md" in out and PAST in out


# ---------------------------------------------------------------------------
# status: due-page count.
# ---------------------------------------------------------------------------


def test_status_counts_due_pages(tmp_path: Path, capsys):
    import json

    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    _set_review_after(root, "entities/acme.md", FUTURE)
    assert main(["status", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["review_due"] == 1


def test_status_zero_due_without_field(tmp_path: Path, capsys):
    import json

    root = _kb(tmp_path)
    assert main(["status", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["review_due"] == 0


def test_status_rendered_output_includes_review_due(tmp_path: Path, capsys):
    root = _kb(tmp_path)
    _set_review_after(root, "concepts/overview.md", PAST)
    assert main(["status", str(root)]) == 0
    out = capsys.readouterr().out
    assert "review_due:" in out
