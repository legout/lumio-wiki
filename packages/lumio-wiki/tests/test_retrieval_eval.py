"""Unit tests for the retrieval evaluation harness (issue #138).

These cover the pure seams: recall@k math, gold-set loading, stage
applicability/availability, default-stage composition, deterministic-embedder
stability, and report structure. The end-to-end gate over the committed
synthetic fixture KB lives in ``eval/test_retrieval_eval_gate.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki import retrieval_eval as ev
from lumio_wiki.knowledge_base import KnowledgeBase
from lumio_wiki.records import (
    Citation,
    CompiledPage,
    Evidence,
    Relationship,
    RetrievalResult,
    RetrievalTrace,
    Source,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _result(title: str, *, trace_name: str = "search") -> RetrievalResult:
    ev_obj = Evidence(
        id=f"{title}.md",
        source_type="compiled_markdown",
        page_path=f"{title}.md",
        page_title=title,
        text=title,
    )
    return RetrievalResult(
        evidence=ev_obj,
        citation=Citation(page_title=title, relative_path=f"{title}.md"),
        snippet=title,
        score=1.0,
        reason="x",
        trace=RetrievalTrace(stages=[]),
    )


def _page(title: str, body: str, *, rels=None) -> CompiledPage:
    return CompiledPage(
        path=f"{title.lower()}.md",
        title=title,
        aliases=[],
        tags=["t"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id=f"s-{title.lower()}", title=title)],
        relationships=rels or [],
        body=body,
        body_start_line=1,
    )


# ---------------------------------------------------------------------------
# recall@k and dedup.
# ---------------------------------------------------------------------------


class TestRecallAtK:
    def test_full_hit(self):
        assert ev.recall_at_k(["a", "b"], {"a", "b"}, 2) == 1.0
        assert ev.recall_at_k(["a", "b"], {"a", "b"}, 5) == 1.0

    def test_partial(self):
        assert ev.recall_at_k(["a", "c"], {"a", "b"}, 5) == 0.5

    def test_top_k_window_only(self):
        # 'b' is relevant but outside the top-1 window.
        assert ev.recall_at_k(["a", "b"], {"b"}, 1) == 0.0
        assert ev.recall_at_k(["a", "b"], {"b"}, 2) == 1.0

    def test_zero_k(self):
        assert ev.recall_at_k(["a"], {"a"}, 0) == 0.0

    def test_empty_relevant_vacuous_when_nothing_retrieved(self):
        assert ev.recall_at_k([], set(), 5) == 1.0

    def test_empty_relevant_something_retrieved_is_zero(self):
        # A query that expects nothing but retrieves something is a miss.
        assert ev.recall_at_k(["a"], set(), 5) == 0.0

    def test_distinct_page_titles_dedup_preserves_rank_order(self):
        results = [_result("A"), _result("B"), _result("A"), _result("C"), _result("B")]
        assert ev.distinct_page_titles(results) == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Gold set loading.
# ---------------------------------------------------------------------------


class TestGoldSet:
    def test_load_parses_queries_and_seeds(self, tmp_path):
        yaml = (
            "version: 1\n"
            'name: "test-gold"\n'
            "ks: [1, 5]\n"
            "queries:\n"
            '  - query: "alpha beta"\n'
            "    relevant:\n"
            '      - "Alpha"\n'
            '      - "Beta"\n'
            "    seeds:\n"
            '      - "Alpha"\n'
            '  - query: "gamma"\n'
            "    relevant:\n"
            '      - "Gamma"\n'
            "    note: \"no seeds\"\n"
        )
        path = tmp_path / "gold.yaml"
        path.write_text(yaml, encoding="utf-8")
        gs = ev.load_gold_set(path)
        assert gs.name == "test-gold"
        assert gs.size == 2
        assert gs.ks == (1, 5)
        first = gs.queries[0]
        assert first.query == "alpha beta"
        assert first.relevant == frozenset({"Alpha", "Beta"})
        assert first.seed_titles == ("Alpha",)
        assert first.has_graph_seeds
        assert not gs.queries[1].has_graph_seeds

    def test_load_drops_rows_with_empty_relevant(self, tmp_path):
        yaml = (
            "queries:\n"
            '  - query: "ok"\n'
            "    relevant:\n"
            '      - "OK"\n'
            '  - query: "blank"\n'
            "    relevant: []\n"
        )
        path = tmp_path / "g.yaml"
        path.write_text(yaml, encoding="utf-8")
        gs = ev.load_gold_set(path)
        assert gs.size == 1
        assert gs.queries[0].query == "ok"

    def test_defaults_ks_when_omitted(self, tmp_path):
        path = tmp_path / "g.yaml"
        path.write_text("queries:\n  - query: q\n    relevant:\n      - R\n", encoding="utf-8")
        assert ev.load_gold_set(path).ks == ev.DEFAULT_KS


# ---------------------------------------------------------------------------
# Stages.
# ---------------------------------------------------------------------------


class TestStages:
    def test_zero_index_always_available_and_applicable(self):
        s = ev.ZeroIndexLexicalStage()
        assert s.available()
        assert s.applicable(ev.GoldQuery("q", frozenset({"A"})))

    def test_graph_stage_inapplicable_without_seeds(self):
        s = ev.GraphExpansionStage()
        assert s.available()
        without = ev.GoldQuery("q", frozenset({"A"}))
        with_seeds = ev.GoldQuery("q", frozenset({"A"}), seed_titles=("A",))
        assert not s.applicable(without)
        assert s.applicable(with_seeds)

    def test_zero_index_run_returns_distinct_titles(self):
        kb = KnowledgeBase(
            root=Path("."),
            pages=[
                _page("Alpha", "alpha beta"),
                _page("Beta", "alpha gamma"),
                _page("Zeta", "zzz"),
            ],
        )
        stage = ev.ZeroIndexLexicalStage()
        out = stage.run(kb, ev.GoldQuery("alpha", frozenset({"Alpha"})), k=5)
        assert "Alpha" in out and "Beta" in out and "Zeta" not in out

    def test_graph_expansion_restricts_to_eligible(self):
        # Hub relates to Satellite; Decoy shares the term but is not eligible.
        hub = _page("Hub", "term alpha", rels=[Relationship(target="Sat", type="relates-to")])
        sat = _page("Sat", "term only")
        dec = _page("Dec", "term alpha beta gamma")
        kb = KnowledgeBase(root=Path("."), pages=[hub, sat, dec])
        graph = ev.GraphExpansionStage()
        query = ev.GoldQuery("term", frozenset({"Hub", "Sat"}), seed_titles=("Hub",))
        out = graph.run(kb, query, k=5)
        # Decoy is excluded by graph eligibility.
        assert "Dec" not in out
        assert "Hub" in out

    def test_default_stages_base_layer_without_lancedb_args(self):
        stages = ev.default_stages()
        names = [s.name for s in stages]
        assert names == ["zero-index-lexical", "graph-expansion"]

    def test_default_stages_adds_lancedb_when_index_dir_and_available(self):
        if not ev.lancedb_available():
            pytest.skip("lumio-lancedb not installed")
        adapter = ev.load_lancedb_adapter()
        assert adapter is not None
        stages = ev.default_stages(lancedb_index_dir=Path("/tmp/x"), lancedb_adapter=adapter)
        names = [s.name for s in stages]
        assert names == ["zero-index-lexical", "graph-expansion", "lancedb-bm25"]

    def test_default_stages_adds_semantic_hybrid_with_embedder(self):
        if not ev.lancedb_available():
            pytest.skip("lumio-lancedb not installed")
        adapter = ev.load_lancedb_adapter()
        assert adapter is not None
        stages = ev.default_stages(
            lancedb_index_dir=Path("/tmp/x"),
            embedder=ev.DeterministicHashEmbedder(),
            lancedb_adapter=adapter,
        )
        names = [s.name for s in stages]
        assert names == [
            "zero-index-lexical",
            "graph-expansion",
            "lancedb-bm25",
            "lancedb-semantic",
            "lancedb-hybrid",
        ]

    def test_default_stages_omits_lancedb_without_injected_adapter(self):
        # lumio-wiki cannot import lumio-lancedb, so no adapter => base ladder
        # only, even when an index dir is supplied (ADR-0010 dependency guard).
        stages = ev.default_stages(lancedb_index_dir=Path("/tmp/x"), lancedb_adapter=None)
        names = [s.name for s in stages]
        assert names == ["zero-index-lexical", "graph-expansion"]


# ---------------------------------------------------------------------------
# Deterministic embedder.
# ---------------------------------------------------------------------------


class TestDeterministicHashEmbedder:
    def test_same_input_same_output_across_instances(self):
        a = ev.DeterministicHashEmbedder()
        b = ev.DeterministicHashEmbedder()
        va = a.embed(["alpha beta"])[0]
        vb = b.embed(["alpha beta"])[0]
        assert va == vb

    def test_dimension_and_model_info(self):
        emb = ev.DeterministicHashEmbedder(dimension=32)
        assert len(emb.embed(["x"])[0]) == 32
        assert emb.model_info.dimension == 32
        assert emb.model_info.name

    def test_shared_tokens_are_more_similar_than_disjoint(self):
        emb = ev.DeterministicHashEmbedder()
        v_share = emb.embed(["alpha beta"])[0]
        v_near = emb.embed(["alpha beta gamma"])[0]
        v_far = emb.embed(["zzz qqq www"])[0]

        def cos(a, b):
            return sum(x * y for x, y in zip(a, b, strict=True))

        assert cos(v_share, v_near) > cos(v_share, v_far)

    def test_synonyms_collapse_paraphrases(self):
        emb = ev.DeterministicHashEmbedder(synonyms={"onboarding": "ingestion"})
        v_on = emb.embed(["onboarding"])[0]
        v_in = emb.embed(["ingestion"])[0]
        assert v_on == v_in


# ---------------------------------------------------------------------------
# evaluate() report structure.
# ---------------------------------------------------------------------------


class TestEvaluate:
    def _kb(self):
        hub = _page("Hub", "term alpha", rels=[Relationship(target="Sat", type="relates-to")])
        sat = _page("Sat", "term only")
        dec = _page("Dec", "term alpha beta gamma")
        return KnowledgeBase(root=Path("."), pages=[hub, sat, dec])

    def test_report_marks_graph_stage_skipped_for_unseeded_queries(self):
        kb = self._kb()
        gs = ev.GoldSet(
            name="t",
            queries=(
                ev.GoldQuery("term", frozenset({"Hub"})),  # no seeds -> graph N/A
            ),
        )
        report = ev.evaluate(kb, gs)
        q = report.queries[0]
        assert "zero-index-lexical" in q.per_stage
        assert "graph-expansion" in q.skipped_stages
        assert "not applicable" in q.skipped_stages["graph-expansion"]

    def test_report_serializes_to_dict_and_table(self):
        kb = self._kb()
        gs = ev.GoldSet(name="t", queries=(ev.GoldQuery("term", frozenset({"Hub"})),))
        report = ev.evaluate(kb, gs)
        d = report.to_dict()
        assert d["gold_set"] == "t"
        assert d["gold_set_size"] == 1
        assert any(s["name"] == "zero-index-lexical" for s in d["stages"])
        table = report.to_table()
        assert "recall@5" in table
        assert "zero-index-lexical" in table
