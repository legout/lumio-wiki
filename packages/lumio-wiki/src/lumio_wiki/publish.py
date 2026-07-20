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
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import msgspec.yaml as yaml

from lumio_wiki.knowledge_base import (
    _as_sources as as_sources,
    _parse_frontmatter as parse_frontmatter,
    load_knowledge_base,
    validate,
)
from lumio_wiki.records import Source, ValidationReport


class PublishError(Exception):
    """Raised when a publish operation fails."""


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


def apply_proposed_pages(
    proposed_pages: list,
    working_dir: str | Path,
) -> None:
    """Write proposed Compiled Pages into the canonical working directory.

    Pages whose canonical title already exists in the working directory update
    the existing file in place; new pages are written to their proposed
    relative path. A page marked as a compound revision (issue #79) is written
    through additive provenance: the existing page's prior Sources are
    preserved and deduplicated against the proposed page's newly informing
    Sources, so the evidence trail is never silently replaced. Each target
    path must resolve inside ``working_dir``.
    """
    working_dir = Path(working_dir).resolve()
    existing_by_title = _existing_paths_by_title(working_dir)
    for page in proposed_pages:
        # An explicit category/path move (issue #80): relocate the page
        # ATOMICALLY from its current canonical path to the proposed path,
        # preserving its full Markdown (Canonical Page Title, aliases,
        # Sources, typed Relationships). The old file is removed only after
        # the new file is written, and both paths must resolve inside the
        # working directory. A destination occupied by a DIFFERENT page is
        # rejected rather than silently destroying it (P1.2).
        if getattr(page, "move_from_path", None):
            source = (working_dir / page.move_from_path).resolve()
            target = (working_dir / page.relative_path).resolve()
            if not source.is_relative_to(working_dir) or not target.is_relative_to(working_dir):
                raise PublishError(
                    f"Move path escapes working directory: "
                    f"{page.move_from_path} -> {page.relative_path}"
                )
            # Destination collision guard: block a move whose target path is
            # already occupied by any file or directory, instead of silently
            # overwriting it. Same-title content at the target is allowed only
            # when it is the source of the move itself (the page is being
            # rewritten at a path it already owns). Malformed or unreadable
            # files are treated as occupied, not overwritten (P1.2).
            if target != source and target.exists():
                raise PublishError(
                    f"Cannot move {page.title!r} to {page.relative_path}: "
                    f"destination is already occupied"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page.markdown, encoding="utf-8")
            if source != target and source.exists():
                source.unlink()
            continue
        # A compound revision compounds an existing Compiled Page by Canonical
        # Title: route it through the additive-provenance writer so prior
        # Sources are merged rather than overwritten (issue #79). Plain pages
        # (including unflagged title collisions) keep replace semantics.
        if getattr(page, "compound_revision", False):
            apply_compound_revision(page, working_dir)
            continue
        # Resolve the target path explicitly: an existing page with the same
        # canonical title is overwritten in place at its recorded path;
        # otherwise the proposed relative path is used. The explicit ``None``
        # check keeps the resolved path a non-optional ``str`` — ``dict.get``
        # alone is inferred as ``str | None`` — and preserves empty-path
        # handling (a title absent from the mapping falls back to the proposed
        # path rather than producing ``Path / None``).
        existing_path = existing_by_title.get(page.title)
        relative_path = existing_path if existing_path is not None else page.relative_path
        target = (working_dir / relative_path).resolve()
        if not target.is_relative_to(working_dir):
            raise PublishError(f"Proposed page path escapes working directory: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.markdown, encoding="utf-8")


def merge_compound_sources(proposed_markdown: str, existing_markdown: str) -> str:
    """Return proposed Markdown with prior Sources preserved and deduplicated.

    Additive provenance for a compound revision (issue #79): the existing
    Compiled Page's Sources are kept and the proposed page's Sources are
    added, deduplicated by ``id`` (preferring the record that carries a URL
    when the same id appears in both). The proposed body and all other
    frontmatter are preserved unchanged — only the ``sources`` list is
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

    merged: list[Source] = []
    seen: set[str] = set()
    # Existing provenance first, in its recorded order.
    for source in existing_sources:
        if not source.id or source.id in seen:
            continue
        seen.add(source.id)
        merged.append(source)
    # Newly informing Sources added; a proposed record with the same id as an
    # existing one replaces it only when it is richer (carries a URL the
    # existing record lacks), so compounding never drops an existing URL.
    for source in proposed_sources:
        if not source.id:
            continue
        if source.id not in seen:
            seen.add(source.id)
            merged.append(source)
        else:
            for i, prior in enumerate(merged):
                if prior.id == source.id and source.url and not prior.url:
                    merged[i] = source
                    break

    proposed_data["sources"] = [
        {"id": s.id, "title": s.title, **({"url": s.url} if s.url else {})} for s in merged
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
    existing_by_title: dict[str, str] = {}
    try:
        loaded_kb, _report = load_knowledge_base(root)
        existing_by_title = {p.title: p.path for p in loaded_kb.pages if p.title and p.path}
    except Exception:
        existing_by_title = {}

    existing_path = existing_by_title.get(page.title)
    relative_path = existing_path if existing_path is not None else page.relative_path
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise PublishError(f"compound revision path escapes working directory: {relative_path}")
    markdown = page.markdown
    if existing_path is not None:
        try:
            existing_markdown = target.read_text(encoding="utf-8")
            markdown = merge_compound_sources(page.markdown, existing_markdown)
        except OSError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target


def validate_candidate_knowledge_base(
    proposed_pages: list,
    knowledge_base_root: str | Path,
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
    """
    root = Path(knowledge_base_root)
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "candidate"
        shutil.copytree(root, candidate, dirs_exist_ok=True)
        apply_proposed_pages(proposed_pages, candidate)
        return validate(candidate)


__all__ = [
    "PublishError",
    "apply_compound_revision",
    "apply_proposed_pages",
    "merge_compound_sources",
    "validate_candidate_knowledge_base",
]
