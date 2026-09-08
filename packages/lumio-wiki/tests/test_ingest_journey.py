"""Direct journey test for the ``lumio-wiki`` ingestion public surface (issue #97).

Retains the existing-Knowledge-Base candidate-validation regression. The
standalone isolation and installed-wheel journeys exercise full ingest,
review, publish and discard behavior without optional dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
from pathlib import Path

import lumio_wiki as lw
import msgspec
import pytest
from lumio_wiki.ingest import _is_self_consistent_restage, _recomputed_mutation_identity
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


def test_public_save_refuses_incoming_terminal_status_on_staged_proposal(tmp_path: Path):
    # Plan 02 / P3 (incoming terminal-status guard): the durable terminal
    # transition belongs to the store's locked publish/discard path ONLY. A
    # public save of an otherwise identical STAGED proposal carrying an
    # incoming ``published``/``discarded`` status must be refused BEFORE any
    # write: writing it would mark the proposal decided without applying the
    # Knowledge Base changes. The refusal preserves the durable ``staged``
    # status, and the ordinary review decision keeps working afterwards.
    # Deterministic: direct ordered public-API calls, no sleeps, no threads.
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store
    )
    durable = store.get(staged.id)
    assert durable is not None and durable.status == "staged"

    for terminal_status in ("published", "discarded"):
        with pytest.raises(lw.ProposalTerminalStateError):
            store.save_proposal(msgspec.structs.replace(durable, status=terminal_status))

    # Nothing was written: the durable proposal keeps its reviewable staged
    # status, byte-for-byte, and the proposed page never reached the KB root.
    reloaded = lw.IngestStore(tmp_path / "ingest")
    preserved = reloaded.get(staged.id)
    assert preserved is not None and preserved.status == "staged"
    assert preserved == durable
    assert not (kb.root / "journey_related_page.md").exists()

    # The proposal is not stranded: the legitimate review decision still
    # publishes it through the pipeline's locked transition.
    published = pipeline.publish(staged.id)
    assert published.status == "published"
    assert (kb.root / "journey_related_page.md").exists()


def test_public_save_refuses_terminal_status_for_new_proposal_record(tmp_path: Path):
    # Same guard for a NEW record: a public save that would create a fresh
    # proposal directly in terminal ``published``/``discarded`` state is
    # refused before any write — no decided proposal may ever come into
    # existence outside the store's locked publish/discard transitions. A
    # reviewable save of the same fresh id stays allowed (with the reviewed
    # claim stripped, as for every public new-record save).
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    staged = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store
    )
    fresh_id = "fresh-proposal-record"
    assert store.get(fresh_id) is None

    for terminal_status in ("published", "discarded"):
        incoming = msgspec.structs.replace(staged, id=fresh_id, status=terminal_status)
        with pytest.raises(lw.ProposalTerminalStateError):
            store.save_proposal(incoming)
        assert store.get(fresh_id) is None

    # The refusal is about the terminal status, not the id: the same fresh
    # record persists once it carries a reviewable status (metadata stripped,
    # as for every public new-record save).
    store.save_proposal(msgspec.structs.replace(staged, id=fresh_id))
    created = store.get(fresh_id)
    assert created is not None and created.status == "staged"
    assert created.preconditions is None and created.reviewed_identity is None


OVERVIEW_REVISION = """---
title: "Lumio Overview"
aliases:
  - "Lumio"
tags:
  - "lumio"
  - "overview"
summary: "A high-level introduction to Lumio, revised after review."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "lumio-overview"
    title: "Lumio public landing page"
    url: "https://example.com/lumio"
synthetic: false
---

# Lumio Overview

Lumio is a deployable chat platform for trusted knowledge and data, revised.
"""


def test_overlapping_proposal_base_conflict_preserves_newer_content(tmp_path: Path):
    # B03/B04 (Plan 02 / P4): a proposal must publish only against the base it
    # was reviewed against. The revision below is staged while overview.md has
    # its reviewed bytes; an OVERLAPPING newer change then lands on the same
    # path (a cooperating publish or an external editor that ignores advisory
    # locks). Publishing must refuse BEFORE any candidate is applied, preserve
    # the newer on-disk content, and leave the proposal reviewable so it can be
    # re-staged — never silently rebased onto, or overwriting, newer content.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    newer = page_path.read_text(encoding="utf-8").replace(
        "deployable chat platform", "deployable chat and agent platform"
    )
    assert newer != page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    # The durable proposal carries the private reviewed base captured at
    # staging (digest of overview.md plus the consumed Control File state).
    durable = store.get(staged.id)
    assert durable is not None
    assert durable.preconditions
    assert any(item.path == "overview.md" for item in durable.preconditions)

    # The overlapping newer change lands AFTER review.
    page_path.write_text(newer, encoding="utf-8")

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "overview.md" in str(excinfo.value)
    assert "restage" in str(excinfo.value).lower()
    # The newer content was preserved byte for byte and the proposal stayed
    # reviewable: nothing was applied behind the refusal.
    assert page_path.read_text(encoding="utf-8") == newer
    fresh = lw.IngestStore(tmp_path / "ingest").get(staged.id)
    assert fresh is not None and fresh.status == "staged"


# ---------------------------------------------------------------------------
# Plan 02 / P4 (B03/B04): disjoint proposals after intervening changes.
# ---------------------------------------------------------------------------


def _disjoint_control() -> str:
    return (
        "version: 2\n"
        "mode: categorized\n"
        "categories:\n"
        "  - {name: concepts}\n"
        "ontology:\n"
        "  entity_types:\n"
        "    concept: {}\n"
        "  predicates:\n"
        "    see:\n"
        "      subject_types: [concept]\n"
        "      object_types: [concept]\n"
    )


def _disjoint_page(title: str, *, entity: str, claims: str = "", body: str = "Body.") -> str:
    claims_block = f"claims:\n{claims}" if claims else ""
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "sources:\n"
        f'  - id: "{entity}-src"\n'
        f'    title: "{title} source"\n'
        f'id: "entity:{entity}"\n'
        "entity_types:\n  - concept\n"
        f"{claims_block}"
        "synthetic: false\n"
        "---\n"
        f"# {title}\n\n{body}\n\n## Evidence\n\nSupporting evidence.\n"
    )


def _disjoint_revision(title: str, entity: str, summary: str) -> str:
    """A routed revision proposal for an existing categorized page."""
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        f'summary: "{summary}"\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "category: concepts\n"
        "type: concept\n"
        "durability_rationale: reviewed durable knowledge revision\n"
        "sources:\n"
        f'  - id: "{entity}-src"\n'
        f'    title: "{title} source"\n'
        f'id: "entity:{entity}"\n'
        "entity_types:\n  - concept\n"
        "synthetic: false\n"
        "---\n"
        f"# {title}\n\nRevised {title} body published by the intervening change.\n"
    )


def _disjoint_kb(tmp_path: Path):
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    (root / "lumio.yaml").write_text(_disjoint_control(), encoding="utf-8")
    (root / "concepts").mkdir()
    (root / "concepts/alpha.md").write_text(
        _disjoint_page("Alpha", entity="alpha"), encoding="utf-8"
    )
    (root / "concepts/gamma.md").write_text(
        _disjoint_page("Gamma", entity="gamma"), encoding="utf-8"
    )
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


DELTA_NEW_PAGE = (
    "---\n"
    'title: "Delta"\n'
    "aliases: []\n"
    'tags:\n  - "t"\n'
    'summary: "Delta summary."\n'
    'lifecycle: "approved"\n'
    'visibility: "public"\n'
    "category: concepts\n"
    "type: concept\n"
    "durability_rationale: reviewed durable knowledge page\n"
    "sources:\n"
    '  - id: "delta-src"\n'
    '    title: "Delta source"\n'
    'id: "entity:delta"\n'
    "entity_types:\n  - concept\n"
    "claims:\n"
    '  - id: "claim:delta-alpha-0"\n'
    '    predicate: "see"\n'
    '    object: "entity:alpha"\n'
    "    status: accepted\n"
    "    evidence:\n"
    '      - section: "Evidence"\n'
    "synthetic: false\n"
    "---\n"
    "# Delta\n\nDelta body.\n\n## Evidence\n\nSupporting evidence.\n"
)


def test_disjoint_proposals_publish_after_intervening_change(tmp_path: Path):
    # B03/B04 (Plan 02 / P4): preconditions are per affected path, never a
    # whole-Knowledge-Base digest. After proposal A publishes (an intervening
    # change to Alpha), the disjoint proposal B against Gamma still publishes:
    # its reviewed base (gamma.md plus the Control File) is untouched. The
    # publish-time gate then REVALIDATES THE FULL CURRENT CANDIDATE: a staged
    # new page whose accepted Claim targets entity:alpha becomes blocked after
    # a disjoint Page Removal of Alpha publishes, even though none of its own
    # reviewed paths moved. Deterministic: direct ordered calls, no sleeps.
    kb = _disjoint_kb(tmp_path)
    root = kb.root
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    proposal_a = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Revised Alpha.").encode("utf-8"),
        "text/markdown",
        "alpha-revision.md",
        kb,
        store=store,
    )
    proposal_b = lw.create_proposal_without_provider(
        _disjoint_revision("Gamma", "gamma", "Revised Gamma.").encode("utf-8"),
        "text/markdown",
        "gamma-revision.md",
        kb,
        store=store,
    )
    assert not proposal_a.blocked and not proposal_b.blocked

    # The intervening change: A publishes, mutating Alpha only.
    published_a = pipeline.publish(proposal_a.id)
    assert published_a.status == "published"

    # The disjoint proposal still publishes against its untouched base.
    published_b = pipeline.publish(proposal_b.id)
    assert published_b.status == "published"
    assert "Revised Alpha body" in (root / "concepts/alpha.md").read_text(encoding="utf-8")
    assert "Revised Gamma body" in (root / "concepts/gamma.md").read_text(encoding="utf-8")

    # Full-candidate revalidation: proposal C stages cleanly now, but a later
    # DISJOINT change (removing Alpha) introduces a blocking Claim conflict the
    # per-path preconditions cannot see — the full current candidate gate must.
    proposal_c = lw.create_proposal_without_provider(
        DELTA_NEW_PAGE.encode("utf-8"),
        "text/markdown",
        "delta-page.md",
        kb,
        store=store,
    )
    assert not proposal_c.blocked, proposal_c.validation_report
    removal = pipeline.propose_page_removal("Alpha")
    assert not removal.blocked, removal.validation_report
    assert pipeline.publish(removal.id).status == "published"
    assert not (root / "concepts/alpha.md").exists()

    with pytest.raises(lw.ProposalBlockedError) as excinfo:
        pipeline.publish(proposal_c.id)
    assert "entity:alpha" in str(excinfo.value)
    assert not (root / "concepts/delta.md").exists()
    fresh = lw.IngestStore(tmp_path / "ingest").get(proposal_c.id)
    assert fresh is not None and fresh.status == "staged"


def test_new_page_capture_records_creation_role_and_stays_blocked_if_occupied(
    tmp_path: Path,
):
    # P4 review v3 (BLOCKER): every non-move destination was persisted with
    # role "revision", so an ordinary new-page capture — whose destination is
    # reviewed as expected-absent — violated the PathPrecondition role
    # contract: "creation" is for new destinations expected to stay absent,
    # "revision" only for replaced/merged/renamed bytes. A new page must
    # stage with (creation, absent), and a destination occupied before
    # staging stays safely blocked (capture degrades to the Control File
    # record alone; publication refuses blocked proposals). Deterministic:
    # direct public-API calls, no sleeps.
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    staged = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"),
        "text/markdown",
        "related.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None and durable.preconditions
    snapshot = {(item.path, item.role, item.kind) for item in durable.preconditions}
    # The unoccupied new destination is reviewed as a creation that must
    # stay lexically absent — never as a revision.
    assert ("journey_related_page.md", "creation", "absent") in snapshot
    assert not any(
        item.path == "journey_related_page.md" and item.role == "revision"
        for item in durable.preconditions
    )

    # An occupant landing on the reviewed-new destination AFTER staging is
    # drift: the publish-time re-resolution can no longer produce the
    # reviewed (creation, absent) record, so publication refuses before any
    # byte is written and preserves the newer file.
    occupant = "An external file landed on the reviewed-new destination.\n"
    (kb.root / "journey_related_page.md").write_text(occupant, encoding="utf-8")
    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "journey_related_page.md" in str(excinfo.value)
    assert "reviewed base" in str(excinfo.value)
    assert (kb.root / "journey_related_page.md").read_text(encoding="utf-8") == occupant

    # A destination occupied BEFORE staging blocks the proposal itself: the
    # durable record captures only the Control File state (no record for the
    # occupied path) and publication refuses.
    occupied = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"),
        "text/markdown",
        "related.md",
        kb,
        store=store,
    )
    assert occupied.blocked, occupied.validation_report
    occupied_durable = store.get(occupied.id)
    assert occupied_durable is not None and occupied_durable.preconditions
    assert not any(
        item.path == "journey_related_page.md" for item in occupied_durable.preconditions
    )
    with pytest.raises(lw.ProposalBlockedError):
        pipeline.publish(occupied.id)
    assert (kb.root / "journey_related_page.md").read_text(encoding="utf-8") == occupant


# ---------------------------------------------------------------------------
# Plan 02 / P4 review: durable proposal tampering defense.
# ---------------------------------------------------------------------------


def test_altered_reviewable_save_cannot_retain_reviewed_preconditions(tmp_path: Path):
    # P4 review (BLOCKER: durable proposal tampering). IngestStore.save_proposal
    # permits replacing a REVIEWABLE proposal, so a caller could alter the
    # reviewed Markdown (or Control File / removals) while RETAINING the old
    # reviewed preconditions and content identity — and publish applied the
    # unreviewed content whenever the old paths had not drifted. An ordinary
    # reviewable save that alters mutation content must not retain the
    # reviewed claim: the durable record loses its reviewed metadata, publish
    # fails closed with restage guidance, and the Knowledge Base bytes never
    # change. Deterministic: direct public-API calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None
    assert durable.preconditions and durable.reviewed_identity

    tampered = msgspec.structs.replace(
        durable,
        proposed_pages=[
            msgspec.structs.replace(
                page, markdown=page.markdown.replace("revised", "UNREVIEWED TAMPERED")
            )
            for page in durable.proposed_pages
        ],
    )
    assert tampered.proposed_pages[0].markdown != durable.proposed_pages[0].markdown
    # The public save carries the OLD reviewed metadata verbatim.
    assert tampered.preconditions == durable.preconditions
    assert tampered.reviewed_identity == durable.reviewed_identity
    store.save_proposal(tampered)

    saved = store.get(staged.id)
    assert saved is not None
    assert saved.status == "staged"  # the save itself persisted (still reviewable)
    assert "UNREVIEWED TAMPERED" in saved.proposed_pages[0].markdown
    # ...but the altered content may not retain the reviewed claim: the
    # ordinary save stripped the stale reviewed metadata.
    assert saved.preconditions is None
    assert saved.reviewed_identity is None

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "restage" in str(excinfo.value).lower()
    # The Knowledge Base bytes are unchanged: the tampered content never applied.
    assert page_path.read_text(encoding="utf-8") == original


def test_public_save_cannot_rebind_reviewed_identity_for_altered_content(tmp_path: Path):
    # P4 FINAL review blocker: an ordinary save that ALTERS reviewed page
    # content used to KEEP its reviewed claim whenever the caller made the
    # record look self-consistent — recomputing the PUBLIC deterministic
    # content identity of the altered content and retaining the old captured
    # preconditions. Publication then accepted the forged identity against
    # the unchanged path set and applied unreviewed content. Caller-side
    # recomputation is not a reviewed binding — the identity is a public
    # deterministic hash any process can compute for any content — so the
    # public save now strips the reviewed metadata from EVERY altering save:
    # publication fails closed with restage guidance and the Knowledge Base
    # bytes never change. Deterministic: direct public-API calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None
    assert durable.preconditions and durable.reviewed_identity

    # Forge the exact shape the old bypass accepted: altered page Markdown
    # over the SAME paths, old preconditions retained verbatim, identity
    # recomputed from the altered content.
    tampered = msgspec.structs.replace(
        durable,
        proposed_pages=[
            msgspec.structs.replace(
                page, markdown=page.markdown.replace("revised", "UNREVIEWED TAMPERED")
            )
            for page in durable.proposed_pages
        ],
    )
    forged = msgspec.structs.replace(
        tampered, reviewed_identity=_recomputed_mutation_identity(tampered)
    )
    assert forged.preconditions == durable.preconditions
    assert _is_self_consistent_restage(forged)  # the old bypass accepted this record

    store.save_proposal(forged)

    saved = store.get(staged.id)
    assert saved is not None
    assert saved.status == "staged"  # the save itself persisted (still reviewable)
    assert "UNREVIEWED TAMPERED" in saved.proposed_pages[0].markdown
    # ...but the reviewed claim is gone: a caller-supplied identity — however
    # self-consistent — is never a freshly reviewed binding, so the altering
    # save strips the metadata entirely.
    assert saved.preconditions is None
    assert saved.reviewed_identity is None

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "restage" in str(excinfo.value).lower()
    # The Knowledge Base bytes are unchanged: the forged content never applied.
    assert page_path.read_text(encoding="utf-8") == original


def test_public_save_cannot_swap_precondition_digests_for_intervening_bytes(
    tmp_path: Path,
):
    # P4 FINAL review blocker (metadata binding): preconditions are excluded
    # from the mutation content identity, so a public save that keeps the
    # mutation content AND the reviewed content identity verbatim could still
    # replace the stored precondition DIGESTS with the digests of the current
    # intervening bytes. The next publish then passed BOTH the structural set
    # check (paths/roles unchanged) and the drift check (the forged digests
    # match the intervening bytes) and overwrote the newer content with pages
    # reviewed against the older base. Reviewed metadata is durable state,
    # never a caller-supplied value: an ordinary save of an existing
    # reviewable proposal keeps its reviewed claim only when the mutation
    # content AND both reviewed fields exactly match the durable record — any
    # divergence (forged digests, added/removed/reordered records, a replaced
    # identity, or new metadata over an unbound record) is stripped and
    # publication fails closed with restage guidance. Deterministic: direct
    # public-API calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    reviewed_bytes = page_path.read_text(encoding="utf-8")
    newer = reviewed_bytes.replace("deployable chat platform", "deployable chat and agent platform")
    assert newer != reviewed_bytes
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None and durable.preconditions and durable.reviewed_identity

    # The intervening revision lands AFTER review, changing overview.md.
    page_path.write_text(newer, encoding="utf-8")

    # The forged save keeps mutation content and reviewed identity verbatim
    # but swaps the reviewed overview.md digest for the digest of the CURRENT
    # intervening bytes — exactly the change that would let the next publish
    # pass drift and overwrite the newer revision.
    forged_preconditions = [
        msgspec.structs.replace(item, digest=hashlib.sha256(newer.encode("utf-8")).hexdigest())
        if item.path == "overview.md"
        else item
        for item in durable.preconditions
    ]
    forged = msgspec.structs.replace(durable, preconditions=forged_preconditions)
    assert forged.proposed_pages == durable.proposed_pages  # content unchanged
    assert forged.reviewed_identity == durable.reviewed_identity  # identity unchanged
    assert forged.preconditions != durable.preconditions  # ...only the digests lie

    store.save_proposal(forged)

    saved = store.get(staged.id)
    assert saved is not None
    assert saved.status == "staged"  # the save itself persisted (still reviewable)
    # ...but the caller-supplied reviewed metadata never persisted: the
    # reviewed claim is stripped, so publication fails closed.
    assert saved.preconditions is None
    assert saved.reviewed_identity is None

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "restage" in str(excinfo.value).lower()
    # The newer intervening bytes survive byte for byte: the forged reviewed
    # base never blessed overwriting them.
    assert page_path.read_text(encoding="utf-8") == newer


def test_public_save_strips_every_reviewed_metadata_divergence(tmp_path: Path):
    # The same binding rule covers the remaining divergence shapes beyond the
    # forged-digest attack: REORDERED precondition records, a REPLACED
    # reviewed identity, and metadata ADDED over a durable record that had
    # none — no public save may introduce or change reviewed metadata, even
    # over byte-identical content. Deterministic: direct public-API calls,
    # no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None and durable.preconditions and durable.reviewed_identity

    # (a) Reordering the precondition records (content and identity
    #     verbatim) is a metadata change and must not persist.
    reordered = msgspec.structs.replace(
        durable, preconditions=list(reversed(durable.preconditions))
    )
    assert reordered.preconditions != durable.preconditions
    store.save_proposal(reordered)
    saved = store.get(staged.id)
    assert saved is not None
    assert saved.preconditions is None and saved.reviewed_identity is None

    # A stripped record cannot publish; restaging through the seam re-binds.
    with pytest.raises(lw.ProposalPreconditionError):
        pipeline.publish(staged.id)
    restaged = pipeline.stage(
        msgspec.structs.replace(saved, preconditions=None, reviewed_identity=None)
    )
    assert restaged.reviewed_identity is not None
    rebound = store.get(staged.id)
    assert rebound is not None and rebound.preconditions

    # (b) Replacing ONLY the reviewed identity (content and precondition
    #     records verbatim) must not persist either.
    identity_swapped = msgspec.structs.replace(rebound, reviewed_identity="f" * 64)
    store.save_proposal(identity_swapped)
    saved = store.get(staged.id)
    assert saved is not None
    assert saved.preconditions is None and saved.reviewed_identity is None

    # (c) Metadata ADDED by a public save over a durable record that carries
    #     none must not bind: strip the record below, then try to re-attach
    #     the original reviewed metadata through an ordinary save.
    store.save_proposal(msgspec.structs.replace(saved, preconditions=None, reviewed_identity=None))
    unbound = store.get(staged.id)
    assert unbound is not None
    assert unbound.preconditions is None and unbound.reviewed_identity is None
    store.save_proposal(durable)  # identical content, original metadata
    saved = store.get(staged.id)
    assert saved is not None
    assert saved.preconditions is None and saved.reviewed_identity is None
    with pytest.raises(lw.ProposalPreconditionError):
        pipeline.publish(staged.id)
    # The Knowledge Base bytes never changed: nothing was ever applied.
    assert page_path.read_text(encoding="utf-8") == original


def test_public_save_of_new_record_cannot_bind_forged_reviewed_metadata(
    tmp_path: Path,
):
    # P4 remediation blocker: the metadata-divergence guard used to run only
    # over an EXISTING durable record (``durable is not None``), so a public
    # caller could save a NEW proposal id carrying caller-supplied
    # preconditions (captured from the current Knowledge Base) plus a
    # caller-computed ``reviewed_identity`` (the PUBLIC deterministic hash) —
    # both persisted verbatim — and publication then accepted the forged
    # reviewed base, bypassing the locked pipeline staging seam entirely.
    # A public save never binds reviewed metadata to a NEW record either:
    # only ProposalPipeline._save_reviewed_proposal →
    # IngestStore._write_reviewed_proposal may create a reviewed record, so
    # the persisted record stays unbound (inspectable, discardable) and
    # publication fails closed with restage guidance. Deterministic: direct
    # public-API calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    prior_registry = msgspec.json.encode(store.source_registry._state)

    # Hand-assembled NEW proposal (never staged): the assembly snapshot is
    # captured against the current Knowledge Base and the caller computes
    # the public deterministic content identity — exactly the
    # self-consistent shape the staging seam would bind, forged entirely
    # outside it.
    unstaged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
    )
    assert unstaged.preconditions  # the assembly snapshot rode along
    forged = msgspec.structs.replace(
        unstaged, reviewed_identity=_recomputed_mutation_identity(unstaged)
    )
    assert _is_self_consistent_restage(forged)  # the shape publish accepts
    assert store.get(forged.id) is None  # NEW record: no durable state exists

    store.save_proposal(forged)

    saved = store.get(forged.id)
    assert saved is not None
    assert saved.status == "staged"  # the save itself persisted (reviewable)
    # ...but the caller-supplied reviewed binding never persisted: a public
    # save strips the reviewed metadata from NEW records too.
    assert saved.preconditions is None
    assert saved.reviewed_identity is None

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(forged.id)
    assert "restage" in str(excinfo.value).lower()
    # The Knowledge Base bytes never changed and the Source Registry is
    # untouched: the forged new record never applied anything.
    assert page_path.read_text(encoding="utf-8") == original
    assert msgspec.json.encode(store.source_registry._state) == prior_registry


def test_content_identical_save_keeps_reviewed_metadata_and_publishes(tmp_path: Path):
    # The guard must stay surgical: an ordinary save that does NOT alter the
    # mutation content (attaching raw-byte path metadata, persisting a
    # status-carrying object) keeps the reviewed claim, because it still
    # describes exactly the durable content — publication re-verifies it
    # against the filesystem under the publish lock and applies the reviewed
    # pages normally. Deterministic: direct public-API calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None and durable.preconditions and durable.reviewed_identity

    store.save_proposal(durable, raw_path=Path("/raw") / staged.id / "overview.md")
    resaved = store.get(staged.id)
    assert resaved is not None
    assert resaved.raw_source_path == f"/raw/{staged.id}/overview.md"
    assert resaved.preconditions == durable.preconditions
    assert resaved.reviewed_identity == durable.reviewed_identity

    published = pipeline.publish(staged.id)
    assert published.status == "published"
    revised = page_path.read_text(encoding="utf-8")
    assert revised != original  # the reviewed content applied
    assert "revised" in revised


def test_restage_through_the_pipeline_rebinds_reviewed_state_and_publishes(tmp_path: Path):
    # The authorized staging seam must keep the LEGITIMATE flow working: a
    # proposal re-staged through the pipeline (metadata stripped, as built
    # outside the pipeline) gets a freshly captured reviewed base plus a
    # freshly bound content identity under the Knowledge Base + store locks,
    # and then publishes normally. Deterministic: direct calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert staged.preconditions is not None and staged.reviewed_identity is not None

    stripped = msgspec.structs.replace(staged, preconditions=None, reviewed_identity=None)
    restaged = pipeline.stage(stripped)
    assert restaged.status == "staged"
    durable = store.get(staged.id)
    assert durable is not None
    # The seam re-bound the reviewed state: captured base and identity are
    # present again and identical to the original binding (content and base
    # are unchanged).
    assert durable.preconditions is not None and durable.preconditions
    assert durable.preconditions == staged.preconditions
    assert durable.reviewed_identity == staged.reviewed_identity

    published = pipeline.publish(staged.id)
    assert published.status == "published"
    revised = page_path.read_text(encoding="utf-8")
    assert revised != original
    assert "revised" in revised


def test_reviewed_staging_seam_refuses_unbound_and_terminal_records(tmp_path: Path):
    # The authorized staging seam is the only writer of freshly bound
    # reviewed metadata, so it must fail closed twice: a record whose binding
    # was never made (identity stripped/mismatched) is refused — a pipeline
    # bug must not dress altered content as reviewed — and a durable
    # terminal state is never replaced, exactly like the ordinary save.
    # Deterministic: direct private-seam calls, no sleeps.
    kb = _kb(tmp_path)
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        RELATED_PAGE.encode("utf-8"), "text/markdown", "related.md", kb, store=store
    )
    assert staged.preconditions is not None and staged.reviewed_identity is not None
    durable_bytes_before = (store.proposals_dir / f"{staged.id}.json").read_bytes()

    unbound = msgspec.structs.replace(staged, reviewed_identity=None)
    with pytest.raises(lw.ProposalPipelineError, match="freshly bound"):
        pipeline._save_reviewed_proposal(unbound)
    identity_mismatch = msgspec.structs.replace(staged, reviewed_identity="0" * 64)
    with pytest.raises(lw.ProposalPipelineError, match="freshly bound"):
        pipeline._save_reviewed_proposal(identity_mismatch)
    # Nothing was written: the durable record is byte-identical.
    assert (store.proposals_dir / f"{staged.id}.json").read_bytes() == durable_bytes_before

    # Terminal safety: a stale second instance holding a reviewable view
    # cannot write through the seam after the proposal was discarded.
    stale_view = lw.ProposalPipeline(kb, lw.IngestStore(tmp_path / "ingest")).review(staged.id)
    assert stale_view is not None and stale_view.status == "staged"
    assert pipeline.discard(staged.id) is not None
    with pytest.raises(lw.ProposalTerminalStateError):
        pipeline._save_reviewed_proposal(stale_view)
    reloaded = lw.IngestStore(tmp_path / "ingest").get(staged.id)
    assert reloaded is not None and reloaded.status == "discarded"


def test_publish_refuses_durable_content_diverging_from_reviewed_identity(tmp_path: Path):
    # Second layer of the same defense: a durable record rewritten BELOW the
    # public API (the store's JSON edited directly, old preconditions and
    # identity retained verbatim) is still refused under the publish lock —
    # the content identity is recomputed from the DURABLE record and must
    # equal the stored reviewed identity. Old serialized proposals stay
    # decodable and inspectable, but they cannot publish.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report

    proposal_path = store.proposals_dir / f"{staged.id}.json"
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    record["proposed_pages"][0]["markdown"] = record["proposed_pages"][0]["markdown"].replace(
        "revised", "UNREVIEWED TAMPERED"
    )
    proposal_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "content identity" in str(excinfo.value)
    assert "restage" in str(excinfo.value).lower()
    assert "UNREVIEWED TAMPERED" not in page_path.read_text(encoding="utf-8")
    assert page_path.read_text(encoding="utf-8") == original


def test_publish_refuses_preconditions_that_no_longer_describe_the_proposal(tmp_path: Path):
    # Third layer: a durable record rewritten BELOW the public API whose
    # content identity was re-forged to match its altered content still
    # cannot retain the old precondition records for changed
    # destinations/removals: the affected-path set is freshly resolved from
    # the durable proposal under the publish lock and must match the stored
    # metadata. (Through the public API this record never even persists with
    # its claim — every altering save strips the reviewed metadata; see the
    # forged-identity regression above. Only a below-API rewrite can pair a
    # re-forged identity with foreign preconditions.) The forged removal
    # never applies and the removed page's bytes stay unchanged.
    # Deterministic: direct durable-JSON edits, no sleeps.
    kb = _kb(tmp_path)
    technology_path = kb.root / "technology.md"
    original_technology = technology_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    staged = lw.create_proposal_without_provider(
        OVERVIEW_REVISION.encode("utf-8"),
        "text/markdown",
        "overview-revision.md",
        kb,
        store=store,
    )
    assert not staged.blocked, staged.validation_report
    durable = store.get(staged.id)
    assert durable is not None and durable.preconditions

    altered = msgspec.structs.replace(
        durable,
        removed_pages=[lw.PageRemoval(title="Technology Stack", lost_support_reason="forged")],
    )
    forged = msgspec.structs.replace(
        altered, reviewed_identity=_recomputed_mutation_identity(altered)
    )
    proposal_path = store.proposals_dir / f"{staged.id}.json"
    proposal_path.write_bytes(msgspec.json.encode(forged))
    saved = store.get(staged.id)
    assert saved is not None
    assert saved.removed_pages and saved.reviewed_identity  # forged below the API

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(staged.id)
    assert "technology.md" in str(excinfo.value)
    assert "restage" in str(excinfo.value).lower()
    assert technology_path.read_text(encoding="utf-8") == original_technology


# ---------------------------------------------------------------------------
# Plan 02 / P4 review: stale-assembly defense (assemble -> stage window).
# ---------------------------------------------------------------------------


def test_stage_refuses_assembly_snapshot_drift(tmp_path: Path):
    # P4 review (BLOCKER: stale self._kb during assembly/stage). assemble
    # built the proposed pages, diff, and review report from the loaded KB
    # snapshot OUTSIDE the staging locks, and stage then captured
    # preconditions from CURRENT disk — so an overlapping change landing
    # between assembly and staging became the captured base while the review
    # still described the older snapshot, and publish could overwrite it.
    # Every assemble route now binds its snapshot at assembly, and stage
    # re-verifies it under the KB + store locks and refuses a drifted
    # assembly — never a recapture that would bless stale content.
    # Deterministic: direct ordered calls, no sleeps.
    kb = _kb(tmp_path)
    page_path = kb.root / "overview.md"
    original = page_path.read_text(encoding="utf-8")
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    proposal = pipeline.assemble(
        OVERVIEW_REVISION,
        lw.SourceProvenance(None, None, "text/markdown"),
        "overview-revision.md",
    )
    assert not proposal.blocked, proposal.validation_report
    # The assembly-time reviewed snapshot rides on the UNSTAGED proposal.
    assert proposal.preconditions
    assert any(
        item.path == "overview.md"
        and item.kind == "file"
        and item.digest == hashlib.sha256(original.encode("utf-8")).hexdigest()
        for item in proposal.preconditions
    )
    assert proposal.reviewed_identity is None  # identity binds only at staging

    # The overlapping change lands AFTER assembly, BEFORE staging.
    drifted = original.replace("deployable chat platform", "deployable chat and agent platform")
    assert drifted != original
    page_path.write_text(drifted, encoding="utf-8")

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.stage(proposal)
    assert "overview.md" in str(excinfo.value)
    assert "re-assemble" in str(excinfo.value).lower()
    # Nothing was persisted: no reviewed record blesses the stale assembly,
    # and the newer on-disk content is preserved.
    assert store.get(proposal.id) is None
    assert page_path.read_text(encoding="utf-8") == drifted

    # The restage guidance works: re-assembly against the current state
    # stages cleanly, and its verified snapshot describes the current bytes.
    restaged = pipeline.stage(
        pipeline.assemble(
            OVERVIEW_REVISION,
            lw.SourceProvenance(None, None, "text/markdown"),
            "overview-revision.md",
        )
    )
    assert restaged.status == "staged"
    assert restaged.reviewed_identity
    restaged_preconditions = restaged.preconditions
    assert restaged_preconditions
    assert any(
        item.path == "overview.md"
        and item.digest == hashlib.sha256(drifted.encode("utf-8")).hexdigest()
        for item in restaged_preconditions
    )

    # The same refusal covers the Control File state consumed at assembly:
    # a Control File appearing after assembly drifts the captured absence.
    control_proposal = pipeline.assemble(
        RELATED_PAGE, lw.SourceProvenance(None, None, "text/markdown"), "related.md"
    )
    assert not control_proposal.blocked, control_proposal.validation_report
    control_snapshot = control_proposal.preconditions
    assert control_snapshot
    assert any(item.path == "lumio.yaml" for item in control_snapshot)
    (kb.root / "lumio.yaml").write_text(_disjoint_control(), encoding="utf-8")
    with pytest.raises(lw.ProposalPreconditionError) as control_excinfo:
        pipeline.stage(control_proposal)
    assert "lumio.yaml" in str(control_excinfo.value)
    assert store.get(control_proposal.id) is None


# ---------------------------------------------------------------------------
# Plan 02 / P5 (B05): ordinary local publish failures restore the COMPLETE
# mutation — every touched page/control/reserved-artifact/Activity-Log byte
# and existence, the durable proposal metadata (still reviewable — never a
# partial published marker), and the private registry state — from a
# pre-mutation backup kept outside canonical Knowledge Base content.
# ---------------------------------------------------------------------------


def _rollback_control(*, hot_pins: list[str] | None = None) -> str:
    """The categorized Control File used by the P5 rollback journeys."""
    pins_block = ""
    if hot_pins:
        pin_lines = "\n".join(f'  - {{title: "{p}"}}' for p in hot_pins)
        pins_block = f"hot_index:\n{pin_lines}\n"
    return (
        "version: 2\n"
        "mode: categorized\n"
        "categories:\n"
        "  - {name: concepts}\n"
        "  - {name: archive}\n"
        "ontology:\n"
        "  entity_types:\n"
        "    concept: {}\n"
        "  predicates:\n"
        "    see:\n"
        "      subject_types: [concept]\n"
        "      object_types: [concept]\n"
        f"{pins_block}"
    )


def _rollback_kb(tmp_path: Path, *, hot_pins: list[str] | None = None, alpha_claims: str = ""):
    """A categorized KB with Alpha and Gamma pages (and optional Hot pins)."""
    root = tmp_path / "kb"
    root.mkdir(parents=True)
    (root / "lumio.yaml").write_text(_rollback_control(hot_pins=hot_pins), encoding="utf-8")
    (root / "concepts").mkdir()
    (root / "concepts/alpha.md").write_text(
        _disjoint_page("Alpha", entity="alpha", claims=alpha_claims), encoding="utf-8"
    )
    (root / "concepts/gamma.md").write_text(
        _disjoint_page("Gamma", entity="gamma"), encoding="utf-8"
    )
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def _rollback_snapshot(root: Path) -> dict[str, bytes | None]:
    """Byte/existence snapshot of every file under root (sorted, relative)."""
    snapshot: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            path = Path(dirpath) / name
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    return snapshot


def _durable_state_snapshot(store: lw.IngestStore, proposal_id: str) -> dict[str, bytes | None]:
    """Snapshot the durable proposal JSON and private registry state bytes."""
    registry_path = store.source_registry.root / "sources.json"
    return {
        "proposal.json": (store.proposals_dir / f"{proposal_id}.json").read_bytes(),
        "registry.json": registry_path.read_bytes() if registry_path.exists() else None,
    }


def _existing_backups() -> set[Path]:
    from lumio_wiki.mutation import backup_root

    backup_root_dir = backup_root()
    return set(backup_root_dir.iterdir()) if backup_root_dir.exists() else set()


def test_local_publish_failure_restores_complete_mutation(tmp_path, monkeypatch):
    # B05 (Plan 02 / P5): an ordinary local failure in the MIDDLE of the live
    # mutation — here the SECOND page's live write dies after only the first
    # revision landed — must restore the COMPLETE pre-mutation state:
    # every touched page/Control File/reserved artifact/Activity Log byte and
    # existence, the durable proposal metadata (still reviewable — never a
    # partial published marker), and the private registry state. The backup
    # lives outside the Knowledge Base, is cleaned up after the successful
    # restoration, and the proposal publishes on retry. Deterministic: an
    # injected OSError on one checked live write, no sleeps.
    import lumio_wiki.publish as publish_module

    kb = _rollback_kb(tmp_path)
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    # A first successful publish establishes the real pre-mutation state:
    # revised Alpha content and generated reserved artifacts.
    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert not baseline.blocked, baseline.validation_report
    assert pipeline.publish(baseline.id).status == "published"

    # The failing proposal: revise Gamma (first live write, succeeds), then
    # move Alpha into the archive category — whose destination write is the
    # SECOND live write and is injected to fail — then add Delta (never
    # reached).
    move_segment = (
        "---\n"
        'title: "Alpha"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        'summary: "Relocated Alpha page."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "category: archive\n"
        "type: concept\n"
        "durability_rationale: reviewed durable knowledge move\n"
        "move_from_path: concepts/alpha.md\n"
        "sources:\n"
        '  - id: "alpha-src"\n'
        '    title: "Alpha source"\n'
        'id: "entity:alpha"\n'
        "entity_types:\n  - concept\n"
        "synthetic: false\n"
        "---\n"
        "# Alpha\n\nThe Alpha page relocated into the archive category.\n"
    )
    failing_markdown = (
        _disjoint_revision("Gamma", "gamma", "Revised Gamma.")
        + "\n<!-- lumio: page-break -->\n"
        + move_segment
        + "\n<!-- lumio: page-break -->\n"
        + DELTA_NEW_PAGE
    )
    failing = lw.create_proposal_without_provider(
        failing_markdown.encode("utf-8"),
        "text/markdown",
        "gamma-move-delta.md",
        kb,
        store=store,
    )
    assert not failing.blocked, failing.validation_report
    durable = store.get(failing.id)
    assert durable is not None and durable.preconditions

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, failing.id)
    backups_before = _existing_backups()

    original_write_destination = publish_module._write_destination
    attempted: list[str] = []

    def fail_second_live_write(destination, working_root):
        if Path(working_root) == root and destination.relative_path == "archive/alpha.md":
            attempted.append(destination.relative_path)
            # publish._write_destination creates the destination's parent
            # directories BEFORE the byte write, so the real second-live-write
            # failure window is "fresh directory created, bytes not written" —
            # reproduce exactly that window, then die.
            (root / "archive").mkdir(parents=True, exist_ok=True)
            raise OSError("injected second-page live write failure")
        return original_write_destination(destination, working_root)

    monkeypatch.setattr(publish_module, "_write_destination", fail_second_live_write)

    with pytest.raises(OSError, match="injected second-page live write failure"):
        pipeline.publish(failing.id)

    # Only the SECOND live write was injected; the first (the Gamma revision)
    # landed before it and the third (Delta) was never reached.
    assert attempted == ["archive/alpha.md"]

    # The complete pre-mutation KB state is restored byte for byte: the
    # landed Gamma revision reverts, the fresh archive directory the move
    # created is pruned again, Alpha never left concepts/, and the never-
    # written Delta page is absent; the Control File, reserved artifacts,
    # and (absent) Activity Log match.
    assert _rollback_snapshot(root) == files_before
    assert not (root / "concepts/delta.md").exists()
    assert not (root / "archive/alpha.md").exists()
    assert not (root / "archive").exists()  # created directory pruned too
    assert (root / "concepts/alpha.md").is_file()

    # Durable proposal and registry state restored byte for byte: no partial
    # published marker, the proposal is still reviewable.
    assert _durable_state_snapshot(store, failing.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(failing.id)
    assert reloaded is not None and reloaded.status == "staged"
    reviewable = pipeline.review(failing.id)
    assert reviewable is not None and lw.is_reviewable_proposal(reviewable)

    # The pre-mutation backup lived outside the KB and was cleaned up after
    # the successful restoration.
    assert _existing_backups() == backups_before

    # Recovery works: with the injection gone the same durable proposal
    # publishes completely.
    monkeypatch.undo()
    republished = pipeline.publish(failing.id)
    assert republished.status == "published"
    assert (root / "concepts/delta.md").is_file()
    assert (root / "archive/alpha.md").is_file()
    assert not (root / "concepts/alpha.md").exists()


def test_local_publish_third_write_failure_restores_complete_mutation(tmp_path, monkeypatch):
    # B05 (Plan 02 / P5) retained LATER-write coverage: the THIRD page's live
    # write dies after a revision AND a move already landed. The complete
    # pre-mutation state must still come back — the revised Gamma, the
    # performed move (source unlinked, fresh archive directory pruned), the
    # Control File, reserved artifacts, Activity Log, durable proposal, and
    # registry state — and the proposal publishes on retry. Deterministic:
    # an injected OSError on one checked live write, no sleeps.
    import lumio_wiki.publish as publish_module

    kb = _rollback_kb(tmp_path)
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert not baseline.blocked, baseline.validation_report
    assert pipeline.publish(baseline.id).status == "published"

    # Write order: revise Gamma (first live write, succeeds), move Alpha into
    # the archive category (second write plus source unlink, succeeds), then
    # add Delta — whose live write is injected to fail.
    move_segment = (
        "---\n"
        'title: "Alpha"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        'summary: "Relocated Alpha page."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "category: archive\n"
        "type: concept\n"
        "durability_rationale: reviewed durable knowledge move\n"
        "move_from_path: concepts/alpha.md\n"
        "sources:\n"
        '  - id: "alpha-src"\n'
        '    title: "Alpha source"\n'
        'id: "entity:alpha"\n'
        "entity_types:\n  - concept\n"
        "synthetic: false\n"
        "---\n"
        "# Alpha\n\nThe Alpha page relocated into the archive category.\n"
    )
    failing_markdown = (
        _disjoint_revision("Gamma", "gamma", "Revised Gamma.")
        + "\n<!-- lumio: page-break -->\n"
        + move_segment
        + "\n<!-- lumio: page-break -->\n"
        + DELTA_NEW_PAGE
    )
    failing = lw.create_proposal_without_provider(
        failing_markdown.encode("utf-8"),
        "text/markdown",
        "gamma-move-delta.md",
        kb,
        store=store,
    )
    assert not failing.blocked, failing.validation_report
    durable = store.get(failing.id)
    assert durable is not None and durable.preconditions

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, failing.id)
    backups_before = _existing_backups()

    original_write_destination = publish_module._write_destination
    attempted: list[str] = []

    def fail_delta_live_write(destination, working_root):
        if Path(working_root) == root and destination.relative_path == "concepts/delta.md":
            attempted.append(destination.relative_path)
            raise OSError("injected third-page live write failure")
        return original_write_destination(destination, working_root)

    monkeypatch.setattr(publish_module, "_write_destination", fail_delta_live_write)

    with pytest.raises(OSError, match="injected third-page live write failure"):
        pipeline.publish(failing.id)

    # The proposal's earlier writes landed, then the injected failure fired.
    assert attempted == ["concepts/delta.md"]

    # The complete pre-mutation KB state is restored byte for byte: the
    # revised Gamma, the performed move, and the failed new page all revert;
    # the Control File, reserved artifacts, and (absent) Activity Log match.
    assert _rollback_snapshot(root) == files_before
    assert not (root / "concepts/delta.md").exists()
    assert not (root / "archive/alpha.md").exists()
    assert not (root / "archive").exists()  # created directory pruned too
    assert (root / "concepts/alpha.md").is_file()

    # Durable proposal and registry state restored byte for byte: no partial
    # published marker, the proposal is still reviewable.
    assert _durable_state_snapshot(store, failing.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(failing.id)
    assert reloaded is not None and reloaded.status == "staged"
    reviewable = pipeline.review(failing.id)
    assert reviewable is not None and lw.is_reviewable_proposal(reviewable)

    # The pre-mutation backup lived outside the KB and was cleaned up after
    # the successful restoration.
    assert _existing_backups() == backups_before

    # Recovery works: with the injection gone the same durable proposal
    # publishes completely.
    monkeypatch.undo()
    republished = pipeline.publish(failing.id)
    assert republished.status == "published"
    assert (root / "concepts/delta.md").is_file()
    assert (root / "archive/alpha.md").is_file()
    assert not (root / "concepts/alpha.md").exists()


def test_reserved_artifact_regeneration_failure_restores_complete_mutation(tmp_path, monkeypatch):
    # B05 focused injection: the reserved-artifact regeneration step dies
    # after the Navigation Indexes were already recommitted (a mid-step
    # failure no existing journey covers — the removal and Control File pin
    # drop already landed). The rollback must restore the removed page, the
    # rewritten Control File, and the half-regenerated reserved artifacts to
    # their exact pre-mutation bytes, keep the proposal reviewable, and let a
    # retry publish completely. Deterministic: injected OSError, no sleeps.
    import lumio_wiki.proposal_pipeline as proposal_pipeline_module
    from lumio_wiki.knowledge_base import (
        NAV_INDEX_BASENAME,
        _commit_reserved_artifacts,
        generate_navigation_indexes,
        load_knowledge_base,
    )

    kb = _rollback_kb(tmp_path, hot_pins=["Alpha"])
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    removal = pipeline.propose_page_removal("Alpha")
    assert not removal.blocked, removal.validation_report

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, removal.id)

    def fail_after_navigation_recommit(path):
        current_kb, _report = load_knowledge_base(path)
        _commit_reserved_artifacts(
            Path(path),
            generate_navigation_indexes(current_kb.pages),
            basename=NAV_INDEX_BASENAME,
            force_write=True,
        )
        raise OSError("injected reserved artifact regeneration failure")

    monkeypatch.setattr(
        proposal_pipeline_module,
        "publish_reserved_artifacts",
        fail_after_navigation_recommit,
    )

    with pytest.raises(OSError, match="injected reserved artifact regeneration failure"):
        pipeline.publish(removal.id)

    # The complete pre-mutation state is restored: the removed page, the pin
    # dropped from the Control File, and the already-recommitted Navigation
    # Indexes are all back to their exact reviewed bytes.
    assert _rollback_snapshot(root) == files_before
    assert (root / "concepts/alpha.md").is_file()
    assert "Alpha" in (root / "index.md").read_text(encoding="utf-8")

    assert _durable_state_snapshot(store, removal.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(removal.id)
    assert reloaded is not None and reloaded.status == "staged"
    reviewable = pipeline.review(removal.id)
    assert reviewable is not None and lw.is_reviewable_proposal(reviewable)

    monkeypatch.undo()
    republished = pipeline.publish(removal.id)
    assert republished.status == "published"
    assert not (root / "concepts/alpha.md").exists()


def test_activity_log_append_failure_restores_complete_mutation(tmp_path, monkeypatch):
    # B05 focused injection: the Activity Log append dies after the page
    # removal, Control File pin drop, and reserved-artifact regeneration (and
    # the Hot Index prune) already landed — until S1 the log is part of the
    # mutation boundary, so the rollback must restore it and every earlier
    # byte too, including the pruned Hot Index and the pre-pin Control File.
    # Deterministic: injected OSError, no sleeps.
    import lumio_wiki.proposal_pipeline as proposal_pipeline_module

    alpha_claims = (
        '  - id: "claim:alpha-gamma-0"\n'
        '    predicate: "see"\n'
        '    object: "entity:gamma"\n'
        "    status: accepted\n"
        '    evidence:\n      - section: "Evidence"\n'
    )
    kb = _rollback_kb(tmp_path, hot_pins=["Alpha"], alpha_claims=alpha_claims)
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    # A first successful removal gives the KB its real pre-failure state:
    # repaired Alpha (claim to Gamma dropped), pruned artifacts, and an
    # Activity Log with one recorded transition.
    gamma_removal = pipeline.propose_page_removal("Gamma")
    assert pipeline.publish(gamma_removal.id).status == "published"
    assert not (root / "concepts/gamma.md").exists()
    log_before = (root / "log.md").read_bytes()
    assert b"page-removal" in log_before

    failing_removal = pipeline.propose_page_removal("Alpha")
    assert not failing_removal.blocked, failing_removal.validation_report

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, failing_removal.id)

    def fail_log_append(path, entry):
        raise OSError("injected activity log append failure")

    monkeypatch.setattr(proposal_pipeline_module, "append_activity_log_entry", fail_log_append)

    with pytest.raises(OSError, match="injected activity log append failure"):
        pipeline.publish(failing_removal.id)

    # Every byte of the pre-mutation state came back: the removed Alpha page,
    # the pinned Control File, the regenerated Navigation Indexes and pruned
    # Hot Index, and the Activity Log without the failed entry.
    assert _rollback_snapshot(root) == files_before
    assert (root / "log.md").read_bytes() == log_before
    assert (root / "concepts/alpha.md").is_file()
    assert (root / "hot.md").is_file()  # the pruned pin artifact is restored

    assert _durable_state_snapshot(store, failing_removal.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(failing_removal.id)
    assert reloaded is not None and reloaded.status == "staged"
    reviewable = pipeline.review(failing_removal.id)
    assert reviewable is not None and lw.is_reviewable_proposal(reviewable)

    monkeypatch.undo()
    republished = pipeline.publish(failing_removal.id)
    assert republished.status == "published"
    assert not (root / "concepts/alpha.md").exists()
    assert b"removed page(s): Alpha" in (root / "log.md").read_bytes()


def test_publish_rejects_external_symlink_activity_log_without_touching_the_target(
    tmp_path, monkeypatch
):
    # P5 review blocker: an existing root log.md SYMLINK (external target
    # included) was accepted and the Activity Log append WROTE THROUGH the
    # link into its referent — a later failure restored only the link while
    # the appended bytes stayed in the (possibly external) target, so the
    # pre-mutation Activity Log could never be restored completely. The
    # publish gate now rejects every non-regular log.md occupant on no-follow
    # lstat BEFORE the backup and before any page or Control File write: the
    # injected late terminal-write failure below must stay unreachable (its
    # OSError would otherwise surface instead of the gate's conflict), the
    # link and its target bytes stay exactly as they were, and no Knowledge
    # Base byte moves. Deterministic: byte equality proves the target was
    # never opened; no sleeps.
    #
    # P5 review remediation (gate ordering): the classification must ALSO
    # precede candidate validation, because the candidate gate mirrors the
    # live tree into a throwaway candidate and MATERIALIZES a symlink's
    # read-through content by OPENING its referent (shutil.copyfile) — an
    # external log.md referent must never even be opened before the occupant
    # is classified. Two fail-fast guards turn a regression into an immediate
    # failure instead of a silent pass: any candidate-mirroring copy of the
    # external referent raises, and candidate validation running at all
    # before the gate raises. The gate's own lstat never opens the entry.
    kb = _rollback_kb(tmp_path, hot_pins=["Alpha"])
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    external_log = tmp_path / "external-activity-log.md"
    # A REAL external Activity Log carries the reserved activity-log marker,
    # so staging validation reads through the link, classifies a valid
    # reserved artifact, and stages the removal — exactly the accepted
    # pre-gate state the review reproduced with its late store.publish
    # failure (the append wrote through the link into these bytes).
    external_bytes = (
        b"---\nlumio:\n  artifact: activity-log\n  version: 1\n---"
        b"\n\n# Activity Log\n\n2026-01-01T00:00:00Z publish: pre-existing external history\n"
    )
    external_log.write_bytes(external_bytes)
    log_link = root / "log.md"
    link_text = os.path.relpath(external_log, root)
    os.symlink(link_text, log_link)

    removal = pipeline.propose_page_removal("Gamma")
    assert not removal.blocked, removal.validation_report

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, removal.id)
    backups_before = _existing_backups()

    def unreachable_terminal_write(_proposal_id):
        raise OSError("terminal proposal persistence must never run")

    monkeypatch.setattr(store, "publish", unreachable_terminal_write)

    # Fail-fast guard 1 (candidate mirroring): candidate validation copies the
    # live tree and materializes an external symlink's read-through content
    # with shutil.copyfile FROM the referent. Any copy sourced from the
    # external Activity Log target means the unsafe log.md reached candidate
    # mirroring before classification — fail immediately instead of silently
    # reading the external file.
    external_referent = external_log.resolve()
    real_copyfile = shutil.copyfile

    def _never_copy_the_external_referent(src, dst, **kwargs):
        if Path(src).resolve() == external_referent:
            raise AssertionError(
                "candidate mirroring opened the external log.md referent "
                f"({src} -> {dst}) before the unsafe-activity-log gate"
            )
        return real_copyfile(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", _never_copy_the_external_referent)

    # Fail-fast guard 2 (gate ordering): the publish path must classify the
    # unsafe log.md BEFORE candidate validation runs. A regression to the old
    # order surfaces as this AssertionError instead of the gate's conflict.
    def _candidate_gate_must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "candidate validation ran before the unsafe-activity-log gate "
            "rejected the non-regular log.md occupant"
        )

    monkeypatch.setattr(
        "lumio_wiki.proposal_pipeline.validate_candidate_knowledge_base",
        _candidate_gate_must_not_run,
    )

    with pytest.raises(DestinationConflict, match="symlink"):
        pipeline.publish(removal.id)

    # The gate rejected the publish BEFORE any live mutation: the link still
    # points at the external file, the target bytes are unchanged (the append
    # never followed the link), and no page, Control File, or registry byte
    # moved — the injected terminal-write failure was never reached.
    assert log_link.is_symlink() and os.readlink(log_link) == link_text
    assert external_log.read_bytes() == external_bytes
    assert _rollback_snapshot(root) == files_before
    assert _durable_state_snapshot(store, removal.id) == durable_before
    # The rejection predates the backup: no pre-mutation backup was created.
    assert _existing_backups() == backups_before

    # Recovery: removing the symlink lets the same reviewable proposal
    # publish; the append creates a fresh regular log.md and the transition
    # is recorded there — the external history file is never touched.
    monkeypatch.undo()
    log_link.unlink()
    published = pipeline.publish(removal.id)
    assert published.status == "published"
    assert not (root / "concepts/gamma.md").exists()
    assert log_link.is_file() and not log_link.is_symlink()
    log_after = (root / "log.md").read_bytes()
    assert external_bytes not in log_after  # the external file stayed external
    assert b"removed page(s): Gamma" in log_after
    assert external_log.read_bytes() == external_bytes


@pytest.mark.parametrize(
    ("occupant", "expected_message"),
    [
        ("fifo", "special file"),
        ("socket", "special file"),
        ("directory", "directory"),
        ("dangling-symlink", "symlink"),
    ],
)
def test_publish_rejects_special_activity_log_occupants_without_opening_them(
    tmp_path, monkeypatch, occupant, expected_message
):
    # P5 review blocker: a FIFO named log.md staged and published "validly"
    # and the append's open("a") blocked forever waiting for a writer; a
    # directory occupant failed only AFTER the page and Control File writes
    # had already landed; a dangling symlink was treated as an absent path
    # and silently replaced link-and-all. The publish gate now rejects every
    # non-regular occupant on non-following lstat BEFORE any live byte. The
    # guarded Path.open turns a regression into a fast failure instead of a
    # hung suite: the gate itself only ever lstats the entry.
    kb = _rollback_kb(tmp_path, hot_pins=["Alpha"])
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    log_path = root / "log.md"

    if occupant == "fifo":
        os.mkfifo(log_path)
    elif occupant == "socket":
        occupant_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        occupant_socket.bind(str(log_path))
        occupant_socket.close()  # the filesystem entry persists after close
    elif occupant == "directory":
        log_path.mkdir()
    else:  # dangling-symlink
        os.symlink(tmp_path / "missing-log-target.md", log_path)

    real_open = Path.open

    def _never_open_the_log(self, *args, **kwargs):
        if self == log_path:
            raise AssertionError("the publish path must never open a special log.md occupant")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _never_open_the_log)

    gamma_before = (root / "concepts/gamma.md").read_bytes()
    control_before = (root / "lumio.yaml").read_bytes()

    def _entry_names(base: Path) -> set[str]:
        names: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(base):
            for name in dirnames + filenames:
                names.add((Path(dirpath) / name).relative_to(base).as_posix())
        return names

    entries_before = _entry_names(root)

    removal = pipeline.propose_page_removal("Gamma")
    assert not removal.blocked, removal.validation_report

    with pytest.raises(DestinationConflict, match=expected_message):
        pipeline.publish(removal.id)

    # Nothing was mutated: the occupant kept its exact non-regular type on
    # lstat, every Knowledge Base entry is unchanged, the Gamma page and the
    # Control File still hold their reviewed bytes, and the proposal is still
    # reviewable for a corrected retry.
    if occupant == "fifo":
        assert log_path.is_fifo()
    elif occupant == "socket":
        assert log_path.is_socket()
    elif occupant == "directory":
        assert log_path.is_dir() and not log_path.is_symlink()
    else:
        assert log_path.is_symlink()
    assert _entry_names(root) == entries_before
    assert (root / "concepts/gamma.md").read_bytes() == gamma_before
    assert (root / "lumio.yaml").read_bytes() == control_before
    reviewable = pipeline.review(removal.id)
    assert reviewable is not None and lw.is_reviewable_proposal(reviewable)

    # Recovery: removing the non-regular occupant lets the same proposal
    # publish; the append creates a fresh regular log.md (its reserved
    # marker written by the Activity Log itself, never an empty file) and
    # records the transition there.
    monkeypatch.undo()
    if occupant == "directory":
        log_path.rmdir()
    else:
        log_path.unlink()
    published = pipeline.publish(removal.id)
    assert published.status == "published"
    assert not (root / "concepts/gamma.md").exists()
    assert b"removed page(s): Gamma" in log_path.read_bytes()


def test_rollback_failure_raises_actionable_recovery_error(tmp_path, monkeypatch):
    # B05: when the restoration ITSELF fails, publish must never claim
    # success or a completed rollback: it raises an actionable recovery error
    # naming the retained backup directory and every failed path (chaining
    # the original failure), keeps the pre-mutation backup for manual
    # recovery, and still restores the durable proposal so it stays
    # reviewable. Deterministic: injected failures, no sleeps.
    import lumio_wiki.mutation as mutation_module
    import lumio_wiki.proposal_pipeline as proposal_pipeline_module

    kb = _rollback_kb(tmp_path, hot_pins=["Alpha"])
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    removal = pipeline.propose_page_removal("Alpha")
    assert not removal.blocked, removal.validation_report
    durable_before = _durable_state_snapshot(store, removal.id)

    def fail_log_append(path, entry):
        raise OSError("injected activity log append failure")

    def fail_restoration(self, working_root, **_kwargs):
        return ["concepts/alpha.md (injected restoration failure)"]

    monkeypatch.setattr(proposal_pipeline_module, "append_activity_log_entry", fail_log_append)
    monkeypatch.setattr(mutation_module.MutationBackup, "restore_all", fail_restoration)

    with pytest.raises(mutation_module.MutationRollbackError) as excinfo:
        pipeline.publish(removal.id)

    error = excinfo.value
    # Actionable: names the retained backup location and the failed path, and
    # chains the original publish failure as the cause.
    assert error.backup_dir is not None and error.backup_dir.is_dir()
    assert str(error.backup_dir) in str(error)
    assert "concepts/alpha.md" in str(error)
    assert error.failed_paths == ("concepts/alpha.md (injected restoration failure)",)
    assert isinstance(error.__cause__, OSError)
    assert "injected activity log append failure" in str(error.__cause__)

    # The rollback did not succeed, so the KB stays unrestored — but the
    # failure is never reported as success, and the durable proposal was
    # still restored to its reviewable pre-mutation bytes.
    assert not (root / "concepts/alpha.md").exists()
    assert _durable_state_snapshot(store, removal.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(removal.id)
    assert reloaded is not None and reloaded.status == "staged"

    # The recovery backup is retained, then removed by this test so the
    # shared temporary backup root stays clean for other journeys.
    assert error.backup_dir.is_dir()
    import shutil

    shutil.rmtree(error.backup_dir)


def test_rollback_chmod_restoration_failure_reported_backup_retained(tmp_path, monkeypatch):
    # P5 remediation: a restored file whose captured permission bits cannot
    # be re-applied is a REAL restoration failure. The bytes come back but
    # the mode does not (the restore's atomic replace leaves temp-file mode
    # bits behind); the rollback must REPORT the failed path, keep the
    # pre-mutation backup for manual recovery, and raise
    # MutationRollbackError chaining the original publish failure — never
    # clean the backup and claim the rollback complete. Deterministic: an
    # injected os.chmod failure on exactly one restored path (os.chmod has
    # exactly one caller in the package: the restoration), no sleeps.
    import shutil
    import stat as stat_module

    import lumio_wiki.mutation as mutation_module
    import lumio_wiki.publish as publish_module

    kb = _rollback_kb(tmp_path)
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    # A distinctive captured mode proves the permission bits are part of the
    # exact pre-mutation state the restore must put back.
    (root / "concepts/gamma.md").chmod(0o640)
    gamma_bytes = (root / "concepts/gamma.md").read_bytes()

    # Same gamma revision + Alpha move as the journey above; the SECOND live
    # write (the move destination) dies so a per-file restoration runs.
    move_segment = (
        "---\n"
        'title: "Alpha"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        'summary: "Relocated Alpha page."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "category: archive\n"
        "type: concept\n"
        "durability_rationale: reviewed durable knowledge move\n"
        "move_from_path: concepts/alpha.md\n"
        "sources:\n"
        '  - id: "alpha-src"\n'
        '    title: "Alpha source"\n'
        'id: "entity:alpha"\n'
        "entity_types:\n  - concept\n"
        "synthetic: false\n"
        "---\n"
        "# Alpha\n\nThe Alpha page relocated into the archive category.\n"
    )
    failing_markdown = (
        _disjoint_revision("Gamma", "gamma", "Revised Gamma.")
        + "\n<!-- lumio: page-break -->\n"
        + move_segment
    )
    failing = lw.create_proposal_without_provider(
        failing_markdown.encode("utf-8"),
        "text/markdown",
        "gamma-move.md",
        kb,
        store=store,
    )
    assert not failing.blocked, failing.validation_report

    files_before = _rollback_snapshot(root)
    backups_before = _existing_backups()

    original_write_destination = publish_module._write_destination
    original_chmod = os.chmod

    def fail_second_live_write(destination, working_root):
        if Path(working_root) == root and destination.relative_path == "archive/alpha.md":
            raise OSError("injected second-page live write failure")
        return original_write_destination(destination, working_root)

    def fail_gamma_restore_chmod(path, mode):
        if Path(path) == root / "concepts/gamma.md":
            raise OSError("injected chmod restoration failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(publish_module, "_write_destination", fail_second_live_write)
    monkeypatch.setattr(os, "chmod", fail_gamma_restore_chmod)

    with pytest.raises(mutation_module.MutationRollbackError) as excinfo:
        pipeline.publish(failing.id)

    error = excinfo.value
    # Actionable: the chmod failure names the restored path and reason, the
    # original write failure is chained as the cause, and the pre-mutation
    # backup is RETAINED for manual recovery (never cleaned up).
    assert error.failed_paths == ("concepts/gamma.md (injected chmod restoration failure)",)
    assert "concepts/gamma.md" in str(error)
    assert "injected chmod restoration failure" in str(error)
    assert isinstance(error.__cause__, OSError)
    assert "injected second-page live write failure" in str(error.__cause__)
    assert error.backup_dir is not None and error.backup_dir.is_dir()
    assert str(error.backup_dir) in str(error)
    assert error.backup_dir in _existing_backups()
    assert _existing_backups() != backups_before

    # The bytes came back but the captured 0o640 permission bits did not:
    # this is exactly the unrecovered state the error reports.
    assert (root / "concepts/gamma.md").read_bytes() == gamma_bytes
    assert stat_module.S_IMODE((root / "concepts/gamma.md").stat().st_mode) != 0o640
    assert _rollback_snapshot(root) == files_before

    # The durable proposal was still restored to its reviewable pre-mutation
    # bytes even though the file restoration reported the failure.
    reloaded = lw.IngestStore(tmp_path / "ingest").get(failing.id)
    assert reloaded is not None and reloaded.status == "staged"

    # The retained recovery backup is removed by this test so the shared
    # temporary backup root stays clean for other journeys.
    shutil.rmtree(error.backup_dir)


def test_rollback_created_directory_removal_failure_reported_backup_retained(tmp_path, monkeypatch):
    # P5 remediation: a mutation-created directory the rollback cannot prune
    # is a REAL restoration failure — the pre-mutation state had no such
    # directory. The rollback must REPORT the leftover (deepest prune order),
    # keep the pre-mutation backup, and raise MutationRollbackError chaining
    # the original publish failure — never prune silently and claim the
    # rollback complete. Deterministic: an injected rmdir failure on the one
    # created directory, no sleeps.
    import shutil

    import lumio_wiki.mutation as mutation_module
    import lumio_wiki.publish as publish_module

    kb = _rollback_kb(tmp_path)
    root = kb.root.resolve()
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)

    baseline = lw.create_proposal_without_provider(
        _disjoint_revision("Alpha", "alpha", "Baseline Alpha revision.").encode("utf-8"),
        "text/markdown",
        "alpha-baseline.md",
        kb,
        store=store,
    )
    assert pipeline.publish(baseline.id).status == "published"

    move_segment = (
        "---\n"
        'title: "Alpha"\n'
        "aliases: []\n"
        'tags:\n  - "t"\n'
        'summary: "Relocated Alpha page."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        "category: archive\n"
        "type: concept\n"
        "durability_rationale: reviewed durable knowledge move\n"
        "move_from_path: concepts/alpha.md\n"
        "sources:\n"
        '  - id: "alpha-src"\n'
        '    title: "Alpha source"\n'
        'id: "entity:alpha"\n'
        "entity_types:\n  - concept\n"
        "synthetic: false\n"
        "---\n"
        "# Alpha\n\nThe Alpha page relocated into the archive category.\n"
    )
    failing_markdown = (
        _disjoint_revision("Gamma", "gamma", "Revised Gamma.")
        + "\n<!-- lumio: page-break -->\n"
        + move_segment
    )
    failing = lw.create_proposal_without_provider(
        failing_markdown.encode("utf-8"),
        "text/markdown",
        "gamma-move.md",
        kb,
        store=store,
    )
    assert not failing.blocked, failing.validation_report

    files_before = _rollback_snapshot(root)
    durable_before = _durable_state_snapshot(store, failing.id)

    original_write_destination = publish_module._write_destination
    original_rmdir = Path.rmdir

    def fail_second_live_write(destination, working_root):
        if Path(working_root) == root and destination.relative_path == "archive/alpha.md":
            # Real second-write failure window: publish creates the fresh
            # destination directory first, then dies writing the bytes.
            (root / "archive").mkdir(parents=True, exist_ok=True)
            raise OSError("injected second-page live write failure")
        return original_write_destination(destination, working_root)

    def fail_archive_prune(self):
        if self == root / "archive":
            raise OSError("injected created-directory removal failure")
        return original_rmdir(self)

    monkeypatch.setattr(publish_module, "_write_destination", fail_second_live_write)
    monkeypatch.setattr(Path, "rmdir", fail_archive_prune)

    with pytest.raises(mutation_module.MutationRollbackError) as excinfo:
        pipeline.publish(failing.id)

    error = excinfo.value
    # Actionable: the prune failure names the leftover created directory and
    # reason, the original write failure stays chained as the cause, and the
    # pre-mutation backup is RETAINED (never cleaned up).
    assert error.failed_paths == ("archive (injected created-directory removal failure)",)
    assert "archive" in str(error)
    assert "injected created-directory removal failure" in str(error)
    assert isinstance(error.__cause__, OSError)
    assert "injected second-page live write failure" in str(error.__cause__)
    assert error.backup_dir is not None and error.backup_dir.is_dir()
    assert error.backup_dir in _existing_backups()

    # The unrecovered artifact is real: the mutation-created directory is
    # still on disk even though every FILE byte came back.
    assert (root / "archive").is_dir()
    assert not (root / "archive/alpha.md").exists()
    assert _rollback_snapshot(root) == files_before

    # Durable proposal still restored to its reviewable pre-mutation bytes.
    assert _durable_state_snapshot(store, failing.id) == durable_before
    reloaded = lw.IngestStore(tmp_path / "ingest").get(failing.id)
    assert reloaded is not None and reloaded.status == "staged"

    # The retained recovery backup and the leftover directory are removed by
    # this test so the shared temporary roots stay clean for other journeys.
    shutil.rmtree(error.backup_dir)
    shutil.rmtree(root / "archive")


def test_mutation_backup_chmod_restoration_failure_is_reported(tmp_path, monkeypatch):
    # P5 remediation, unit level: restore_all reports a restored file whose
    # captured permission bits cannot be re-applied (bytes come back, mode
    # does not) instead of silently suppressing the chmod failure.
    import stat as stat_module

    from lumio_wiki.mutation import MutationBackup

    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    target = kb_root / "page.md"
    target.write_bytes(b"before")
    target.chmod(0o640)

    backup = MutationBackup("unit-chmod")
    backup.capture_path(kb_root, "page.md")
    target.write_bytes(b"mutated")

    original_chmod = os.chmod

    def fail_chmod(path, mode):
        if Path(path) == target:
            raise OSError("injected chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", fail_chmod)

    failures = backup.restore_all(kb_root)

    assert failures == ["page.md (injected chmod failure)"]
    # The bytes were restored; the captured 0o640 permission bits were not.
    assert target.read_bytes() == b"before"
    assert stat_module.S_IMODE(target.stat().st_mode) != 0o640

    backup.cleanup()


def test_mutation_backup_created_directory_prune_failures_deepest_first(tmp_path, monkeypatch):
    # P5 remediation, unit level: restore_all reports directories the
    # mutation created that the prune cannot remove — deepest first —
    # instead of silently keeping them; a created directory that is already
    # absent again is the restored pre-mutation state and never a failure.
    from lumio_wiki.mutation import MutationBackup

    kb_root = tmp_path / "kb"
    kb_root.mkdir()

    backup = MutationBackup("unit-prune")
    backup.capture_path(kb_root, "a/b/c/new.md")  # records created ancestors a, a/b, a/b/c
    backup.capture_path(kb_root, "gone/leaf.md")  # records created ancestor "gone"

    (kb_root / "a/b/c").mkdir(parents=True)
    (kb_root / "a/b/c/new.md").write_bytes(b"created")
    # "gone" is never actually created by the mutation: the prune sees it
    # already absent (the restored pre-mutation state) and never fails.

    original_rmdir = Path.rmdir

    def fail_deepest_rmdir(self):
        if self == kb_root / "a/b/c":
            raise OSError("injected deepest prune failure")
        return original_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", fail_deepest_rmdir)

    failures = backup.restore_all(kb_root)

    # Deepest first: the injected a/b/c failure is reported before the
    # natural cascade (a/b and a can no longer be pruned while a/b/c
    # survives), and the never-created "gone" reports nothing.
    assert [failure.split(" (")[0] for failure in failures] == ["a/b/c", "a/b", "a"]
    assert "injected deepest prune failure" in failures[0]
    assert (kb_root / "a/b/c").is_dir()

    shutil.rmtree(kb_root / "a")
    backup.cleanup()
