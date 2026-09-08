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


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory mode bits are required to deny an unlink"
)
def test_unremovable_move_source_parent_is_blocked_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v5 blocker 1): an OWNED,
    # regular, readable move source whose parent directory does not permit
    # removing entries (chmod 0555) passed every preflight — unlink(2)
    # permission comes from the parent directory's mode bits, not from the
    # file. Live apply then wrote the target first and died on the source
    # unlink with a raw PermissionError, stranding a partial move, while
    # candidate validation leaked the same filesystem exception instead of a
    # destination issue. The preflight now evaluates the parent's mode bits
    # (and sticky bit) DIRECTLY — never ``os.access``, which false-passes as
    # root — so the conflict raises as a DestinationConflict before ANY byte
    # is written, and candidate validation aggregates the identical
    # destination issue.
    kb = _kb(tmp_path)
    locked = kb.root / "locked"
    locked.mkdir()
    source = locked / "overview.md"
    (kb.root / "overview.md").rename(source)
    kb, load_report = lw.load_knowledge_base(kb.root)
    assert load_report.is_valid, load_report
    original = source.read_text(encoding="utf-8")
    page = lw.ProposedPage(
        relative_path="moved.md",
        title="Lumio Overview",
        markdown=_probe_markdown("Lumio Overview", "overview-locked-parent-review-fix"),
        move_from_path="locked/overview.md",
    )

    os.chmod(locked, 0o555)
    try:
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages([page], kb.root)
        candidate_report = lw.validate_candidate_knowledge_base([page], kb.root)
        # No partial move: the target was never written and the intact
        # recorded page survives behind the locked parent directory.
        assert not (kb.root / "moved.md").exists()
        assert source.read_text(encoding="utf-8") == original
        assert not candidate_report.is_valid
        issues = [issue for issue in candidate_report.issues if issue.severity == "error"]
        assert len(issues) == 1, issues
        assert issues[0].field == "destination"
        # Candidate/live parity: the identical conflict, aggregated as an issue.
        assert issues[0].message == str(live.value)
        assert "locked/overview.md" in issues[0].message
        # The message names the fixable parent directory, not a raw OSError.
        assert "locked" in issues[0].message

        # The removability requirement is scoped to moves that will actually
        # unlink: moving a page onto its own recorded path overwrites in
        # place (no unlink), so it stays valid even while the parent is
        # locked — in candidate validation and live apply alike.
        in_place = lw.ProposedPage(
            relative_path="locked/overview.md",
            title="Lumio Overview",
            markdown=_probe_markdown("Lumio Overview", "overview-in-place-review-fix"),
            move_from_path="locked/overview.md",
        )
        in_place_report = lw.validate_candidate_knowledge_base([in_place], kb.root)
        assert in_place_report.is_valid, in_place_report
        lw.apply_proposed_pages([in_place], kb.root)
        assert source.read_text(encoding="utf-8") != original
    finally:
        os.chmod(locked, 0o755)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory mode bits are required to deny an unlink"
)
def test_unremovable_removal_target_is_blocked_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v6 blocker 1): a DECLARED
    # Page Removal whose existing target lives in a parent directory that
    # does not permit removing entries (chmod 0555) passed every preflight —
    # unlink(2) permission comes from the parent directory's mode bits, not
    # from the target file. Live apply then wrote every proposed page first
    # and died on the removal unlink with a raw PermissionError, stranding a
    # partial apply, while candidate validation leaked the same filesystem
    # exception instead of a destination issue. The removal resolution now
    # preflights the removability of every existing declared target with the
    # SAME mode-bit-aware parent check the move source preflight uses, so the
    # conflict raises as a DestinationConflict before ANY byte is written,
    # and candidate validation aggregates the identical destination issue.
    kb = _kb(tmp_path)
    locked = kb.root / "locked"
    locked.mkdir()
    recorded = locked / "overview.md"
    (kb.root / "overview.md").rename(recorded)
    kb, load_report = lw.load_knowledge_base(kb.root)
    assert load_report.is_valid, load_report
    original = recorded.read_text(encoding="utf-8")
    fresh = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-locked-removal-review-fix"),
    )

    os.chmod(locked, 0o555)
    try:
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages([fresh], kb.root, removed_titles=["Lumio Overview"])
        candidate_report = lw.validate_candidate_knowledge_base(
            [fresh], kb.root, removed_titles=["Lumio Overview"]
        )
        # No partial apply: the proposed page was never written and the
        # locked removal target survives byte-unchanged.
        assert not (kb.root / "fresh.md").exists()
        assert recorded.read_text(encoding="utf-8") == original
        assert not candidate_report.is_valid
        issues = [issue for issue in candidate_report.issues if issue.severity == "error"]
        assert len(issues) == 1, issues
        assert issues[0].field == "destination"
        # Candidate/live parity: the identical conflict, aggregated as an issue.
        assert issues[0].message == str(live.value)
        assert "locked/overview.md" in issues[0].message
        # The message names the fixable parent directory, not a raw OSError.
        assert "locked" in issues[0].message
    finally:
        os.chmod(locked, 0o755)

    # Positive control: with a writable parent the same proposal publishes —
    # the proposed page lands and the declared removal performs its unlink.
    lw.apply_proposed_pages([fresh], kb.root, removed_titles=["Lumio Overview"])
    assert (kb.root / "fresh.md").is_file()
    assert not recorded.exists()


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


def test_control_file_dangling_symlink_destination_is_rejected_before_page_writes(
    tmp_path: Path,
):
    # Review-fix regression (Plan 02 / P2, review v7 blocker 1): a DANGLING
    # lumio.yaml symlink slipped through the Control File preflight because
    # exists() follows the link and saw an absent path. Candidate validation
    # then reached ``copytree`` and leaked a raw ``shutil.Error`` (the link
    # has no target to copy) while live apply wrote the proposal's pages and
    # silently replaced the dangling entry with a regular Control File — and
    # the parent-directory helper even skipped the sticky-bit replacement
    # ownership check for the same reason, because it gated ``lstat`` on the
    # same following ``exists()``. The preflight now detects the dangling
    # entry with non-following metadata and rejects it BEFORE any candidate
    # copy or page write, and candidate validation aggregates the identical
    # destination issue instead of a filesystem exception.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    control_path = kb.root / "lumio.yaml"
    control_path.symlink_to("ghost-target.yaml")
    control = lw.KnowledgeBaseControlFile(version=1)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-control-dangling-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root, control_file=control)
    # No page write and no replacement: the dangling entry survives as a
    # symlink pointing at the still-absent target.
    assert not (kb.root / "fresh.md").exists()
    assert control_path.is_symlink()
    assert not control_path.exists()

    # Candidate validation returns the SAME blocking destination issue.
    report = lw.validate_candidate_knowledge_base([page], kb.root, control_file=control)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "lumio.yaml" in issues[0].message
    assert "symlink" in issues[0].message


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX directory mode bits are required to deny a Control File write",
)
def test_unwritable_control_file_parent_is_blocked_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v6 blocker 2): when a
    # Control File is supplied, the parent directory of ``lumio.yaml`` (the
    # Knowledge Base root) must permit the atomic write — the temp file is
    # created there and ``os.replace`` modifies its directory entry. A
    # mode-denied root (chmod 0555) passed every preflight: live apply wrote
    # the proposed revision FIRST and then died creating the
    # ``.lumio-artifact-*.tmp`` file with a raw PermissionError, while
    # candidate validation leaked the same filesystem exception. The Control
    # File preflight now evaluates the parent's write/search mode bits (and
    # the sticky-bit replacement rule) DIRECTLY — never ``os.access``, which
    # false-passes as root — so the conflict raises as a DestinationConflict
    # before ANY page byte is written, and candidate validation aggregates
    # the identical destination issue. The denial applies to a MISSING
    # lumio.yaml (creation) and to an existing regular one (replacement)
    # alike.
    control = lw.KnowledgeBaseControlFile(version=1)
    revision = lw.ProposedPage(
        relative_path="overview.md",
        title="Lumio Overview",
        markdown=_probe_markdown("Lumio Overview", "overview-locked-control-review-fix"),
    )
    for name, preexisting in (("missing", False), ("regular", True)):
        root = tmp_path / name
        shutil.copytree(FIXTURES, root)
        control_path = root / "lumio.yaml"
        if preexisting:
            control_path.write_text("legacy: junk\n", encoding="utf-8")
        overview = root / "overview.md"
        original = overview.read_text(encoding="utf-8")

        os.chmod(root, 0o555)
        try:
            with pytest.raises(DestinationConflict) as live:
                lw.apply_proposed_pages([revision], root, control_file=control)
            report = lw.validate_candidate_knowledge_base([revision], root, control_file=control)
            # No partial revision: the recorded page keeps its bytes, no
            # temp file was ever created, and the Control File destination is
            # unchanged (still missing, or still the legacy occupant).
            assert overview.read_text(encoding="utf-8") == original, name
            assert not list(root.glob(".lumio-artifact-*.tmp")), name
            if preexisting:
                assert "legacy" in control_path.read_text(encoding="utf-8"), name
            else:
                assert not control_path.exists(), name
        finally:
            os.chmod(root, 0o755)

        assert not report.is_valid, name
        issues = [issue for issue in report.issues if issue.severity == "error"]
        assert len(issues) == 1, (name, issues)
        assert issues[0].field == "destination"
        # Candidate/live parity: the identical conflict, aggregated as an issue.
        assert issues[0].message == str(live.value), name
        assert "lumio.yaml" in issues[0].message, name

        # Positive control: with a writable root the same proposal publishes
        # the revision AND the Control File, exactly as before the new
        # preflight.
        lw.apply_proposed_pages([revision], root, control_file=control)
        assert overview.read_text(encoding="utf-8") != original, name
        written = control_path.read_text(encoding="utf-8")
        assert "version: 1" in written, name
        if preexisting:
            assert "legacy" not in written, name


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


def test_dangling_symlink_page_destination_is_rejected_with_candidate_parity(
    tmp_path: Path,
):
    # Review-fix regression (Plan 02 / P2, review v8 blocker 1): a DANGLING
    # symlink at the SUBMITTED page destination slipped through the new-page
    # preflight because ``_checked_relative_path`` RESOLVED the link before
    # the occupancy check ran: "fresh.md -> ghost.md" (missing target) was
    # silently normalized to the free path "ghost.md" and accepted. Candidate
    # validation then died inside ``copytree`` with a raw ``shutil.Error``
    # (the retained dangling entry has no content to copy) while live apply
    # wrote the page at ``ghost.md`` behind the still-dangling link — a
    # different path than the one the proposal submitted. The shared
    # destination resolution now detects the dangling entry with
    # non-following ``lstat`` BEFORE resolution and rejects it for every
    # write destination, so candidate validation returns the identical
    # destination issue and the submitted link survives untouched.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    link = kb.root / "fresh.md"
    link.symlink_to("ghost.md")
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-dangling-destination-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    # Rejected before ANY write: no file was created behind the link and the
    # submitted entry survives as the dangling symlink it was.
    assert not (kb.root / "ghost.md").exists()
    assert link.is_symlink()
    assert not link.exists()

    # Candidate validation returns the SAME blocking destination issue —
    # never a raw ``shutil.Error`` from the candidate copy.
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "symlink" in issues[0].message
    assert "fresh.md" in issues[0].message


def test_dangling_symlink_move_destination_is_rejected_with_candidate_parity(
    tmp_path: Path,
):
    # Same parity for a MOVE whose submitted destination is a dangling
    # symlink: resolution normalized "fresh.md -> ghost.md" (missing target)
    # to a free "ghost.md", so the move passed its occupancy guard — live
    # apply then wrote ``ghost.md`` AND unlinked the verified move source
    # ``technology.md``, leaving the dangling link silently pointing at the
    # relocated page, while candidate validation died in ``copytree`` on the
    # uncopyable entry. The destination is now rejected on non-following
    # ``lstat`` BEFORE resolution, so the source is never unlinked and the
    # candidate reports the identical destination issue.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    source = kb.root / "technology.md"
    original = source.read_text(encoding="utf-8")
    link = kb.root / "fresh.md"
    link.symlink_to("ghost.md")
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Technology Stack",
        markdown=_probe_markdown("Technology Stack", "tech-dangling-move-review-fix"),
        move_from_path="technology.md",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    # Nothing written, nothing removed: the move source keeps its bytes and
    # the dangling destination entry was never followed or replaced.
    assert source.read_text(encoding="utf-8") == original
    assert not (kb.root / "ghost.md").exists()
    assert link.is_symlink()
    assert not link.exists()

    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].message == str(live.value)
    assert "symlink" in issues[0].message


def test_dangling_symlink_ancestor_is_rejected_with_candidate_parity(
    tmp_path: Path,
):
    # Review-fix regression (Plan 02 / P2, review v9 blocker 1): a DANGLING
    # symlink ANCESTOR of a submitted destination slipped through the
    # dangling-entry preflight because only the LEAF was examined before
    # resolution: "link -> ghost" (missing target) with the proposed path
    # "link/fresh.md" resolved to the free "ghost/fresh.md" — the occupancy
    # and ancestor checks judged the RESOLVED route, so candidate validation
    # still reached ``copytree`` and died with a raw ``shutil.Error`` (the
    # retained dangling entry has no content to copy) while live apply
    # silently created the ``ghost/`` directory behind the still-dangling
    # link and wrote the page THERE, not at the submitted path. Destination
    # resolution now walks every existing lexical component from the root
    # through the destination's parents down to its leaf with the same
    # non-following metadata, so the conflict raises as a DestinationConflict
    # before any candidate copy or live write, the submitted link survives
    # untouched, and candidate validation returns the identical destination
    # issue.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    link = kb.root / "link"
    link.symlink_to("ghost")
    page = lw.ProposedPage(
        relative_path="link/fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-dangling-ancestor-review-fix"),
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root)
    # Rejected before ANY write: nothing was created behind the dangling
    # ancestor and the submitted entry survives as the dangling link it was.
    assert not (kb.root / "ghost").exists()
    assert link.is_symlink()
    assert not link.exists()

    # Candidate validation returns the SAME blocking destination issue —
    # never a raw ``shutil.Error`` from the candidate copy.
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid
    issues = [issue for issue in report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].file == "link/fresh.md"
    assert issues[0].message == str(live.value)
    assert "symlink" in issues[0].message
    assert "link/fresh.md" in issues[0].message

    # Positive controls: a genuinely MISSING ordinary parent stays valid in
    # candidate validation and live apply alike — it is created on demand.
    # These controls also pin the copy-side fix (P2 review v9): the rejected
    # proposal's dangling ``link`` entry legitimately survives in the real
    # Knowledge Base, so the throwaway candidate copy must tolerate it
    # (``ignore_dangling_symlinks``) instead of leaking a raw
    # ``shutil.Error`` for any subsequent proposal on the same KB.
    nested = lw.ProposedPage(
        relative_path="fresh_sub/fresh.md",
        title="Fresh Nested",
        markdown=_probe_markdown("Fresh Nested", "fresh-nested-parent-review-fix"),
    )
    nested_report = lw.validate_candidate_knowledge_base([nested], kb.root)
    assert nested_report.is_valid, nested_report
    lw.apply_proposed_pages([nested], kb.root)
    assert (kb.root / "fresh_sub/fresh.md").is_file()

    # And a VALID symlink ancestor (its target exists) still resolves like an
    # ordinary directory: the write lands at the resolved target and the
    # alias survives intact. Live apply only — the candidate copy re-creates
    # the alias verbatim (P2 review v10), so this control pins the live
    # resolution route; candidate validation of a further proposal on this
    # KB reads the alias exactly as live validation does.
    (kb.root / "real_dir").mkdir()
    alias = kb.root / "alias"
    alias.symlink_to("real_dir")
    aliased = lw.ProposedPage(
        relative_path="alias/aliased.md",
        title="Aliased",
        markdown=_probe_markdown("Aliased", "aliased-valid-ancestor-review-fix"),
    )
    lw.apply_proposed_pages([aliased], kb.root)
    assert (kb.root / "real_dir/aliased.md").is_file()
    assert alias.is_symlink()
    assert alias.resolve() == (kb.root / "real_dir").resolve()


def test_valid_relative_symlink_is_represented_in_candidate_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # Review-fix regression (Plan 02 / P2, review v10 blocker 1): the
    # candidate copy's ``ignore_dangling_symlinks=True`` workaround judged a
    # RELATIVE link target with ``os.path.exists`` against the PROCESS CWD,
    # not against the link's own directory, so a VALID KB-relative symlink
    # such as "alias.md -> real.md" was silently omitted from the candidate
    # whenever no same-named file happened to sit in the CWD. Live validation
    # reads through the link (``_markdown_files`` follows symlinked ``.md``
    # entries), so the live tree reports the alias's read-through page too —
    # here a duplicate canonical title — while candidate validation, blind
    # to the omitted alias, passed the very same tree and publication would
    # have written an invalid Knowledge Base. The copy now re-creates
    # symlinks verbatim (source-entry-relative), so candidate validation sees
    # exactly what live validation sees and invalid content is not hidden.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    (kb.root / "real.md").write_text(
        _probe_markdown("Anchor Topic", "anchor-valid-symlink-review-fix"),
        encoding="utf-8",
    )
    (kb.root / "alias.md").symlink_to("real.md")
    # Baseline: the LIVE tree's own validation reads through the alias and
    # reports the duplicated canonical title on both entries.
    live = lw.validate(kb.root)
    live_duplicates = sorted(
        (issue.file, issue.message)
        for issue in live.issues
        if issue.severity == "error" and "duplicate canonical title" in issue.message
    )
    assert [file for file, _ in live_duplicates] == ["alias.md", "real.md"]

    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-valid-symlink-review-fix"),
    )
    # The process CWD holds no ``real.md``: this is exactly the configuration
    # in which the old skip consulted ``os.path.exists("real.md")``, saw it
    # missing, and dropped the valid alias from the candidate.
    monkeypatch.chdir(tmp_path)
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    candidate_duplicates = sorted(
        (issue.file, issue.message)
        for issue in report.issues
        if issue.severity == "error" and "duplicate canonical title" in issue.message
    )
    # Consistent representation: the candidate reports the duplicate-title
    # errors on exactly the entries live validation names — the valid
    # relative symlink is represented, and its invalid duplicated content is
    # not hidden from the publication gate.
    assert candidate_duplicates == live_duplicates

    # A proposal on the SAME KB stays live-consistent after the alias's
    # target is gone too: removing the target leaves the link dangling, and
    # the candidate copy re-creates it (which cannot fail) instead of dying
    # on it — validation ignores the contentless entry on both trees.
    (kb.root / "real.md").unlink()
    dangling_report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert dangling_report.is_valid, dangling_report


def test_external_relative_symlink_content_is_represented_in_candidate_validation(
    tmp_path: Path,
):
    # Review-fix regression (Plan 02 / P2 review fix11): the structural mirror
    # re-created relative symlinks VERBATIM inside the throwaway candidate
    # tree. For a valid link whose target lies OUTSIDE the Knowledge Base
    # root — "external-alias.md -> ../outside.md" — that preserved nothing:
    # the link text keeps resolving against its own directory in the live
    # tree (live validation follows it and reads the external page), but the
    # same text beside the candidate resolves beside the TEMP directory,
    # where nothing exists. Candidate validation missed exactly the content
    # the live gate reads, so an invalid-to-be Knowledge Base validated
    # cleanly and publication bypassed the live gate. The mirror now
    # source-resolves such external FILE links and materializes their live
    # read-through content into the candidate as an ordinary regular file at
    # the link's own path — same bytes, same relative path, the real
    # Knowledge Base never touched — so candidate and live validation read
    # the same pages.
    _skip_if_symlinks_unavailable(tmp_path)
    kb = _kb(tmp_path)
    # The external target sits OUTSIDE the Knowledge Base root: a sibling of
    # "kb/" in the parent directory, reachable only through the relative
    # "../" hop — invisible to any candidate tree rooted at a temp path.
    (tmp_path / "outside.md").write_text(
        _probe_markdown("Dup Topic", "outside-external-symlink-review-fix"),
        encoding="utf-8",
    )
    (kb.root / "external-alias.md").symlink_to("../outside.md")
    # An EXISTING regular page whose canonical title collides with the
    # external page's title. Its path deliberately sorts AFTER the link so
    # the shared destination preflight's title map (one recorded path per
    # title, last page wins) keeps pointing at this regular file and the
    # proposal below reaches the candidate mirror at all.
    (kb.root / "zdup.md").write_text(
        _probe_markdown("Dup Topic", "zdup-external-symlink-review-fix"),
        encoding="utf-8",
    )
    # Baseline: the LIVE tree reads through the link and reports the
    # duplicated canonical title on BOTH entries — the link entry included.
    live = lw.validate(kb.root)
    live_errors = sorted(
        (issue.file, issue.message) for issue in live.issues if issue.severity == "error"
    )
    assert live_errors == [
        ("external-alias.md", "duplicate canonical title: Dup Topic"),
        ("zdup.md", "duplicate canonical title: Dup Topic"),
    ]

    # Any innocuous new-page proposal must be rejected by the candidate gate
    # with EXACTLY the issues live validation reports: the candidate now
    # contains the external page's content, so the duplication the link
    # exposes cannot hide from publication.
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-external-symlink-review-fix"),
    )
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not report.is_valid, report
    candidate_errors = sorted(
        (issue.file, issue.message) for issue in report.issues if issue.severity == "error"
    )
    assert candidate_errors == live_errors

    # Content parity in the other direction too: an external page the live
    # gate reads as INVALID (here: missing every required frontmatter field)
    # must fail candidate validation with the identical issues on the link's
    # own relative path instead of vanishing beside the temp candidate. With
    # no parseable title the page also stays out of the preflight's title
    # map, so this is judged purely by what the mirror represents.
    (tmp_path / "outside.md").write_text(
        '---\nsummary: "External page missing every required frontmatter field."\n---\n',
        encoding="utf-8",
    )
    live_invalid = lw.validate(kb.root)
    assert not live_invalid.is_valid
    external_live_errors = sorted(
        (issue.file, issue.message)
        for issue in live_invalid.issues
        if issue.severity == "error" and issue.file == "external-alias.md"
    )
    assert external_live_errors == [
        ("external-alias.md", "missing required field: lifecycle"),
        ("external-alias.md", "missing required field: tags"),
        ("external-alias.md", "missing required field: title"),
        ("external-alias.md", "missing required field: visibility"),
        ("external-alias.md", "non-synthetic page must have at least one source"),
    ]
    invalid_report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert not invalid_report.is_valid
    external_candidate_errors = sorted(
        (issue.file, issue.message)
        for issue in invalid_report.issues
        if issue.severity == "error" and issue.file == "external-alias.md"
    )
    assert external_candidate_errors == external_live_errors

    # Positive control: with the external target gone the link dangles — as
    # contentless on the candidate (verbatim recreation) as it is live — and
    # the SAME innocuous proposal passes: the mirror represents external
    # content, it does not invent conflicts. The real Knowledge Base was
    # never touched: the entry survives as the symlink it is, and live
    # validation agrees.
    (tmp_path / "outside.md").unlink()
    assert lw.validate(kb.root).is_valid
    restored_report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert restored_report.is_valid, restored_report
    assert (kb.root / "external-alias.md").is_symlink()


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX directory mode bits are required to deny a page write",
)
def test_unwritable_page_parent_is_blocked_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v9 blocker 2): a proposed
    # page whose missing destination sits under a parent directory that does
    # not permit creating entries (chmod 0555) passed every resolution-time
    # preflight — mkdir(2) permission comes from the parent directory's mode
    # bits, not from the destination. Live apply then wrote the FIRST page of
    # the proposal and died creating the second with a raw PermissionError,
    # stranding a partial apply, while candidate validation leaked the same
    # filesystem exception from its throwaway tree. The shared check-only
    # phase now preflights every destination's creation permission — the
    # nearest EXISTING parent directory's write/search mode bits, evaluated
    # directly (never ``os.access``, which false-passes as root) — so the
    # conflict raises as a DestinationConflict before ANY byte is written,
    # and candidate validation aggregates the identical destination issue.
    kb = _kb(tmp_path)
    locked = kb.root / "locked"
    locked.mkdir()
    pages = [
        lw.ProposedPage(
            relative_path="fresh.md",
            title="Fresh",
            markdown=_probe_markdown("Fresh", "fresh-locked-page-parent-review-fix"),
        ),
        lw.ProposedPage(
            relative_path="locked/blocked.md",
            title="Blocked",
            markdown=_probe_markdown("Blocked", "blocked-locked-page-parent-review-fix"),
        ),
    ]

    os.chmod(locked, 0o555)
    try:
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages(pages, kb.root)
        candidate_report = lw.validate_candidate_knowledge_base(pages, kb.root)
        # No partial apply: even the first destination was never written —
        # the whole proposal is rejected before any byte lands.
        assert not (kb.root / "fresh.md").exists()
        assert not (kb.root / "locked/blocked.md").exists()
        assert not candidate_report.is_valid
        issues = [issue for issue in candidate_report.issues if issue.severity == "error"]
        assert len(issues) == 1, issues
        assert issues[0].field == "destination"
        assert issues[0].file == "locked/blocked.md"
        # Candidate/live parity: the identical conflict, aggregated as an issue.
        assert issues[0].message == str(live.value)
        assert "locked/blocked.md" in issues[0].message
        # The message names the fixable parent directory, not a raw OSError.
        assert "locked" in issues[0].message
    finally:
        os.chmod(locked, 0o755)

    # Positive control: with the parent writable the SAME proposal applies
    # in candidate validation and live apply alike, exactly as before the
    # new preflight.
    writable_report = lw.validate_candidate_knowledge_base(pages, kb.root)
    assert writable_report.is_valid, writable_report
    lw.apply_proposed_pages(pages, kb.root)
    assert (kb.root / "fresh.md").is_file()
    assert (kb.root / "locked/blocked.md").is_file()


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX file mode bits are required to deny a page revision",
)
def test_readonly_page_revision_is_blocked_with_candidate_parity(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v9 blocker 2): a same-title
    # revision whose existing regular recorded file is read-only (chmod 0444)
    # passed every occupancy preflight — the occupant is the page's own
    # recorded path, readable, a regular file — but ``write_text`` truncates
    # the file in place, and open(2) for writing is authorized by the FILE's
    # own mode bits. Live apply then wrote the FIRST page of the proposal and
    # died on the revision with a raw PermissionError, stranding a partial
    # apply, while candidate validation leaked the same filesystem exception.
    # The shared check-only phase now preflights every destination's revision
    # permission too — the existing regular file's write bit for the
    # effective user, evaluated directly (never ``os.access``, which
    # false-passes as root; page writes never replace a directory entry, so
    # the Control File's sticky-replacement rule has no page equivalent) —
    # and candidate validation aggregates the identical destination issue.
    kb = _kb(tmp_path)
    recorded = kb.root / "overview.md"
    original = recorded.read_text(encoding="utf-8")
    pages = [
        lw.ProposedPage(
            relative_path="fresh.md",
            title="Fresh",
            markdown=_probe_markdown("Fresh", "fresh-readonly-revision-review-fix"),
        ),
        lw.ProposedPage(
            relative_path="overview.md",
            title="Lumio Overview",
            markdown=_probe_markdown("Lumio Overview", "overview-readonly-revision-review-fix"),
        ),
    ]

    os.chmod(recorded, 0o444)
    try:
        with pytest.raises(DestinationConflict) as live:
            lw.apply_proposed_pages(pages, kb.root)
        candidate_report = lw.validate_candidate_knowledge_base(pages, kb.root)
        # No partial apply: the first page was never written and the
        # read-only recorded page keeps its bytes.
        assert not (kb.root / "fresh.md").exists()
        assert recorded.read_text(encoding="utf-8") == original
        assert not candidate_report.is_valid
        issues = [issue for issue in candidate_report.issues if issue.severity == "error"]
        assert len(issues) == 1, issues
        assert issues[0].field == "destination"
        assert issues[0].file == "overview.md"
        # Candidate/live parity: the identical conflict, aggregated as an issue.
        assert issues[0].message == str(live.value)
        assert "overview.md" in issues[0].message
        assert "write access" in issues[0].message

        # The same protection covers an IN-PLACE self-move of the read-only
        # page: it overwrites the file without unlinking anything, so the
        # file's own write bits decide, exactly as for a revision.
        in_place = lw.ProposedPage(
            relative_path="overview.md",
            title="Lumio Overview",
            markdown=_probe_markdown("Lumio Overview", "overview-readonly-selfmove-review-fix"),
            move_from_path="overview.md",
        )
        with pytest.raises(DestinationConflict) as live_move:
            lw.apply_proposed_pages([in_place], kb.root)
        assert recorded.read_text(encoding="utf-8") == original
        assert "write access" in str(live_move.value)
    finally:
        os.chmod(recorded, 0o644)

    # Positive control: a writable recorded file revises in place exactly as
    # before the new preflight — read-only occupants that publication is
    # meant to replace stay replaceable once the mode bits allow it.
    writable_report = lw.validate_candidate_knowledge_base(pages, kb.root)
    assert writable_report.is_valid, writable_report
    lw.apply_proposed_pages(pages, kb.root)
    assert (kb.root / "fresh.md").is_file()
    assert recorded.read_text(encoding="utf-8") != original


def test_move_plus_removal_of_same_title_is_rejected_before_any_write(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v9 blocker 3): a proposal
    # that MOVES a title to a new path while ``removed_titles`` also declares
    # that same title silently ignored the removal. Removals are resolved
    # from the pre-mutation path, but the overlap check only compared removal
    # paths with move DESTINATIONS, and apply moves/unlinks the old source
    # before the removal phase — which then found the entry already gone and
    # skipped it, leaving the moved page alive behind a removal the proposal
    # declared. The vacated source path is now claimed too: the
    # source/removal overlap is rejected as hidden-removal safety BEFORE any
    # write, and candidate validation returns the identical destination
    # issue.
    kb = _kb(tmp_path)
    source = kb.root / "architecture.md"
    original = source.read_text(encoding="utf-8")
    page = lw.ProposedPage(
        relative_path="moved.md",
        title="Architecture",
        markdown=_probe_markdown("Architecture", "arch-move-removal-review-fix"),
        move_from_path="architecture.md",
    )

    with pytest.raises(DestinationConflict) as live:
        lw.apply_proposed_pages([page], kb.root, removed_titles=["Architecture"])
    candidate_report = lw.validate_candidate_knowledge_base(
        [page], kb.root, removed_titles=["Architecture"]
    )
    # Rejected before ANY write: the source was neither moved nor removed and
    # the move destination was never created.
    assert source.read_text(encoding="utf-8") == original
    assert not (kb.root / "moved.md").exists()
    assert not candidate_report.is_valid
    issues = [issue for issue in candidate_report.issues if issue.severity == "error"]
    assert len(issues) == 1, issues
    assert issues[0].field == "destination"
    assert issues[0].file == "architecture.md"
    # Candidate/live parity: the identical conflict, aggregated as an issue.
    assert issues[0].message == str(live.value)
    assert "removal" in issues[0].message
    assert "moved page" in issues[0].message

    # Positive control: the SAME move without the overlapping removal still
    # publishes in candidate validation and live apply alike, so only the
    # contradictory combination is blocked.
    plain_report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert plain_report.is_valid, plain_report
    lw.apply_proposed_pages([page], kb.root)
    assert not source.exists()
    assert (kb.root / "moved.md").is_file()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="POSIX FIFOs are required to model a special-file KB entry",
)
def test_unrelated_fifo_elsewhere_in_kb_returns_normal_candidate_report(tmp_path: Path):
    # Review-fix regression (Plan 02 / P2, review v10 blocker 2): the
    # candidate ``copytree`` died with a raw ``shutil.Error`` — collected
    # from copyfile's SpecialFileError — on an UNRELATED FIFO elsewhere in
    # the Knowledge Base, for EVERY proposal, even one that never touches
    # the entry. A FIFO is not Knowledge Base content and can never satisfy
    # a proposed destination (the real-root preflights reject special
    # occupants up front, and the copy classifies entries by non-following
    # stat metadata without ever opening one), so the mirrored copy skips it
    # and candidate validation returns its normal report — matching live
    # apply, which succeeds and never touches the FIFO either.
    kb = _kb(tmp_path)
    unrelated = kb.root / "artifacts"
    unrelated.mkdir()
    fifo = unrelated / "progress"
    os.mkfifo(fifo)
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-fifo-neighbor-review-fix"),
    )
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert report.is_valid, report
    # The unrelated entry was neither opened nor disturbed.
    assert fifo.exists()
    # Live parity: the same proposal applies cleanly beside the FIFO.
    lw.apply_proposed_pages([page], kb.root)
    assert (kb.root / "fresh.md").is_file()
    assert fifo.exists()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="AF_UNIX sockets are required to model a special-file KB entry",
)
def test_unrelated_unix_socket_elsewhere_in_kb_returns_normal_candidate_report(
    tmp_path: Path,
):
    # Review-fix regression (Plan 02 / P2, review v10 blocker 2): like the
    # unrelated FIFO above, an unrelated Unix socket inode elsewhere in the
    # Knowledge Base used to kill the candidate copy with a raw
    # ``shutil.Error`` (copyfile cannot open a socket) for any proposal.
    # The mirrored copy skips the contentless entry, so candidate validation
    # returns its normal ValidationReport and live apply succeeds — no raw
    # copy error either side.
    kb = _kb(tmp_path)
    unrelated = kb.root / "artifacts"
    unrelated.mkdir()
    sock_path = unrelated / "daemon.sock"
    daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        daemon.bind(str(sock_path))
    except OSError:
        pytest.skip("AF_UNIX socket file cannot be created on this platform")
    finally:
        daemon.close()
    # The bound inode persists after close: an unrelated special entry.
    assert sock_path.exists()
    page = lw.ProposedPage(
        relative_path="fresh.md",
        title="Fresh",
        markdown=_probe_markdown("Fresh", "fresh-socket-neighbor-review-fix"),
    )
    report = lw.validate_candidate_knowledge_base([page], kb.root)
    assert report.is_valid, report
    assert sock_path.exists()
    # Live parity: the same proposal applies cleanly beside the socket.
    lw.apply_proposed_pages([page], kb.root)
    assert (kb.root / "fresh.md").is_file()
    assert sock_path.exists()


def test_discarded_proposal_cannot_publish_from_stale_store(tmp_path: Path):
    # B03 (Plan 02 / P3): durable terminal state is the only authority. Two
    # ALREADY-OPEN IngestStore/Pipeline instances over the same private store:
    # instance B first takes a reviewable view of the staged proposal, then
    # instance A discards it. B's publish attempt must fail against the freshly
    # read durable state (read under the shared store lock) — the terminal
    # discard wins, the proposal is never resurrected as published, and the
    # proposed page never reaches the Knowledge Base root.
    kb = _kb(tmp_path)
    store_a = lw.IngestStore(tmp_path / "ingest")
    store_b = lw.IngestStore(tmp_path / "ingest")
    pipeline_a = lw.ProposalPipeline(kb, store_a)
    pipeline_b = lw.ProposalPipeline(kb, store_b)

    proposal = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store_a
    )
    stale = pipeline_b.review(proposal.id)
    assert stale is not None and stale.status == "staged"

    discarded = pipeline_a.discard(proposal.id)
    assert discarded is not None and discarded.status == "discarded"

    with pytest.raises(lw.ProposalPipelineError):
        pipeline_b.publish(proposal.id)

    # Terminal discard is durable and visible to every instance; nothing was
    # published behind it.
    assert not (kb.root / "journey_related_page.md").exists()
    reloaded = lw.IngestStore(tmp_path / "ingest")
    reloaded_proposal = reloaded.get(proposal.id)
    assert reloaded_proposal is not None and reloaded_proposal.status == "discarded"
    for pipeline in (pipeline_b, pipeline_a):
        viewed = pipeline.review(proposal.id)
        assert viewed is not None and viewed.status == "discarded"


def test_stale_restage_cannot_overwrite_discarded_proposal_or_publish(tmp_path: Path):
    # Plan 02 / P3 review blocker: terminal protection must also cover the
    # STALE RESTAGING overwrite, not just the stale publish. Two ALREADY-OPEN
    # IngestStore/Pipeline instances over the same private store: instance B
    # stages and keeps a staged view; instance A discards the durable
    # proposal; stale instance B then re-stages its stale staged object.
    # Previously that blind save flipped the durable ``discarded`` proposal
    # back to ``staged`` — from which B could publish a decision another
    # instance had already made. The restage must now be refused BEFORE any
    # write (no raw bytes, no proposal overwrite), the durable ``discarded``
    # status must survive, and the subsequent stale publish must not publish
    # the page. Deterministic: every interleaving is direct, ordered calls on
    # the two shared instances under the same store locks — no sleeps, no
    # threads.
    kb = _kb(tmp_path)
    store_a = lw.IngestStore(tmp_path / "ingest")
    store_b = lw.IngestStore(tmp_path / "ingest")
    pipeline_a = lw.ProposalPipeline(kb, store_a)
    pipeline_b = lw.ProposalPipeline(kb, store_b)

    proposal = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store_a
    )
    stale = pipeline_b.review(proposal.id)
    assert stale is not None and stale.status == "staged"
    raw_dir = tmp_path / "ingest" / "raw" / proposal.id
    raw_files_before = sorted(path.name for path in raw_dir.iterdir())
    assert raw_files_before, "the initial stage persists the raw source"

    discarded = pipeline_a.discard(proposal.id)
    assert discarded is not None and discarded.status == "discarded"

    # Stale instance B re-stages its stale staged object: refused at the
    # pipeline boundary BEFORE any raw or proposal write lands.
    with pytest.raises(lw.ProposalPipelineError):
        pipeline_b.stage(stale, raw_bytes=b"stale bytes", filename="stale-related.md")
    assert sorted(path.name for path in raw_dir.iterdir()) == raw_files_before

    # The store's ordinary save refuses the same stale object directly, and
    # the durable proposal keeps its terminal discarded status.
    with pytest.raises(lw.ProposalTerminalStateError):
        store_b.save_proposal(stale)
    reloaded = lw.IngestStore(tmp_path / "ingest")
    reloaded_proposal = reloaded.get(proposal.id)
    assert reloaded_proposal is not None and reloaded_proposal.status == "discarded"

    # The stale publish after the refused restage cannot resurrect either:
    # the page never reaches the Knowledge Base root.
    with pytest.raises(lw.ProposalPipelineError):
        pipeline_b.publish(proposal.id)
    assert not (kb.root / "journey_related_page.md").exists()
    for pipeline in (pipeline_b, pipeline_a):
        viewed = pipeline.review(proposal.id)
        assert viewed is not None and viewed.status == "discarded"
