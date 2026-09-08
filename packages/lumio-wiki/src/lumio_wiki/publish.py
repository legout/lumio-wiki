"""Publish helpers for the standalone ingestion journey (issue #97).

The candidate-validation and apply helpers that the Proposal Pipeline needs to
publish proposed pages live here so the model-free journey can reach
publication without the full application. They are pure Knowledge Base
operations: they depend only on the Compiled Page model, validation, and
frontmatter parsing owned by ``lumio-wiki``.

Operational publish state (Write Mode, Published Version records), the
move-link gate, and the atomic candidate-swap-with-rollback remain in the full
application (``lumio.publish``); ``lumio-wiki`` re-exports the helpers defined
here so existing importers keep working.

Plan 02 / P2 (B01): every write branch — plain new page, revision, compound
revision, explicit move, Page Removal, Control File, and the temporary
candidate tree — resolves and checks its destinations through ONE resolution
BEFORE any candidate or live byte is written. A new ``A_B`` can no longer
replace an existing ``A B`` at ``a_b.md``: occupied targets, duplicate
targets within one proposal, existing directories, unreadable occupants,
special-file occupants (FIFOs, sockets, devices — typed with a nonblocking
``stat`` so a FIFO is never opened), non-directory ancestors of any
proposed destination, move source, removal target, or the Control File
destination, an unusable Control File destination (a dangling symlink,
an existing directory, special file, or unreadable file at ``lumio.yaml``
— the dangling link detected with non-following metadata, since
``exists()`` follows the link away and used to treat the entry as absent —
or a Knowledge Base root whose mode bits deny the publishing user the
atomic temp-file creation and replace, evaluated directly so a root-owned
environment cannot false-pass), a DANGLING page-destination symlink
(rejected on non-following ``lstat`` BEFORE resolution — resolution would
follow the link to its missing target and silently reroute the write that
the candidate's ``copytree`` could never perform), an existing declared
removal target whose parent directory cannot release it (the same direct
mode-bit, sticky-bit unlink evaluation the move source preflight uses),
escaping paths,
compound-fallback collisions, and move sources that are not safely
removable — an existing directory (unremovable after the write), a symlink
(rejected by non-following ``lstat`` BEFORE resolution — dangling links
included — so a submitted link is never followed), a special file (FIFO,
socket, device — typed with the same nonblocking ``stat`` probe so a FIFO
source is never opened), an unreadable file, a file whose parent directory
does not permit removing entries (unlink permission is decided by the
parent's mode bits and sticky bit, evaluated directly so a root-owned
environment cannot false-pass), or a file that is not the recorded path of
the proposed page's own title (a different page's file,
the Control File, or any untracked file, so a move can never delete content
the proposal does not own) — are rejected up front. So is a DANGLING
symlink ANYWHERE along a submitted page destination's lexical path: the
non-following check walks every existing component from the root through
the destination's parents to its leaf (P2 review v9), because a dangling
ANCESTOR such as ``link -> ghost`` in ``link/fresh.md`` used to resolve to
the free ``ghost/fresh.md`` and strand the uncopyable entry in the
candidate while the live write landed behind the dangling link. A page
destination the write itself could not perform is rejected too (P2 review
v9): a missing destination whose nearest existing parent directory denies
the effective user write/search, or a same-title revision whose existing
regular file denies write, evaluated with the same direct mode-bit
algorithm (never ``os.access``, which false-passes as root), so neither
candidate validation nor live apply can strand a partial apply behind a
raw ``PermissionError``. And a move whose vacated source path the same
proposal also declares to remove is rejected before any write: the
removal is resolved from the pre-mutation path, so the move's own unlink
would silently swallow it and leave the moved page behind (P2 review v9).
The candidate gate
resolves against the REAL root before its throwaway copy is made, so an
occupant ``copytree`` could never get past still produces the same
destination issue as live apply, and the throwaway copy itself mirrors the
real tree's structure — symlinks re-created verbatim so a KB-relative link
reads identically on both trees, unrelated FIFOs/sockets/devices skipped as
contentless entries — so candidate validation sees exactly what live
validation sees (P2 review v10). There is no automatic suffix allocation
and no removal that was not declared.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec.yaml as yaml

from lumio_wiki.knowledge_base import (
    ACTIVITY_LOG_BASENAME,
    CONTROL_FILE_BASENAME,
    HOT_INDEX_BASENAME,
    NAV_INDEX_BASENAME,
    KnowledgeBaseControlFile,
    load_knowledge_base,
    validate,
    write_control_file,
)
from lumio_wiki.knowledge_base import (
    _as_sources as as_sources,
)
from lumio_wiki.knowledge_base import (
    _parse_frontmatter as parse_frontmatter,
)
from lumio_wiki.records import Source, ValidationIssue, ValidationReport


class PublishError(Exception):
    """Raised when a publish operation fails."""


class DestinationConflict(PublishError):
    """A proposed destination set failed its pre-write checks (Plan 02 / P2, B01).

    Raised before ANY candidate or live byte is written, so a conflict can
    never silently overwrite another page, allocate a suffix, or leave a
    partial candidate behind. ``file`` names the offending proposed path so
    candidate validation can aggregate the conflict into a
    ``ValidationIssue`` instead of leaking an exception through staging.
    """

    def __init__(self, message: str, *, file: str = "") -> None:
        super().__init__(message)
        self.file = file


@dataclass(frozen=True)
class _Destination:
    """One checked write destination resolved for a proposed page (B01).

    ``kind`` records which write branch the page takes after resolution
    (``write``, ``compound``, or ``move``), ``relative_path`` is the checked
    destination, ``source_relative_path`` carries a move's checked source
    path, and ``merge_base`` carries a compound revision's existing Markdown
    read at resolution time. Relative paths keep the set applicable to the
    throwaway candidate tree and the live root alike, so candidate validation
    and application consume exactly the same checked destinations. Proposed
    pages are duck-typed (this module never imports the ingest record),
    hence ``Any``.
    """

    page: Any
    kind: str
    relative_path: str
    source_relative_path: str | None = None
    merge_base: str | None = None


def _existing_paths_by_title(working_dir: Path) -> dict[str, str]:
    """Return a mapping of canonical page title to existing relative path.

    Only pages with both a title and a non-empty path are recorded, so the
    mapping's value type is a non-optional ``str``. A page lacking a resolvable
    path is treated as absent (the caller falls back to the proposed relative
    path) rather than risking a ``Path / None`` resolution.
    """
    try:
        kb, _ = load_knowledge_base(working_dir)
    except Exception:
        return {}
    return {page.title: page.path for page in kb.pages if page.title and page.path}


def _reserved_basenames() -> frozenset[str]:
    """Return the reserved-artifact basenames a Page Removal must never delete."""
    return frozenset({NAV_INDEX_BASENAME, HOT_INDEX_BASENAME, ACTIVITY_LOG_BASENAME})


def _checked_relative_path(root: Path, relative_path: str, *, page_title: str) -> str:
    """Return the normalized root-relative destination, rejecting escapes (B01).

    A DANGLING symlink at the submitted path is rejected FIRST, on
    non-following ``lstat`` metadata, BEFORE ``resolve()`` follows the link
    away (P2 review v8): resolution used to normalize ``fresh.md ->
    ghost.md`` (missing target) to a free ``ghost.md``, so the occupancy
    checks accepted the write while the candidate's ``copytree`` retained
    the uncopyable dangling entry (raw ``shutil.Error``) and the live write
    silently landed at ``ghost.md`` behind the still-dangling link — a move
    could even unlink its verified source. The SAME non-following check now
    walks EVERY existing lexical component from the Knowledge Base root
    through the submitted destination's parents down to its leaf (P2 review
    v9): a dangling symlink ANCESTOR such as ``link -> ghost`` in
    ``link/fresh.md`` used to resolve to the free ``ghost/fresh.md``
    because only the leaf was examined — the occupancy and ancestor checks
    then judged the RESOLVED path, candidate validation still died in
    ``copytree`` on the uncopyable entry, and live apply silently created
    the ``ghost/`` directory behind the still-dangling link. A VALID
    symlink component (its target exists) keeps resolving like any other
    component, and missing ordinary parents stay acceptable (they are
    created on demand by the write). Because every write branch resolves
    its destination here, one check covers the new page, compound fallback,
    revision, rename, and move target alike. A valid symlink to an
    EXISTING file still resolves (the occupancy rules then judge the
    resolved target, preserving regular-file revisions) and a genuinely
    missing destination stays acceptable.
    """
    link = root / relative_path
    # Walk every lexical component BEFORE resolution (lstat-backed
    # ``is_symlink``, never resolve()/exists() alone): a dangling symlink
    # ANCESTOR is an EXISTING directory entry whose followed target is
    # absent — exactly the entry the resolved-path checks used to miss
    # because they examined the post-resolution route (P2 review v9; the
    # same non-following detection as the leaf check and the Control File
    # preflight).
    ancestor = root
    for component in Path(relative_path).parts[:-1]:
        ancestor = ancestor / component
        if ancestor.is_symlink() and not ancestor.exists():
            raise DestinationConflict(
                f"Proposed page {page_title!r} destination {relative_path!r} "
                f"traverses the dangling symlink ancestor {component!r} that "
                f"must not be followed to its missing target or copied: the "
                f"dangling entry breaks the candidate copy and the live write "
                f"would silently land at the linked path behind it; publish "
                f"the page at a real, unoccupied path",
                file=relative_path,
            )
    # lstat-backed ``is_symlink`` (never resolve()/exists() alone): a
    # dangling symlink is an EXISTING directory entry whose followed target
    # is absent — exactly the entry the free-path verdict below used to
    # hand out after resolution had rerouted the write to the missing
    # target (P2 review v8; the same non-following detection as the
    # Control File preflight).
    if link.is_symlink() and not link.exists():
        raise DestinationConflict(
            f"Proposed page {page_title!r} destination {relative_path!r} is a "
            f"dangling symlink that must not be followed to its missing target "
            f"or copied: the dangling entry breaks the candidate copy and the "
            f"live write would silently land at the linked path; publish the "
            f"page at a real, unoccupied path",
            file=relative_path,
        )
    target = link.resolve()
    if not target.is_relative_to(root):
        raise DestinationConflict(
            f"Proposed page {page_title!r} path escapes working directory: {relative_path}",
            file=relative_path,
        )
    return target.relative_to(root).as_posix()


def _special_or_unreadable_reason(target: Path) -> str | None:
    """Return why ``target`` must not be opened or removed, or ``None``.

    A nonblocking ``stat`` type probe runs FIRST: ``stat(2)`` never opens the
    file, so a FIFO (or socket) is classified without ever being opened — an
    ``open(2)`` on a FIFO blocks until a writer appears, which would hang the
    preflight. Only a regular file is then probe-opened to check readability
    honestly (``os.access`` lies when running as root). Returns ``"special"``
    for a non-regular file (FIFO, socket, device), ``"unreadable"`` for a
    file that cannot be stat'ed or read, and ``None`` for a regular readable
    file (B01).
    """
    try:
        mode = os.stat(target).st_mode
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(mode):
        return "special"
    try:
        with target.open("rb"):
            pass
    except OSError:
        return "unreadable"
    return None


def _parent_denies_removal(parent: Path, source: Path) -> str | None:
    """Return why ``parent`` cannot release ``source`` via unlink, or ``None``.

    Removing a directory entry is authorized by the PARENT directory's
    permission metadata, not by the entry itself: unlink(2) needs write and
    search permission on the parent for the effective user, and a sticky
    parent additionally requires owning the parent or the entry. The classic
    owner/group/other mode-bit algorithm is evaluated directly from ``stat(2)``
    metadata instead of ``os.access``: as root, ``os.access`` reports write
    access even for a mode-denied directory, so a preflight built on it would
    false-pass a root-owned environment and let the same tree fail with a raw
    PermissionError mid-apply for every non-root publisher. Nothing is mutated
    and nothing is opened — the probe is ``stat(2)`` metadata only (B01, P2
    review v5).
    """
    try:
        parent_stat = os.stat(parent)
        source_stat = os.stat(source)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            # The parent or the entry vanished between the existence probe
            # and now: the same tolerance as a missing source (nothing to
            # unlink).
            return None
        return "its parent directory's metadata cannot be read to verify removal permission"
    if not stat.S_ISDIR(parent_stat.st_mode):
        # A non-directory parent is rejected with its own precise message by
        # the non-directory-ancestor preflight.
        return None
    effective_uid = os.geteuid()
    if effective_uid == parent_stat.st_uid:
        write_bit, search_bit = stat.S_IWUSR, stat.S_IXUSR
    elif parent_stat.st_gid in (os.getegid(), *os.getgroups()):
        write_bit, search_bit = stat.S_IWGRP, stat.S_IXGRP
    else:
        write_bit, search_bit = stat.S_IWOTH, stat.S_IXOTH
    if not parent_stat.st_mode & write_bit or not parent_stat.st_mode & search_bit:
        return "its parent directory's mode bits deny the effective user write or search access"
    if parent_stat.st_mode & stat.S_ISVTX and effective_uid not in (
        parent_stat.st_uid,
        source_stat.st_uid,
    ):
        return "its parent directory's sticky bit denies the effective user removing the entry"
    return None


def _parent_denies_control_file_write(parent: Path, target: Path) -> str | None:
    """Return why ``parent`` cannot host the atomic Control File write, or ``None``.

    ``write_control_file`` creates a temporary file in the parent directory
    and atomically replaces ``lumio.yaml`` with it via ``os.replace``:
    creating the temp file and modifying the directory entry both need WRITE
    and SEARCH permission on the parent for the effective user, and
    replacing an EXISTING entry in a sticky parent additionally requires
    owning the parent or the replaced entry. As in
    :func:`_parent_denies_removal`, the classic
    owner/group/other mode-bit algorithm is evaluated directly from
    ``stat(2)`` metadata — never ``os.access``, which false-passes as root —
    so a mode-denied root (root-owned environments included) is rejected
    before any page byte is written instead of stranding a partial revision
    behind a raw PermissionError (B01, P2 review v6). Nothing is mutated and
    nothing is opened — the probe is ``stat(2)`` metadata only. A missing
    parent stays tolerated (it is created on demand) and a non-directory
    parent is rejected with its own precise message by the
    non-directory-ancestor preflight.
    """
    try:
        parent_stat = os.stat(parent)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            # The parent is created on demand by the write itself.
            return None
        return "its parent directory's metadata cannot be read to verify write permission"
    if not stat.S_ISDIR(parent_stat.st_mode):
        # A non-directory parent is rejected with its own precise message by
        # the non-directory-ancestor preflight.
        return None
    effective_uid = os.geteuid()
    if effective_uid == parent_stat.st_uid:
        write_bit, search_bit = stat.S_IWUSR, stat.S_IXUSR
    elif parent_stat.st_gid in (os.getegid(), *os.getgroups()):
        write_bit, search_bit = stat.S_IWGRP, stat.S_IXGRP
    else:
        write_bit, search_bit = stat.S_IWOTH, stat.S_IXOTH
    if not parent_stat.st_mode & write_bit or not parent_stat.st_mode & search_bit:
        return "its parent directory's mode bits deny the effective user write or search access"
    # lexists (not exists): an entry's presence is lexical, so a DANGLING
    # symlink is an existing entry whose sticky replacement ownership must
    # be verified too — gating the probe on the following ``exists()`` used
    # to skip the check for exactly those entries (P2 review v7).
    if parent_stat.st_mode & stat.S_ISVTX and os.path.lexists(target):
        # Replacing an EXISTING entry in a sticky directory additionally
        # requires owning the parent or the replaced entry; creating a fresh
        # one needs only the write/search bits checked above. lstat (not
        # stat): rename(2) judges the entry itself, and a symlink at the
        # destination is replaced link-and-all, never followed.
        try:
            target_stat = os.lstat(target)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                # A vanished entry is tolerated below (creation semantics);
                # anything else cannot be verified.
                return (
                    "the existing Control File's metadata cannot be read to "
                    "verify replacement permission"
                )
            target_stat = None
        if target_stat is not None and effective_uid not in (
            parent_stat.st_uid,
            target_stat.st_uid,
        ):
            return (
                "its parent directory's sticky bit denies the effective user "
                "replacing the existing file"
            )
    return None


def _page_write_denial(root: Path, relative_path: str) -> str | None:
    """Return why a page destination cannot be created or revised, or ``None``.

    The write branches create a destination's missing parents and file with
    ``mkdir(parents=True)`` plus ``write_text`` — or truncate an EXISTING
    regular file in place for a revision. Both are permission-checked here
    with the same root-safe mode-bit algorithm the removal, move-source, and
    Control File preflights use (P2 review v9):

    - a MISSING destination needs its nearest EXISTING parent directory to
      grant the effective user WRITE and SEARCH: the intermediate missing
      components are created fresh (owned by the publisher, default mode
      bits), so only the nearest existing ancestor gates ``mkdir``;
    - an EXISTING regular destination (a same-title revision, a rename, or
      an in-place self-move — the occupancy checks already vetted the type
      and owner) needs the effective user's WRITE bit on the FILE itself,
      because ``write_text`` truncates in place.

    Page writes never replace or remove a directory entry (no ``os.replace``
    and no unlink), so the Control File's sticky-bit replacement rule has NO
    page equivalent: a sticky parent adds no constraint beyond the
    write/search bits above. Non-regular existing occupants (directories,
    special files) are rejected with their own precise messages by the
    occupancy preflights and are not re-judged here. Nothing is mutated and
    nothing is opened — the probe is ``stat(2)`` metadata only, never
    ``os.access``, which false-passes as root.
    """
    target = root / relative_path
    try:
        target_stat = os.stat(target)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            # Vanished between resolution and now: the write (re)creates it.
            target_stat = None
        else:
            return "the destination's metadata cannot be read to verify write permission"
    if target_stat is not None:
        if not stat.S_ISREG(target_stat.st_mode):
            # Occupancy preflights reject non-regular occupants with their
            # own precise messages; nothing further to add here.
            return None
        effective_uid = os.geteuid()
        if effective_uid == target_stat.st_uid:
            write_bit = stat.S_IWUSR
        elif target_stat.st_gid in (os.getegid(), *os.getgroups()):
            write_bit = stat.S_IWGRP
        else:
            write_bit = stat.S_IWOTH
        if not target_stat.st_mode & write_bit:
            return "the existing page file's mode bits deny the effective user write access"
        return None
    # Missing destination: walk UP to the nearest existing ancestor — every
    # intermediate component is created fresh by ``mkdir(parents=True)``, so
    # only that ancestor's mode bits gate the creation.
    parent = target.parent
    while True:
        try:
            parent_stat = os.stat(parent)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                return (
                    "its nearest existing parent directory's metadata cannot be "
                    "read to verify write permission"
                )
            # A vanished component is created on demand by the write; keep
            # walking up to the nearest existing ancestor.
            if parent == root:
                return "its parent directory cannot be created"
            parent = parent.parent
            continue
        break
    if not stat.S_ISDIR(parent_stat.st_mode):
        # Rejected with its own precise message by the non-directory-ancestor
        # preflight.
        return None
    effective_uid = os.geteuid()
    if effective_uid == parent_stat.st_uid:
        write_bit, search_bit = stat.S_IWUSR, stat.S_IXUSR
    elif parent_stat.st_gid in (os.getegid(), *os.getgroups()):
        write_bit, search_bit = stat.S_IWGRP, stat.S_IXGRP
    else:
        write_bit, search_bit = stat.S_IWOTH, stat.S_IXOTH
    if not parent_stat.st_mode & write_bit or not parent_stat.st_mode & search_bit:
        nearest = parent.relative_to(root).as_posix()
        return (
            f"its nearest existing parent directory {nearest!r} denies the "
            f"effective user write or search access"
        )
    return None


def _reject_unwritable_page_destinations(
    destinations: list[_Destination],
    root: Path,
) -> None:
    """Reject a page destination the write itself could not perform (B01).

    Runs in the shared check-only phase BEFORE any candidate or live byte is
    written (P2 review v9): the resolution phase vetted occupancy, types,
    and ownership, but the raw ``mkdir``/``write_text`` performed by
    :func:`_write_destination` can still fail on permission metadata — a
    missing destination whose nearest existing parent directory denies the
    effective user write/search, or a same-title revision whose existing
    regular file is read-only. Left unchecked, the FIRST such page wrote
    successfully and a LATER page of the same proposal then died with a raw
    ``PermissionError``, stranding a partial apply, while candidate
    validation leaked the same filesystem exception from its throwaway tree.
    The mode bits are evaluated directly with the same root-safe algorithm
    as the other preflights — never ``os.access``, which false-passes as
    root — so the conflict raises as :class:`DestinationConflict` before ANY
    byte is written and candidate validation aggregates the identical
    destination issue.
    """
    for destination in destinations:
        denial = _page_write_denial(root, destination.relative_path)
        if denial is not None:
            raise DestinationConflict(
                f"Proposed page {destination.page.title!r} cannot be written "
                f"at {destination.relative_path} because {denial}; make the "
                f"page file, or the directory that must host it, writable and "
                f"searchable for the publishing user before publishing this "
                f"proposal",
                file=destination.relative_path,
            )


def _reject_occupied_target(
    root: Path,
    relative_path: str,
    page_title: str,
    title_by_path: dict[str, str],
    *,
    revising: str | None = None,
) -> None:
    """Reject an occupied destination before any write (B01).

    Generalizes the explicit move collision guard (issue #80, P1.2) to every
    write branch: an existing directory, an unreadable occupant, a special
    file (FIFO, socket, device — rejected via a nonblocking ``stat`` type
    probe so the preflight never opens a FIFO, whose read side would block
    forever), or a file owned by a different page is never overwritten.
    ``revising`` names the one
    title allowed to occupy the path — the page's own recorded file for a
    revision (identity continuity), the old title's file for a reviewed rename
    (ADR-0016), or nobody for a new page. A missing file is always acceptable:
    the write (re)creates the page at its checked destination.
    """
    target = root / relative_path
    if not target.exists():
        return
    if target.is_dir():
        raise DestinationConflict(
            f"Proposed page {page_title!r} destination is an existing directory "
            f"and must not be replaced: {relative_path}",
            file=relative_path,
        )
    # The occupant is typed and readability-probed through the shared
    # nonblocking probe: stat(2) never opens the occupant, so a FIFO cannot
    # hang the preflight the way a readability open(2) would, and only a
    # regular file is ever opened (B01).
    reason = _special_or_unreadable_reason(target)
    if reason == "unreadable":
        raise DestinationConflict(
            f"Proposed page {page_title!r} destination is an unreadable file "
            f"that must not be overwritten: {relative_path}",
            file=relative_path,
        )
    if reason == "special":
        raise DestinationConflict(
            f"Proposed page {page_title!r} destination is occupied by an "
            f"unreadable special file that must not be opened or overwritten: "
            f"{relative_path}",
            file=relative_path,
        )
    occupant = title_by_path.get(relative_path)
    if occupant is not None and occupant == revising:
        # Occupied by the very page this write revises at its recorded path.
        return
    if occupant is not None:
        raise DestinationConflict(
            f"Proposed page {page_title!r} would overwrite existing page {occupant!r} "
            f"at {relative_path}; existing pages are revised at their recorded path "
            f"and new pages need an unoccupied destination",
            file=relative_path,
        )
    raise DestinationConflict(
        f"Proposed page {page_title!r} destination is already occupied by an "
        f"untracked file: {relative_path}",
        file=relative_path,
    )


@dataclass(frozen=True)
class _PathExpectation:
    """One expected pre-mutation path state produced by precondition capture.

    Internal capture shape; :meth:`ProposalPipeline._with_captured_preconditions`
    converts these into the durable :class:`lumio_wiki.ingest.PathPrecondition`
    records. ``kind`` is ``"file"`` (``digest`` carries the reviewed SHA-256)
    or ``"absent"``; ``role`` is the stable restage-guidance vocabulary from
    the ``PathPrecondition`` docstring.
    """

    relative_path: str
    role: str
    kind: str
    digest: str = ""


def _file_digest(path: Path) -> str | None:
    """Return the SHA-256 of a readable regular file's bytes, else ``None``.

    A path that is absent, a directory, a dangling or unreadable entry is not
    a regular file, so it carries no reviewed bytes (drift, never a crash).
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _expect_path(root: Path, relative_path: str, role: str) -> _PathExpectation:
    """Capture the current state of one path as a reviewed expectation."""
    path = root / relative_path
    digest = _file_digest(path)
    if digest is not None:
        return _PathExpectation(relative_path, role, "file", digest)
    return _PathExpectation(relative_path, role, "absent")


def _capture_mutation_preconditions(
    proposed_pages: list,
    working_dir: str | Path,
    *,
    removed_titles: list[str] | None = None,
) -> list[_PathExpectation]:
    """Capture the reviewed pre-mutation state of every affected path (P4).

    The staging-side half of B03/B04: for a proposal about to be persisted,
    record the CURRENT state of exactly the paths the reviewed mutation was
    authoritatively told it would touch — never a whole-Knowledge-Base digest,
    so disjoint proposals stay publishable after intervening disjoint
    changes. The Control File (category catalog, Hot Index pins, ontology
    redirects) is always captured, because every proposal either consumes it
    (routing, pin and redirect state) or rewrites it.

    Destination and removal paths come from the SAME resolution live
    application uses (:func:`_resolve_proposed_destinations`,
    :func:`_resolve_removal_destinations`), so the captured base covers
    precisely the paths the checked write branches will consume: a
    revision/compound/rename destination's reviewed bytes, a move's source
    bytes and expected-absent target, a new page's expected-absent
    destination, a declared removal's reviewed bytes. When resolution raises a
    :class:`DestinationConflict` the proposal is (or will be) blocked anyway —
    publication refuses blocked proposals before preconditions are consulted —
    so capture degrades to the Control File expectation alone instead of
    breaking staging.
    """
    root = Path(working_dir).resolve()
    expectations: list[_PathExpectation] = [_expect_path(root, CONTROL_FILE_BASENAME, "control")]
    existing_by_title = _existing_paths_by_title(root)
    removal_expectations: list[_PathExpectation] = []
    if removed_titles:
        try:
            removals = _resolve_removal_destinations(removed_titles, root, existing_by_title)
        except DestinationConflict:
            removals = []
        removal_expectations = [
            _expect_path(root, normalized, "removal") for _title, normalized in removals
        ]
    try:
        destinations = _resolve_proposed_destinations(proposed_pages, root, existing_by_title)
    except DestinationConflict:
        # Blocked proposal: never publishable, so the reviewed base that
        # matters is the Control File state (and any resolvable removals).
        return expectations + removal_expectations
    for destination in destinations:
        if destination.kind == "move":
            source = destination.source_relative_path
            if source is not None and source != destination.relative_path:
                # An existing move source is reviewed by bytes; a MISSING one
                # is reviewed as absence, so a file the reviewer never saw
                # appearing later refuses publication too.
                expectations.append(_expect_path(root, source, "move-source"))
                # The move's target must still be absent when it is applied.
                expectations.append(_expect_path(root, destination.relative_path, "creation"))
            else:
                # A self-move overwrites its own recorded path in place.
                expectations.append(_expect_path(root, destination.relative_path, "revision"))
        else:
            # write/compound/rename: the destination's reviewed bytes (or its
            # reviewed absence, when the recorded file is already gone).
            expectations.append(_expect_path(root, destination.relative_path, "revision"))
    expectations.extend(removal_expectations)
    unique: dict[str, _PathExpectation] = {}
    for expectation in expectations:
        unique.setdefault(expectation.relative_path, expectation)
    return list(unique.values())


def _precondition_drift(preconditions, root: str | Path) -> list[str]:
    """Compare reviewed preconditions against the current state (P4, B03/B04).

    The publish-side half: every record must still hold — a ``file`` record's
    path must still be a readable regular file with the reviewed digest, and
    an ``absent`` record's path must still be lexically free (a dangling
    symlink counts as occupied). Returns one human-readable drift finding per
    violated record; an empty list means the current state still matches what
    the Maintainer reviewed. Records are duck-typed durable
    :class:`lumio_wiki.ingest.PathPrecondition` rows (``path``/``role``/
    ``kind``/``digest``), so no ingest-module import is needed here.
    """
    root = Path(root).resolve()
    drift: list[str] = []
    for record in preconditions:
        path = root / record.path
        # Records come from the durable private store, so their paths are
        # trusted-but-verified like every reviewed relative path: anything
        # escaping the Knowledge Base root is drift (a read-only check, and
        # the P2 destination resolution escape-guards every real write).
        if not Path(os.path.realpath(path, strict=False)).is_relative_to(root):
            drift.append(
                f"reviewed {record.role} precondition {record.path} no longer "
                "resolves inside the Knowledge Base"
            )
            continue
        if record.kind == "file":
            digest = _file_digest(path)
            if digest is None:
                drift.append(
                    f"reviewed {record.role} base for {record.path} is gone "
                    "(the path is now absent or no longer a readable regular file)"
                )
            elif digest != record.digest:
                drift.append(
                    f"{record.path} changed since review "
                    f"(reviewed {record.role} base no longer matches)"
                )
        elif os.path.lexists(path):
            drift.append(
                f"{record.path} is now occupied, but the reviewed {record.role} expected it absent"
            )
    return drift


def _resolve_proposed_destinations(
    proposed_pages: list,
    root: Path,
    existing_by_title: dict[str, str],
    *,
    force_compound: bool = False,
) -> list[_Destination]:
    """Resolve and check every proposed write destination BEFORE any write (B01).

    Consumes the proposed relative paths plus the current page identity
    (canonical title → recorded path) and produces one checked destination
    per proposed page, in proposal order:

    - a title that already exists is a REVISION written at its recorded path
      (same path, same identity — a v2 Entity ID change alone never reroutes
      or replaces a page, and legacy flat pages keep the explicit title/path
      behavior);
    - a new title writes its proposed relative path, which must be unoccupied
      (a slug collision with an existing page — new ``A_B`` onto existing
      ``A B`` at ``a_b.md`` — is rejected, never silently replaced);
    - a compound revision merges at the recorded path, and its new-page
      fallback is held to the same occupancy rule (compound-fallback
      collision rejected);
    - an explicit move (issue #80) keeps its atomic-move collision guard, and
      a reviewed rename (ADR-0016) keeps the old title's recorded path while
      blocking renames onto an existing title;
    - two pages resolving to the same destination within one proposal are a
      duplicate-target conflict (no last-writer-wins);
    - every destination must resolve inside the working directory;
    - a move source submitted as a SYMLINK is rejected on non-following
      ``lstat`` BEFORE resolution (dangling links included): resolving would
      follow the link and lose the submitted lexical path, so only the actual
      regular recorded page file is movable;
    - a submitted write destination that lexically is a DANGLING SYMLINK is
      rejected on non-following ``lstat`` BEFORE resolution (new page,
      compound fallback, revision, rename, and move target alike):
      resolution follows the link to its missing target, which used to
      normalize ``fresh.md -> ghost.md`` to a free ``ghost.md`` while the
      candidate's ``copytree`` retained the uncopyable entry and the live
      write silently landed at ``ghost.md`` behind the still-dangling link;
    - an EXISTING move source must be a removable, regular, readable file AND
      the recorded path of the proposed page's own title: a directory source
      (unremovable after the write), a special file (FIFO, socket, device —
      probed with a nonblocking ``stat`` so a FIFO source is never opened),
      an unreadable file, a file whose parent directory denies the unlink
      (the parent's mode bits and sticky bit are evaluated directly — never
      ``os.access``, which false-passes as root), or a file owned by a
      different page or untracked (the Control File included) is rejected, so
      a move can never delete content the proposal does not own; a MISSING
      source stays tolerated (the move writes the target and removes nothing).

    ``force_compound`` forces the compound branch for every proposed page:
    the public compound-revision writer (:func:`apply_compound_revision`) is
    an unconditional merge (issue #79), so it routes the shared checks with
    this flag even when the caller's record omits the ``compound_revision``
    flag.
    """
    title_by_path = {
        _checked_relative_path(root, path, page_title=title): title
        for title, path in existing_by_title.items()
    }
    destinations: list[_Destination] = []
    claimed: dict[str, tuple[str, str]] = {}  # relative path -> (title, kind)

    def _claim(relative_path: str, title: str, kind: str) -> None:
        prior = claimed.get(relative_path)
        if prior is not None:
            raise DestinationConflict(
                f"Proposed pages {prior[0]!r} and {title!r} resolve to the same "
                f"destination {relative_path}; a proposal must not carry two "
                f"writes for one path",
                file=relative_path,
            )
        claimed[relative_path] = (title, kind)

    for page in proposed_pages:
        move_from = getattr(page, "move_from_path", None)
        rename_from = getattr(page, "rename_from", None)
        compound = force_compound or bool(getattr(page, "compound_revision", False))
        title = page.title
        if move_from:
            # Explicit category/path move (issue #80): relocate ATOMICALLY.
            # Both paths are escape-checked, and the original collision guard
            # (issue #80, P1.2) is reused as the occupancy rule for the move.
            # A move source submitted as a SYMLINK is rejected FIRST, on
            # non-following lstat, BEFORE resolution loses the lexical path
            # (P2 review v4): ``_checked_relative_path`` resolves the link,
            # so "link.md -> architecture.md" used to be accepted as the
            # owned regular architecture.md — live apply then deleted
            # architecture.md and left the dangling link behind while the
            # candidate dereferenced the link into a second page. lstat also
            # sees a DANGLING symlink (exists()/stat() would not), so only
            # the actual regular recorded page file is ever movable.
            if (root / move_from).is_symlink():
                raise DestinationConflict(
                    f"Cannot move {title!r} from {move_from}: the move source "
                    f"is a symlink and must not be followed or removed; only "
                    f"the proposed page's own recorded regular file may be moved",
                    file=move_from,
                )
            source_relative = _checked_relative_path(root, move_from, page_title=title)
            target_relative = _checked_relative_path(root, page.relative_path, page_title=title)
            source = root / source_relative
            if source.is_dir():
                # unlink(2) removes directory entries, never directories: a
                # directory source cannot be removed after the target is
                # written, so accepting it would strand a half-done move (the
                # target present, the source directory untouched). Rejected
                # at resolution time, before any byte is written (B01).
                raise DestinationConflict(
                    f"Cannot move {title!r} from {source_relative}: the move "
                    f"source is an existing directory and cannot be removed "
                    f"after the move",
                    file=source_relative,
                )
            if source.exists():
                # An EXISTING move source must be safely removable AND owned by
                # the proposed page (Plan 02 / P2 review). First the shared
                # nonblocking type/readability probe (so a FIFO or socket
                # source is never opened and candidate validation — which
                # would otherwise die in ``copytree`` on the uncopyable source
                # — sees the exact same conflict live apply raises), then the
                # title/path identity check: a source that belongs to another
                # page, to the Control File, or to nobody would turn the
                # post-write unlink into an undeclared removal (a new
                # "Hijacker" page "moving from overview.md" used to delete the
                # existing overview page). A MISSING source stays tolerated:
                # the move writes the target and unlinks nothing.
                reason = _special_or_unreadable_reason(source)
                if reason == "special":
                    raise DestinationConflict(
                        f"Cannot move {title!r} from {source_relative}: the move "
                        f"source is a special file (FIFO, socket, or device) "
                        f"that must not be opened or removed",
                        file=source_relative,
                    )
                if reason == "unreadable":
                    raise DestinationConflict(
                        f"Cannot move {title!r} from {source_relative}: the move "
                        f"source is an unreadable file that must not be opened "
                        f"or removed",
                        file=source_relative,
                    )
                if source_relative != target_relative:
                    # unlink(2) permission comes from the parent directory's
                    # permission metadata, not from the source file, and
                    # ``os.access`` false-passes as root — so the parent's
                    # mode bits (and sticky bit) are evaluated directly (P2
                    # review v5). A source whose parent cannot release it is
                    # rejected BEFORE the target is written, so candidate
                    # validation and live apply can never strand a partial
                    # move behind a raw PermissionError. A self-move (source
                    # == target) overwrites in place and never unlinks, so
                    # it needs no removability.
                    denial = _parent_denies_removal(source.parent, source)
                    if denial is not None:
                        raise DestinationConflict(
                            f"Cannot move {title!r} from {source_relative}: the "
                            f"move source cannot be removed because {denial}; "
                            f"make the parent directory writable and searchable "
                            f"for the publishing user (for example chmod u+wx "
                            f"{Path(source_relative).parent.as_posix()}) before "
                            f"publishing this move",
                            file=source_relative,
                        )
                owner = title_by_path.get(source_relative)
                if owner is not None and owner != title:
                    raise DestinationConflict(
                        f"Cannot move {title!r} from {source_relative}: that "
                        f"file is the recorded path of existing page {owner!r}, "
                        f"not of {title!r}; a move only relocates the proposed "
                        f"page's own recorded path",
                        file=source_relative,
                    )
                if owner is None:
                    raise DestinationConflict(
                        f"Cannot move {title!r} from {source_relative}: the move "
                        f"source is an untracked file (another page's file, the "
                        f"Control File, or a stray file) that no proposed page "
                        f"owns; moving it would delete content the proposal "
                        f"does not declare",
                        file=source_relative,
                    )
            if source_relative != target_relative and (root / target_relative).exists():
                raise DestinationConflict(
                    f"Cannot move {title!r} to {page.relative_path}: "
                    f"destination is already occupied",
                    file=page.relative_path,
                )
            _claim(target_relative, title, "move")
            if source_relative != target_relative:
                # The vacated source is claimed too, so no other proposed
                # write can create a file the move would silently delete.
                _claim(source_relative, title, "move-source")
            destinations.append(
                _Destination(
                    page=page,
                    kind="move",
                    relative_path=target_relative,
                    source_relative_path=source_relative,
                )
            )
            continue
        existing_relative = existing_by_title.get(title)
        if existing_relative is not None:
            # Same existing page revision: keep the recorded path and
            # identity. A changed v2 Entity ID alone is not an implicit
            # replacement, and the recorded path wins over the proposed one.
            target_relative = _checked_relative_path(root, existing_relative, page_title=title)
            _reject_occupied_target(root, target_relative, title, title_by_path, revising=title)
            kind = "compound" if compound else "write"
            merge_base = None
            if kind == "compound":
                try:
                    merge_base = (root / target_relative).read_text(encoding="utf-8")
                except OSError as exc:
                    raise DestinationConflict(
                        f"Compound revision of {title!r} cannot read the existing "
                        f"page at {target_relative}: {exc}",
                        file=target_relative,
                    ) from exc
        elif rename_from:
            # Reviewed title rename (ADR-0016): the identity changes, the old
            # title's recorded path is authoritative, and renaming onto an
            # existing title is blocked (uniqueness).
            if title != rename_from and title in existing_by_title:
                raise DestinationConflict(
                    f"Cannot rename {rename_from!r} to {title!r}: a page with that "
                    f"title already exists",
                    file=page.relative_path,
                )
            old_relative = existing_by_title.get(rename_from)
            target_relative = _checked_relative_path(
                root,
                old_relative if old_relative is not None else page.relative_path,
                page_title=title,
            )
            _reject_occupied_target(
                root, target_relative, title, title_by_path, revising=rename_from
            )
            kind = "write"
            merge_base = None
        else:
            # New page: its proposed path must be unoccupied. The compound
            # fallback shares this rule, so a compound revision can never
            # clobber another page's file either (compound-fallback collision).
            target_relative = _checked_relative_path(root, page.relative_path, page_title=title)
            _reject_occupied_target(root, target_relative, title, title_by_path)
            kind = "compound" if compound else "write"
            merge_base = None
        _claim(target_relative, title, kind)
        destinations.append(
            _Destination(
                page=page,
                kind=kind,
                relative_path=target_relative,
                merge_base=merge_base,
            )
        )
    return destinations


def _resolve_removal_destinations(
    removed_titles: list[str],
    root: Path,
    existing_by_title: dict[str, str],
) -> list[tuple[str, str]]:
    """Resolve every declared removal's path BEFORE any page write (B01).

    A removal is explicit: only declared titles are resolved, and a title
    already absent on disk is skipped (nothing to remove — never inferred).
    Every resolved path must resolve inside the working directory, must not
    be a reserved published artifact (Navigation/Hot Index, Activity Log),
    and must not be a directory. Every EXISTING target must also be
    removable: its parent directory's mode bits (and sticky-bit ownership)
    are evaluated directly with the SAME helper the move source preflight
    uses — never ``os.access``, which false-passes as root — so an
    unremovable target is rejected up front and no proposed page is ever
    written for a removal that could only fail mid-apply. Resolving up front
    means a removal proposal fails its checks before any dependent-page
    revision is written, so the temporary candidate validation sees the same
    conflict live apply would.
    """
    reserved = _reserved_basenames()
    removals: list[tuple[str, str]] = []
    for title in removed_titles:
        relative = existing_by_title.get(title)
        if relative is None:
            # The page is already absent on disk (e.g. removed by a prior
            # operation): nothing to remove. A removal proposal always
            # validates the candidate tree first, so an unresolvable title
            # never silently corrupts the Knowledge Base.
            continue
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise DestinationConflict(
                f"Removed page path escapes working directory: {relative}",
                file=relative,
            )
        normalized = target.relative_to(root).as_posix()
        if Path(normalized).name in reserved:
            raise DestinationConflict(
                f"Cannot remove reserved published artifact: {normalized}",
                file=normalized,
            )
        if (root / normalized).is_dir():
            raise DestinationConflict(
                f"Cannot remove {title!r}: its path is an existing directory: {normalized}",
                file=normalized,
            )
        # unlink(2) permission comes from the target's PARENT directory's
        # permission metadata, not from the target file, and ``os.access``
        # false-passes as root — so the parent's mode bits (and sticky bit)
        # are evaluated directly with the same mode-bit-aware helper the move
        # source preflight uses (P2 review v6). An existing removal target
        # whose parent cannot release it is rejected BEFORE any proposed page
        # is written, so candidate validation and live apply can never strand
        # a partial apply (the revisions landed, the locked removal target
        # still present) behind a raw PermissionError. A target that vanishes
        # between the checks above and this probe stays tolerated: there is
        # then nothing to unlink, exactly as at apply time.
        removal_path = root / normalized
        denial = _parent_denies_removal(removal_path.parent, removal_path)
        if denial is not None:
            raise DestinationConflict(
                f"Cannot remove {title!r} at {normalized}: the removal target "
                f"cannot be removed because {denial}; make the parent directory "
                f"writable and searchable for the publishing user (for example "
                f"chmod u+wx {Path(normalized).parent.as_posix()}) before "
                f"publishing this removal",
                file=normalized,
            )
        removals.append((title, normalized))
    return removals


def _write_destination(destination: _Destination, root: Path) -> Path:
    """Write one checked destination produced by the pre-write resolution (B01).

    All occupancy, duplicate, and escaping guards already passed at
    resolution time; this performs the branch's byte writes. A compound
    revision merges its resolution-time merge base (existing prior Sources
    are preserved and deduplicated, issue #79); a move unlinks its source
    only after the destination is written (issue #80) — that source was
    verified removable (regular, readable, with a parent directory whose
    mode bits and sticky bit permit removing entries) and owned by the
    proposed page's title at resolution time, so the unlink can never remove
    an undeclared file or fail with a raw PermissionError after the target
    landed. The destination's own write — creating missing parents plus the
    file, or truncating an existing regular revision in place — was
    permission-preflighted at resolution time with the same root-safe
    mode-bit evaluation (P2 review v9), so ``mkdir``/``write_text`` cannot
    strand a partial apply behind a raw PermissionError either.
    """
    page = destination.page
    target = root / destination.relative_path
    markdown = page.markdown
    if destination.kind == "compound" and destination.merge_base is not None:
        markdown = merge_compound_sources(page.markdown, destination.merge_base)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    if destination.kind == "move":
        source = root / (destination.source_relative_path or "")
        if source != target and source.exists():
            source.unlink()
    return target


def _reject_proposal_overlaps(
    destinations: list[_Destination],
    removals: list[tuple[str, str]],
) -> None:
    """Reject cross-destination proposal conflicts BEFORE any byte is written (B01).

    Runs after destination resolution, still in the check-only phase: a
    proposed page must never target ``lumio.yaml`` (whether or not the
    proposal also updates the Control File, so the file can never be
    replaced by a page the loader would then never see), and of the declared
    removals only the removal of the very page a write revises may overlap a
    destination — any other overlap would silently discard content. A
    removal also never overlaps a MOVE's vacated source path: apply unlinks
    that path after writing the move target, so the removal — resolved from
    the pre-mutation path — would find the entry already gone and be
    silently skipped, leaving the moved page alive behind a removal the
    proposal declared (P2 review v9). That source/removal overlap is
    rejected here as hidden-removal safety, before any byte is written.
    """
    if CONTROL_FILE_BASENAME in {destination.relative_path for destination in destinations}:
        raise DestinationConflict(
            "A proposed page destination collides with the Control File "
            f"({CONTROL_FILE_BASENAME}); the Control File is never written by a page",
            file=CONTROL_FILE_BASENAME,
        )
    claimed = {destination.relative_path: destination for destination in destinations}
    move_sources = {
        destination.source_relative_path: destination
        for destination in destinations
        if destination.kind == "move" and destination.source_relative_path
    }
    for title, relative in removals:
        claimant = claimed.get(relative)
        if claimant is None:
            mover = move_sources.get(relative)
            if mover is not None:
                # The apply phase unlinks a move's source AFTER writing the
                # target, so a declared removal resolved from the SAME
                # pre-mutation path finds the entry already gone and is
                # silently skipped: the proposal moves the page and then
                # quietly keeps it, ignoring the removal it declares (P2
                # review v9). Rejected before ANY write as hidden-removal
                # safety; a same-title write/compound revision plus removal
                # of that title stays allowed below (the removal wins there).
                raise DestinationConflict(
                    f"Proposed move of {mover.page.title!r} from {relative} "
                    f"collides with the same proposal's removal of {title!r} "
                    f"at {relative}: the removal is resolved from the pre-move "
                    f"path, so the move's own unlink would silently swallow it "
                    f"and leave the moved page behind; run the move and the "
                    f"removal as separate proposals",
                    file=relative,
                )
            continue
        # A proposal may revise a page and remove that SAME page (the removal
        # stays authoritative and the file ends up gone, as before). Any other
        # overlap — a different page writing over a removal path, or a move —
        # would silently discard content, so it is rejected up front.
        if claimant.kind in ("write", "compound") and claimant.page.title == title:
            continue
        raise DestinationConflict(
            f"Proposed page {claimant.page.title!r} targets {relative}, which the "
            f"same proposal removes ({title!r}); destinations must not overlap",
            file=relative,
        )


def _reject_unusable_control_file_path(
    root: Path,
    control_file: KnowledgeBaseControlFile | None,
) -> None:
    """Reject an unusable Control File destination BEFORE any page write (B01).

    ``write_control_file`` replaces ``lumio.yaml`` atomically (temp file plus
    ``os.replace``), which fails with ``IsADirectoryError`` while an existing
    directory occupies the path — a failure that would otherwise surface only
    AFTER the proposal's page writes had already landed, stranding them behind
    a Control File that can never be written. When a Control File is supplied,
    the shared preflight therefore requires the path to be absent or a
    replaceable regular readable file, applying the SAME occupant rules as
    page destinations (P2 review v4): a directory (directly or through a
    symlink), a special file (FIFO, socket, device — typed with the shared
    nonblocking ``stat`` probe so a FIFO is never opened), or an unreadable
    file is rejected up front, and candidate validation aggregates the same
    conflict as a destination issue before its throwaway copy is even made.
    A DANGLING symlink destination is rejected first, on non-following
    metadata (P2 review v7): ``exists()`` follows the link away and used to
    treat the entry as an absent path, so candidate validation reached
    ``copytree`` and leaked a raw ``shutil.Error`` (the link has no target
    to copy) while live apply silently replaced the dangling entry with a
    regular Control File AFTER the proposal's page writes.
    The parent directory of the destination (the Knowledge Base root) must
    also permit the atomic write itself: its write/search mode bits — and the
    sticky-bit replacement rule for an existing entry — are evaluated
    directly from ``stat(2)`` metadata with the same root-safe algorithm as
    the removal and move-source preflights (never ``os.access``, which
    false-passes as root), so a mode-denied root is rejected up front too
    instead of surfacing as a raw PermissionError from the temp-file creation
    only after the proposal's page writes had already landed (P2 review v6).
    A regular readable file keeps the normal replacement behavior and a
    missing path is created.
    """
    if control_file is None:
        return
    target = root / CONTROL_FILE_BASENAME
    # lstat-backed ``is_symlink`` (not exists): a DANGLING symlink is an
    # existing directory entry that the missing-path branch below would
    # otherwise accept for creation, breaking candidate/live parity (P2
    # review v7).
    if target.is_symlink() and not target.exists():
        raise DestinationConflict(
            f"Control File destination is a dangling symlink that must not be "
            f"replaced by the proposed {CONTROL_FILE_BASENAME}: the dangling "
            f"entry is an existing occupant that breaks the candidate copy "
            f"and would be silently replaced link-and-all by the live write",
            file=CONTROL_FILE_BASENAME,
        )
    if not target.exists():
        # A missing path is created — but creating it still needs a parent
        # whose mode bits permit the temp file and the final atomic replace.
        denial = _parent_denies_control_file_write(target.parent, target)
        if denial is not None:
            raise DestinationConflict(
                f"Control File destination {CONTROL_FILE_BASENAME} cannot be "
                f"created because {denial}; make the directory holding "
                f"{CONTROL_FILE_BASENAME} writable and searchable for the "
                f"publishing user before publishing this proposal",
                file=CONTROL_FILE_BASENAME,
            )
        return
    if target.is_dir():
        raise DestinationConflict(
            f"Control File destination is an existing directory and cannot be "
            f"replaced by the proposed {CONTROL_FILE_BASENAME}: the Control "
            f"File write would only fail after the proposal's page writes",
            file=CONTROL_FILE_BASENAME,
        )
    # Same nonblocking type/readability probe as page-destination occupants:
    # stat(2) never opens the occupant, so a FIFO or socket at lumio.yaml is
    # classified — and rejected — without ever being opened, and only a
    # regular file is probe-opened for honest readability (P2 review v4).
    reason = _special_or_unreadable_reason(target)
    if reason == "unreadable":
        raise DestinationConflict(
            f"Control File destination is an unreadable file that cannot be "
            f"replaced by the proposed {CONTROL_FILE_BASENAME}: the Control "
            f"File write would only fail after the proposal's page writes",
            file=CONTROL_FILE_BASENAME,
        )
    if reason == "special":
        raise DestinationConflict(
            f"Control File destination is occupied by a special file (FIFO, "
            f"socket, or device) that must not be opened or replaced by the "
            f"proposed {CONTROL_FILE_BASENAME}",
            file=CONTROL_FILE_BASENAME,
        )
    # The occupant is replaceable — but the atomic replacement still needs a
    # parent whose mode bits permit the temp-file creation and the replace
    # (P2 review v6).
    denial = _parent_denies_control_file_write(target.parent, target)
    if denial is not None:
        raise DestinationConflict(
            f"Control File destination {CONTROL_FILE_BASENAME} cannot be "
            f"replaced because {denial}; make the directory holding "
            f"{CONTROL_FILE_BASENAME} writable and searchable for the "
            f"publishing user before publishing this proposal",
            file=CONTROL_FILE_BASENAME,
        )


def _reject_non_directory_ancestors(
    destinations: list[_Destination],
    removals: list[tuple[str, str]],
    root: Path,
    control_file: KnowledgeBaseControlFile | None,
) -> None:
    """Reject blocked parent components BEFORE any candidate or live write (B01).

    ``Path.mkdir(parents=True)`` cannot create a parent path through an
    existing non-directory: a page file where a subdirectory is needed makes
    the apply phase fail with ``NotADirectoryError``/``FileExistsError`` —
    after earlier pages of the same proposal were already written. Every
    parent component of every proposed destination, of every move source that
    must actually be unlinked, of every declared removal target, and of the
    Control File destination is therefore checked here, still in the shared
    check-only phase: an existing non-directory ancestor (a dangling symlink
    included) raises :class:`DestinationConflict`, while missing parents stay
    valid (they are created on demand) and symlinked directories keep
    resolving like directories, preserving the escape checks made at
    resolution time. Candidate validation runs the same preflight against the
    REAL root before the throwaway copy, so the conflict comes back as a
    destination issue instead of leaking a filesystem exception.
    """
    checked: list[tuple[str, str]] = []
    for destination in destinations:
        checked.append(
            (destination.relative_path, f"Proposed page {destination.page.title!r} destination")
        )
        if destination.kind == "move" and destination.source_relative_path:
            # "As applicable": a move source is only unlinked when it exists,
            # so only an existing source needs its parent path guaranteed.
            if (root / destination.source_relative_path).exists():
                checked.append(
                    (
                        destination.source_relative_path,
                        f"Move source of proposed page {destination.page.title!r}",
                    )
                )
    checked.extend((relative, f"Removal target of {title!r}") for title, relative in removals)
    if control_file is not None:
        checked.append((CONTROL_FILE_BASENAME, "Control File destination"))
    for relative, label in checked:
        for parent in (root / relative).parents:
            if parent == root:
                break
            # lexists (not exists): a dangling symlink ancestor would also
            # break mkdir(parents=True) with FileExistsError mid-apply.
            if os.path.lexists(parent) and not parent.is_dir():
                raise DestinationConflict(
                    f"{label} at {relative} is blocked by a non-directory "
                    f"ancestor: {parent} exists and is not a directory",
                    file=relative,
                )


def _apply_checked_destinations(
    destinations: list[_Destination],
    removals: list[tuple[str, str]],
    root: Path,
    control_file: KnowledgeBaseControlFile | None,
) -> None:
    """Apply an already-checked destination set — the only mutating phase (B01).

    Every path below was resolved, checked, and claimed before this runs, so
    the phase makes no further decisions: page writes in proposal order, then
    the declared removals (each verified removable at resolution time), then
    the optional Control File (whose parent's writability was verified at
    resolution time). Root-relative checked paths apply unchanged to the
    live root and to the temporary candidate tree alike, so candidate
    validation consumes exactly the same mutations live application would
    perform.
    """
    for destination in destinations:
        _write_destination(destination, root)
    for _title, relative in removals:
        target = root / relative
        if target.exists():
            target.unlink()
    if control_file is not None:
        write_control_file(root, control_file)


def apply_proposed_pages(
    proposed_pages: list,
    working_dir: str | Path,
    *,
    removed_titles: list[str] | None = None,
    control_file: KnowledgeBaseControlFile | None = None,
) -> None:
    """Write proposed Compiled Pages into the canonical working directory.

    Destinations are resolved and checked ONCE, up front, before ANY page is
    written (Plan 02 / P2, B01): see :func:`_resolve_proposed_destinations`.
    A conflict raises :class:`DestinationConflict` while the Knowledge Base
    is still untouched, and the temporary candidate validation consumes the
    same resolution, so neither candidate validation nor live application can
    silently overwrite another page, allocate a suffix, or remove anything
    undeclared. Pages whose canonical title already exists update the
    existing file in place at its recorded path; new pages are written to
    their proposed relative path only when that path is unoccupied. A page
    marked as a compound revision (issue #79) is written through additive
    provenance: the existing page's prior Sources are preserved and
    deduplicated against the proposed page's newly informing Sources, so the
    evidence trail is never silently replaced. Each target path must resolve
    inside ``working_dir``.

    ``removed_titles`` (issue #135) removes the corresponding Compiled Page
    files by Canonical Title AFTER the proposed page revisions are written, so
    a single atomic proposal can repair dependent pages (dropping the
    Relationships that targeted a removed page) and then remove the page. The
    removal paths themselves are resolved BEFORE any page write (from the
    pre-mutation state) so the resolution — and any conflict it finds — is
    visible to candidate validation, which applies this same function to a
    throwaway tree. A removal is an explicit, declared mutation: a page
    simply absent from ``proposed_pages`` is never removed. A reserved
    published artifact (Navigation/Hot Index, Activity Log) is never removed,
    and each removal path must resolve inside ``working_dir``. An EXISTING
    removal target must also be removable: its parent directory's mode bits
    (and sticky-bit ownership) are evaluated directly with the same
    root-safe helper the move-source preflight uses, so an unremovable
    target is rejected as a conflict before any proposed page is written
    instead of stranding the revisions behind a raw PermissionError. A
    declared removal whose path the same proposal MOVES is rejected too
    (P2 review v9): the removal is resolved from the pre-mutation path, so
    the move's own source unlink would silently swallow it and leave the
    moved page alive behind a removal the proposal declared.

    Every proposed destination is also preflighted for the write the apply
    phase actually performs (P2 review v9): a missing destination whose
    nearest existing parent directory denies the effective user write and
    search, and a same-title revision whose existing regular file denies
    write, are rejected as :class:`DestinationConflict` BEFORE any page
    byte is written — the first page of a proposal used to write
    successfully and a later one die with a raw PermissionError, stranding
    a partial apply (candidate validation leaked the same filesystem
    exception). The mode bits are evaluated directly with the same
    root-safe algorithm as the other preflights, never ``os.access``.

    ``control_file`` (issue #135) writes a proposed Knowledge Base Control
    File — used by a Page Removal that must drop a stale Hot Index pin
    atomically with the removal. It is written only when supplied, and its
    fixed path (``lumio.yaml`` at the root) is resolved against the proposed
    destinations before any page write, so a page can never overwrite the
    Control File. A Control File destination occupied by an existing
    directory, a special file (FIFO, socket, device), or an unreadable file
    is rejected in the same shared preflight, BEFORE any page write — the
    atomic Control File replacement could otherwise only fail after the
    proposal's pages had already landed. The parent directory of the
    destination must also permit the atomic write (temp-file creation and
    replace): its write/search mode bits and the sticky-bit replacement rule
    are evaluated directly, so a mode-denied root — root-owned environments
    included — is rejected up front too. Likewise, every parent
    component of every proposed destination, move source, removal target,
    and the Control File path must be an existing (or creatable) directory:
    a non-directory ancestor is rejected up front instead of surfacing as a
    ``NotADirectoryError``/``FileExistsError`` after earlier pages of the
    same proposal were written. A Control File destination occupied by a
    DANGLING symlink is likewise rejected up front — detected with
    non-following metadata, because ``exists()`` follows the link away and
    used to accept the entry as a missing path (P2 review v7). The SAME
    rejection now guards every proposed page destination: a submitted path
    that lexically is a dangling symlink (``fresh.md -> ghost.md`` with
    ``ghost.md`` absent) is rejected on non-following ``lstat`` BEFORE
    resolution reroutes the write to the missing target (P2 review v8), and
    the same walk covers a DANGLING symlink ANCESTOR such as
    ``link -> ghost`` in ``link/fresh.md``, which used to resolve to the
    free ``ghost/fresh.md`` behind the uncopyable entry (P2 review v9).
    """
    root = Path(working_dir).resolve()
    existing_by_title = _existing_paths_by_title(root)
    # Phase 1 — resolve and check everything BEFORE any mutation (B01).
    destinations = _resolve_proposed_destinations(proposed_pages, root, existing_by_title)
    removals = (
        _resolve_removal_destinations(removed_titles, root, existing_by_title)
        if removed_titles
        else []
    )
    _reject_proposal_overlaps(destinations, removals)
    # Phase 1b — preflight the write-tool paths themselves BEFORE any mutation
    # (B01): an unusable Control File destination, every non-directory
    # ancestor of every proposed destination, move source, removal target,
    # and the Control File path, and every page destination's own creation /
    # revision permission (nearest existing parent write+search for a
    # missing destination, file write bits for an existing one) are rejected
    # while the KB is still untouched.
    _reject_unusable_control_file_path(root, control_file)
    _reject_non_directory_ancestors(destinations, removals, root, control_file)
    _reject_unwritable_page_destinations(destinations, root)
    # Phase 2 — apply the checked mutations in the documented order.
    _apply_checked_destinations(destinations, removals, root, control_file)


def merge_compound_sources(proposed_markdown: str, existing_markdown: str) -> str:
    """Return proposed Markdown with prior Sources preserved and deduplicated.

    Additive provenance for a compound revision (issue #79): the existing
    Compiled Page's Sources are kept and the proposed page's Sources are
    added, deduplicated by ``id`` (preferring the record that carries a URL
    when the same id appears in both). Sources WITHOUT an ``id`` are valid
    (the base validator accepts them) and are preserved too: they deduplicate
    on ``(title, url)`` instead of being silently dropped — dropping them
    emptied the merged ``sources`` list and falsely blocked compound
    revisions of pages whose provenance carries no ids. The proposed body and
    all other frontmatter are preserved unchanged — only the ``sources`` list is
    widened. Existing-page order is retained first so the evidence trail stays
    stable across compounding publishes. Pure data: no file I/O.
    """
    proposed_data, body, _ = parse_frontmatter(proposed_markdown, Path("compound.md"))
    try:
        existing_data, _existing_body, _ = parse_frontmatter(existing_markdown, Path("existing.md"))
    except Exception:
        existing_data = {}

    existing_sources = as_sources(existing_data.get("sources"))
    proposed_sources = as_sources(proposed_data.get("sources"))

    def _dedup_key(source: Source) -> str:
        # Id-less sources are valid; fall back to their content identity.
        return source.id or f"title:{source.title}\nurl:{source.url or ''}"

    merged: list[Source] = []
    seen: set[str] = set()
    # Existing provenance first, in its recorded order.
    for source in existing_sources:
        key = _dedup_key(source)
        if key in seen:
            continue
        seen.add(key)
        merged.append(source)
    # Newly informing Sources added; a proposed record with the same identity
    # as an existing one replaces it only when it is richer (carries a URL the
    # existing record lacks), so compounding never drops an existing URL.
    for source in proposed_sources:
        key = _dedup_key(source)
        if key not in seen:
            seen.add(key)
            merged.append(source)
        else:
            for i, prior in enumerate(merged):
                if _dedup_key(prior) == key and source.url and not prior.url:
                    merged[i] = source
                    break

    proposed_data["sources"] = [
        {
            **({"id": s.id} if s.id else {}),
            "title": s.title,
            **({"url": s.url} if s.url else {}),
        }
        for s in merged
    ]
    frontmatter = yaml.encode(proposed_data).decode("utf-8").strip()
    return f"---\n{frontmatter}\n---\n{body}"


def apply_compound_revision(page, working_dir: str | Path) -> Path:
    """Write one compound-revision page, merging additive Source provenance.

    Resolves the existing Compiled Page by Canonical Title (falling back to
    the proposed relative path), merges its prior Sources with the proposed
    page's Sources via :func:`merge_compound_sources`, and writes the merged
    Markdown at the resolved target. The merge is UNCONDITIONAL here: this is
    the public compound-revision writer (issue #79), so it routes the shared
    destination checks with the compound branch FORCED for this helper —
    even when the caller's record does not carry the ``compound_revision``
    flag, the existing page's prior Sources are preserved and deduplicated,
    never silently replaced. The target must resolve inside ``working_dir``;
    an occupied, duplicate, or escaping destination raises
    :class:`DestinationConflict` before any byte is written.
    """
    root = Path(working_dir).resolve()
    existing_by_title = _existing_paths_by_title(root)
    (destination,) = _resolve_proposed_destinations(
        [page], root, existing_by_title, force_compound=True
    )
    _reject_non_directory_ancestors([destination], [], root, None)
    _reject_unwritable_page_destinations([destination], root)
    return _write_destination(destination, root)


def _copy_candidate_tree(source: Path, destination: Path, root: Path, candidate_root: Path) -> None:
    """Copy the real Knowledge Base into the throwaway candidate tree (B01).

    This replaces the previous
    ``shutil.copytree(..., ignore_dangling_symlinks=True)`` workaround (P2
    review v9): CPython's dangling-symlink skip judges a RELATIVE link target
    with ``os.path.exists`` against the PROCESS CWD instead of the link's own
    directory, so a VALID KB-relative link such as ``alias.md -> real.md``
    was silently OMITTED from the candidate whenever no same-named file
    happened to sit in the CWD. The candidate then hid exactly the content
    live validation still reads through the link (``_markdown_files`` follows
    symlinked ``.md`` entries), so candidate validation passed a proposal
    whose live application left the Knowledge Base invalid (P2 review v10).

    The copy mirrors the real tree's STRUCTURE instead — source-entry-relative
    by construction:

    - a RELATIVE link resolving INSIDE the Knowledge Base (its own mirrored
      counterpart position) and a DANGLING link are re-created verbatim, so a
      relative link keeps resolving against its own (mirrored) directory
      exactly as it does in the live Knowledge Base and validation reads the
      same content on both trees. Re-creating a dangling link cannot fail
      (``os.symlink`` needs no existing target), so dangling entries are
      handled consistently instead of by CWD-dependent omission;
    - an EXTERNAL relative link — one whose live resolution leaves the
      Knowledge Base root, e.g. ``external-alias.md -> ../external.md`` — is
      NOT representable verbatim: beside the temp candidate the same link
      text resolves beside the TEMP directory, where nothing exists, so the
      candidate would miss exactly the content live validation reads through
      the link (P2 review fix11). Such a file link is source-resolved instead:
      its live read-through content is materialized into the candidate as an
      ordinary regular file at the link's own path — the same bytes at the
      same relative path live validation reads — while the real Knowledge
      Base is never touched (the candidate is a throwaway read-only
      representation, and ``unlink`` removes only the candidate's own verbatim
      recreation, never the source entry);
    - an ABSOLUTE link keeps resolving to the very same target file on both
      trees, and a link to a DIRECTORY reads through to no validation content
      on either tree (``_markdown_files`` never descends a symlinked
      directory and ``is_file()`` is False for the entry), so both stay
      verbatim — content parity without invention;
    - a link whose chain is unreadable on the LIVE tree too (dangling or
      looped) stays verbatim: the contentless entry is as invisible to
      validation as its live counterpart;
    - a platform that cannot re-create the link (e.g. unprivileged Windows)
      falls back to copying the link's read-through content, so nothing VALID
      is hidden; a dangling entry has no content and is skipped — the same
      contentless tolerance the v9 fix established;
    - FIFOs, sockets, and devices are skipped: they are not Knowledge Base
      content, every real-root preflight already rejected such occupants at
      any PROPOSED destination BEFORE this copy runs, and ``copytree`` used
      to die on UNRELATED special entries elsewhere in the tree with a raw
      ``shutil.Error`` collected from ``SpecialFileError``/``OSError`` (P2
      review v10);
    - regular files are copied by content with fresh default permissions:
      every permission decision belongs to the real-root preflights, and
      candidate validation only reads.

    Every resolution here is anchored to the entry's own source directory and
    the candidate's own mirror directory (``os.path.realpath`` on absolute
    paths) — the process CWD is never consulted, so the v9 CWD-relative skip
    cannot reappear.

    Nothing here opens a FIFO or a socket: entries are classified by
    non-following ``DirEntry`` metadata alone (``stat(2)`` never opens a
    file).
    """
    destination.mkdir(parents=True, exist_ok=True)
    with os.scandir(source) as entries:
        for entry in entries:
            target = destination / entry.name
            if entry.is_symlink():
                link_text = os.readlink(entry.path)
                try:
                    os.symlink(link_text, target)
                except (NotImplementedError, OSError):
                    # A platform that cannot re-create the link (unprivileged
                    # Windows): fall back to the link's read-through content
                    # so valid content is never hidden from candidate
                    # validation. Classification is non-following-stat-only;
                    # a dangling link has no target to read and is skipped —
                    # the contentless tolerance fix9 established — and a
                    # special target (FIFO/socket/device) has no Knowledge
                    # Base content to mirror either.
                    try:
                        followed = os.stat(entry.path)
                    except OSError:
                        continue
                    if stat.S_ISDIR(followed.st_mode):
                        _copy_candidate_tree(Path(entry.path), target, root, candidate_root)
                    elif stat.S_ISREG(followed.st_mode):
                        shutil.copyfile(entry.path, target)
                    continue
                # Parity check (P2 review fix11): the verbatim recreation
                # only represents the live entry when it reads through to
                # the same content. Fully resolve BOTH sides — the entry
                # against its own source directory, the recreation against
                # its own mirror directory — and compare.
                source_resolved = Path(os.path.realpath(entry.path))
                candidate_resolved = Path(os.path.realpath(target))
                if candidate_resolved == source_resolved:
                    # An absolute target (or any link resolving to the very
                    # same file): both trees read identical bytes.
                    continue
                if source_resolved.is_relative_to(root) and candidate_resolved == (
                    candidate_root / source_resolved.relative_to(root)
                ):
                    # A KB-relative link at its mirrored counterpart: the
                    # candidate reads its own byte-identical mirror of the
                    # same entry (dangling included — both sides invisible).
                    continue
                try:
                    followed = os.stat(entry.path)
                except OSError:
                    # The chain is unreadable on the LIVE tree too (dangling
                    # or looped): keep the contentless verbatim entry, which
                    # is exactly as invisible to validation as the live link.
                    continue
                if stat.S_ISREG(followed.st_mode):
                    # EXTERNAL (or differently-resolving) FILE link: the
                    # verbatim text would read beside the temp candidate and
                    # miss the content live validation reads. Materialize the
                    # link's live read-through content as an ordinary file at
                    # the link's own mirror path (P2 review fix11). Only the
                    # candidate's own verbatim recreation is removed first —
                    # writing through it would mutate the real external file.
                    os.unlink(target)
                    shutil.copyfile(entry.path, target)
                # A directory link (in-KB or external) reads through to no
                # validation content on either tree, and any other followed
                # type (FIFO, socket, device) is not Knowledge Base content:
                # the verbatim entry is the faithful representation on both.
                continue
            if entry.is_dir(follow_symlinks=False):
                _copy_candidate_tree(Path(entry.path), target, root, candidate_root)
            elif entry.is_file(follow_symlinks=False):
                shutil.copyfile(entry.path, target)
            # Any other entry type (FIFO, socket, device) is not Knowledge
            # Base content: skipping it keeps candidate validation from
            # leaking a raw copy error for an unrelated special entry the
            # proposal does not touch.


def validate_candidate_knowledge_base(
    proposed_pages: list,
    knowledge_base_root: str | Path,
    *,
    removed_titles: list[str] | None = None,
    control_file: KnowledgeBaseControlFile | None = None,
) -> ValidationReport:
    """Validate the fully applied candidate Knowledge Base before publication.

    Builds a throwaway tree that applies ``proposed_pages`` on top of the
    existing Knowledge Base at ``knowledge_base_root`` and validates the
    complete candidate. This enforces cross-Knowledge-Base invariants that
    isolated proposed-page validation cannot see: alias uniqueness across the
    whole Knowledge Base (CONTEXT.md) and Relationship targets that must
    resolve to an existing canonical title. Nothing on disk is mutated; the
    temporary tree is discarded, so a validation failure never leaves a partial
    candidate behind (#37 spec review).

    ``removed_titles`` and ``control_file`` (issue #135) apply a Page
    Removal's page deletions and Hot Index pin update to the throwaway
    candidate so the removal and its dependent-edge repairs validate together
    as ONE candidate — exactly the gate publication uses.

    The candidate consumes the same pre-write destination resolution as live
    application (Plan 02 / P2, B01): every destination and removal is
    resolved and checked against the REAL root BEFORE the throwaway candidate
    tree is copied, so a conflict — including an uncopyable occupant such as
    a Unix socket at a proposed destination, which ``copytree`` itself could
    never get past, a FIFO occupant the preflight must never open, an
    unowned or special/unreadable move source, an unremovable existing
    removal target (its parent directory's mode bits and sticky bit deny
    the unlink, evaluated directly), a non-directory ancestor of
    any write/removal/Control File path, a dangling page-destination
    symlink — leaf or ANCESTOR along the submitted lexical path — that
    ``copytree`` could never copy, an unwritable page destination (a
    missing destination whose nearest existing parent denies the effective
    user write/search, or a read-only same-title revision file), an unusable
    Control File destination (a dangling ``lumio.yaml`` symlink that
    ``copytree`` could never copy included), a move combined with the
    removal of its own moved path (the move's unlink would silently swallow
    the declared removal), or a Control File parent whose mode bits deny the
    atomic temp-file creation and replace — comes back as an error
    ``ValidationIssue`` instead of raised, exactly the conflict live
    application would raise. The throwaway copy itself mirrors the real
    tree's STRUCTURE (:func:`_copy_candidate_tree`, P2 review v10) instead
    of relying on ``copytree``'s CWD-relative ``ignore_dangling_symlinks``
    heuristic: a KB-relative link such as ``alias.md -> real.md`` — and any
    dangling entry — is re-created verbatim, so it is represented on the
    candidate exactly as live validation reads it (the old skip judged the
    relative target against the process CWD and silently OMITTED the valid
    link, hiding the very content the live tree still exposes through it;
    a re-created dangling entry stays as invisible to validation as it is
    live — a rejected dangling destination legitimately survives, and a
    user-authored dangling entry can predate any proposal, P2 review v9).
    An EXTERNAL relative link — one whose live resolution leaves the
    Knowledge Base root, e.g. ``external-alias.md -> ../external.md`` — is
    not representable verbatim: beside the temp candidate the same link
    text resolves beside the TEMP directory, where nothing exists, so the
    verbatim mirror hid exactly the content live validation reads through
    the link (P2 review fix11). Such a file link is source-resolved and its
    live read-through content is materialized into the candidate as an
    ordinary regular file at the link's own path — same bytes, same relative
    path, real Knowledge Base never touched — so candidate validation and
    the live gate read the same pages. Absolute links, directory links, and
    unreadable/looped chains stay verbatim (content parity without
    invention), and an unrelated FIFO, socket, or device elsewhere in the
    Knowledge Base is skipped as the contentless entry it is instead of
    surfacing a raw ``shutil.Error`` for a proposal that never touches it.
    Only :class:`DestinationConflict` is translated; unexpected errors still
    propagate. The checked destination set is then applied to the candidate
    unchanged, so the validated tree is the tree publication would write.
    """

    def _conflict_report(exc: DestinationConflict) -> ValidationReport:
        return ValidationReport(
            issues=[
                ValidationIssue(
                    file=exc.file or "proposal",
                    field="destination",
                    message=str(exc),
                )
            ]
        )

    root = Path(knowledge_base_root).resolve()
    existing_by_title = _existing_paths_by_title(root)
    try:
        # Resolve and check EVERY destination and removal against the REAL
        # root BEFORE the candidate copy exists (Plan 02 / P2, B01): an
        # uncopyable occupant at a proposed destination must surface as the
        # same :class:`DestinationConflict` live apply raises — not as a
        # ``shutil.copytree`` failure — so candidate validation keeps live
        # parity and still aggregates the conflict into an error issue.
        destinations = _resolve_proposed_destinations(proposed_pages, root, existing_by_title)
        removals = (
            _resolve_removal_destinations(removed_titles, root, existing_by_title)
            if removed_titles
            else []
        )
        _reject_proposal_overlaps(destinations, removals)
        # The same preflights run here so a blocked Control File destination,
        # a non-directory ancestor, or an unwritable page destination
        # (missing destination under a mode-denied nearest existing parent,
        # or a read-only same-title revision file) surfaces as an aggregated
        # destination issue against the REAL root, never as a filesystem
        # exception from the candidate apply phase (Plan 02 / P2, B01).
        _reject_unusable_control_file_path(root, control_file)
        _reject_non_directory_ancestors(destinations, removals, root, control_file)
        _reject_unwritable_page_destinations(destinations, root)
    except DestinationConflict as exc:
        return _conflict_report(exc)
    with tempfile.TemporaryDirectory() as tmp:
        # Resolved so the mirror's KB-relative parity check compares in one
        # canonical namespace: a system temp dir reached THROUGH a symlink
        # (e.g. macOS ``/tmp``) would otherwise map mirrored paths into a
        # different textual prefix than ``realpath`` reports and downgrade
        # valid KB-relative links to materialized copies (content parity
        # would hold; the verbatim representation would not).
        candidate = (Path(tmp) / "candidate").resolve()
        # The throwaway copy mirrors the real tree's STRUCTURE instead of
        # ``copytree``'s CWD-relative ``ignore_dangling_symlinks`` heuristic
        # (P2 review v10): KB-relative links — and dangling entries — are
        # re-created verbatim, so they are represented on both trees exactly
        # alike (the old skip judged the relative target against the process
        # CWD and silently omitted the VALID link, hiding from candidate
        # validation the very content live validation reads through it), an
        # EXTERNAL relative FILE link's live read-through content is
        # materialized into the candidate at the link's own path because the
        # verbatim text would resolve beside the temp candidate and hide
        # exactly the page the live gate reads (P2 review fix11), an
        # unrelated FIFO/socket/device is skipped as the contentless entry it
        # is instead of surfacing a raw ``shutil.Error`` for a proposal that
        # never touches it, and a dangling entry re-created in the candidate
        # stays as invisible to validation as it is live. Masking nothing
        # still holds: every conflict preflight above ran against the REAL
        # root before this copy exists, a dangling or special entry can never
        # be a page or Control File, and every write branch rejects
        # destinations that lexically traverse one.
        _copy_candidate_tree(root, candidate, root, candidate)
        try:
            # The checked set applies unchanged to the byte-identical
            # candidate tree: every path is root-relative by construction.
            _apply_checked_destinations(destinations, removals, candidate, control_file)
        except DestinationConflict as exc:
            return _conflict_report(exc)
        return validate(candidate)


__all__ = [
    "DestinationConflict",
    "PublishError",
    "apply_compound_revision",
    "apply_proposed_pages",
    "merge_compound_sources",
    "validate_candidate_knowledge_base",
]
