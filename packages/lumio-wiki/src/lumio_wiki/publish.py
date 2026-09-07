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
escaping paths, and compound-fallback collisions are rejected up front.
There is no automatic suffix allocation and no removal that was not
declared.
"""

from __future__ import annotations

import shutil
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
    """Return the normalized root-relative destination, rejecting escapes (B01)."""
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise DestinationConflict(
            f"Proposed page {page_title!r} path escapes working directory: {relative_path}",
            file=relative_path,
        )
    return target.relative_to(root).as_posix()


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
    write branch: an existing directory, an unreadable occupant, or a file
    owned by a different page is never overwritten. ``revising`` names the one
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
    try:
        # Probe readability honestly: os.access lies when running as root, and
        # an occupant we cannot read is one we must not overwrite (B01).
        with target.open("rb"):
            pass
    except OSError as exc:
        raise DestinationConflict(
            f"Proposed page {page_title!r} destination is an unreadable file "
            f"that must not be overwritten: {relative_path}",
            file=relative_path,
        ) from exc
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


def _resolve_proposed_destinations(
    proposed_pages: list,
    root: Path,
    existing_by_title: dict[str, str],
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
    - every destination must resolve inside the working directory.
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
        compound = bool(getattr(page, "compound_revision", False))
        title = page.title
        if move_from:
            # Explicit category/path move (issue #80): relocate ATOMICALLY.
            # Both paths are escape-checked, and the original collision guard
            # (issue #80, P1.2) is reused as the occupancy rule for the move.
            source_relative = _checked_relative_path(root, move_from, page_title=title)
            target_relative = _checked_relative_path(root, page.relative_path, page_title=title)
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
    and must not be a directory. Resolving up front means a removal proposal
    fails its checks before any dependent-page revision is written, so the
    temporary candidate validation sees the same conflict live apply would.
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
        removals.append((title, normalized))
    return removals


def _write_destination(destination: _Destination, root: Path) -> Path:
    """Write one checked destination produced by the pre-write resolution (B01).

    All occupancy, duplicate, and escaping guards already passed at
    resolution time; this performs the branch's byte writes. A compound
    revision merges its resolution-time merge base (existing prior Sources
    are preserved and deduplicated, issue #79); a move unlinks its source
    only after the destination is written (issue #80).
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
    and each removal path must resolve inside ``working_dir``.

    ``control_file`` (issue #135) writes a proposed Knowledge Base Control
    File — used by a Page Removal that must drop a stale Hot Index pin
    atomically with the removal. It is written only when supplied, and its
    fixed path (``lumio.yaml`` at the root) is resolved against the proposed
    destinations before any page write, so a page can never overwrite the
    Control File.
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
    # The Control File path is resolved before any page write: a proposed page
    # must never target ``lumio.yaml`` (whether or not this proposal also
    # updates the Control File), so the file can never be replaced by a page
    # the loader would then never see.
    if CONTROL_FILE_BASENAME in {destination.relative_path for destination in destinations}:
        raise DestinationConflict(
            "A proposed page destination collides with the Control File "
            f"({CONTROL_FILE_BASENAME}); the Control File is never written by a page",
            file=CONTROL_FILE_BASENAME,
        )
    claimed = {destination.relative_path: destination for destination in destinations}
    for title, relative in removals:
        claimant = claimed.get(relative)
        if claimant is None:
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
    # Phase 2 — apply the checked mutations in the documented order.
    for destination in destinations:
        _write_destination(destination, root)
    for _title, relative in removals:
        target = root / relative
        if target.exists():
            target.unlink()
    if control_file is not None:
        write_control_file(root, control_file)


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
    Markdown at the resolved target. The target must resolve inside
    ``working_dir``. Used by the publish-path writer for compound revisions
    (issue #79); it never silently replaces the evidence trail.
    """
    root = Path(working_dir).resolve()
    existing_by_title = _existing_paths_by_title(root)
    (destination,) = _resolve_proposed_destinations([page], root, existing_by_title)
    return _write_destination(destination, root)


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
    application (Plan 02 / P2, B01): a destination conflict (occupied or
    duplicate target, escaping path, …) is returned as an error
    ``ValidationIssue`` instead of raised, so staging surfaces a blocked
    proposal and the publish gate refuses through the ordinary candidate
    report. Only :class:`DestinationConflict` is translated; unexpected
    errors still propagate.
    """
    root = Path(knowledge_base_root)
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "candidate"
        shutil.copytree(root, candidate, dirs_exist_ok=True)
        try:
            apply_proposed_pages(
                proposed_pages,
                candidate,
                removed_titles=removed_titles,
                control_file=control_file,
            )
        except DestinationConflict as exc:
            return ValidationReport(
                issues=[
                    ValidationIssue(
                        file=exc.file or "proposal",
                        field="destination",
                        message=str(exc),
                    )
                ]
            )
        return validate(candidate)


__all__ = [
    "DestinationConflict",
    "PublishError",
    "apply_compound_revision",
    "apply_proposed_pages",
    "merge_compound_sources",
    "validate_candidate_knowledge_base",
]
