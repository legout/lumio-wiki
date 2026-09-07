"""Retrieval gold-set evaluation gate (issue #138).

This is the CI gate for retrieval-touching changes. It runs the versioned gold
set over the committed synthetic fixture Knowledge Base through the PUBLIC
retrieval seam (:meth:`KnowledgeBase.retrieve`) and asserts the issue's three
acceptance criteria:

* **AC1 — deterministic, no provider, no network.** Two runs produce identical
  reports; the base layer (zero-index + graph) needs no LanceDB and no embedder.
* **AC2 — obvious queries pass.** The seeded "obviously relevant" lexical query
  set recalls near-perfectly on the current pipeline.
* **AC3 — a degraded stage measurably drops recall@k.** Disabling Discovery
  Graph expansion drops recall@5 on graph-dependent queries versus the
  graph-enabled run.

LanceDB stages (BM25 / semantic / hybrid) are exercised when the adapter is
installed; they are skipped otherwise via ``pytest.importorskip``. The
deterministic hash embedder keeps the semantic stage offline.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest
from lumio_wiki import retrieval_eval as ev
from lumio_wiki.cli import main as cli_main

K = (1, 3, 5)

# ---------------------------------------------------------------------------
# AC1: deterministic, no provider, no network.
# ---------------------------------------------------------------------------


def test_ac1_base_layer_is_deterministic(fixture_kb, gold_set):
    """Two evaluations of the base layer produce identical recall reports."""
    base_stages = [ev.ZeroIndexLexicalStage(), ev.GraphExpansionStage()]
    report_a = ev.evaluate(fixture_kb, gold_set, stages=base_stages, ks=K)
    report_b = ev.evaluate(fixture_kb, gold_set, stages=base_stages, ks=K)
    assert report_a.to_dict() == report_b.to_dict()

    # No embedder, no index_dir required to construct or call these stages.


# ---------------------------------------------------------------------------
# AC2: obvious queries pass on the current pipeline.
# ---------------------------------------------------------------------------


def _lexical_only_queries(report: ev.EvalReport):
    """Gold queries with no graph seeds (pure lexical relevance)."""
    return [q for q in report.queries if "graph-expansion" not in q.per_stage]


def test_ac2_obvious_lexical_queries_recall_well(fixture_kb, gold_set):
    report = ev.evaluate(fixture_kb, gold_set, ks=K)
    lex = _lexical_only_queries(report)
    assert lex, "gold set must contain lexical-only (seedless) queries"
    recalls = [q.per_stage["zero-index-lexical"].recall_by_k[5] for q in lex]
    mean_recall = statistics.mean(recalls)
    # "Obviously relevant" queries should recall very well on the current pipeline.
    assert mean_recall >= 0.9, f"lexical-only recall@5 too low: {mean_recall:.3f}"
    # And every such query must surface at least one relevant page.
    assert all(r > 0.0 for r in recalls), "a lexical-only query returned nothing relevant"


# ---------------------------------------------------------------------------
# AC3: a degraded stage (graph expansion disabled) drops recall@k.
# ---------------------------------------------------------------------------


def _graph_applicable_queries(report: ev.EvalReport):
    return [q for q in report.queries if "graph-expansion" in q.per_stage]


def test_ac3_graph_expansion_measurably_lifts_recall(fixture_kb, gold_set):
    """Graph-dependent queries recall better WITH graph expansion than without."""
    report = ev.evaluate(fixture_kb, gold_set, ks=K)
    graph_qs = _graph_applicable_queries(report)
    assert graph_qs, "gold set must contain graph-dependent (seeded) queries"

    zero_recall = [q.per_stage["zero-index-lexical"].recall_by_k[5] for q in graph_qs]
    graph_recall = [q.per_stage["graph-expansion"].recall_by_k[5] for q in graph_qs]

    mean_zero = statistics.mean(zero_recall)
    mean_graph = statistics.mean(graph_recall)
    # Graph expansion must measurably beat graph-disabled (zero-index) on the
    # queries it applies to — the core regression-detection claim.
    assert mean_graph > mean_zero, (
        f"graph expansion did not lift recall@5: graph={mean_graph:.3f} <= zero={mean_zero:.3f}"
    )
    helped = sum(1 for a, b in zip(zero_recall, graph_recall, strict=True) if b > a)
    assert helped >= len(graph_qs) // 2, (
        f"graph expansion helped only {helped}/{len(graph_qs)} queries"
    )
    # And graph never makes any query worse (eligibility only narrows the pool).
    worse = [
        q.query
        for q in graph_qs
        if q.per_stage["graph-expansion"].recall_by_k[5]
        < q.per_stage["zero-index-lexical"].recall_by_k[5]
    ]
    assert not worse, f"graph expansion regressed queries: {worse}"


# ---------------------------------------------------------------------------
# LanceDB stages (when installed).
# ---------------------------------------------------------------------------


@pytest.fixture()
def lancedb_report(fixture_kb, gold_set, tmp_path):
    pytest.importorskip("lancedb")
    adapter = ev.load_lancedb_adapter()
    assert adapter is not None, "lumio-lancedb adapter should load when lancedb is installed"
    embedder = ev.DeterministicHashEmbedder(
        synonyms={"onboarding": "ingestion", "authorization": "access"}
    )
    return ev.evaluate(
        fixture_kb,
        gold_set,
        ks=K,
        lancedb_index_dir=tmp_path / "lance",
        embedder=embedder,
        lancedb_adapter=adapter,
    )


def test_lancedb_stages_available_and_measured(lancedb_report):
    names = {s.name for s in lancedb_report.stages}
    assert {"lancedb-bm25", "lancedb-semantic", "lancedb-hybrid"} <= names
    for stage in lancedb_report.stages:
        if stage.name.startswith("lancedb"):
            assert stage.available
            assert stage.n == lancedb_report.gold_set_size
            # Each LanceDB stage must produce non-trivial aggregate recall@5.
            assert stage.mean_recall_by_k[5] > 0.0


def test_lancedb_semantic_catches_synonym_paraphrase(lancedb_report):
    """The deterministic embedder's synonym map lifts semantic recall for the paraphrase probes."""
    probe = next(q for q in lancedb_report.queries if q.query == "onboarding pipeline")
    sem = probe.per_stage["lancedb-semantic"].recall_by_k[5]
    assert sem > 0.0, "synonym-collapsed semantic stage must recall the ingestion cluster"


# ---------------------------------------------------------------------------
# CLI surface: `lumio-wiki eval`.
# ---------------------------------------------------------------------------


def test_cli_eval_prints_recall_table(capsys, fixture_kb):
    rc = cli_main(
        [
            "eval",
            str(Path(__file__).parent / "fixture_kb"),
            "--gold-set",
            str(Path(__file__).parent / "gold_set.yaml"),
            "--no-lancedb",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "recall@5" in out
    assert "zero-index-lexical" in out
    assert "graph-expansion" in out


def test_cli_eval_json_is_valid(capsys, fixture_kb, tmp_path):
    pytest.importorskip("lancedb")
    rc = cli_main(
        [
            "eval",
            str(Path(__file__).parent / "fixture_kb"),
            "--gold-set",
            str(Path(__file__).parent / "gold_set.yaml"),
            "--index-dir",
            str(tmp_path / "lance"),
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["gold_set_size"] > 0
    assert any(s["name"] == "lancedb-bm25" for s in data["stages"])
