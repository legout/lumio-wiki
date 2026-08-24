"""Ontology evaluation gate (issue #173).

Runs the versioned ontology gold set over the committed ontology corpus
(``eval/ontology_corpus``) through the public ``KnowledgeBase`` seams and
asserts the issue's evaluation acceptance criterion: the report measures
only what actually ran — exact match rates for entity resolution,
accepted-edge traversal, canonical/discovery separation, and page-title
recall — and discloses corpus, mode, warm-up, fallback, and what is
deliberately not measured. No LLM-as-judge, no answer-quality or entailment
scoring anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lumio_wiki import ontology_eval
from lumio_wiki.cli import main as cli_main
from lumio_wiki.knowledge_base import load_knowledge_base

EVAL_DIR = Path(__file__).parent
CORPUS = EVAL_DIR / "ontology_corpus"
GOLD_SET = EVAL_DIR / "ontology_gold_set.yaml"


@pytest.fixture(scope="module")
def corpus_kb():
    kb, report = load_knowledge_base(CORPUS)
    assert report.is_valid, [i.message for i in report.issues]
    return kb


@pytest.fixture(scope="module")
def gold() -> ontology_eval.OntologyGoldSet:
    return ontology_eval.load_ontology_gold_set(GOLD_SET)


def test_every_area_measures_perfectly_on_the_corpus(corpus_kb, gold):
    """The committed corpus and gold set agree on every measured row."""
    report = ontology_eval.evaluate_ontology(corpus_kb, gold)
    assert report.entity_resolution.n == 6 and report.entity_resolution.correct == 6
    assert report.title_recall.n == 5 and report.title_recall.correct == 5
    assert report.edge_traversal.n == 9 and report.edge_traversal.correct == 9
    assert report.scope_separation.n == 3 and report.scope_separation.correct == 3
    assert report.all_pass


def test_two_runs_are_identical(corpus_kb, gold):
    """Deterministic: no provider, no network, no sampling."""
    first = ontology_eval.evaluate_ontology(corpus_kb, gold)
    second = ontology_eval.evaluate_ontology(corpus_kb, gold)
    assert first.to_dict() == second.to_dict()


def test_a_wrong_expectation_is_a_measured_failure_not_a_crash(corpus_kb, tmp_path):
    """The gate reports degraded metrics truthfully instead of hiding them."""
    wrong = tmp_path / "wrong_gold_set.yaml"
    wrong.write_text(
        "name: wrong-v1\n"
        "edge_traversal:\n"
        '  - source: "Lumio"\n'
        "    scope: canonical\n"
        "    direction: outgoing\n"
        '    expect: ["Sage Wiki"]  # wrong: disputed claims never traverse\n',
        encoding="utf-8",
    )
    report = ontology_eval.evaluate_ontology(
        corpus_kb, ontology_eval.load_ontology_gold_set(wrong)
    )
    assert report.edge_traversal.n == 1 and report.edge_traversal.correct == 0
    assert not report.all_pass
    assert report.edge_traversal.failures, "the failure must name the row"


def test_report_discloses_corpus_mode_warm_up_and_fallback(corpus_kb, gold):
    """AC: evaluation reports only measured metrics and discloses its setup."""
    report = ontology_eval.evaluate_ontology(corpus_kb, gold)
    assert report.corpus  # the corpus is named
    assert "zero-index" in report.mode
    assert "no model" in report.mode
    assert "no warm-up" in report.warm_up
    assert "fallback" in report.fallback
    assert "answer quality" in report.not_measured
    # The table and the JSON both carry the disclosure.
    table = report.to_table()
    for line in ("mode:", "warm-up:", "fallback:", "not measured:"):
        assert line in table
    payload = report.to_dict()
    assert set(payload["disclosure"]) == {"mode", "warm_up", "fallback", "not_measured"}


def test_empty_gold_set_sections_measure_as_vacuously_true(tmp_path, corpus_kb):
    empty = tmp_path / "empty_gold_set.yaml"
    empty.write_text("name: empty-v1\n", encoding="utf-8")
    report = ontology_eval.evaluate_ontology(
        corpus_kb, ontology_eval.load_ontology_gold_set(empty)
    )
    assert report.all_pass
    assert all(a.n == 0 for a in (
        report.entity_resolution, report.title_recall,
        report.edge_traversal, report.scope_separation,
    ))


def test_cli_eval_ontology_table_and_exit_code(corpus_kb, capsys):
    code = cli_main(["eval-ontology", str(CORPUS), "--gold-set", str(GOLD_SET)])
    out = capsys.readouterr().out
    assert code == 0
    assert "entity-resolution" in out
    assert "page-title-recall" in out
    assert "edge-traversal" in out
    assert "scope-separation" in out
    assert "fallback:" in out


def test_cli_eval_ontology_json_is_valid(corpus_kb, capsys):
    code = cli_main(
        ["eval-ontology", str(CORPUS), "--gold-set", str(GOLD_SET), "--json"]
    )
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["all_pass"] is True
    assert payload["disclosure"]["mode"].startswith("zero-index")
    assert payload["disclosure"]["not_measured"].startswith("answer quality")
