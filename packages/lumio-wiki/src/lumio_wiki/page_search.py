"""LanceDB-free page-oriented lexical search helpers.

These ranking and query-normalization helpers are pure functions over
``CompiledPage`` records. They are extracted from the LanceDB index module so
that zero-index retrieval (and any client that only needs in-memory page
search) can land without the optional ``lancedb`` / ``pyarrow`` dependencies.
"""

from __future__ import annotations

import re
import unicodedata

import msgspec

from lumio_wiki.records import CompiledPage, PageSearchResult

MAX_SEARCH_QUERY_LENGTH = 256

_SEARCH_FIELDS: tuple[tuple[str, int], ...] = (
    ("title", 100),
    ("alias", 80),
    ("tag", 60),
    ("summary", 40),
    ("body", 20),
)
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def normalize_search_query(query: str) -> str:
    """Normalize a page-search query and enforce its server-side bound."""
    if not isinstance(query, str):
        return ""
    normalized = unicodedata.normalize("NFKC", query).replace("\x00", " ")
    normalized = " ".join(normalized.split())
    if len(normalized) > MAX_SEARCH_QUERY_LENGTH:
        raise ValueError("search query is too long")
    return normalized


def _search_tokens(query: str) -> list[str]:
    return _TOKEN_RE.findall(query.casefold())


def _search_field_values(page: CompiledPage) -> dict[str, list[str]]:
    return {
        "title": [page.title],
        "alias": list(page.aliases),
        "tag": list(page.tags),
        "summary": [page.summary or ""],
        "body": [page.body],
    }


def _search_excerpt(text: str, tokens: list[str], max_length: int = 260) -> str:
    """Return a short plain-text excerpt around the first matching token."""
    compact = " ".join(text.split())
    if len(compact) <= max_length:
        return compact
    folded = compact.casefold()
    position = min(
        (
            folded.find(token.casefold())
            for token in tokens
            if folded.find(token.casefold()) >= 0
        ),
        default=0,
    )
    radius = max_length // 2
    start = max(0, position - radius)
    end = min(len(compact), start + max_length)
    if end - start < max_length:
        start = max(0, end - max_length)
    excerpt = compact[start:end]
    if start:
        excerpt = "… " + excerpt
    if end < len(compact):
        excerpt += " …"
    return excerpt


def search_pages(
    pages: list[CompiledPage],
    query: str,
    limit: int = 20,
) -> list[PageSearchResult]:
    """Return deterministic page-oriented lexical search results.

    Ranking and snippets are computed from the supplied Compiled Pages with
    stable, field-aware metadata. This is the LanceDB-free core of page
    search; the index module adds an optional derived-index candidate filter
    on top of it.
    """
    normalized = normalize_search_query(query)
    tokens = _search_tokens(normalized)
    if not tokens or limit <= 0:
        return []
    candidates = pages

    ranked: list[tuple[float, str, str, PageSearchResult]] = []
    for page in candidates:
        fields = _search_field_values(page)
        matched_fields: list[str] = []
        matched_terms: list[str] = []
        score = 0.0
        snippet = ""

        for field_name, weight in _SEARCH_FIELDS:
            values = fields[field_name]
            field_score = 0.0
            field_matched = False
            for value in values:
                folded = value.casefold()
                value_tokens = set(_search_tokens(value))
                matching_tokens = [token for token in tokens if token in value_tokens]
                # A substring fallback keeps aliases and tags useful for a
                # query such as ``stack`` even when punctuation joined tokens.
                if not matching_tokens:
                    matching_tokens = [
                        token for token in tokens if token and token in folded
                    ]
                if not matching_tokens:
                    continue
                field_matched = True
                matched_terms.extend(
                    token for token in matching_tokens if token not in matched_terms
                )
                occurrences = sum(folded.count(token) for token in matching_tokens)
                field_score += weight * min(occurrences, 3)
                if normalized.casefold() in folded and len(tokens) > 1:
                    field_score += weight * 0.5
                if not snippet:
                    snippet = _search_excerpt(value, matching_tokens)
            if field_matched:
                matched_fields.append(field_name)
                score += field_score

        if not matched_fields:
            continue
        # Keep the score informative but stable for rendered metadata.
        score = round(score, 4)
        ranked.append(
            (
                score,
                page.title.casefold(),
                page.path.casefold(),
                PageSearchResult(
                    page=page,
                    score=score,
                    matched_fields=matched_fields,
                    matched_terms=matched_terms,
                    snippet=snippet,
                ),
            )
        )

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    kept = ranked[:limit]
    if not kept:
        return []
    # Deterministic accounting: candidates inspected and results cut by the
    # ``limit``. Stamped only on the returned entries so the payload the
    # caller sees describes exactly this call.
    dropped = max(len(ranked) - limit, 0)
    return [
        msgspec.structs.replace(item[3], candidates_seen=len(ranked), results_dropped=dropped)
        for item in kept
    ]
