"""Deterministic Source identity resolution (issue #176, ADR-0020).

`source resolve` and the enriched unknown-source errors share one resolution
model. A query resolves to ONE registered Knowledge Source deterministically
when the mapping is unambiguous:

1. an exact registered Source ID;
2. page identity — Entity ID, Canonical Page Title, alias, or page path —
   where the matched page declares exactly ONE registered source.

Anything else is a truthful outcome, never a guess:

* a page with several (registered) sources, a title/alias claimed by several
  pages, or one page declaring several sources → ``ambiguous`` with bounded
  candidates;
* a page that declares no sources, a declared-but-unregistered source id, or
  a query matching nothing → ``unknown`` with a bounded set of close
  registered Source IDs (issue #176: never dump unbounded registries).

Resolution is read-only over public page metadata and the private registry's
id list; it discloses no object keys, no binding history, and no artifact
existence. A Reader-facing surface must not call it with private input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lumio_wiki.records import CompiledPage
from lumio_wiki.source_registry import SourceRegistryError

#: Resolution outcomes.
OUTCOME_RESOLVED = "resolved"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_UNKNOWN = "unknown"

#: Which surface a resolved query matched.
SOURCE_MATCH_SOURCE_ID = "source-id"
SOURCE_MATCH_ENTITY_ID = "entity-id"
SOURCE_MATCH_CANONICAL_TITLE = "canonical-title"
SOURCE_MATCH_ALIAS = "alias"
SOURCE_MATCH_PATH = "path"

#: Bounded suggestion surface (issue #176: never dump unbounded registries).
MAX_SUGGESTIONS = 8

#: Bounded ambiguity surface (issue #176: a page may declare arbitrarily
#: many sources; the candidate set is capped with a truthful truncation note).
MAX_CANDIDATES = 8


@dataclass(frozen=True)
class SourceIdentity:
    """One candidate (source_id, page) pair for an ambiguous resolution."""

    source_id: str
    page_title: str = ""
    page_path: str = ""


@dataclass(frozen=True)
class SourceResolution:
    """The deterministic result of resolving one query to a Source identity."""

    outcome: str
    source_id: str | None = None
    matched_by: str = ""
    page: CompiledPage | None = None
    candidates: list[SourceIdentity] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    note: str = ""


def _normalize(value: str) -> str:
    """Case-fold and collapse separators so title-ish queries match id labels."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _words(value: str) -> set[str]:
    """Significant words (≥4 chars) for overlap matching."""
    return {word for word in re.split(r"[^a-z0-9]+", value.lower()) if len(word) >= 4}


def suggest_source_ids(known_ids, query: str) -> list[str]:
    """Return a bounded, sorted set of close registered Source IDs.

    Matching is separator-normalized containment or significant-word overlap:
    a Canonical Page Title shape ("Atlas Series Product Catalog") points at
    the registered id shape (``atlas-series-product-catalog``); a partial
    title ("Atlas Catalog Wrong") still overlaps on its significant words.
    Bounded to :data:`MAX_SUGGESTIONS` (issue #176: never dump registries).
    """
    needle = _normalize(query)
    if not needle:
        return []
    query_words = _words(query)
    matches = set()
    for source_id in known_ids:
        normalized = _normalize(source_id)
        if not normalized:
            continue
        if needle in normalized or normalized in needle:
            matches.add(source_id)
        elif query_words and query_words & _words(source_id):
            matches.add(source_id)
    return sorted(matches)[:MAX_SUGGESTIONS]


def _identity(source_id: str, page: CompiledPage) -> SourceIdentity:
    return SourceIdentity(source_id=source_id, page_title=page.title, page_path=page.path)


def _echoable_source_id(source_id: str) -> bool:
    """Whether a page-declared source id is a safe label that may be echoed.

    Page frontmatter is public, but a declared id could still carry
    secret-bearing text: mirror the registry's boundary validation
    (:func:`lumio_wiki.source_registry._validate_source_id`) — only a
    validated label is echoed back; anything else renders a generic label.
    """
    from lumio_wiki.source_registry import _validate_source_id

    try:
        _validate_source_id(source_id)
    except SourceRegistryError:
        return False
    return True


def _by_page_identity(pages: list[CompiledPage], query: str, known_ids) -> SourceResolution:
    """Resolve through page identity: exact Entity ID, title, alias, then path."""
    key = query.strip()

    def registered(page: CompiledPage) -> list[str]:
        return [source.id for source in page.sources if source.id in known_ids]

    def candidates_from(matched: list[CompiledPage], matched_by: str) -> SourceResolution | None:
        """Decisive result for the pages one surface matched, or ``None``.

        A mapping is unambiguous only when every matched page declares exactly
        one DISTINCT source id and that id is registered. Anything else is a
        truthful ambiguity (all declared ids — page frontmatter is public) or
        a truthful unknown (no sources declared, or the one declared id is
        not registered/bound) — never a guess.
        """
        if not matched:
            return None
        declared: list[tuple[str, CompiledPage]] = []
        for page in matched:
            declared.extend((source.id, page) for source in page.sources if source.id)
        if not declared:
            return SourceResolution(
                outcome=OUTCOME_UNKNOWN,
                note=(
                    f"page {matched[0].title!r} declares no Knowledge Sources, so "
                    "no Source identity can be resolved from it"
                ),
            )
        distinct = {source_id for source_id, _ in declared}
        unregistered = distinct - set(known_ids)
        if len(distinct) == 1 and not unregistered:
            source_id, page = declared[0]
            return SourceResolution(
                outcome=OUTCOME_RESOLVED, source_id=source_id, matched_by=matched_by, page=page
            )
        if len(distinct) == 1:
            (only,) = distinct
            label = only if _echoable_source_id(only) else "an unsafe label"
            return SourceResolution(
                outcome=OUTCOME_UNKNOWN,
                note=(
                    f"the page declares source id {label!r} with no registered "
                    "Knowledge Source or Published Version binding"
                ),
            )
        # Bounded ambiguity (issue #176): a page may declare arbitrarily many
        # sources, so the candidate set is capped with a truthful truncation
        # note — never an unbounded dump.
        ordered: list[tuple[str, CompiledPage]] = []
        seen: set[str] = set()
        for source_id, page in declared:
            if source_id not in seen:
                seen.add(source_id)
                ordered.append((source_id, page))
        truncated_count = max(len(ordered) - MAX_CANDIDATES, 0)
        note = (
            f"{truncated_count} more declared source id(s) not shown — inspect "
            "the matched page's sources frontmatter for the complete set"
            if truncated_count
            else ""
        )
        return SourceResolution(
            outcome=OUTCOME_AMBIGUOUS,
            matched_by=matched_by,
            candidates=[_identity(source_id, page) for source_id, page in ordered[:MAX_CANDIDATES]],
            note=note,
        )

    def lookup(
        mapping: dict[str, list[CompiledPage]], key: str, matched_by: str
    ) -> SourceResolution | None:
        return candidates_from(mapping.get(key, []), matched_by)

    index: dict[str, CompiledPage] = {}
    titles: dict[str, list[CompiledPage]] = {}
    aliases: dict[str, list[CompiledPage]] = {}
    paths: dict[str, list[CompiledPage]] = {}
    for page in pages:
        if page.id:
            index.setdefault(page.id, page)
        if page.title:
            titles.setdefault(page.title, []).append(page)
        for alias in page.aliases:
            aliases.setdefault(alias, []).append(page)
        paths.setdefault(page.path, []).append(page)

    # Entity ID is globally unique (the loader's index keeps the first); the
    # duplicate-titles check below still guards non-categorized legacy pages.
    page = index.get(key)
    if page is not None:
        result = candidates_from([page], SOURCE_MATCH_ENTITY_ID)
        if result is not None:
            return result
    for mapping, matched_by in (
        (titles, SOURCE_MATCH_CANONICAL_TITLE),
        (aliases, SOURCE_MATCH_ALIAS),
        (paths, SOURCE_MATCH_PATH),
    ):
        result = lookup(mapping, key, matched_by)
        if result is not None:
            return result
    return SourceResolution(outcome=OUTCOME_UNKNOWN)


def resolve_source(
    pages: list[CompiledPage], known_ids, query: str
) -> SourceResolution:
    """Resolve one query to ONE registered Source identity or a truthful outcome.

    ``known_ids`` is the private registry's Source ID set (a Reader-facing
    surface must not supply it with private input). Deterministic priority:
    exact registered Source ID, then page identity (Entity ID, Canonical Page
    Title, alias, page path) where exactly one registered source is declared.
    """
    known = set(known_ids)
    key = (query or "").strip()
    if not key:
        return SourceResolution(outcome=OUTCOME_UNKNOWN)
    if key in known:
        return SourceResolution(
            outcome=OUTCOME_RESOLVED, source_id=key, matched_by=SOURCE_MATCH_SOURCE_ID
        )
    result = _by_page_identity(pages, key, known)
    if result.outcome != OUTCOME_UNKNOWN:
        # An exact page surface (entity id/title/alias/path) is deterministic.
        return result
    # Bounded close-id suggestions for the discovery error (issue #176):
    # the exact registered id or nothing — never an unbounded registry.
    suggestions = suggest_source_ids(known, key)
    if suggestions:
        return SourceResolution(
            outcome=OUTCOME_UNKNOWN,
            suggestions=suggestions,
            note=result.note,
        )
    return result
