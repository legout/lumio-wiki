"""Gold-set evaluation harness for retrieval stages (issue #138).

Lumio had retrieval *inspection* (the Retrieval Trace) but no retrieval
*measurement*. This module turns retrieval tuning from vibes into engineering:
a versioned gold set (query -> expected relevant Canonical Page Titles) is run
through the **public retrieval seam** — :meth:`lumio_wiki.knowledge_base.KnowledgeBase.retrieve`
— and recall@k is reported per pipeline stage. The result is a defensible,
deterministic gate that catches retrieval regressions.

Design tenets (issue #138):

* **Public seam only.** Stages call ``KnowledgeBase.retrieve`` (the same surface
  the Core SDK ``lumio`` uses; ``lumio.core.retrieval`` is a compatibility alias
  for ``lumio_wiki.retrieval``). No internals are exercised, so a re-rank,
  fusion, or graph-expansion change is measured exactly the way a client sees it.
* **Model-free at the base layer.** No LLM-as-judge and no answer-quality
  scoring — retrieval-stage recall only. The lexical (zero-index) and graph
  stages need no provider and no network. The LanceDB BM25 stage needs the
  optional ``lumio-lancedb`` adapter but still no embedder. The semantic/hybrid
  stages accept any :class:`~lumio_wiki.embeddings.Embedder`; for a fully
  offline, deterministic run pass :class:`DeterministicHashEmbedder`.
* **Per-stage attribution.** Each stage is a small object so the report table
  shows what each stage (lexical / graph-expansion / BM25 / semantic / hybrid)
  buys. Stages that are unavailable (LanceDB not installed) or inapplicable to a
  query (graph expansion needs seed titles) are reported as skipped, never as a
  silent zero that would look like a recall failure.
* **Recall@k at the Canonical Page Title level.** A retrieved result's stable
  identity is its page title (``evidence.page_title``); Evidence ids carry line
  ranges that change when bodies change, so page-title recall is the durable,
  human-meaningful unit.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import msgspec

from lumio_wiki.composition import lancedb_available, load_lancedb_adapter
from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    Embedder,
    EmbeddingModelInfo,
    RetrievalMode,
    normalize_vector,
)
from lumio_wiki.knowledge_base import (
    GRAPH_DIRECTION_OUTGOING,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
)
from lumio_wiki.records import RetrievalResult
from lumio_wiki.retrieval import RetrievalAdapter

__all__ = [
    "DEFAULT_KS",
    "DeterministicHashEmbedder",
    "EvalReport",
    "GoldQuery",
    "GoldSet",
    "GraphExpansionStage",
    "LanceDBBM25Stage",
    "LanceDBHybridStage",
    "LanceDBSemanticStage",
    "QueryOutcome",
    "QueryStageOutcome",
    "Stage",
    "StageAggregate",
    "ZeroIndexLexicalStage",
    "default_stages",
    "distinct_page_titles",
    "evaluate",
    "lancedb_available",
    "load_gold_set",
    "load_lancedb_adapter",
    "recall_at_k",
]

#: Default ``k`` cutoffs reported by :func:`evaluate`.
DEFAULT_KS: tuple[int, ...] = (1, 3, 5)

#: Graph expansion defaults used by :class:`GraphExpansionStage` (issue #112).
DEFAULT_GRAPH_MAX_DEPTH = 2


# ---------------------------------------------------------------------------
# Recall@k and result helpers.
# ---------------------------------------------------------------------------


def distinct_page_titles(results: Sequence[RetrievalResult]) -> list[str]:
    """Return retrieved page titles deduplicated in rank order.

    A page may surface via several Evidences (body + sections); for page-level
    recall@k it is one relevant unit, counted at its first (highest-ranked)
    appearance.
    """
    seen: set[str] = set()
    titles: list[str] = []
    for result in results:
        title = result.evidence.page_title
        if title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


def recall_at_k(retrieved: Sequence[str], relevant: set[str], k: int) -> float:
    """Return recall@k: the fraction of ``relevant`` titles in the top-``k`` of ``retrieved``.

    ``retrieved`` is an ordered list of distinct page titles (rank order).
    Returns ``0.0`` when ``relevant`` is empty only if something was retrieved;
    an empty relevant set with no retrievals is vacuously ``1.0`` so a query
    that expects nothing and gets nothing is not scored as a failure.
    """
    if k <= 0:
        return 0.0
    if not relevant:
        return 1.0 if not retrieved[:k] else 0.0
    top_k = set(retrieved[:k])
    hits = len(top_k & relevant)
    return hits / len(relevant)


# ---------------------------------------------------------------------------
# Gold set (versioned query -> relevant-evidence map).
# ---------------------------------------------------------------------------


class _GoldQueryYaml(msgspec.Struct):
    query: str
    relevant: list[str]
    seeds: list[str] | None = None
    passages: list[str] | None = None
    note: str = ""


class _GoldSetYaml(msgspec.Struct):
    version: int = 1
    name: str = ""
    description: str = ""
    ks: list[int] = msgspec.field(default_factory=lambda: list(DEFAULT_KS))
    queries: list[_GoldQueryYaml] = msgspec.field(default_factory=list)


@dataclass(frozen=True)
class GoldQuery:
    """One gold row, including optional expected passage identities.

    Empty ``relevant`` is an explicit negative query: it measures whether a
    stage returns no answer rather than disappearing from the denominator.
    ``passages`` use ``Page Title#Section Title`` (or just a section title).
    """

    query: str
    relevant: frozenset[str]
    seed_titles: tuple[str, ...] = ()
    note: str = ""
    passages: tuple[str, ...] = ()

    @property
    def has_graph_seeds(self) -> bool:
        return bool(self.seed_titles)


@dataclass(frozen=True)
class GoldSet:
    """A versioned set of :class:`GoldQuery` rows plus metadata."""

    name: str
    queries: tuple[GoldQuery, ...]
    version: int = 1
    description: str = ""
    ks: tuple[int, ...] = DEFAULT_KS
    source: str = ""

    @property
    def size(self) -> int:
        return len(self.queries)


def load_gold_set(path: str | Path) -> GoldSet:
    """Load a versioned gold set from a YAML file (``msgspec``-decoded)."""
    raw = Path(path).read_text(encoding="utf-8")
    parsed = msgspec.yaml.decode(raw, type=_GoldSetYaml)
    queries: list[GoldQuery] = []
    for row in parsed.queries:
        relevant = frozenset(r.strip() for r in row.relevant if r and r.strip())
        if row.query.strip():
            queries.append(
                GoldQuery(
                    query=row.query.strip(),
                    relevant=relevant,
                    seed_titles=tuple(s.strip() for s in (row.seeds or []) if s and s.strip()),
                    passages=tuple(
                        passage.strip()
                        for passage in (row.passages or [])
                        if passage and passage.strip()
                    ),
                    note=(row.note or "").strip(),
                )
            )
    return GoldSet(
        name=parsed.name or Path(path).stem,
        queries=tuple(queries),
        version=parsed.version,
        description=parsed.description,
        ks=tuple(parsed.ks) if parsed.ks else DEFAULT_KS,
        source=str(path),
    )


# ---------------------------------------------------------------------------
# Stages: each calls the public ``KnowledgeBase.retrieve`` seam.
# ---------------------------------------------------------------------------


@runtime_checkable
class Stage(Protocol):
    """One measurable retrieval pipeline stage over the public seam.

    ``available`` reports whether the stage's dependencies are present (e.g.
    LanceDB installed). ``applicable`` reports whether a given gold query
    supports this stage (graph expansion needs seed titles). ``run`` returns the
    distinct retrieved page titles in rank order for recall@k. Stages may also
    provide ``run_results`` to make citation and passage metrics measurable.
    """

    name: str
    description: str

    def available(self) -> bool: ...

    def applicable(self, query: GoldQuery) -> bool: ...

    def run(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[str]: ...


class ZeroIndexLexicalStage:
    """Always-available zero-index lexical retrieval (the default adapter).

    No derived index, no provider, no network. This is the base rung of the
    retrieval ladder: every other stage is measured against it.
    """

    name = "zero-index-lexical"
    description = "Zero-index field-aware lexical retrieval (no derived index)"

    def available(self) -> bool:
        return True

    def applicable(self, query: GoldQuery) -> bool:
        del query
        return True

    def run_results(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[RetrievalResult]:
        # Default adapter, no graph expansion. ``k`` is the retrieval limit so a
        # deeper ``k`` is measured from a deeper candidate pool.
        return kb.retrieve(query.query, limit=k)

    def run(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[str]:
        return distinct_page_titles(self.run_results(kb, query, k=k))


class GraphExpansionStage:
    """Discovery Graph expansion that selects eligible pages before ranking (issue #112).

    Graph expansion is a *modifier* of lexical ranking, not a standalone ranker:
    it broadens the eligible page set along canonical accepted Claims plus
    Extracted References so weakly-matching but relevant pages surface in top-k
    instead of being outranked by strong-but-irrelevant matches. A query without
    seed titles is inapplicable (N/A) rather than scored as zero recall.
    """

    name = "graph-expansion"
    description = (
        "Discovery Graph expansion (canonical + extracted refs) selects "
        "eligible pages before zero-index lexical ranking"
    )

    def __init__(
        self, *, scope: str = GRAPH_SCOPE_DISCOVERY, max_depth: int = DEFAULT_GRAPH_MAX_DEPTH
    ) -> None:
        self._scope = scope
        self._max_depth = max_depth

    def available(self) -> bool:
        return True

    def applicable(self, query: GoldQuery) -> bool:
        return query.has_graph_seeds

    def run_results(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[RetrievalResult]:
        return kb.retrieve(
            query.query,
            limit=k,
            graph_seed_titles=list(query.seed_titles),
            graph_scope=self._scope,
            graph_direction=GRAPH_DIRECTION_OUTGOING,
            graph_max_depth=self._max_depth,
        )

    def run(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[str]:
        return distinct_page_titles(self.run_results(kb, query, k=k))


class _LanceDBStageBase:
    """Shared LanceDB adapter plumbing for the BM25 / semantic / hybrid stages."""

    name = "lancedb-base"
    description = ""

    def __init__(
        self,
        *,
        index_dir: Path,
        embedder: Embedder | None = None,
        mode: RetrievalMode = "lexical",
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ) -> None:
        self._index_dir = index_dir
        self._embedder = embedder
        self._mode: RetrievalMode = mode
        self._score_threshold = score_threshold
        # The adapter-bound KnowledgeBase is injected by ``evaluate`` after it
        # builds the LanceDB index; until then the caller-supplied ``kb`` is used
        # (which only works for the zero-index adapter, never for LanceDB).
        self._bound_kb: KnowledgeBase | None = None

    def available(self) -> bool:
        return lancedb_available()

    def applicable(self, query: GoldQuery) -> bool:
        del query
        return True

    def run_results(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[RetrievalResult]:
        if not self.available():
            return []
        bound = self._bound_kb if self._bound_kb is not None else kb
        return bound.retrieve(
            query.query,
            limit=k,
            mode=self._mode,
            embedder=self._embedder,
            score_threshold=self._score_threshold,
            index_dir=self._index_dir,
        )

    def run(self, kb: KnowledgeBase, query: GoldQuery, *, k: int) -> list[str]:
        return distinct_page_titles(self.run_results(kb, query, k=k))


class LanceDBBM25Stage(_LanceDBStageBase):
    """LanceDB BM25 full-text search over the derived Evidence index (no embedder)."""

    name = "lancedb-bm25"
    description = "LanceDB BM25 full-text ranking over a derived Evidence index (no embedder)"

    def __init__(self, *, index_dir: Path) -> None:
        super().__init__(index_dir=index_dir, mode="lexical")


class LanceDBSemanticStage(_LanceDBStageBase):
    """LanceDB cosine vector search (semantic). Requires an :class:`Embedder`."""

    name = "lancedb-semantic"
    description = "LanceDB cosine vector search (semantic) over Evidence embeddings"

    def __init__(
        self,
        *,
        index_dir: Path,
        embedder: Embedder,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ) -> None:
        super().__init__(
            index_dir=index_dir,
            embedder=embedder,
            mode="semantic",
            score_threshold=score_threshold,
        )


class LanceDBHybridStage(_LanceDBStageBase):
    """LanceDB reciprocal-rank-fusion of lexical + semantic (hybrid)."""

    name = "lancedb-hybrid"
    description = "LanceDB reciprocal-rank-fusion of lexical + semantic (hybrid)"

    def __init__(
        self,
        *,
        index_dir: Path,
        embedder: Embedder,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ) -> None:
        super().__init__(
            index_dir=index_dir,
            embedder=embedder,
            mode="hybrid",
            score_threshold=score_threshold,
        )


def default_stages(
    *,
    lancedb_index_dir: Path | None = None,
    embedder: Embedder | None = None,
    lancedb_adapter: RetrievalAdapter | None = None,
) -> list[Stage]:
    """Build the canonical stage ladder.

    Always includes zero-index lexical and graph expansion. Adds LanceDB BM25
    when the adapter is installed, an index dir is supplied, AND a retrieval
    adapter is injected. lumio-wiki cannot import ``lumio-lancedb`` itself
    (ADR-0010), so the caller passes the loaded ``LanceDBRetrievalAdapter`` via
    :func:`load_lancedb_adapter`. Adds semantic / hybrid when an embedder is
    also supplied.
    """
    stages: list[Stage] = [ZeroIndexLexicalStage(), GraphExpansionStage()]
    if lancedb_index_dir is not None and lancedb_adapter is not None and lancedb_available():
        stages.append(LanceDBBM25Stage(index_dir=lancedb_index_dir))
        if embedder is not None:
            stages.append(LanceDBSemanticStage(index_dir=lancedb_index_dir, embedder=embedder))
            stages.append(LanceDBHybridStage(index_dir=lancedb_index_dir, embedder=embedder))
    return stages


# ---------------------------------------------------------------------------
# Report types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryStageOutcome:
    """One (query, stage) cell: titles, recall, and evidence disclosures."""

    retrieved: list[str]
    recall_by_k: dict[int, float]
    citation_coverage: float = 0.0
    passage_hits: int = 0
    passage_expected: int = 0
    negative_success: bool | None = None


@dataclass(frozen=True)
class QueryOutcome:
    """Per-query results across every applicable, available stage."""

    query: str
    relevant: list[str]
    seed_titles: list[str]
    note: str
    per_stage: dict[str, QueryStageOutcome]
    skipped_stages: dict[str, str]
    passages: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageAggregate:
    """Aggregate recall plus negative, citation, and passage measurements."""

    name: str
    description: str
    n: int
    mean_recall_by_k: dict[int, float]
    available: bool
    skipped_reason: str | None
    negative_n: int = 0
    negative_success_rate: float | None = None
    citation_coverage: float | None = None
    passage_recall: float | None = None


@dataclass(frozen=True)
class EvalReport:
    """The full evaluation result: per-stage aggregates + per-query detail."""

    gold_set: str
    gold_set_size: int
    ks: tuple[int, ...]
    stages: list[StageAggregate]
    queries: list[QueryOutcome]
    # Name of the embedder used for semantic/hybrid stages (its
    # ``model_info.name``), or ``None`` for a model-free run. Surfaced so a
    # reviewer can tell the deterministic hash stand-in from a real embedding
    # model at a glance (issue #158).
    embedder: str | None = None
    corpus_pages: int = 0
    disclosures: tuple[str, ...] = (
        "lifecycle mutation/edit/delete transitions are covered by focused "
        "regression tests, not this gold-set run",
        "semantic ranking quality is not certified by the deterministic hash embedder",
    )

    def to_dict(self) -> dict:
        """Machine-readable serialization (stable for CI diffing / JSON output)."""
        return {
            "gold_set": self.gold_set,
            "gold_set_size": self.gold_set_size,
            "corpus_pages": self.corpus_pages,
            "embedder": self.embedder,
            "disclosures": list(self.disclosures),
            "lifecycle": {
                "measured": False,
                "note": "edit/delete/rebuild lifecycle is covered by focused regression tests",
            },
            "ks": list(self.ks),
            "stages": [
                {
                    "name": s.name,
                    "description": s.description,
                    "n": s.n,
                    "mean_recall_by_k": {
                        str(k): round(v, 4) for k, v in s.mean_recall_by_k.items()
                    },
                    "available": s.available,
                    "skipped_reason": s.skipped_reason,
                    "negative_n": s.negative_n,
                    "negative_success_rate": (
                        round(s.negative_success_rate, 4)
                        if s.negative_success_rate is not None
                        else None
                    ),
                    "citation_coverage": (
                        round(s.citation_coverage, 4) if s.citation_coverage is not None else None
                    ),
                    "passage_recall": (
                        round(s.passage_recall, 4) if s.passage_recall is not None else None
                    ),
                }
                for s in self.stages
            ],
            "queries": [
                {
                    "query": q.query,
                    "relevant": list(q.relevant),
                    "seed_titles": list(q.seed_titles),
                    "passages": list(q.passages),
                    "note": q.note,
                    "per_stage": {
                        name: {
                            "retrieved": out.retrieved,
                            "recall_by_k": {
                                str(k): round(v, 4) for k, v in out.recall_by_k.items()
                            },
                            "citation_coverage": round(out.citation_coverage, 4),
                            "passage_hits": out.passage_hits,
                            "passage_expected": out.passage_expected,
                            "negative_success": out.negative_success,
                        }
                        for name, out in q.per_stage.items()
                    },
                    "skipped_stages": dict(q.skipped_stages),
                }
                for q in self.queries
            ],
        }

    def stage(self, name: str) -> StageAggregate | None:
        for s in self.stages:
            if s.name == name:
                return s
        return None

    def to_table(self) -> str:
        """Human-readable recall@k table: one row per stage."""
        k_header = "  ".join(f"recall@{k}" for k in self.ks)
        lines = [f"Gold set: {self.gold_set} ({self.gold_set_size} queries)"]
        if self.embedder:
            lines.append(f"Embedder: {self.embedder}")
        lines += [
            "",
            f"{'stage':<22} {'n':>3}  {k_header}  status",
            "-" * (22 + 3 + 2 + len(self.ks) * 10 + 2 + 12),
        ]
        for s in self.stages:
            if not s.available:
                row_recall = "  ".join("   -  " for _ in self.ks)
                status = f"skipped: {s.skipped_reason or 'unavailable'}"
            elif s.n == 0:
                row_recall = "  ".join("   -  " for _ in self.ks)
                status = "no applicable queries"
            else:
                row_recall = "  ".join(f"{s.mean_recall_by_k.get(k, 0.0):.3f}" for k in self.ks)
                status = "ok"
            lines.append(f"{s.name:<22} {s.n:>3}  {row_recall}  {status}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evaluation entry point.
# ---------------------------------------------------------------------------


def _build_lancedb_index(
    kb: KnowledgeBase,
    index_dir: Path,
    retrieval_adapter: RetrievalAdapter,
    *,
    embedder: Embedder | None,
) -> KnowledgeBase:
    """Build a LanceDB derived index via ``retrieval_adapter``; return the bound KB.

    The adapter is injected by the caller (loaded via
    :func:`load_lancedb_adapter`); lumio-wiki never imports the adapter package
    itself (ADR-0010 dependency direction).
    """
    return kb.build_index(index_dir, retrieval=retrieval_adapter, embedder=embedder)


def _citation_is_valid(result: RetrievalResult) -> bool:
    citation = result.citation
    has_coordinates = (
        citation.line_start is not None
        and citation.line_end is not None
        and citation.line_start <= citation.line_end
    )
    return bool(
        citation.page_title
        and citation.relative_path
        and has_coordinates
        and result.snippet.strip()
    )


def _passage_matches(results: Sequence[RetrievalResult], expected: Sequence[str]) -> int:
    if not expected:
        return 0
    found = {
        f"{result.citation.page_title}#{result.citation.section_title}"
        for result in results
        if result.citation.section_title
    }
    found.update(
        result.citation.section_title for result in results if result.citation.section_title
    )
    return sum(1 for passage in expected if passage in found)


def _run_stage_results(
    stage: Stage, kb: KnowledgeBase, query: GoldQuery, *, k: int
) -> list[RetrievalResult]:
    run_results = getattr(stage, "run_results", None)
    if run_results is None:
        # Third-party stages retain the original title-only contract. Their
        # recall remains measurable, while evidence metrics are disclosed as
        # unavailable rather than fabricated.
        return []
    return run_results(kb, query, k=k)


def evaluate(
    kb: KnowledgeBase,
    gold_set: GoldSet,
    *,
    stages: Sequence[Stage] | None = None,
    ks: Sequence[int] | None = None,
    lancedb_index_dir: Path | None = None,
    embedder: Embedder | None = None,
    lancedb_adapter: RetrievalAdapter | None = None,
) -> EvalReport:
    """Run ``gold_set`` through ``stages`` over ``kb`` and return a recall@k report.

    When ``stages`` is omitted, :func:`default_stages` builds the canonical
    ladder. LanceDB stages require a ``lancedb_adapter`` (a loaded
    ``LanceDBRetrievalAdapter``, obtained via :func:`load_lancedb_adapter`):
    lumio-wiki cannot import ``lumio-lancedb`` (ADR-0010), so the caller injects
    it. Without it, only the base (zero-index + graph) ladder runs. When LanceDB
    stages run but no ``lancedb_index_dir`` is supplied, a fresh temp index is
    built so semantic/hybrid stages can run.
    """
    resolved_ks = tuple(ks) if ks is not None else (gold_set.ks or DEFAULT_KS)

    if stages is None:
        if lancedb_adapter is not None and lancedb_available() and lancedb_index_dir is None:
            import tempfile

            lancedb_index_dir = Path(tempfile.mkdtemp(prefix="lumio-eval-lance-"))
        stages = default_stages(
            lancedb_index_dir=lancedb_index_dir,
            embedder=embedder,
            lancedb_adapter=lancedb_adapter,
        )

    # Build the LanceDB index once if any stage needs it and an adapter was injected.
    lance_stages = [s for s in stages if isinstance(s, _LanceDBStageBase)]
    if lance_stages and lancedb_adapter is not None and lancedb_available():
        index_dir = lance_stages[0]._index_dir  # noqa: SLF001 — all share one dir
        bound_kb = _build_lancedb_index(kb, index_dir, lancedb_adapter, embedder=embedder)
        for stage in lance_stages:
            stage._bound_kb = bound_kb  # noqa: SLF001 — inject adapter-bound KB

    query_outcomes: list[QueryOutcome] = []
    # Accumulators: stage_name -> {k -> [recall, ...]} over applicable queries.
    accum: dict[str, dict[int, list[float]]] = {}
    measurements: dict[str, dict[str, list[float]]] = {}
    stage_meta: dict[str, Stage] = {}
    for stage in stages:
        stage_meta[stage.name] = stage
        accum.setdefault(stage.name, {k: [] for k in resolved_ks})
        measurements[stage.name] = {
            "negative": [],
            "citation": [],
            "passage_hits": [],
            "passage_expected": [],
        }

    for gq in gold_set.queries:
        per_stage: dict[str, QueryStageOutcome] = {}
        skipped: dict[str, str] = {}
        for stage in stages:
            if not stage.available():
                skipped[stage.name] = "stage unavailable (dependency missing)"
                continue
            if not stage.applicable(gq):
                skipped[stage.name] = "not applicable (e.g. no graph seeds)"
                continue
            run_results = getattr(stage, "run_results", None)
            has_evidence_results = callable(run_results)
            results = _run_stage_results(stage, kb, gq, k=max(resolved_ks))
            retrieved = (
                distinct_page_titles(results)
                if has_evidence_results
                else stage.run(kb, gq, k=max(resolved_ks))
            )
            recall_by_k = {k: recall_at_k(retrieved, set(gq.relevant), k) for k in resolved_ks}
            citation_coverage = (
                sum(_citation_is_valid(result) for result in results) / len(results)
                if results
                else 0.0
            )
            passage_hits = _passage_matches(results, gq.passages)
            negative_success = (not retrieved) if not gq.relevant else None
            per_stage[stage.name] = QueryStageOutcome(
                retrieved=retrieved,
                recall_by_k=recall_by_k,
                citation_coverage=citation_coverage,
                passage_hits=passage_hits,
                passage_expected=len(gq.passages),
                negative_success=negative_success,
            )
            if has_evidence_results and results:
                measurements[stage.name]["citation"].append(citation_coverage)
            if negative_success is not None:
                measurements[stage.name]["negative"].append(1.0 if negative_success else 0.0)
            if has_evidence_results and gq.passages:
                measurements[stage.name]["passage_hits"].append(passage_hits)
                measurements[stage.name]["passage_expected"].append(len(gq.passages))
            for k in resolved_ks:
                accum[stage.name][k].append(recall_by_k[k])
        query_outcomes.append(
            QueryOutcome(
                query=gq.query,
                relevant=sorted(gq.relevant),
                seed_titles=list(gq.seed_titles),
                note=gq.note,
                per_stage=per_stage,
                skipped_stages=skipped,
                passages=gq.passages,
            )
        )

    stage_aggregates: list[StageAggregate] = []
    for stage in stages:
        recalls = accum[stage.name]
        if not stage.available():
            agg = StageAggregate(
                name=stage.name,
                description=stage.description,
                n=0,
                mean_recall_by_k={k: 0.0 for k in resolved_ks},
                available=False,
                skipped_reason="lumio-lancedb not installed",
            )
        else:
            counts = {k: len(recalls[k]) for k in resolved_ks}
            n = max(counts.values()) if counts else 0
            means = {
                k: (sum(recalls[k]) / len(recalls[k]) if recalls[k] else 0.0) for k in resolved_ks
            }
            stage_measurements = measurements[stage.name]
            passage_expected = sum(stage_measurements["passage_expected"])
            agg = StageAggregate(
                name=stage.name,
                description=stage.description,
                n=n,
                mean_recall_by_k=means,
                available=True,
                skipped_reason=None,
                negative_n=len(stage_measurements["negative"]),
                negative_success_rate=(
                    sum(stage_measurements["negative"]) / len(stage_measurements["negative"])
                    if stage_measurements["negative"]
                    else None
                ),
                citation_coverage=(
                    sum(stage_measurements["citation"]) / len(stage_measurements["citation"])
                    if stage_measurements["citation"]
                    else None
                ),
                passage_recall=(
                    sum(stage_measurements["passage_hits"]) / passage_expected
                    if passage_expected
                    else None
                ),
            )
        stage_aggregates.append(agg)

    return EvalReport(
        gold_set=gold_set.name,
        gold_set_size=gold_set.size,
        ks=resolved_ks,
        stages=stage_aggregates,
        queries=query_outcomes,
        embedder=embedder.model_info.name if embedder is not None else None,
        corpus_pages=len(kb.pages),
    )


# ---------------------------------------------------------------------------
# Deterministic, offline embedder for semantic/hybrid stages (model-free).
# ---------------------------------------------------------------------------


class DeterministicHashEmbedder:
    """Deterministic, offline embedder for semantic/hybrid stages.

    Maps text into a fixed-length vector by summing per-token pseudo-random
    unit vectors (seeded by each token's SHA-256, then L2-normalized). Texts
    that share tokens end up cosine-similar; unrelated texts are near-orthogonal.
    An optional ``synonyms`` map collapses paraphrases to a shared token so the
    harness can demonstrate semantic recall that lexical search misses.

    This is NOT a learned model and never makes a network call — it exists so
    the semantic/hybrid stages can be exercised deterministically inside the
    model-free base layer (issue #138). Production deployments pass a real
    :class:`~lumio_wiki.embeddings.Embedder`.
    """

    DEFAULT_MODEL_NAME = "lumio-eval-deterministic-hash"
    DEFAULT_DIMENSION = 128
    _TOKEN_RE = re.compile(r"[a-z0-9]+")

    def __init__(
        self,
        *,
        dimension: int = DEFAULT_DIMENSION,
        synonyms: dict[str, str] | None = None,
        model_name: str = DEFAULT_MODEL_NAME,
    ) -> None:
        self._dimension = dimension
        self._synonyms: dict[str, str] = {k.lower(): v.lower() for k, v in (synonyms or {}).items()}
        self._model_name = model_name
        self._concept_cache: dict[str, list[float]] = {}

    def _concept_vector(self, concept: str) -> list[float]:
        cached = self._concept_cache.get(concept)
        if cached is None:
            seed = int.from_bytes(hashlib.sha256(concept.encode("utf-8")).digest()[:8], "big")
            rng = random.Random(seed)
            raw = [rng.gauss(0.0, 1.0) for _ in range(self._dimension)]
            cached = normalize_vector(raw)
            self._concept_cache[concept] = cached
        return cached

    def _concepts(self, text: str) -> list[str]:
        tokens = self._TOKEN_RE.findall(text.casefold())
        concepts: list[str] = []
        for tok in tokens:
            mapped = self._synonyms.get(tok)
            concepts.append(mapped if mapped is not None else tok)
        return concepts

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            acc = [0.0] * self._dimension
            for concept in self._concepts(text):
                concept_vec = self._concept_vector(concept)
                for i, value in enumerate(concept_vec):
                    acc[i] += value
            vectors.append(normalize_vector(acc))
        return vectors

    @property
    def model_info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(name=self._model_name, dimension=self._dimension)
