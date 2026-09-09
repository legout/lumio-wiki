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

from lumio_wiki.records import (
    Citation,
    CompiledPage,
    Evidence,
    RetrievalResult,
    RetrievalTrace,
)

_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*(?:\r?\n)?$")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?:[^\r\n]*)?(?:\r?\n)?$")


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
    """Return (Evidence, source_id) pairs for a page body and its sections."""
    source = page.sources[0].id if page.sources else None
    pairs: list[tuple[Evidence, str | None]] = []
    page_ev = page_evidence(page)
    if page_ev is not None:
        pairs.append((page_ev, source))
    pairs.extend((ev, source) for ev in section_evidences(page))
    return pairs


def retrieval_result_from_evidence(
    evidence: Evidence,
    *,
    source: str | None,
    score: float,
    reason: str,
    trace: RetrievalTrace,
) -> RetrievalResult:
    """Build a citation-ready RetrievalResult from an Evidence plus its source.

    The snippet is the evidence text, trimmed to a citation-friendly length.
    The citation carries the evidence's section title when present so a result
    can point back to a stable section even without a line range.
    """
    snippet = evidence.text
    if len(snippet) > 300:
        snippet = snippet[:300] + "..."
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
