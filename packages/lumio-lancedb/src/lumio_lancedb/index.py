"""LanceDB Evidence index over a Knowledge Base (``lumio-lancedb`` adapter).

This is the optional enhanced-retrieval adapter: BM25 full-text search over a
derived Evidence index, plus citation-ready cosine vector search (semantic /
hybrid) when an embedding provider is configured. It implements the
:class:`lumio_wiki.retrieval.RetrievalAdapter` contract without changing the
Knowledge Base format, Evidence, citations, or client behavior. ``lumio-wiki``
never imports this module; callers inject it explicitly (e.g.
``KnowledgeBase.build_index(..., retrieval=LanceDBRetrievalAdapter())``).

Indexes live either on the local filesystem (the original behavior) or under an
immutable S3 Published Version's ``derived/lance/`` prefix. Both are expressed
as a typed :class:`~lumio_lancedb.location.IndexLocation` so build and search
work uniformly; a missing or unhealthy index selects truthful zero-index
retrieval over the same snapshot and records the fallback in the Retrieval
Trace (ADR-0013).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import msgspec

# ``lancedb`` / ``pyarrow`` are required to actually build or search a derived
# index, but importing this module is kept safe when they are absent so the
# broader ``lumio_lancedb`` package and its smoke tests can be imported in
# environments where the dependency failed to initialize. The build/search call
# sites below resolve to ``None`` only when the optional dependency is
# unavailable; a real build/search then raises an actionable error.
try:  # pragma: no cover - exercised indirectly via build/search tests
    import pyarrow as pa
    from lancedb.index import FTS
except ImportError:  # pragma: no cover - optional dependency unavailable
    pa = None
    FTS = None

from lumio_wiki import evidence, page_search
from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    EMBEDDING_MODEL_FILE,
    RRF_K,
    Embedder,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingNotBuiltError,
    load_embedding_model,  # noqa: F401  (re-exported for callers/tests)
    normalize_vector,
    reciprocal_rank_fusion,
    save_embedding_model,  # noqa: F401  (re-exported for callers/tests)
)
from lumio_wiki.fingerprint_store import (  # noqa: F401  (re-exported for KnowledgeBase)
    FINGERPRINT_FILE,
    load_stored_fingerprint,
    save_stored_fingerprint,
)
from lumio_wiki.records import (
    CompiledPage,
    EmbeddingModelInfo,
    Evidence,
    PageSearchResult,
    RetrievalResult,
    RetrievalTrace,
    SourceFingerprint,
    TraceStage,
)
from lumio_wiki.retrieval import ZeroIndexRetrieval

from lumio_lancedb.location import (
    IndexLocation,
    as_location,
)

TABLE_NAME = "evidence"
PAGE_TABLE_NAME = "pages"
VECTOR_TABLE_NAME = "evidence_vectors"


# ---------------------------------------------------------------------------
# Location-aware metadata sidecars (fingerprint + embedding-model identity).
#
# The Path-based ``lumio_wiki`` helpers are re-exported above for callers that
# still work with a local directory; internally every build/search path goes
# through an :class:`IndexLocation` so the sidecars move beside the tables on a
# local filesystem OR an object store (ADR-0013). A mismatch between the
# recorded fingerprint / model identity and the current source is rejected so a
# stale or rebuilt-with-a-different-model index can never silently serve.
# ---------------------------------------------------------------------------


def _save_fingerprint(index: IndexLocation, fingerprint: SourceFingerprint) -> None:
    index.write_sidecar(FINGERPRINT_FILE, msgspec.json.encode(fingerprint))


def _load_fingerprint(index: IndexLocation) -> SourceFingerprint | None:
    data = index.read_sidecar(FINGERPRINT_FILE)
    if data is None:
        return None
    return msgspec.json.decode(data, type=SourceFingerprint)


def _save_model(index: IndexLocation, info: EmbeddingModelInfo) -> None:
    index.write_sidecar(EMBEDDING_MODEL_FILE, msgspec.json.encode(info))


def _load_model(index: IndexLocation) -> EmbeddingModelInfo | None:
    data = index.read_sidecar(EMBEDDING_MODEL_FILE)
    if data is None:
        return None
    return msgspec.json.decode(data, type=EmbeddingModelInfo)


def _zero_index_fallback(
    pages,
    query: str,
    limit: int,
    eligible_pages,
    index: IndexLocation,
    *,
    exc: BaseException | None = None,
    missing: bool = False,
    fallback_callback: Callable[[str], None] | None = None,
) -> list[RetrievalResult]:
    """Fall back to always-available zero-index retrieval and record it.

    The LanceDB index is progressive enhancement: when it is missing or
    unhealthy, retrieval must still answer over the same snapshot rather than
    crash or return nothing. The fallback records an ``index-fallback`` stage
    in every result's trace so the degradation is observable (ADR-0013).
    """
    reason = "missing" if missing else "unavailable"
    detail = (
        f"LanceDB index {reason} at {index.describe}; "
        f"fell back to zero-index retrieval over the same snapshot"
    )
    if exc is not None:
        detail += f" ({type(exc).__name__}: {exc})"
    if fallback_callback is not None:
        fallback_callback(detail)
    results = ZeroIndexRetrieval().retrieve(
        list(pages),
        query,
        limit=limit,
        index_dir=None,
        mode="lexical",
        eligible_pages=eligible_pages,
    )
    if not results:
        return []
    # Zero-index retrieval builds one shared RetrievalTrace for every result;
    # prepend the fallback explanation once so each result records it.
    results[0].trace.stages.insert(0, TraceStage("index-fallback", detail))
    return results


def _indexed_page_scores(index: IndexLocation, query: str) -> dict[str, float] | None:
    """Return BM25 scores by page path from the published page index.

    ``None`` means the index predates page search or is unavailable; callers
    can safely use the loaded page set as a compatibility fallback.
    """
    if not index.has_index():
        return None
    db = index.connect()
    if PAGE_TABLE_NAME not in db.list_tables().tables:
        return None
    table = db.open_table(PAGE_TABLE_NAME)
    tokens = page_search._search_tokens(query)
    row_count = table.count_rows()
    if not tokens or row_count == 0:
        return {}
    rows = (
        table.search(" ".join(tokens), query_type="fts")
        .select(["page_path", "_score"])
        .limit(row_count)
        .to_list()
    )
    return {row["page_path"]: float(row["_score"]) for row in rows}


def search_pages(
    pages: list[CompiledPage],
    query: str,
    limit: int = 20,
    index_dir: str | Path | IndexLocation | None = None,
) -> list[PageSearchResult]:
    """Return deterministic page-oriented lexical search results.

    When ``index_dir`` points at a built index, the page table supplies the
    candidate set; ranking and snippets are then computed from the loaded
    Compiled Pages for stable field-aware metadata. A missing page table falls
    back to the loaded pages so older derived indexes remain readable.
    """
    normalized = page_search.normalize_search_query(query)
    index = as_location(index_dir)
    indexed_scores = _indexed_page_scores(index, normalized) if index is not None else None
    if indexed_scores is None:
        return page_search.search_pages(pages, query, limit=limit)
    candidates = [page for page in pages if page.path in indexed_scores]
    results = page_search.search_pages(candidates, query, limit=len(candidates))
    results.sort(
        key=lambda result: (
            -indexed_scores[result.page.path],
            result.page.path.casefold(),
        )
    )
    return [
        msgspec.structs.replace(
            result,
            score=round(indexed_scores[result.page.path], 4),
        )
        for result in results[:limit]
    ]


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


def build_lexical_index(
    pages: list[CompiledPage], index_dir: str | Path | IndexLocation
) -> None:
    """Build fresh Evidence and page-oriented full-text indexes."""
    index = as_location(index_dir)
    index.prepare()
    db = index.connect()

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


def _eligible_page_predicate(paths: set[str]) -> str:
    """Build a LanceDB SQL ``page_path IN (...)`` predicate for eligible pages.

    ``paths`` is the non-empty set of eligible Compiled Page paths selected by
    Discovery Graph expansion (issue #112). Single quotes are SQL-escaped by
    doubling so page paths cannot break out of the string literal. The caller
    short-circuits an empty eligible set to ``[]`` before reaching here.
    """
    quoted = ",".join(
        "'" + path.replace("'", "''") + "'" for path in sorted(paths)
    )
    return f"page_path IN ({quoted})"


def _apply_eligible_filter(search, eligible_paths: set[str] | None):
    """Apply the eligible-page prefilter to a LanceDB search builder, or pass through.

    Centralizes the ``eligible_paths`` -> ``.where(...)`` application so the
    lexical and semantic ranking paths cannot drift (issue #112). ``None`` means
    no filtering (every page eligible); the caller short-circuits an empty set.
    """
    if eligible_paths is not None:
        return search.where(_eligible_page_predicate(eligible_paths))
    return search


def search_lexical_index(
    index_dir: str | Path | IndexLocation,
    query: str,
    limit: int,
    *,
    eligible_paths: set[str] | None = None,
) -> list[RetrievalResult]:
    """Search the lexical index and return citation-ready results.

    When ``eligible_paths`` is provided, only Evidence from those Compiled
    Pages is ranked (issue #112): LanceDB applies the filter as a prefilter so
    ``limit`` binds over eligible-only Evidence. ``None`` means every page.
    """
    index = as_location(index_dir)
    if not index.has_index():
        return []
    if eligible_paths is not None and not eligible_paths:
        return []

    db = index.connect()
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    trace = RetrievalTrace(
        stages=[
            TraceStage("search", "LanceDB BM25 full-text search"),
            TraceStage("rank", "ranked by BM25 score"),
        ]
    )

    search = table.search(query, query_type="fts").select(
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
    search = _apply_eligible_filter(search, eligible_paths)
    rows = search.limit(limit).to_list()

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
    pages: list[CompiledPage],
    index_dir: str | Path | IndexLocation,
    embedder: Embedder,
) -> None:
    """Build a citation-ready vector index from Evidence text + an embedder.

    Stores normalized vectors in a dedicated LanceDB table alongside the
    lexical FTS index and persists the embedding-model identity so later
    builds can detect a model change and rebuild vectors from source (#75).
    Lexical-only deployments never call this.
    """
    index = as_location(index_dir)
    index.prepare()
    info = embedder.model_info
    pairs = [pair for page in pages for pair in evidence.page_evidences(page)]
    db = index.connect()

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
    _save_model(index, info)


def has_semantic_index(index_dir: str | Path | IndexLocation) -> bool:
    """Return True when a vector table and embedding-model metadata are present."""
    index = as_location(index_dir)
    if not index.has_index():
        return False
    db = index.connect()
    if VECTOR_TABLE_NAME not in db.list_tables().tables:
        return False
    return _load_model(index) is not None


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
    index_dir: str | Path | IndexLocation,
    query_vector: list[float],
    limit: int,
    score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    *,
    eligible_paths: set[str] | None = None,
) -> list[RetrievalResult]:
    """Search the vector index and return citation-ready results above threshold.

    Candidates whose cosine similarity falls below ``score_threshold`` are
    dropped so unsupported questions return no results (-> "not covered by this
    knowledge base") rather than a weak nearest-neighbour hit (#75). When
    ``eligible_paths`` is provided only Evidence from those Compiled Pages is
    ranked (issue #112); ``None`` means every page.
    """
    index = as_location(index_dir)
    if eligible_paths is not None and not eligible_paths:
        return []
    if not has_semantic_index(index):
        raise EmbeddingNotBuiltError(
            "semantic retrieval requires a built vector index; "
            "call KnowledgeBase.build_index(embedder=...) first"
        )
    info = _load_model(index)
    db = index.connect()
    table = db.open_table(VECTOR_TABLE_NAME)
    if table.count_rows() == 0:
        return []

    query = normalize_vector(query_vector)
    fetch = min(table.count_rows(), max(limit * 4, limit))
    search = table.search(query).metric("cosine").select(_SEMANTIC_SELECT)
    search = _apply_eligible_filter(search, eligible_paths)
    rows = search.limit(fetch).to_list()

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
    index_dir: str | Path | IndexLocation,
    query: str,
    query_vector: list[float],
    limit: int,
    score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    *,
    eligible_paths: set[str] | None = None,
) -> list[RetrievalResult]:
    """Hybrid retrieval: reciprocal-rank-fuse lexical and semantic results.

    Ranking is bounded and deterministic (fixed RRF constant, stable tie-break
    on evidence_id). Citation and trace behavior is preserved: every result is
    a citation-ready RetrievalResult over Knowledge Base Evidence. When
    ``eligible_paths`` is provided both fused streams rank only Evidence from
    those Compiled Pages, so fusion is deterministic over eligible-only (issue
    #112).
    """
    lexical = search_lexical_index(index_dir, query, limit, eligible_paths=eligible_paths)
    semantic = search_semantic_index(
        index_dir, query_vector, limit, score_threshold, eligible_paths=eligible_paths
    )

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

    ``index_dir`` accepts a local path/URI or a typed
    :class:`~lumio_lancedb.location.IndexLocation` (e.g. a
    :class:`~lumio_lancedb.location.RemoteIndexLocation` pointing at an
    immutable S3 Published Version's ``derived/lance/`` prefix). A missing or
    unhealthy index selects truthful zero-index retrieval over the same
    snapshot and records the fallback in the Retrieval Trace (ADR-0013).
    """

    def __init__(
        self,
        *,
        index_location: IndexLocation | None = None,
        expected_fingerprint: SourceFingerprint | None = None,
    ) -> None:
        # Bind a pre-published index location (e.g. a remote S3 index for an S3
        # Snapshot) and the source fingerprint it must match. The Knowledge
        # Base's default ``retrieve()`` path passes ``index_dir=None`` for an
        # S3 Snapshot, so a bound location lets enhanced retrieval reach the
        # published remote index without a local build (ADR-0013, #124). An
        # explicit per-call ``index_dir`` always wins over the bound location.
        self._index_location = index_location
        self._expected_fingerprint = expected_fingerprint
        self._last_fallback_detail: str | None = None

    @property
    def index_location(self) -> IndexLocation | None:
        """The construction-bound index location, if any (remote S3 index)."""
        return self._index_location

    @property
    def expected_fingerprint(self):
        """The construction-bound source fingerprint gate, if any."""
        return self._expected_fingerprint

    @property
    def last_fallback_detail(self) -> str | None:
        """The latest fallback explanation, including empty-result fallbacks."""
        return self._last_fallback_detail

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
        index = as_location(index_dir)
        build_lexical_index(list(pages), index)
        if fingerprint is not None:
            _save_fingerprint(index, fingerprint)
        if embedder is not None:
            build_semantic_index(list(pages), index, embedder)

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
        eligible_pages=None,
        expected_fingerprint=None,
    ):
        self._last_fallback_detail = None
        # The LanceDB index is built over every Compiled Page; when the caller
        # supplies graph-selected eligible pages (#112), we restrict ranked
        # Evidence to them via a LanceDB prefilter so ``limit`` binds over
        # eligible-only Evidence. The adapter consumes only page identities: it
        # never imports graph code or owns traversal.
        eligible_paths = (
            None if eligible_pages is None else {page.path for page in eligible_pages}
        )
        # A caller-supplied index_dir always wins (explicit per-call location);
        # otherwise fall back to a location bound at construction (e.g. a remote
        # S3 index for an S3 Snapshot, ADR-0013/#124). When neither is set,
        # enhanced retrieval was never configured and returns no results.
        effective_index = index_dir if index_dir is not None else self._index_location
        if effective_index is None:
            return []
        index = as_location(effective_index)
        effective_fingerprint = (
            expected_fingerprint
            if expected_fingerprint is not None
            else self._expected_fingerprint
        )
        # Validate logic up front so configuration/programming errors raise
        # clearly instead of being masked by the availability fallback below.
        if mode not in ("lexical", "semantic", "hybrid"):
            raise EmbeddingError(
                f"unknown retrieval mode {mode!r}; use 'lexical', 'semantic', or 'hybrid'"
            )
        if mode != "lexical" and embedder is None:
            raise EmbeddingError(
                f"{mode} retrieval requires an embedder; pass embedder= to retrieve()"
            )
        # A missing index selects truthful zero-index retrieval over the same
        # snapshot rather than returning nothing; an unhealthy index (connect /
        # storage failure during the search) degrades the same way and records
        # the underlying error in the trace. Logic errors (model mismatch, a
        # semantic index that was never built) are re-raised so a misconfigured
        # or stale-with-a-different-model index is never silently served.
        try:
            if not index.has_index():
                return _zero_index_fallback(
                    pages,
                    query,
                    limit,
                    eligible_pages,
                    index,
                    missing=True,
                    fallback_callback=self._record_fallback,
                )
            # Verify the stored fingerprint matches the current Knowledge Base
            # so a stale remote index (built from a different Published Version)
            # is never silently served (ADR-0013, #122 AC3).
            stored_fp = _load_fingerprint(index)
            if stored_fp is not None and effective_fingerprint is not None:
                if stored_fp.digest != effective_fingerprint.digest:
                    return _zero_index_fallback(
                        pages, query, limit, eligible_pages, index,
                        exc=ValueError(
                            f"remote LanceDB fingerprint mismatch: "
                            f"{stored_fp.digest[:12]}… != {effective_fingerprint.digest[:12]}…"
                        ),
                        fallback_callback=self._record_fallback,
                    )
            if mode == "lexical":
                return search_lexical_index(
                    index, query, limit, eligible_paths=eligible_paths
                )
            stored_model = _load_model(index)
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
                return search_semantic_index(
                    index,
                    query_vector,
                    limit,
                    score_threshold,
                    eligible_paths=eligible_paths,
                )
            return search_hybrid_index(
                index,
                query,
                query_vector,
                limit,
                score_threshold,
                eligible_paths=eligible_paths,
            )
        except EmbeddingError:
            # Configuration/identity errors propagate — never masked by fallback.
            raise
        except Exception as exc:
            return _zero_index_fallback(
                pages,
                query,
                limit,
                eligible_pages,
                index,
                exc=exc,
                fallback_callback=self._record_fallback,
            )

    def _record_fallback(self, detail: str) -> None:
        self._last_fallback_detail = detail


def build_lancedb_index(kb, index_dir, *, embedder: Embedder | None = None):
    """Build a LanceDB-backed derived index on ``kb`` and bind the adapter.

    Convenience helper for production call sites (app/CLI) and tests that want
    the full enhanced retrieval behavior (BM25 + optional semantic/hybrid) with
    a single call. Builds a local index through the Knowledge Base; for a
    remote (S3) index, construct a :class:`~lumio_lancedb.location.RemoteIndexLocation`
    and call :meth:`LanceDBRetrievalAdapter.build_index` directly.
    """
    return kb.build_index(
        index_dir,
        embedder=embedder,
        retrieval=LanceDBRetrievalAdapter(),
    )
