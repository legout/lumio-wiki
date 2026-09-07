"""Direct journey test for the ``lumio-wiki`` ingestion public surface (issue #97).

Retains the existing-Knowledge-Base candidate-validation regression. The
standalone isolation and installed-wheel journeys exercise full ingest,
review, publish and discard behavior without optional dependencies.
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.publish import DestinationConflict

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


SLUG_COLLISION_EXISTING = """---
title: "A B"
aliases: []
tags:
  - "slug-collision"
summary: "Existing page whose file a slug-colliding proposal must never replace."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "ab-existing"
    title: "A B source"
synthetic: false
---

# A B

Existing content that a slug-colliding proposal must never replace.
"""

SLUG_COLLIDING_PROPOSAL = """---
title: "A_B"
aliases: []
tags:
  - "slug-collision"
summary: "A new page whose slug collides with the existing A B page."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "ab-colliding"
    title: "A_B source"
synthetic: false
---

# A_B

New page content whose slug resolves to the same a_b.md destination.
"""


def test_new_page_slug_collision_preserves_existing_page(tmp_path: Path):
    # B01 (Plan 02 / P2): a NEW page titled "A_B" slugs to a_b.md — the path
    # where the existing page "A B" lives. Publication must never silently
    # replace the existing page: candidate validation blocks the proposal,
    # publish refuses, and a direct apply raises before any byte changes.
    # No suffix allocation and no hidden removal happen either.
    kb = _kb(tmp_path)
    existing_path = kb.root / "a_b.md"
    existing_path.write_text(SLUG_COLLISION_EXISTING, encoding="utf-8")
    kb, report = lw.load_knowledge_base(kb.root)
    assert report.is_valid, report
    original = existing_path.read_text(encoding="utf-8")

    store = lw.IngestStore(tmp_path / "ingest")
    proposal = lw.create_proposal_without_provider(
        SLUG_COLLIDING_PROPOSAL.encode("utf-8"),
        "text/markdown",
        "colliding.md",
        kb,
        store=store,
    )
    errors = [
        issue.message for issue in proposal.validation_report.issues if issue.severity == "error"
    ]
    assert any("a_b.md" in message and "A B" in message for message in errors), errors
    assert proposal.blocked

    with pytest.raises(lw.ProposalBlockedError):
        lw.ProposalPipeline(kb, store=store).publish(proposal.id)
    # Original bytes and staged proposal state are intact; no suffix allocation.
    assert existing_path.read_text(encoding="utf-8") == original
    staged = store.get(proposal.id)
    assert staged is not None and staged.status == "staged"
    assert sorted(page.name for page in kb.root.glob("a_b*.md")) == ["a_b.md"]

    # The live apply branch is guarded too: writing the colliding page
    # directly raises before touching the existing file.
    colliding_page = lw.ProposedPage(
        relative_path="a_b.md",
        title="A_B",
        markdown=SLUG_COLLIDING_PROPOSAL,
    )
    with pytest.raises(lw.PublishError):
        lw.apply_proposed_pages([colliding_page], kb.root)
    assert existing_path.read_text(encoding="utf-8") == original


DUPLICATE_DESTINATION_SOURCE = """---
title: "Fresh Idea"
aliases: []
tags:
  - "duplicate-destination"
summary: "First proposed page slugging to fresh_idea.md."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "fresh-one"
    title: "Fresh idea source"
synthetic: false
---

# Fresh Idea

First page body.

<!-- lumio: page-break -->

---
title: "Fresh_Idea"
aliases: []
tags:
  - "duplicate-destination"
summary: "Second proposed page slugging to the same fresh_idea.md."
lifecycle: "draft"
visibility: "internal"
sources:
  - id: "fresh-two"
    title: "Fresh idea variant source"
synthetic: false
---

# Fresh_Idea

Second page body.
"""


def test_duplicate_proposed_destinations_are_blocked(tmp_path: Path):
    # B01 (Plan 02 / P2): two proposed pages in ONE proposal whose slugs
    # collapse to the same destination (fresh_idea.md) must not silently let
    # the last writer win. Candidate validation blocks the proposal and
    # publication refuses; no file is ever written.
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    proposal = lw.create_proposal_without_provider(
        DUPLICATE_DESTINATION_SOURCE.encode("utf-8"),
        "text/markdown",
        "duplicates.md",
        kb,
        store=store,
    )
    errors = [
        issue.message for issue in proposal.validation_report.issues if issue.severity == "error"
    ]
    assert any("fresh_idea.md" in message for message in errors), errors
    assert proposal.blocked

    with pytest.raises(lw.ProposalBlockedError):
        lw.ProposalPipeline(kb, store=store).publish(proposal.id)
    assert not (kb.root / "fresh_idea.md").exists()
    staged = store.get(proposal.id)
    assert staged is not None and staged.status == "staged"


def _probe_markdown(title: str, source_id: str) -> str:
    """Return minimal valid page Markdown for the review-fix regressions."""
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        "tags:\n"
        '  - "review-fix"\n'
        f'summary: "{title} regression probe."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{title} source"\n'
        "synthetic: false\n"
        "---\n"
        "\n"
        f"# {title}\n"
    )


def test_apply_compound_revision_merges_without_the_flag(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2): ``apply_compound_revision`` is the
    # public compound-revision writer — its additive merge is UNCONDITIONAL.
    # A caller passing an ordinary ProposedPage (compound_revision defaults to
    # False) must still get the existing page's prior Sources preserved and
    # deduplicated, not a silent evidence-trail replacement. The helper routes
    # the shared pre-write destination checks with the compound branch forced
    # for this helper only.
    kb = _kb(tmp_path)
    existing = kb.root / "overview.md"
    assert "lumio-overview" in existing.read_text(encoding="utf-8")

    page = lw.ProposedPage(
        relative_path="overview.md",
        title="Lumio Overview",
        markdown=_probe_markdown("Lumio Overview", "compound-review-fix"),
    )
    assert not page.compound_revision

    target = lw.apply_compound_revision(page, kb.root)
    assert target == existing.resolve()
    merged = target.read_text(encoding="utf-8")
    # Prior provenance preserved AND the newly informing source added.
    assert "lumio-overview" in merged
    assert "compound-review-fix" in merged


def test_move_from_directory_source_is_rejected_before_any_write(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2): a move source that is an existing
    # directory cannot be removed by the post-write unlink. Resolution used to
    # accept it, wrote the target first, and then died on IsADirectoryError —
    # leaving a partial move behind. The directory source is now rejected as a
    # DestinationConflict at resolution time, BEFORE any byte is written, and
    # candidate validation aggregates exactly that conflict as a destination
    # issue.
    kb = _kb(tmp_path)
    (kb.root / "old-dir").mkdir()
    page = lw.ProposedPage(
        relative_path="new.md",
        title="Moved",
        markdown=_probe_markdown("Moved", "moved-review-fix"),
        move_from_path="old-dir",
    )

    with pytest.raises(DestinationConflict):
        lw.apply_proposed_pages([page], kb.root)
    # No partial move: the target was never written and the source survives.
    assert not (kb.root / "new.md").exists()
    assert (kb.root / "old-dir").is_dir()

    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert any(issue.field == "destination" and "old-dir" in issue.message for issue in issues), (
        issues
    )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix domain sockets")
def test_candidate_validation_reports_socket_occupant_like_live_apply(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2): candidate validation used to copy
    # the Knowledge Base BEFORE resolving destinations, so an uncopyable
    # occupant (a Unix socket) at a proposed destination escaped as a raw
    # ``shutil.Error`` from ``copytree`` while live apply raised the intended
    # DestinationConflict. Destinations are now resolved and checked against
    # the real root first: candidate validation returns the SAME conflict as
    # an aggregated error issue, and ``copytree`` never runs for a blocked
    # proposal.
    kb = _kb(tmp_path)
    socket_path = kb.root / "a_b.md"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    try:
        page = lw.ProposedPage(
            relative_path="a_b.md",
            title="A_B",
            markdown=_probe_markdown("A_B", "ab-socket-review-fix"),
        )
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages([page], kb.root)
        report = lw.validate_candidate_knowledge_base([page], kb.root)
    finally:
        sock.close()
        socket_path.unlink(missing_ok=True)

    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    # Candidate/live parity: the identical conflict, aggregated as an issue.
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "unreadable" in issues[0].message
