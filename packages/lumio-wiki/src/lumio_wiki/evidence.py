"""LanceDB-free Evidence extraction and retrieval-result assembly.

These helpers derive ``Evidence`` records from a ``CompiledPage`` (its body and
ATX sections) and assemble citation-ready ``RetrievalResult`` objects from an
``Evidence`` plus a source id. They are pure functions over records, extracted
from the LanceDB index module so zero-index retrieval can build evidence and
results without the optional ``lancedb`` / ``pyarrow`` dependencies.
"""

from __future__ import annotations

import re

from lumio_wiki.records import (
    Citation,
    CompiledPage,
    Evidence,
    RetrievalResult,
    RetrievalTrace,
)


def body_sections(body: str, body_start_line: int) -> list[tuple[str | None, int, int, str]]:
    """Return (title, file_line_start, file_line_end, text) for each ATX section."""
    lines = body.splitlines()
    sections: list[tuple[str | None, int, int, str]] = []
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if not match:
            i += 1
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        j = i + 1
        while j < n:
            next_match = re.match(r"^(#{1,3})\s+", lines[j])
            if next_match and len(next_match.group(1)) <= level:
                break
            j += 1
        section_lines = lines[i:j]
        text = "\n".join(section_lines).strip()
        file_line_start = body_start_line + i
        file_line_end = body_start_line + j - 1
        sections.append((title, file_line_start, file_line_end, text))
        i = j
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
