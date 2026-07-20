"""Shared retrieval adapter seam and zero-index implementation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    Embedder,
    EmbeddingError,
    RetrievalMode,
)
from lumio_wiki.evidence import page_evidences, retrieval_result_from_evidence
from lumio_wiki.fingerprint_store import save_stored_fingerprint
from lumio_wiki.page_search import normalize_search_query
from lumio_wiki.records import (
    CompiledPage,
    RetrievalResult,
    RetrievalTrace,
    SourceFingerprint,
    TraceStage,
)

_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


class RetrievalAdapter(Protocol):
    """Progressive-enhancement seam for derived retrieval backends."""

    @property
    def name(self) -> str: ...

    def build_index(
        self,
        pages: Sequence[CompiledPage],
        index_dir: Path,
        *,
        embedder: Embedder | None = None,
        fingerprint: SourceFingerprint | None = None,
    ) -> None: ...

    def retrieve(
        self,
        pages: Sequence[CompiledPage],
        query: str,
        *,
        limit: int = 5,
        index_dir: Path | None = None,
        mode: RetrievalMode = "lexical",
        embedder: Embedder | None = None,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        eligible_pages: Sequence[CompiledPage] | None = None,
    ) -> list[RetrievalResult]: ...


def default_retrieval_adapter() -> RetrievalAdapter:
    return ZeroIndexRetrieval()


class ZeroIndexRetrieval:
    """Always-available lexical retrieval over loaded Compiled Pages."""

    @property
    def name(self) -> str:
        return "zero-index"

    def build_index(
        self,
        pages: Sequence[CompiledPage],
        index_dir: Path,
        *,
        embedder: Embedder | None = None,
        fingerprint: SourceFingerprint | None = None,
    ) -> None:
        del pages, embedder
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        if fingerprint is not None:
            save_stored_fingerprint(index_dir, fingerprint)

    def retrieve(
        self,
        pages: Sequence[CompiledPage],
        query: str,
        *,
        limit: int = 5,
        index_dir: Path | None = None,
        mode: RetrievalMode = "lexical",
        embedder: Embedder | None = None,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        eligible_pages: Sequence[CompiledPage] | None = None,
    ) -> list[RetrievalResult]:
        del index_dir, embedder, score_threshold
        if mode != "lexical":
            raise EmbeddingError(
                f"{mode} retrieval requires the LanceDB retrieval adapter; "
                "zero-index only supports mode='lexical'"
            )
        # When the caller supplies graph-selected eligible pages (issue #112),
        # Evidence is ranked ONLY from them; otherwise every loaded page is
        # searchable. The graph owns page selection; the adapter owns ranking.
        search_pages = eligible_pages if eligible_pages is not None else pages
        normalized = normalize_search_query(query)
        tokens = _TOKEN_RE.findall(normalized.casefold())
        if not tokens or limit <= 0:
            return []

        trace = RetrievalTrace(
            stages=[
                TraceStage("search", "zero-index lexical match over Compiled Pages"),
                TraceStage("rank", "deterministic field-aware Evidence ranking"),
            ]
        )
        scored: list[tuple[float, str, RetrievalResult]] = []
        for page in search_pages:
            for evidence, source in page_evidences(page):
                haystack = f"{evidence.page_title}\n{evidence.text}"
                folded = haystack.casefold()
                value_tokens = set(_TOKEN_RE.findall(folded))
                matching = [t for t in tokens if t in value_tokens]
                if not matching:
                    matching = [t for t in tokens if t and t in folded]
                if not matching:
                    continue
                score = float(len(matching))
                title_folded = evidence.page_title.casefold()
                if any(t in title_folded for t in matching):
                    score += 2.0
                score = round(score, 4)
                scored.append(
                    (
                        score,
                        evidence.id,
                        retrieval_result_from_evidence(
                            evidence,
                            source=source,
                            score=score,
                            reason="zero-index lexical match",
                            trace=trace,
                        ),
                    )
                )
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]
