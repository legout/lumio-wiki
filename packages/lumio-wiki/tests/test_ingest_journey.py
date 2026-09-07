"""Direct journey test for the ``lumio-wiki`` ingestion public surface (issue #97).

Retains the existing-Knowledge-Base candidate-validation regression. The
standalone isolation and installed-wheel journeys exercise full ingest,
review, publish and discard behavior without optional dependencies.
"""

from __future__ import annotations

import os
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


def test_move_source_owned_by_another_page_or_control_file_is_rejected(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v3 blocker 1): the move
    # branch accepted ANY existing non-directory move_from_path, wrote the
    # target, and then unlinked the "source" — so a new "Hijacker" page
    # "moving from overview.md" validated, created hijacker.md, and DELETED
    # the existing Lumio Overview page (an undeclared removal); the Control
    # File was equally within reach. An EXISTING move source must be the
    # recorded path of the proposed page's own title (title/path identity
    # map); anything else — another page's file, lumio.yaml, any untracked
    # file — is rejected at resolution time, BEFORE any byte is written.
    kb = _kb(tmp_path)
    overview = kb.root / "overview.md"
    original = overview.read_text(encoding="utf-8")
    hijacker = lw.ProposedPage(
        relative_path="hijacker.md",
        title="Hijacker",
        markdown=_probe_markdown("Hijacker", "hijacker-move-review-fix"),
        move_from_path="overview.md",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([hijacker], kb.root)
    # Nothing written, nothing removed: overview.md is intact, no hijacker.md.
    assert overview.read_text(encoding="utf-8") == original
    assert not (kb.root / "hijacker.md").exists()

    # Candidate validation returns the SAME blocking destination issue.
    report = lw.validate_candidate_knowledge_base([hijacker], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "overview.md" in issues[0].message
    assert "Lumio Overview" in issues[0].message

    # The Control File is not a page, so no title owns it: a move source of
    # lumio.yaml is rejected too, and the Control File survives untouched.
    control = kb.root / "lumio.yaml"
    lw.write_control_file(kb.root, lw.KnowledgeBaseControlFile(version=1))
    assert control.is_file()
    control_original = control.read_text(encoding="utf-8")
    thief = lw.ProposedPage(
        relative_path="thief.md",
        title="Thief",
        markdown=_probe_markdown("Thief", "thief-move-review-fix"),
        move_from_path="lumio.yaml",
    )
    with pytest.raises(DestinationConflict):
        lw.apply_proposed_pages([thief], kb.root)
    assert control.read_text(encoding="utf-8") == control_original
    assert not (kb.root / "thief.md").exists()


def test_valid_move_of_owned_source_and_missing_source_still_behave(tmp_path: Path):
    # Positive control for the move-source preflight: the new ownership and
    # type guards must not disturb legitimate moves. An explicit move whose
    # source IS the recorded path of the proposed title relocates the page
    # atomically (target written, source unlinked) and candidate validation
    # accepts it; a move whose source is MISSING stays tolerated (target
    # written, nothing to unlink), exactly as before.
    kb = _kb(tmp_path)
    page = lw.ProposedPage(
        relative_path="moved/architecture.md",
        title="Architecture",
        markdown=_probe_markdown("Architecture", "arch-move-review-fix"),
        move_from_path="architecture.md",
    )
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert report.is_valid, report

    lw.apply_proposed_pages([page], kb.root)
    assert not (kb.root / "architecture.md").exists()
    assert (kb.root / "moved/architecture.md").is_file()

    fresh = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-ghost-move-review-fix"),
        move_from_path="ghost/missing.md",
    )
    lw.apply_proposed_pages([fresh], kb.root)
    assert (kb.root / "fresh.md").is_file()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix domain sockets")
def test_move_source_socket_is_rejected_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v3 blocker 2): the move
    # branch only rejected DIRECTORY sources, so an existing move source that
    # was a special file (a Unix socket) slipped through resolution; candidate
    # validation then died with a raw ``shutil.Error`` from ``copytree`` (the
    # socket cannot be copied) while live apply wrote the target and UNLINKED
    # the socket. Move sources are now preflighted with the same nonblocking
    # type probe as occupants — only a regular, readable file may be moved —
    # so the DestinationConflict raises before any write/unlink, and candidate
    # validation returns the SAME blocking destination issue instead of a
    # copytree failure.
    kb = _kb(tmp_path)
    socket_path = kb.root / "source.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(socket_path))
    try:
        page = lw.ProposedPage(
            relative_path="moved.md",
            title="Moved",
            markdown=_probe_markdown("Moved", "moved-socket-review-fix"),
            move_from_path="source.sock",
        )
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages([page], kb.root)
        report = lw.validate_candidate_knowledge_base([page], kb.root)
        # No target write, no source unlink: the socket survives untouched.
        assert not (kb.root / "moved.md").exists()
        assert socket_path.is_socket()
    finally:
        sock.close()
        socket_path.unlink(missing_ok=True)

    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "source.sock" in issues[0].message
    assert "special file" in issues[0].message


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_move_source_fifo_is_rejected_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Review-fix regression (Plan 02 / P2, review v3 blocker 2): a FIFO move
    # source used to pass the directory-only move guard. The preflight now
    # types the source with a nonblocking stat FIRST and rejects every
    # non-regular special file, so a FIFO source is never opened (an open(2)
    # on a FIFO blocks until a writer appears and would hang the suite), live
    # apply raises DestinationConflict before any write/unlink, and candidate
    # validation aggregates the same blocking destination issue. The guarded
    # Path.open below turns a regression back into a fast failure instead of
    # a hung suite.
    kb = _kb(tmp_path)
    fifo_path = kb.root / "source.fifo"
    os.mkfifo(fifo_path)
    real_open = Path.open

    def _never_open_the_fifo(self, *args, **kwargs):
        if self == fifo_path:
            raise AssertionError("the preflight must never open a FIFO move source")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _never_open_the_fifo)
    page = lw.ProposedPage(
        relative_path="moved.md",
        title="Moved",
        markdown=_probe_markdown("Moved", "moved-fifo-review-fix"),
        move_from_path="source.fifo",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    report = lw.validate_candidate_knowledge_base([page], kb.root)

    # The FIFO source was never opened, never removed, and the target was
    # never written.
    assert fifo_path.is_fifo()
    assert not (kb.root / "moved.md").exists()
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "special file" in issues[0].message


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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_fifo_occupant_is_rejected_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Review-fix regression (Plan 02 / P2): the occupied-target preflight used
    # to probe readability by OPENING every non-directory occupant. A FIFO
    # occupant blocked that open(2) forever (its read side waits for a writer
    # that never comes), hanging the preflight before any conflict could be
    # raised. The preflight now types the occupant with a nonblocking stat
    # FIRST and rejects every non-regular special file: a FIFO is never
    # opened, live apply raises DestinationConflict promptly, and candidate
    # validation aggregates the same blocking destination issue. The guarded
    # Path.open below turns a regression back into a fast failure (instead of
    # a hung suite) if the preflight ever opens the FIFO again.
    kb = _kb(tmp_path)
    fifo_path = kb.root / "a_b.md"
    os.mkfifo(fifo_path)
    real_open = Path.open

    def _never_open_the_fifo(self, *args, **kwargs):
        if self == fifo_path:
            raise AssertionError("the preflight must never open a FIFO occupant")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _never_open_the_fifo)
    page = lw.ProposedPage(
        relative_path="a_b.md",
        title="A_B",
        markdown=_probe_markdown("A_B", "ab-fifo-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    report = lw.validate_candidate_knowledge_base([page], kb.root)

    # The FIFO occupant was never opened and never replaced.
    assert fifo_path.is_fifo()
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "special file" in issues[0].message


def test_blocked_ancestor_parent_prevents_any_partial_write(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2): a proposed destination whose
    # parent component is an existing non-directory (a page file where a
    # subdirectory would be needed) used to fail with a raw
    # NotADirectoryError/FileExistsError from mkdir(parents=True) in the APPLY
    # phase — after earlier pages of the same proposal had already been
    # written. The shared preflight now checks every parent component of
    # every proposed destination (and of every move source, removal target,
    # and the Control File destination) BEFORE any write: the conflict raises
    # up front, the earlier VALID page is never written, and candidate
    # validation returns the conflict as a destination issue instead of
    # leaking the filesystem exception. Missing parents stay creatable.
    kb = _kb(tmp_path)
    assert (kb.root / "overview.md").is_file()
    first = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-ancestor-review-fix"),
    )
    blocked = lw.ProposedPage(
        relative_path="overview.md/extra/inner.md",
        title="Inner",
        markdown=_probe_markdown("Inner", "inner-ancestor-review-fix"),
    )

    with pytest.raises(DestinationConflict):
        lw.apply_proposed_pages([first, blocked], kb.root)
    # The valid page came FIRST in proposal order: it must still not exist,
    # proving the preflight ran before ANY page write.
    assert not (kb.root / "fresh.md").exists()
    assert not (kb.root / "overview.md").is_dir()
    assert (kb.root / "overview.md").is_file()

    report = lw.validate_candidate_knowledge_base([first, blocked], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert issues and all(issue.field == "destination" for issue in issues), issues
    assert any("overview.md" in issue.message for issue in issues), issues


def test_control_file_directory_destination_is_rejected_before_page_writes(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2): when a Control File is supplied
    # and lumio.yaml is occupied by an existing directory, the atomic Control
    # File write used to fail with IsADirectoryError in the APPLY phase —
    # after the proposal's page writes had already landed. The shared
    # preflight now rejects the unusable Control File destination up front:
    # live apply raises DestinationConflict with the pages untouched, and
    # candidate validation returns the same conflict as a destination issue.
    kb = _kb(tmp_path)
    (kb.root / "lumio.yaml").mkdir()
    control = lw.KnowledgeBaseControlFile(version=1)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-control-dir-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root, control_file=control)
    # Pages untouched; the directory occupant survives.
    assert not (kb.root / "fresh.md").exists()
    assert (kb.root / "lumio.yaml").is_dir()

    report = lw.validate_candidate_knowledge_base([page], kb.root, control_file=control)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "lumio.yaml" in issues[0].message


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires Unix domain sockets")
def test_control_file_socket_destination_is_rejected_before_page_writes(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v4 blocker 1): the Control
    # File destination preflight only rejected a DIRECTORY at lumio.yaml, so
    # a special-file occupant (a Unix socket) passed preflight: candidate
    # validation died with a raw ``shutil.Error`` from ``copytree`` (the
    # socket cannot be copied) while live apply wrote the proposed pages and
    # then REPLACED the socket with a regular Control File. The Control File
    # destination now runs the same nonblocking special/unreadable probe as
    # page destinations: the conflict raises before ANY page write, and
    # candidate validation returns the SAME blocking destination issue.
    kb = _kb(tmp_path)
    control_path = kb.root / "lumio.yaml"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(control_path))
    control = lw.KnowledgeBaseControlFile(version=1)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-control-socket-review-fix"),
    )
    try:
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages([page], kb.root, control_file=control)
        report = lw.validate_candidate_knowledge_base([page], kb.root, control_file=control)
        # No page write, no Control File replacement: the socket survives.
        assert not (kb.root / "fresh.md").exists()
        assert control_path.is_socket()
    finally:
        sock.close()
        control_path.unlink(missing_ok=True)

    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "special file" in issues[0].message


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
def test_control_file_fifo_destination_is_rejected_without_opening_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Review-fix regression (Plan 02 / P2, review v4 blocker 1): a FIFO at
    # lumio.yaml used to pass the directory-only Control File preflight. The
    # destination is now typed with the same nonblocking stat probe as page
    # destinations, so the preflight never opens the FIFO (an open(2) on a
    # FIFO blocks until a writer appears and would hang the suite), live
    # apply raises DestinationConflict before ANY page write, and candidate
    # validation aggregates the same blocking destination issue. The guarded
    # Path.open below turns a regression back into a fast failure (instead of
    # a hang) if the preflight ever opens the FIFO again.
    kb = _kb(tmp_path)
    fifo_path = kb.root / "lumio.yaml"
    os.mkfifo(fifo_path)
    real_open = Path.open

    def _never_open_the_fifo(self, *args, **kwargs):
        if self == fifo_path:
            raise AssertionError("the preflight must never open a FIFO Control File destination")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _never_open_the_fifo)
    control = lw.KnowledgeBaseControlFile(version=1)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-control-fifo-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root, control_file=control)
    report = lw.validate_candidate_knowledge_base([page], kb.root, control_file=control)

    # The FIFO was never opened, never replaced, and no page was written.
    assert fifo_path.is_fifo()
    assert not (kb.root / "fresh.md").exists()
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "special file" in issues[0].message


def test_control_file_replacement_still_works_for_regular_and_missing_paths(tmp_path: Path):
    # Review-fix companion (Plan 02 / P2): the new Control File destination
    # preflight must not disturb the normal behavior — an existing REGULAR
    # lumio.yaml is replaced and a MISSING one is created, in the same atomic
    # proposal as the proposed pages.
    control = lw.KnowledgeBaseControlFile(version=1)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-control-replace-review-fix"),
    )
    for name, preexisting in (("regular", True), ("missing", False)):
        root = tmp_path / name
        shutil.copytree(FIXTURES, root)
        if preexisting:
            (root / "lumio.yaml").write_text("legacy: junk\n", encoding="utf-8")

        lw.apply_proposed_pages([page], root, control_file=control)

        assert (root / "fresh.md").exists(), name
        control_path = root / "lumio.yaml"
        assert control_path.is_file(), name
        written = control_path.read_text(encoding="utf-8")
        assert "version: 1" in written, name
        if preexisting:
            assert "legacy" not in written, name


def _skip_if_symlinks_unavailable(tmp_path: Path) -> None:
    """Skip when the platform cannot create symlinks (e.g. unprivileged Windows)."""
    if not hasattr(os, "symlink"):
        pytest.skip("platform has no os.symlink")
    probe = tmp_path / "_symlink-probe"
    try:
        probe.symlink_to("probe-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable for this user/platform")
    finally:
        probe.unlink(missing_ok=True)


def test_symlink_move_source_is_rejected_before_resolution(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v4 blocker 2): a move
    # source submitted as a SYMLINK was silently resolved (followed) by the
    # escape check, so "source-link.md -> architecture.md" was accepted as
    # the owned regular architecture.md: live apply wrote the new target,
    # UNLINKED architecture.md, and left the now-dangling source-link.md
    # behind, while candidate validation dereferenced the link into a second
    # page and failed on ordinary duplicate-title issues — no destination
    # issue at all. A move source is now rejected on non-following lstat
    # BEFORE resolution loses the lexical path: only the actual regular
    # recorded page file is movable, and live apply and candidate validation
    # report the same destination conflict.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    architecture = kb.root / "architecture.md"
    original = architecture.read_text(encoding="utf-8")
    link = kb.root / "source-link.md"
    link.symlink_to("architecture.md")
    page = lw.ProposedPage(
        relative_path="moved.md",
        title="Architecture",
        markdown=_probe_markdown("Architecture", "arch-symlink-move-review-fix"),
        move_from_path="source-link.md",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    # Nothing written, nothing removed: the recorded page keeps its bytes
    # and the submitted link still points at it (NOT left dangling).
    assert architecture.read_text(encoding="utf-8") == original
    assert not (kb.root / "moved.md").exists()
    assert link.is_symlink()
    assert link.resolve() == architecture.resolve()

    # Candidate validation returns the SAME blocking destination issue.
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "symlink" in issues[0].message


def test_dangling_symlink_move_source_is_rejected_before_resolution(tmp_path: Path):
    # Same parity for a DANGLING symlink move source: exists() and stat()
    # both follow the link and see nothing, so the missing-source tolerance
    # used to accept the link — live apply then wrote the target while the
    # unremovable dangling entry stayed behind, and candidate validation died
    # in ``copytree`` on the uncopyable link. Non-following lstat still sees
    # the link itself, so it is rejected up front and candidate validation
    # returns the same destination issue instead of a filesystem exception.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    link = kb.root / "dangling-link.md"
    link.symlink_to("ghost-target.md")
    page = lw.ProposedPage(
        relative_path="moved.md",
        title="Moved",
        markdown=_probe_markdown("Moved", "moved-dangling-link-review-fix"),
        move_from_path="dangling-link.md",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    # Rejected up front: the link was never followed and never removed.
    assert link.is_symlink()
    assert not (kb.root / "moved.md").exists()

    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "symlink" in issues[0].message
