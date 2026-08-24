"""Deterministic Source identity resolution (issue #176, ADR-0020).

Locks the resolution semantics behind ``source resolve`` and the enriched
unknown-source errors: exact Source ID first, then page identity (Entity ID,
Canonical Page Title, alias, path) where the page→source mapping is
unambiguous; truthful ambiguity with bounded candidates; bounded close-id
suggestions on unknown input; and never a guess.
"""

from __future__ import annotations

from lumio_wiki.records import CompiledPage, Source
from lumio_wiki.source_resolution import (
    MAX_SUGGESTIONS,
    OUTCOME_AMBIGUOUS,
    OUTCOME_RESOLVED,
    OUTCOME_UNKNOWN,
    SOURCE_MATCH_ALIAS,
    SOURCE_MATCH_CANONICAL_TITLE,
    SOURCE_MATCH_ENTITY_ID,
    SOURCE_MATCH_PATH,
    SOURCE_MATCH_SOURCE_ID,
    resolve_source,
    suggest_source_ids,
)


def _page(
    title: str,
    path: str,
    source_ids: list[str],
    *,
    page_id: str = "",
    aliases: list[str] | None = None,
) -> CompiledPage:
    return CompiledPage(
        path=path,
        title=title,
        id=page_id,
        aliases=list(aliases or []),
        sources=[Source(id=source_id, title=f"{source_id} source") for source_id in source_ids],
    )


PAGES = [
    _page(
        "Atlas Heatworks Product Catalog",
        "references/atlas-catalog.md",
        ["atlas-heatworks-product-catalog"],
        page_id="entity:atlas-catalog",
        aliases=["Atlas Catalog"],
    ),
    _page("Field Handbook Notes", "handbook.md", ["handbook-src"], page_id="entity:handbook"),
    _page("Dual Source Page", "dual.md", ["alpha-src", "beta-src"]),
    _page("No Source Page", "bare.md", []),
]
KNOWN = {"atlas-heatworks-product-catalog", "handbook-src", "alpha-src", "beta-src"}


# ---------------------------------------------------------------------------
# suggest_source_ids: bounded close-id suggestions on unknown input.
# ---------------------------------------------------------------------------


def test_suggest_matches_a_page_title_shaped_query_to_the_source_id():
    # The reader-trial shape (issue #176 AC): a title-ish query resolves to
    # the exact registered id through separator normalization.
    assert suggest_source_ids(KNOWN, "Atlas Heatworks Product Catalog") == [
        "atlas-heatworks-product-catalog"
    ]


def test_suggest_matches_substring_prefixes_and_is_sorted():
    assert suggest_source_ids(KNOWN, "src") == ["alpha-src", "beta-src", "handbook-src"]
    assert suggest_source_ids({"a-report", "b-report"}, "a-repo") == ["a-report"]


def test_suggest_is_bounded_and_empty_on_no_match():
    many = {f"team-src-{i}" for i in range(50)}
    assert len(suggest_source_ids(many, "team-src")) == MAX_SUGGESTIONS
    assert suggest_source_ids(KNOWN, "unrelated query") == []
    assert suggest_source_ids(KNOWN, "   ") == []


# ---------------------------------------------------------------------------
# resolve_source: deterministic priority, truthful ambiguity, no guesses.
# ---------------------------------------------------------------------------


def test_resolve_exact_known_source_id_wins_first():
    result = resolve_source(PAGES, KNOWN, "handbook-src")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "handbook-src"
    assert result.matched_by == SOURCE_MATCH_SOURCE_ID
    assert result.page is None


def test_resolve_by_entity_id():
    result = resolve_source(PAGES, KNOWN, "entity:atlas-catalog")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "atlas-heatworks-product-catalog"
    assert result.matched_by == SOURCE_MATCH_ENTITY_ID
    assert result.page is not None
    assert result.page.title == "Atlas Heatworks Product Catalog"
    assert result.page.path == "references/atlas-catalog.md"


def test_resolve_by_canonical_title():
    result = resolve_source(PAGES, KNOWN, "Atlas Heatworks Product Catalog")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "atlas-heatworks-product-catalog"
    assert result.matched_by == SOURCE_MATCH_CANONICAL_TITLE


def test_resolve_by_alias():
    result = resolve_source(PAGES, KNOWN, "Atlas Catalog")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "atlas-heatworks-product-catalog"
    assert result.matched_by == SOURCE_MATCH_ALIAS


def test_resolve_by_page_path():
    result = resolve_source(PAGES, KNOWN, "references/atlas-catalog.md")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "atlas-heatworks-product-catalog"
    assert result.matched_by == SOURCE_MATCH_PATH


def test_resolve_page_with_multiple_sources_is_truthfully_ambiguous():
    result = resolve_source(PAGES, KNOWN, "Dual Source Page")
    assert result.outcome == OUTCOME_AMBIGUOUS
    candidates = {candidate.source_id for candidate in result.candidates}
    assert candidates == {"alpha-src", "beta-src"}
    # The candidates carry the page so an agent can pick deliberately.
    assert all(candidate.page_title == "Dual Source Page" for candidate in result.candidates)


def test_resolve_duplicate_titles_are_ambiguous_never_a_guess():
    pages = [
        _page("Shared", "one.md", ["one-src"]),
        _page("Shared", "two.md", ["two-src"]),
    ]
    result = resolve_source(pages, {"one-src", "two-src"}, "Shared")
    assert result.outcome == OUTCOME_AMBIGUOUS
    assert {candidate.source_id for candidate in result.candidates} == {"one-src", "two-src"}


def test_resolve_duplicate_titles_declaring_one_source_resolve():
    pages = [
        _page("Shared", "one.md", ["same-src"]),
        _page("Shared", "two.md", ["same-src"]),
    ]
    result = resolve_source(pages, {"same-src"}, "Shared")
    assert result.outcome == OUTCOME_RESOLVED
    assert result.source_id == "same-src"
    assert result.matched_by == SOURCE_MATCH_CANONICAL_TITLE


def test_resolve_partially_registered_multi_source_page_stays_ambiguous():
    # One of two declared sources is registered; the page still declares TWO.
    # Dropping the unregistered one silently would be a guess — report both.
    pages = [_page("Mixed", "mixed.md", ["registered-src", "ghost-src"])]
    result = resolve_source(pages, {"registered-src"}, "Mixed")
    assert result.outcome == OUTCOME_AMBIGUOUS
    assert {candidate.source_id for candidate in result.candidates} == {
        "registered-src",
        "ghost-src",
    }


def test_resolve_shared_alias_is_ambiguous():
    pages = [
        _page("One", "one.md", ["one-src"], aliases=["Nickname"]),
        _page("Two", "two.md", ["two-src"], aliases=["Nickname"]),
    ]
    result = resolve_source(pages, {"one-src", "two-src"}, "Nickname")
    assert result.outcome == OUTCOME_AMBIGUOUS
    assert {candidate.source_id for candidate in result.candidates} == {"one-src", "two-src"}


def test_resolve_page_without_sources_is_unknown_with_truthful_note():
    result = resolve_source(PAGES, KNOWN, "No Source Page")
    assert result.outcome == OUTCOME_UNKNOWN
    assert "declares no Knowledge Sources" in result.note
    assert result.suggestions == []


def test_resolve_declared_source_id_without_registration_is_unknown():
    pages = [_page("Orphan", "orphan.md", ["never-registered"])]
    result = resolve_source(pages, set(), "Orphan")
    assert result.outcome == OUTCOME_UNKNOWN
    assert "never-registered" in result.note
    assert "no registered Knowledge Source" in result.note


def test_resolve_unknown_input_carries_bounded_suggestions():
    result = resolve_source(PAGES, KNOWN, "Atlas Heatworks Product Catalogzz")
    assert result.outcome == OUTCOME_UNKNOWN
    # No exact/substring page match; the normalized suggestion surface still
    # points at the close registered id.
    assert "atlas-heatworks-product-catalog" in result.suggestions


def test_resolve_blank_query_is_unknown_without_echo():
    result = resolve_source(PAGES, KNOWN, "   ")
    assert result.outcome == OUTCOME_UNKNOWN
    assert result.suggestions == []
