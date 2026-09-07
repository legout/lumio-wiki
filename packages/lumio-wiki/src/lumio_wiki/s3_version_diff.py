"""S3 Published Version diff — read-only deterministic comparison of manifests.

t_66f162f2. Answers "what changed since the version I read?" (gap analysis
recommendation 4) over the immutable Published Version protocol (ADR-0013):
two complete versions are compared as parsed canonical content, never as raw
paths or digests alone. The report carries stable added/removed/changed Page
paths and Source identities, plus lifecycle/visibility transitions only when
the canonical data names both sides. This is a pure read-only report: no prose
digests, no mutable activity log (ADR-0022), no publication state — an agent
turns the deterministic delta into prose. Every ``.md`` file of both versions
must parse as a Compiled Page (or be a reserved artifact such as the generated
Navigation Index, which is derived state and excluded): anything else fails
closed rather than guessing whether unknown content changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import msgspec

from lumio_wiki.knowledge_base import (
    FrontmatterError,
    KnowledgeBaseError,
    _load_page,
    _reserved_artifact_basename,
)
from lumio_wiki.records import CompiledPage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = [
    "PageChange",
    "PublishedVersionDiff",
    "compare_published_versions",
]


class PageChange(msgspec.Struct, frozen=True):
    """One changed Compiled Page with the transitions canonical data names.

    ``lifecycle``/``visibility`` carry the ``(from, to)`` pair and are
    ``None`` when unchanged; a side the page data does not name is ``None``
    inside the pair — never invented.
    """

    path: str
    """Relative canonical path of the page in both versions."""

    title: tuple[str | None, str | None]
    """The ``(from, to)`` Canonical Page Title pair (both sides always known)."""

    lifecycle: tuple[str | None, str | None] | None = None
    """The ``(from, to)`` lifecycle pair, or ``None`` when unchanged."""

    visibility: tuple[str | None, str | None] | None = None
    """The ``(from, to)`` visibility pair, or ``None`` when unchanged."""

    added_sources: tuple[str, ...] = ()
    """Source ids this page gained (stable ``sources[].id`` identities)."""

    removed_sources: tuple[str, ...] = ()
    """Source ids this page lost."""


class PublishedVersionDiff(msgspec.Struct, frozen=True):
    """The deterministic delta between two immutable Published Versions.

    Page identity is the stable relative path; Source identity is the stable
    ``sources[].id``. Generated reserved artifacts (the Navigation Index) are
    excluded: they are derived, deterministic, and diff noise, not knowledge.
    """

    from_version: str
    to_version: str
    added_pages: tuple[str, ...] = ()
    removed_pages: tuple[str, ...] = ()
    changed: tuple[PageChange, ...] = ()
    added_sources: tuple[str, ...] = ()
    removed_sources: tuple[str, ...] = ()

    @property
    def changed_pages(self) -> tuple[str, ...]:
        """Paths of the changed pages (parallel to :attr:`changed`)."""
        return tuple(change.path for change in self.changed)

    @property
    def has_changes(self) -> bool:
        """Whether any page or source identity changed between the versions."""
        return bool(
            self.added_pages
            or self.removed_pages
            or self.changed
            or self.added_sources
            or self.removed_sources
        )


def compare_published_versions(
    from_version: str,
    to_version: str,
    from_content: Mapping[str, bytes],
    to_content: Mapping[str, bytes] | None = None,
) -> PublishedVersionDiff:
    """Compare two immutable Published Versions' canonical content trees.

    Pure and read-only: two ``{relative_path: bytes}`` trees in (as the
    manifest materializes them), the report out — nothing is written and no
    publication state is consulted or created. The optional ``to_content``
    defaults to ``from_content`` so ``compare(v1, v2, content)`` is a valid
    no-change comparison.

    Every ``.md`` file must parse as a Compiled Page or be a reserved
    artifact (the generated Navigation Index); anything unparseable raises
    :class:`KnowledgeBaseError` instead of a guessed delta.
    """
    to_content = from_content if to_content is None else to_content

    before = _page_views(from_content)
    after = _page_views(to_content)

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))

    changed: list[PageChange] = []
    from_sources: set[str] = set()
    to_sources: set[str] = set()
    for path in sorted(set(before) & set(after)):
        old, new = before[path], after[path]
        from_sources |= old.source_ids
        to_sources |= new.source_ids
        if old.raw == new.raw:
            continue
        changed.append(
            PageChange(
                path=path,
                title=(old.title, new.title),
                lifecycle=_transition(old.lifecycle, new.lifecycle),
                visibility=_transition(old.visibility, new.visibility),
                added_sources=tuple(sorted(new.source_ids - old.source_ids)),
                removed_sources=tuple(sorted(old.source_ids - new.source_ids)),
            )
        )
    # Sources of added/removed pages count toward the version-side union too:
    # a page present in only one version still contributes its sources there.
    for path, view in before.items():
        if path not in after:
            from_sources |= view.source_ids
    for path, view in after.items():
        if path not in before:
            to_sources |= view.source_ids

    return PublishedVersionDiff(
        from_version=from_version,
        to_version=to_version,
        added_pages=tuple(added),
        removed_pages=tuple(removed),
        changed=tuple(changed),
        added_sources=tuple(sorted(to_sources - from_sources)),
        removed_sources=tuple(sorted(from_sources - to_sources)),
    )


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------


class _PageView:
    """The diff-relevant projection of one parsed Compiled Page."""

    __slots__ = ("lifecycle", "raw", "source_ids", "title", "visibility")

    def __init__(self, page: CompiledPage) -> None:
        self.raw = page
        self.title: str | None = page.title or None
        self.lifecycle: str | None = page.lifecycle or None
        self.visibility: str | None = page.visibility or None
        self.source_ids = frozenset(source.id for source in page.sources if source.id)


def _page_views(content: Mapping[str, bytes]) -> dict[str, _PageView]:
    """Parse every Compiled Page of one version; reserved artifacts excluded.

    Non-Markdown canonical files (the Control File) never diff. A reserved
    basename (the generated Navigation Index) is derived state and excluded.
    Anything else must parse — a tree the loader cannot fully classify must
    not produce a partially-guessed diff.
    """
    views: dict[str, _PageView] = {}
    for rel, raw in content.items():
        if not rel.lower().endswith(".md"):
            continue
        if _reserved_artifact_basename(Path(rel)) is not None:
            continue
        try:
            page, _data = _load_page(raw.decode("utf-8"), rel)
        except (FrontmatterError, UnicodeDecodeError) as exc:
            raise KnowledgeBaseError(
                f"cannot diff: {rel!r} is not a parsable Compiled Page: {exc}"
            ) from exc
        views[rel] = _PageView(page)
    return views


def _transition(before: str | None, after: str | None) -> tuple[str | None, str | None] | None:
    """Return the ``(from, to)`` pair, or ``None`` when the values are equal."""
    return None if before == after else (before, after)
