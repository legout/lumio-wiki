# Retrieval Evaluation Harness (issue #138)

Lumio had retrieval *inspection* (the Retrieval Trace) but no retrieval
*measurement*. This directory is the defensible, deterministic gate that turns
retrieval tuning from vibes into engineering: a versioned gold set run through
the **public retrieval seam** (`KnowledgeBase.retrieve`), reported as **recall@k
per pipeline stage**.

## Layout

```
eval/
  fixture_kb/            # synthetic Knowledge Base (24 Compiled Pages, 6 topical clusters)
  gold_set.yaml          # versioned gold set: ~50 queries → expected relevant Canonical Page Titles
  conftest.py            # shared fixtures (loads fixture_kb + gold_set)
  test_retrieval_eval_gate.py   # the CI gate (acceptance criteria AC1–AC3)
  ontology_corpus/       # entity-claim ontology corpus (issue #173)
  ontology_gold_set.yaml # ontology gold set (four typed sections)
  test_ontology_eval_gate.py    # the ontology gate (issue #173)
  README.md              # this file
```

## Ontology evaluation (issue #173)

`eval-ontology` is the model-free gate for the entity-claim ontology. It runs
`eval/ontology_gold_set.yaml` over `eval/ontology_corpus` — one behavioural
corpus covering entity and literal Claims, an inverse Predicate pair,
disputed/superseded lifecycle, discovery-only Extracted References, an Entity
Merge redirect, and visibility classes — and reports **exact match rates**
for four areas: entity resolution, page-title recall, accepted-edge
traversal, and canonical/discovery scope separation. The report discloses
corpus, mode, warm-up, and fallback, and never claims answer quality or
entailment (no LLM-as-judge).

```bash
uv run pytest -q eval/test_ontology_eval_gate.py     # the CI gate
uv run lumio-wiki eval-ontology eval/ontology_corpus --gold-set eval/ontology_gold_set.yaml
uv run lumio-wiki eval-ontology eval/ontology_corpus --gold-set eval/ontology_gold_set.yaml --json
```

The same corpus drives `packages/lumio-lancedb/tests/test_ontology_parity.py`:
the identical traversal/retrieval assertions run against the zero-index
MessagePack projection and the LanceDB `entities`/`graph_edges` projections.

The harness library itself lives in the `lumio-wiki` package:
`lumio_wiki.retrieval_eval`.

## Stages measured

| stage               | always? | needs                | what it proves                                  |
|---------------------|---------|----------------------|-------------------------------------------------|
| `zero-index-lexical`| yes     | nothing              | base lexical ranking (the control rung)         |
| `graph-expansion`   | yes\*   | nothing              | Discovery Graph eligibility lifts weakly-relevant pages into top-k |
| `lancedb-bm25`      | no      | `lumio-lancedb`      | BM25 full-text ranking over a derived index     |
| `lancedb-semantic`  | no      | `lumio-lancedb` + embedder | cosine vector search                       |
| `lancedb-hybrid`    | no      | `lumio-lancedb` + embedder | reciprocal-rank-fusion of lexical + semantic |

\* Graph expansion is measured only for queries that declare graph `seeds`.

## Acceptance criteria (the gate)

* **AC1 — deterministic, no provider, no network.** Two base-layer runs produce
  identical reports. Zero-index + graph need no LanceDB and no embedder.
* **AC2 — obvious queries pass.** The seedless ("obviously relevant") lexical
  query set recalls ≥ 0.9 at k=5 on the current pipeline.
* **AC3 — a degraded stage measurably drops recall@k.** Disabling Discovery
  Graph expansion drops recall@5 on graph-dependent queries versus the
  graph-enabled run.

## Running

```bash
# The full gate (part of the default suite)
uv run pytest -q eval
uv run pytest -q                       # whole workspace, eval included

# The CLI. By default it runs every INSTALLED stage (zero-index + graph, plus
# LanceDB BM25 when lumio-lancedb is present) so the table shows what each
# stage buys. Pass --no-lancedb for a base-layer-only run.
uv run lumio-wiki eval eval/fixture_kb --gold-set eval/gold_set.yaml
uv run lumio-wiki eval eval/fixture_kb --gold-set eval/gold_set.yaml --json
uv run lumio-wiki eval eval/fixture_kb --gold-set eval/gold_set.yaml --no-lancedb
uv run lumio-wiki eval eval/fixture_kb --gold-set eval/gold_set.yaml \
    --semantic --synonym onboarding=ingestion
```

## Two gold sets

* **`gold_set.yaml`** — the deterministic synthetic fixture set. This is the CI
  gate: no provider, no network, no LanceDB at the base layer.
* **`project_wiki_gold_set.yaml`** — a real-world starter set over the Lumio
  project Knowledge Base (the `.wiki`). It is the issue's "start with the
  project .wiki KB plus one synthetic fixture KB" companion: a small, verified
  baseline humans grow as the project KB evolves. Run it against the real KB:

  ```bash
  uv run lumio-wiki eval "$LUMIO_KB_PATH" --gold-set eval/project_wiki_gold_set.yaml
  ```

## Growing the gold set

Edit `gold_set.yaml` and add rows:

```yaml
queries:
  - query: "natural language query"
    relevant:
      - "Exact Canonical Page Title"
      - "Another Page"
    seeds:                 # optional: only for graph-dependent queries
      - "Seed Page Title"
    note: "why these are relevant"
```

The synthetic fixture KB is intentionally small and tuned so the dynamics are
visible (decoys outrank weakly-matching relevant pages without graph
expansion). Add real queries against a real Knowledge Base by pointing the CLI
at it with a co-located `gold_set.yaml`.

## Model-free by design

No LLM-as-judge and no answer-quality scoring — retrieval-stage recall only.
The semantic/hybrid stages use a deterministic hash embedder
(`DeterministicHashEmbedder`) so they run fully offline; production deployments
pass a real `Embedder` through the same seam.
