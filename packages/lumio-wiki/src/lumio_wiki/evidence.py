"""LanceDB-free Evidence extraction and retrieval-result assembly.

These helpers derive ``Evidence`` records from a ``CompiledPage`` (its body and
ATX sections) and assemble citation-ready ``RetrievalResult`` objects from an
``Evidence`` plus a source id. They are pure functions over records, extracted
from the LanceDB index module so zero-index retrieval can build evidence and
results without the optional ``lancedb`` / ``pyarrow`` dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from lumio_wiki.page_search import _search_tokens
from lumio_wiki.records import (
    Citation,
    CompiledPage,
    Evidence,
    RetrievalResult,
    RetrievalTrace,
)

_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*(?:\r?\n)?$")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?:[^\r\n]*)?(?:\r?\n)?$")
_MAX_SNIPPET_CHARS = 300


def markdown_section_ranges(
    lines: Sequence[str], *, line_offset: int = 0
) -> list[tuple[str, int, int]]:
    """Return fence-aware ``(title, inclusive_start, inclusive_end)`` ranges.

    This is the one Markdown section parser shared by page reads, Claim
    Evidence validation, and zero-index Evidence extraction. ATX headings in
    fenced code are never sections. An opening fence may have info text; its
    matching closing fence must contain only the same fence character (at
    least the opening length) and trailing whitespace, so ````` explanation``
    remains code instead of closing the block.
    """
    headings: list[tuple[int, int, str]] = []
    open_fence: str | None = None
    for index, line in enumerate(lines):
        if open_fence is not None:
            fence_char = re.escape(open_fence[0])
            close_re = re.compile(
                rf"^[ \t]{{0,3}}{fence_char}{{{len(open_fence)},}}[ \t]*(?:\r?\n)?$"
            )
            if close_re.fullmatch(line):
                open_fence = None
            continue
        opening = _FENCE_OPEN_RE.fullmatch(line)
        if opening is not None:
            open_fence = opening.group("fence")
            continue
        heading = _ATX_HEADING_RE.fullmatch(line)
        if heading is not None:
            headings.append((index, len(heading.group(1)), heading.group(2).strip()))

    ranges: list[tuple[str, int, int]] = []
    for position, (start_index, level, title) in enumerate(headings):
        end_index = len(lines) - 1
        for next_index, next_level, _next_title in headings[position + 1 :]:
            if next_level <= level:
                end_index = next_index - 1
                break
        ranges.append((title, line_offset + start_index + 1, line_offset + end_index + 1))
    return ranges


def body_sections(body: str, body_start_line: int) -> list[tuple[str | None, int, int, str]]:
    """Return fence-aware ``(title, file_line_start, file_line_end, text)`` sections."""
    lines = body.splitlines()
    sections: list[tuple[str | None, int, int, str]] = []
    for title, start, end in markdown_section_ranges(lines, line_offset=body_start_line - 1):
        section_lines = lines[start - body_start_line : end - body_start_line + 1]
        sections.append((title, start, end, "\n".join(section_lines).strip()))
    return sections


def page_evidence(page: CompiledPage) -> Evidence | None:
    text = page.body.strip()
    if not text:
        return None
    body_lines = page.body.splitlines()
    line_end = page.body_start_line + len(body_lines) - 1
    return Evidence(
        id=page.path,
        source_type="compiled_markdown",
        page_path=page.path,
        page_title=page.title,
        section_title=None,
        line_start=page.body_start_line,
        line_end=line_end if line_end >= page.body_start_line else page.body_start_line,
        text=text,
    )


def section_evidences(page: CompiledPage) -> list[Evidence]:
    evidences: list[Evidence] = []
    for title, line_start, line_end, text in body_sections(page.body, page.body_start_line):
        if not text:
            continue
        evidences.append(
            Evidence(
                id=f"{page.path}#L{line_start}-{line_end}",
                source_type="compiled_markdown",
                page_path=page.path,
                page_title=page.title,
                section_title=title,
                line_start=line_start,
                line_end=line_end,
                text=text,
            )
        )
    return evidences


def page_evidences(page: CompiledPage) -> list[tuple[Evidence, str | None]]:
    """Return focused page/section Evidence without a duplicate whole-page hit."""
    source = page.sources[0].id if page.sources else None
    sections = section_evidences(page)
    if not sections:
        page_ev = page_evidence(page)
        if page_ev is None:
            return []
        return [(page_ev, source)]

    first_line = sections[0].line_start
    if first_line is None:  # pragma: no cover - section evidence always has coordinates
        raise AssertionError("section evidence is missing line coordinates")
    first_heading = first_line - page.body_start_line
    preamble = "\n".join(page.body.splitlines()[:first_heading]).strip()
    pairs: list[tuple[Evidence, str | None]] = []
    if preamble:
        start = page.body_start_line
        pairs.append(
            (
                Evidence(
                    id=f"{page.path}#L{start}-{start + first_heading - 1}",
                    source_type="compiled_markdown",
                    page_path=page.path,
                    page_title=page.title,
                    section_title=None,
                    line_start=start,
                    line_end=start + first_heading - 1,
                    text=preamble,
                ),
                source,
            )
        )
    pairs.extend((evidence, source) for evidence in sections)
    return pairs


def _snippet(text: str, query: str | None) -> str:
    """Return a bounded snippet centered on a matching query term when possible."""
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    if query:
        folded = text.casefold()
        positions = [
            folded.find(token.casefold())
            for token in _search_tokens(query)
            if folded.find(token.casefold()) >= 0
        ]
        if positions:
            start = max(0, min(positions) - 90)
            end = min(len(text), start + _MAX_SNIPPET_CHARS)
            if end - start < _MAX_SNIPPET_CHARS:
                start = max(0, end - _MAX_SNIPPET_CHARS)
            return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")
    return text[:_MAX_SNIPPET_CHARS] + "..."


def deduplicate_results(
    results: list[RetrievalResult],
    *,
    limit: int,
    candidates_seen: int | None = None,
    query: str | None = None,
) -> list[RetrievalResult]:
    """Prefer the narrowest matching Evidence span before applying ``limit``."""
    selected: list[RetrievalResult] = []
    selected_by_page: dict[str, list[int]] = {}
    for result in results:
        replaced = False
        for index in selected_by_page.get(result.evidence.page_path, []):
            prior = selected[index]
            if prior.evidence.page_path != result.evidence.page_path:
                continue
            prior_start = prior.evidence.line_start or 0
            prior_end = prior.evidence.line_end or prior_start
            current_start = result.evidence.line_start or 0
            current_end = result.evidence.line_end or current_start
            if prior_start <= current_start and current_end <= prior_end:
                selected[index] = result
                replaced = True
                break
            if current_start <= prior_start and prior_end <= current_end:
                replaced = True
                break
        if not replaced:
            selected_by_page.setdefault(result.evidence.page_path, []).append(len(selected))
            selected.append(result)
    selected = selected[: max(limit, 0)]
    if not selected:
        return []
    first = selected[0].trace
    trace = RetrievalTrace(
        stages=list(first.stages),
        candidates_seen=candidates_seen if candidates_seen is not None else len(results),
        results_returned=len(selected),
        results_dropped=max(
            (candidates_seen if candidates_seen is not None else len(results)) - len(selected),
            0,
        ),
    )
    return [
        RetrievalResult(
            evidence=result.evidence,
            citation=result.citation,
            snippet=_snippet(result.evidence.text, query) if query is not None else result.snippet,
            score=result.score,
            reason=result.reason,
            trace=trace,
        )
        for result in selected
    ]


def retrieval_result_from_evidence(
    evidence: Evidence,
    *,
    source: str | None,
    score: float,
    reason: str,
    trace: RetrievalTrace,
    query: str | None = None,
) -> RetrievalResult:
    """Build a citation-ready RetrievalResult from an Evidence plus its source.

    The snippet is the evidence text, trimmed to a citation-friendly length.
    The citation carries the evidence's section title when present so a result
    can point back to a stable section even without a line range.
    """
    snippet = _snippet(evidence.text, query)
    return RetrievalResult(
        evidence=evidence,
        citation=Citation(
            page_title=evidence.page_title,
            relative_path=evidence.page_path,
            source=source,
            line_start=evidence.line_start,
            line_end=evidence.line_end,
            section_title=evidence.section_title,
        ),
        snippet=snippet,
        score=score,
        reason=reason,
        trace=trace,
    )
