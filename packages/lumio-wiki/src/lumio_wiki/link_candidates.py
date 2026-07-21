"""Deterministic link-candidate finder (issue #90, ADR-0011).

A model-free Core SDK operation that scans Compiled Page bodies for UNLINKED
mentions of known pages by Canonical Page Title or Alias and emits
``LinkCandidate`` proposals for the missing authored links.

It complements the Extracted Reference resolver (issue #107): a mention that
already sits inside a Markdown link ``[...](...)`` or wikilink ``[[...]]`` is
derived as an Extracted Reference by that shared resolver and is never
re-proposed here. The link- and wikilink-construct patterns are imported from
``knowledge_base`` (``_MARKDOWN_LINK_RE`` / ``_WIKILINK_RE``) so the finder's
notion of "inside a link" is the same single source of truth the resolver uses
rather than a second, drift-prone link parser.

A candidate is strictly non-canonical. It does not enter the Discovery Graph
and never becomes an Extracted Reference until a Maintainer approves it and
publishes the resulting Markdown proposal (#91). Once published, the link is
derived as an Extracted Reference on the next graph derivation without a
second approval step (ADR-0011).

The finder is pure and deterministic: identical input yields identical output
regardless of insertion order, and no LLM or randomness is involved.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence

from lumio_wiki.knowledge_base import _MARKDOWN_LINK_RE, _WIKILINK_RE
from lumio_wiki.records import CompiledPage, LinkCandidate

# Maximum retained snippet length (the source line trimmed around the match).
_SNIPPET_MAX = 160


def _inline_code_spans(body: str) -> list[tuple[int, int]]:
    """Return ``[start, end)`` character spans covered by inline code.

    Mirrors the code-span handling of the Extracted Reference scanner so the
    finder treats the same backtick runs as non-prose; a mention inside inline
    code is not a navigational reference.
    """
    spans: list[tuple[int, int]] = []
    in_code = False
    fence_len = 0
    code_start = 0
    i = 0
    n = len(body)
    while i < n:
        if body[i] == "`":
            j = i
            run = 0
            while j < n and body[j] == "`":
                run += 1
                j += 1
            if not in_code:
                in_code = True
                fence_len = run
                code_start = i
            elif run == fence_len:
                in_code = False
                spans.append((code_start, j))
                fence_len = 0
            i = j
        else:
            i += 1
    if in_code:
        spans.append((code_start, n))
    return spans


def _masked_spans(body: str) -> list[tuple[int, int]]:
    """Return sorted ``[start, end)`` spans already covered by a link or code.

    A mention landing in any of these spans is anchor text, a link destination,
    or inline code, and is therefore not an unlinked mention. Markdown-link and
    wikilink spans reuse the shared Extracted Reference parser definitions so
    the finder never drifts from the resolver's notion of a link.
    """
    code = _inline_code_spans(body)

    def in_code(pos: int) -> bool:
        return any(start <= pos < end for start, end in code)

    spans: list[tuple[int, int]] = list(code)
    for match in _MARKDOWN_LINK_RE.finditer(body):
        if not in_code(match.start()):
            spans.append((match.start(), match.end()))
    for match in _WIKILINK_RE.finditer(body):
        if not in_code(match.start()):
            spans.append((match.start(), match.end()))
    spans.sort()
    return spans


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """Return whether ``[start, end)`` overlaps any masked span.

    ``spans`` is sorted by start offset, so once a span starts at or after
    ``end`` no later span can overlap and the scan stops early.
    """
    for ms, me in spans:
        if start < me and ms < end:
            return True
        if ms >= end:
            break
    return False


def _line_starts(body: str) -> list[int]:
    """Return the character offset of the start of each 1-based body line."""
    starts = [0]
    for idx, ch in enumerate(body):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def _snippet(line_text: str, column: int) -> str:
    """Return ``line_text`` trimmed around ``column`` to at most ``_SNIPPET_MAX``."""
    text = line_text.strip()
    if len(text) <= _SNIPPET_MAX:
        return text
    center = max(0, min(column - 1, len(line_text) - 1))
    half = _SNIPPET_MAX // 2
    start = max(0, center - half)
    end = min(len(line_text), start + _SNIPPET_MAX)
    start = max(0, end - _SNIPPET_MAX)
    window = line_text[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(line_text) else ""
    return f"{prefix}{window}{suffix}"


def find_link_candidates(pages: Sequence[CompiledPage]) -> list[LinkCandidate]:
    """Return deterministic, non-canonical link proposals for unlinked mentions.

    Scans each Compiled Page body for mentions of a known Canonical Page Title
    or Alias that are NOT already inside a Markdown link, wikilink, or
    inline-code span, and emits one ``LinkCandidate`` per mention location.

    Resolution rules:

    * Titles and aliases match case-insensitively as whole, word-bounded
      phrases so ``Beta`` does not match inside ``Betamax``.
    * A term resolving to more than one distinct page (an alias shared by two
      pages, an alias colliding with another title, or a duplicate title) is
      **ambiguous and skipped silently** to avoid proposing a link whose target
      is unclear.
    * A page mentioning its own title or alias (a self-mention) is skipped.
    * Mentions inside any Markdown-link or wikilink construct are skipped: for
      internal links the shared Extracted Reference resolver already derives
      the reference, and anchor/destination text is never re-proposed.

    Pages without a title are skipped (they have no source identity and cannot
    contribute a resolvable target). Candidates are sorted by ``(source_path,
    line, column, target_title, term)``. The operation is pure and model-free;
    it never mutates a page and never publishes a link. Publishing the approved
    Markdown proposal (#91) makes the link available as an Extracted Reference
    on the next graph derivation without a second approval step.
    """
    ordered = sorted(pages, key=lambda p: (p.path, p.title))

    # 1. Build a case-folded term -> [(page, registered_term)] index.
    raw: dict[str, list[tuple[CompiledPage, str]]] = {}
    for page in ordered:
        if not page.title:
            continue
        registered: list[str] = [page.title]
        for alias in page.aliases:
            if alias:
                registered.append(alias)
        for term in registered:
            raw.setdefault(term.casefold(), []).append((page, term))

    # 2. Keep only unambiguous terms (exactly one distinct target page). Prefer
    #    the target's canonical title as the recorded term when it matches.
    resolvable: dict[str, tuple[CompiledPage, str]] = {}
    for key, pairs in raw.items():
        distinct: list[CompiledPage] = []
        for page, _term in pairs:
            if not any(p is page for p in distinct):
                distinct.append(page)
        if len(distinct) != 1:
            continue
        target = distinct[0]
        term = (
            target.title
            if target.title and target.title.casefold() == key
            else pairs[0][1]
        )
        resolvable[key] = (target, term)

    # 3. One case-insensitive, word-bounded pattern per term, in stable order.
    patterns: list[tuple[re.Pattern[str], CompiledPage, str]] = sorted(
        (
            (re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE), target, term)
            for _key, (target, term) in resolvable.items()
        ),
        key=lambda item: item[2].casefold(),
    )

    candidates: list[LinkCandidate] = []
    for page in ordered:
        body = page.body
        if not body or not page.title:
            continue
        starts = _line_starts(body)
        masked = _masked_spans(body)
        body_start = page.body_start_line

        for pattern, target, term in patterns:
            if target is page:
                continue  # self-mention
            for match in pattern.finditer(body):
                start = match.start()
                end = match.end()
                if _overlaps(masked, start, end):
                    continue
                body_line_1 = bisect.bisect_right(starts, start)  # 1-based body line
                column = start - starts[body_line_1 - 1] + 1
                file_line = body_start + body_line_1 - 1
                line_off = starts[body_line_1 - 1]
                nl = body.find("\n", line_off)
                line_text = body[line_off : nl if nl != -1 else len(body)]
                candidates.append(
                    LinkCandidate(
                        source_path=page.path,
                        source_title=page.title,
                        target_path=target.path,
                        target_title=target.title,
                        term=term,
                        line=file_line,
                        column=column,
                        snippet=_snippet(line_text, column),
                    )
                )

    candidates.sort(
        key=lambda c: (c.source_path, c.line, c.column, c.target_title, c.term)
    )
    return candidates
