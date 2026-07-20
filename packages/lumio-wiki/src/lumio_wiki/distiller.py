"""Distiller: convert normalized material into proposed Compiled Page Markdown
(issue #95, AC2; canonical owner moved into ``lumio-wiki`` by issue #97).

A Distiller is the second stage of ingestion. It turns a :class:`NormalizedSource`
into the Markdown a proposal is built from. The base interface carries no model
provider: the host coding agent may act as the Distiller
(:class:`PassthroughMarkdownDistiller`). An OpenAI-compatible Distiller is an
adapter that lives in the full application (``lumio.distiller.ProviderDistiller``)
so the base wheel never depends on a model provider.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from lumio_wiki.source_processor import NormalizedSource


@runtime_checkable
class Distiller(Protocol):
    """Convert normalized material into proposed Compiled Page Markdown."""

    def distill(
        self, normalized: NormalizedSource, *, categories: list[str] | None = None
    ) -> str: ...


class PassthroughMarkdownDistiller:
    """Model-free Distiller: the normalized text is already authored Markdown.

    Enables text and Markdown Knowledge Sources to reach a reviewable Ingest
    Proposal without an OpenAI-compatible provider (issue #95, AC4). The host
    coding agent authors the Compiled Page Markdown; this Distiller passes the
    normalized text through unchanged so downstream page extraction, summary
    generation, and category routing apply as they do for any distilled source.
    """

    def distill(
        self, normalized: NormalizedSource, *, categories: list[str] | None = None
    ) -> str:
        return normalized.text


__all__ = ["Distiller", "PassthroughMarkdownDistiller"]
