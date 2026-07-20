"""Source Processor: convert Knowledge Source bytes into normalized text and
stable sections (issue #95, AC1; canonical owner moved into ``lumio-wiki`` by
issue #97).

A Source Processor is the first stage of ingestion. It turns raw Knowledge
Source bytes into a :class:`NormalizedSource` — normalized text plus stable,
line-addressable sections — without depending on a model provider or a web
request. Text and Markdown are handled by UTF-8 decoding and require no
heavyweight converter (AC4); document formats are handled by
:class:`DocumentSourceProcessor`, whose converter is injected by the caller so
this module never imports LiteParse or MarkItDown.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import PurePath
from typing import Protocol, runtime_checkable

import msgspec

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


class NormalizedSection(msgspec.Struct, frozen=True):
    """A stable, line-addressable section derived from normalized source text."""

    id: str
    title: str
    line_start: int
    line_end: int
    text: str


class NormalizedSource(msgspec.Struct, frozen=True):
    """Normalized material produced by a Source Processor.

    ``text`` is the full normalized source a Distiller consumes (the exact
    string the historical converter produced). ``sections`` are stable,
    heading-based, line-addressable slices available to Distillers that want
    finer-grained provenance. ``converted_by`` records which processor produced
    the material so it can flow into ``SourceProvenance`` unchanged.
    """

    text: str
    sections: list[NormalizedSection]
    converted_by: str
    source_hash: str
    filename: str | None
    content_type: str | None


class SourceProcessorError(ValueError):
    """A Knowledge Source could not be processed into normalized material."""


@runtime_checkable
class SourceProcessor(Protocol):
    """Convert Knowledge Source bytes into normalized text and stable sections."""

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource: ...


def _sections(filename: str | None, text: str, digest: str) -> list[NormalizedSection]:
    """Split normalized text into stable, heading-based, line-addressable sections."""
    lines = text.splitlines()
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(2), len(match.group(1))))
    label = PurePath(filename or "source").name
    if not headings:
        end = max(len(lines), 1)
        return [NormalizedSection(f"{digest[:16]}:section:1", label, 1, end, text.strip())]
    result: list[NormalizedSection] = []
    for position, (line_start, title, level) in enumerate(headings):
        line_end = len(lines)
        for next_start, _next_title, next_level in headings[position + 1 :]:
            if next_level <= level:
                line_end = next_start - 1
                break
        section_text = "\n".join(lines[line_start - 1 : line_end]).strip()
        if section_text:
            result.append(
                NormalizedSection(
                    f"{digest[:16]}:section:{position + 1}",
                    title,
                    line_start,
                    line_end,
                    section_text,
                )
            )
    return result or [
        NormalizedSection(
            f"{digest[:16]}:section:1", label, 1, max(len(lines), 1), text.strip()
        )
    ]


def _is_markdown(filename: str | None, content_type: str | None) -> bool:
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in {".md", ".markdown"}:
        return True
    return (content_type or "").split(";")[0].strip().lower() == "text/markdown"


class TextMarkdownSourceProcessor:
    """Dependency-free processor for UTF-8 text and Markdown Knowledge Sources.

    Never imports LiteParse, MarkItDown, or any model provider, so a text or
    Markdown Knowledge Source can reach a reviewable Ingest Proposal without
    those installed (issue #95, AC4). ``converted_by`` is ``"markdown"`` for
    Markdown sources and ``"text"`` otherwise.
    """

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        if not content:
            raise SourceProcessorError("Knowledge Source is empty")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceProcessorError(
                "text/markdown Knowledge Source must be valid UTF-8"
            ) from exc
        if not text.strip():
            raise SourceProcessorError("Knowledge Source is empty")
        digest = hashlib.sha256(content).hexdigest()
        return NormalizedSource(
            text=text,
            sections=_sections(filename, text, digest),
            converted_by="markdown" if _is_markdown(filename, content_type) else "text",
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


class DocumentSourceProcessor:
    """Adapter over an existing byte-to-text converter (LiteParse / MarkItDown).

    The converter callable is injected so this module stays free of heavyweight
    imports; the caller (the full application's source-processor routing)
    supplies the lazily-imported converter. ``converted_by`` records the
    converter name so it flows into ``SourceProvenance`` unchanged.
    """

    def __init__(
        self, converted_by: str, convert: Callable[[bytes, str | None], str]
    ) -> None:
        self._converted_by = converted_by
        self._convert = convert  # type: ignore[assignment]

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        text = self._convert(content, filename)
        digest = hashlib.sha256(content).hexdigest()
        return NormalizedSource(
            text=text,
            sections=_sections(filename, text, digest),
            converted_by=self._converted_by,
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


__all__ = [
    "DocumentSourceProcessor",
    "NormalizedSection",
    "NormalizedSource",
    "SourceProcessor",
    "SourceProcessorError",
    "TextMarkdownSourceProcessor",
]
