"""LanceDB Evidence index over a Knowledge Base (``lumio-lancedb`` adapter).

This is the optional enhanced-retrieval adapter: BM25 full-text search over a
derived Evidence index, plus citation-ready cosine vector search (semantic /
hybrid) when an embedding provider is configured. It implements the
:class:`lumio_wiki.retrieval.RetrievalAdapter` contract without changing the
Knowledge Base format, Evidence, citations, or client behavior. ``lumio-wiki``
never imports this module; callers inject it explicitly (e.g.
``KnowledgeBase.build_index(..., retrieval=LanceDBRetrievalAdapter())``).
"""

from __future__ import annotations

from pathlib import Path

# ``lancedb`` / ``pyarrow`` are required to actually build or search a derived
# index, but importing this module is kept safe when they are absent so the
# broader ``lumio_lancedb`` package and its smoke tests can be imported in
# environments where the dependency failed to initialize. The build/search call
# sites below resolve to ``None`` only when the optional dependency is
# unavailable; a real build/search then raises an actionable error.
try:  # pragma: no cover - exercised indirectly via build/search tests
    import lancedb
    import pyarrow as pa
    from lancedb.index import FTS
except ImportError:  # pragma: no cover - optional dependency unavailable
    lancedb = None
    pa = None
    FTS = None

from lumio_wiki import evidence, page_search
from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    RRF_K,
    Embedder,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingNotBuiltError,
    load_embedding_model,
    normalize_vector,
    reciprocal_rank_fusion,
    save_embedding_model,
)
from lumio_wiki.fingerprint_store import (  # noqa: F401  (re-exported for KnowledgeBase)
    load_stored_fingerprint,
    save_stored_fingerprint,
)
from lumio_wiki.records import (
    CompiledPage,
    Evidence,
    PageSearchResult,
    RetrievalResult,
    RetrievalTrace,
    TraceStage,
)

TABLE_NAME = "evidence"
PAGE_TABLE_NAME = "pages"
VECTOR_TABLE_NAME = "evidence_vectors"


def _indexed_page_paths(index_dir: Path, query: str) -> set[str] | None:
    """Return paths matched by the published page-oriented lexical index.

    ``None`` means the index predates page search or is unavailable; callers
    can safely use the loaded page set as a compatibility fallback.
    """
    index_dir = Path(index_dir)
    if not index_dir.exists():
        return None
    db = lancedb.connect(index_dir)
    if PAGE_TABLE_NAME not in db.list_tables().tables:
        return None
    table = db.open_table(PAGE_TABLE_NAME)
    tokens = page_search._search_tokens(query)
    if not tokens or table.count_rows() == 0:
        return set()
    rows = (
        table.search(" ".join(tokens), query_type="fts")
        .select(["page_path"])
        .limit(table.count_rows())
        .to_list()
    )
    return {row["page_path"] for row in rows}


def search_pages(
    pages: list[CompiledPage],
    query: str,
    limit: int = 20,
    index_dir: Path | None = None,
) -> list[PageSearchResult]:
    """Return deterministic page-oriented lexical search results.

    When ``index_dir`` points at a built index, the page table supplies the
    candidate set; ranking and snippets are then computed from the loaded
    Compiled Pages for stable field-aware metadata. A missing page table falls
    back to the loaded pages so older derived indexes remain readable.
    """
    normalized = page_search.normalize_search_query(query)
    indexed_paths = _indexed_page_paths(index_dir, normalized) if index_dir else None
    candidates = (
        [page for page in pages if page.path in indexed_paths]
        if indexed_paths is not None
        else pages
    )
    return page_search.search_pages(candidates, query, limit=limit)


def _evidence_rows(page: CompiledPage) -> list[dict]:
    """Return LanceDB table rows for a page and its sections."""
    return [_row_from_evidence(ev, source) for ev, source in evidence.page_evidences(page)]


def _row_from_evidence(evidence_obj: Evidence, source: str | None) -> dict:
    return {
        "evidence_id": evidence_obj.id,
        "source_type": evidence_obj.source_type,
        "page_path": evidence_obj.page_path,
        "page_title": evidence_obj.page_title,
        "source": source or "",
        "section_title": evidence_obj.section_title or "",
        "line_start": evidence_obj.line_start,
        "line_end": evidence_obj.line_end,
        "text": evidence_obj.text,
    }


def _schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("evidence_id", pa.string()),
            pa.field("source_type", pa.string()),
            pa.field("page_path", pa.string()),
            pa.field("page_title", pa.string()),
            pa.field("source", pa.string()),
            pa.field("section_title", pa.string()),
            pa.field("line_start", pa.int64()),
            pa.field("line_end", pa.int64()),
            pa.field("text", pa.string()),
        ]
    )


def _vector_schema(dimension: int) -> pa.Schema:
    """Schema for the citation-ready vector index: Evidence fields plus a vector."""
    return pa.schema(
        [
            pa.field("evidence_id", pa.string()),
            pa.field("source_type", pa.string()),
            pa.field("page_path", pa.string()),
            pa.field("page_title", pa.string()),
            pa.field("source", pa.string()),
            pa.field("section_title", pa.string()),
            pa.field("line_start", pa.int64()),
            pa.field("line_end", pa.int64()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimension)),
        ]
    )


def _retrieval_result_from_row(
    row: dict,
    *,
    score: float,
    reason: str,
    trace: RetrievalTrace,
) -> RetrievalResult:
    """Build a citation-ready RetrievalResult from an index row."""
    evidence_obj = Evidence(
        id=row["evidence_id"],
        source_type=row["source_type"],
        page_path=row["page_path"],
        page_title=row["page_title"],
        section_title=row["section_title"] or None,
        line_start=row["line_start"],
        line_end=row["line_end"],
        text=row["text"],
    )
    return evidence.retrieval_result_from_evidence(
        evidence_obj,
        source=row["source"] or None,
        score=score,
        reason=reason,
        trace=trace,
    )


def _page_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("page_path", pa.string()),
            pa.field("page_title", pa.string()),
            pa.field("search_text", pa.string()),
        ]
    )


def _page_search_row(page: CompiledPage) -> dict[str, str]:
    values = page_search._search_field_values(page)
    text = "\n".join(value for field in values.values() for value in field if value)
    return {
        "page_path": page.path,
        "page_title": page.title,
        "search_text": text,
    }


def build_lexical_index(pages: list[CompiledPage], index_dir: Path) -> None:
    """Build fresh Evidence and page-oriented full-text indexes."""
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(index_dir)

    rows: list[dict] = []
    page_rows: list[dict[str, str]] = []
    for page in pages:
        rows.extend(_evidence_rows(page))
        page_rows.append(_page_search_row(page))

    table = db.create_table(TABLE_NAME, data=rows, schema=_schema(), mode="overwrite")
    if rows:
        table.create_index("text", config=FTS())
    page_table = db.create_table(
        PAGE_TABLE_NAME,
        data=page_rows,
        schema=_page_schema(),
        mode="overwrite",
    )
    if page_rows:
        page_table.create_index("search_text", config=FTS())


def search_lexical_index(index_dir: Path, query: str, limit: int) -> list[RetrievalResult]:
    """Search the lexical index and return citation-ready results."""
    index_dir = Path(index_dir)
    if not index_dir.exists():
        return []

    db = lancedb.connect(index_dir)
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    trace = RetrievalTrace(
        stages=[
            TraceStage("search", "LanceDB BM25 full-text search"),
            TraceStage("rank", "ranked by BM25 score"),
        ]
    )

    rows = (
        table.search(query, query_type="fts")
        .select(
            [
                "evidence_id",
                "source_type",
                "page_path",
                "page_title",
                "source",
                "section_title",
                "line_start",
                "line_end",
                "text",
                "_score",
            ]
        )
        .limit(limit)
        .to_list()
    )

    return [
        _retrieval_result_from_row(
            row, score=row["_score"], reason="BM25 lexical match", trace=trace
        )
        for row in rows
    ]


def _validate_embedding_vectors(
    vectors: list[list[float]], expected_count: int, dimension: int
) -> None:
    """Validate an embedder response: count and per-vector dimension."""
    if len(vectors) != expected_count:
        raise EmbeddingError(
            f"embedder returned {len(vectors)} vectors for {expected_count} texts"
        )
    for i, vec in enumerate(vectors):
        if len(vec) != dimension:
            raise EmbeddingDimensionMismatch(
                f"embedding {i} has dimension {len(vec)}; declared {dimension}"
            )


def build_semantic_index(
    pages: list[CompiledPage], index_dir: Path, embedder: Embedder
) -> None:
    """Build a citation-ready vector index from Evidence text + an embedder.

    Stores normalized vectors in a dedicated LanceDB table alongside the
    lexical FTS index and persists the embedding-model identity so later
    builds can detect a model change and rebuild vectors from source (#75).
    Lexical-only deployments never call this.
    """
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    info = embedder.model_info
    pairs = [pair for page in pages for pair in evidence.page_evidences(page)]
    db = lancedb.connect(index_dir)

    if not pairs:
        rows: list[dict] = []
    else:
        texts = [ev.text for ev, _ in pairs]
        vectors = embedder.embed(texts)
        _validate_embedding_vectors(vectors, len(texts), info.dimension)
        rows = [
            {**_row_from_evidence(ev, source), "vector": normalize_vector(vec)}
            for (ev, source), vec in zip(pairs, vectors, strict=True)
        ]

    db.create_table(
        VECTOR_TABLE_NAME,
        data=rows,
        schema=_vector_schema(info.dimension),
        mode="overwrite",
    )
    save_embedding_model(index_dir, info)


def has_semantic_index(index_dir: Path) -> bool:
    """Return True when a vector table and embedding-model metadata are present."""
    index_dir = Path(index_dir)
    if not index_dir.exists():
        return False
    db = lancedb.connect(index_dir)
    if VECTOR_TABLE_NAME not in db.list_tables().tables:
        return False
    return load_embedding_model(index_dir) is not None


_SEMANTIC_SELECT = [
    "evidence_id",
    "source_type",
    "page_path",
    "page_title",
    "source",
    "section_title",
    "line_start",
    "line_end",
    "text",
    "_distance",
]


def search_semantic_index(
    index_dir: Path,
    query_vector: list[float],
    limit: int,
    score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> list[RetrievalResult]:
    """Search the vector index and return citation-ready results above threshold.

    Candidates whose cosine similarity falls below ``score_threshold`` are
    dropped so unsupported questions return no results (-> "not covered by this
    knowledge base") rather than a weak nearest-neighbour hit (#75).
    """
    index_dir = Path(index_dir)
    if not has_semantic_index(index_dir):
        raise EmbeddingNotBuiltError(
            "semantic retrieval requires a built vector index; "
            "call KnowledgeBase.build_index(embedder=...) first"
        )
    info = load_embedding_model(index_dir)
    db = lancedb.connect(index_dir)
    table = db.open_table(VECTOR_TABLE_NAME)
    if table.count_rows() == 0:
        return []

    query = normalize_vector(query_vector)
    fetch = min(table.count_rows(), max(limit * 4, limit))
    rows = (
        table.search(query)
        .metric("cosine")
        .select(_SEMANTIC_SELECT)
        .limit(fetch)
        .to_list()
    )

    scored: list[tuple[float, dict]] = []
    for row in rows:
        similarity = 1.0 - float(row["_distance"])
        if similarity < score_threshold:
            continue
        scored.append((similarity, row))
    # Deterministic re-rank: similarity desc, then evidence_id asc for stable ties.
    scored.sort(key=lambda item: (-item[0], item[1]["evidence_id"]))
    scored = scored[:limit]

    trace = RetrievalTrace(
        stages=[
            TraceStage(
                "semantic-search",
                f"LanceDB cosine vector search "
                f"(model: {info.name if info else 'unknown'}, "
                f"dim {info.dimension if info else '?'})",
            ),
            TraceStage("semantic-rank", "ranked by cosine similarity"),
            TraceStage(
                "threshold",
                f"dropped candidates below similarity {score_threshold}",
            ),
        ]
    )
    return [
        _retrieval_result_from_row(
            row, score=similarity, reason="cosine semantic similarity", trace=trace
        )
        for similarity, row in scored
    ]


def search_hybrid_index(
    index_dir: Path,
    query: str,
    query_vector: list[float],
    limit: int,
    score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> list[RetrievalResult]:
    """Hybrid retrieval: reciprocal-rank-fuse lexical and semantic results.

    Ranking is bounded and deterministic (fixed RRF constant, stable tie-break
    on evidence_id). Citation and trace behavior is preserved: every result is
    a citation-ready RetrievalResult over Knowledge Base Evidence.
    """
    lexical = search_lexical_index(index_dir, query, limit)
    semantic = search_semantic_index(index_dir, query_vector, limit, score_threshold)

    fused = reciprocal_rank_fusion(
        [
            [r.evidence.id for r in lexical],
            [r.evidence.id for r in semantic],
        ]
    )
    by_id: dict[str, RetrievalResult] = {}
    for result in (*lexical, *semantic):
        by_id.setdefault(result.evidence.id, result)

    trace = RetrievalTrace(
        stages=[
            TraceStage("lexical-search", "LanceDB BM25 full-text search"),
            TraceStage("semantic-search", "LanceDB cosine vector search"),
            TraceStage("fusion", f"reciprocal rank fusion (k={RRF_K})"),
            TraceStage("hybrid-rank", "fused deterministic ranking"),
        ]
    )
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        RetrievalResult(
            evidence=by_id[eid].evidence,
            citation=by_id[eid].citation,
            snippet=by_id[eid].snippet,
            score=score,
            reason="hybrid lexical+semantic fusion",
            trace=trace,
        )
        for eid, score in ordered
    ]


class LanceDBRetrievalAdapter:
    """Retrieval adapter that materializes LanceDB lexical/semantic/hybrid indexes.

    This is the enhanced-app retrieval backend: BM25 full-text search over a
    derived Evidence index, plus optional citation-ready cosine vector search
    (semantic / hybrid). It is injected into :class:`KnowledgeBase` via
    ``build_index(..., retrieval=LanceDBRetrievalAdapter())`` (or the
    :func:`build_lancedb_index` helper). The canonical Knowledge Base never
    imports this class, so LanceDB/pyarrow remain optional for callers that do
    not need enhanced retrieval.
    """

    @property
    def name(self) -> str:
        return "lancedb"

    def build_index(
        self,
        pages,
        index_dir,
        *,
        embedder: Embedder | None = None,
        fingerprint=None,
    ) -> None:
        index_dir = Path(index_dir)
        build_lexical_index(list(pages), index_dir)
        if fingerprint is not None:
            save_stored_fingerprint(index_dir, fingerprint)
        if embedder is not None:
            build_semantic_index(list(pages), index_dir, embedder)

    def retrieve(
        self,
        pages,
        query,
        *,
        limit=5,
        index_dir=None,
        mode="lexical",
        embedder: Embedder | None = None,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ):
        del pages
        if index_dir is None:
            # Match prior KB requirement for enhanced retrieve without a built dir.
            return []
        resolved = Path(index_dir)
        if mode == "lexical":
            return search_lexical_index(resolved, query, limit)
        if mode not in ("semantic", "hybrid"):
            raise EmbeddingError(
                f"unknown retrieval mode {mode!r}; use 'lexical', 'semantic', or 'hybrid'"
            )
        if embedder is None:
            raise EmbeddingError(
                f"{mode} retrieval requires an embedder; pass embedder= to retrieve()"
            )
        stored_model = load_embedding_model(resolved)
        if stored_model is None:
            raise EmbeddingNotBuiltError(
                "no semantic index built; call build_index(..., embedder=...) first"
            )
        if stored_model != embedder.model_info:
            raise EmbeddingError(
                "semantic index was built with a different embedding model; "
                "rebuild the index with build_index(..., embedder=...)"
            )
        query_vector = embedder.embed([query])[0]
        if mode == "semantic":
            return search_semantic_index(resolved, query_vector, limit, score_threshold)
        return search_hybrid_index(resolved, query, query_vector, limit, score_threshold)


def build_lancedb_index(kb, index_dir, *, embedder: Embedder | None = None):
    """Build a LanceDB-backed derived index on ``kb`` and bind the adapter.

    Convenience helper for production call sites (app/CLI) and tests that want
    the full enhanced retrieval behavior (BM25 + optional semantic/hybrid) with
    a single call.
    """
    return kb.build_index(
        index_dir,
        embedder=embedder,
        retrieval=LanceDBRetrievalAdapter(),
    )
