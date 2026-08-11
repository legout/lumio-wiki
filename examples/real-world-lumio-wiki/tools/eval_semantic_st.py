"""Spike: run the gold set through the retrieval stages with a REAL embedding model.

`lumio-wiki eval --semantic` hardcodes the DeterministicHashEmbedder (a bag of
hash-seeded random token vectors), so the shipped CLI cannot measure a learned
embedder — see issue #158 for the CLI parity fix. The engine itself accepts
any structural Embedder, so this script plugs sentence-transformers
(``all-MiniLM-L6-v2`` by default) directly into ``retrieval_eval.evaluate``
and prints the same per-stage recall@k attribution.

Prerequisites (from the example directory):

    uv pip install --python .venv/bin/python '<repo>/packages/lumio-lancedb[embeddings]'

The model downloads from HuggingFace once (~90 MB for MiniLM); runs are fully
offline afterwards (HF caches under ~/.cache/huggingface).

Usage:

    python tools/eval_semantic_st.py                       # defaults
    python tools/eval_semantic_st.py --model BAAI/bge-small-en-v1.5

Output: prints the stage table + per-question semantic/hybrid tops, and writes
.eval/semantic-st.json (same shape as `lumio-wiki eval --json`).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "all-MiniLM-L6-v2"


class SentenceTransformersEmbedder:
    """Structural ``lumio_wiki.embeddings.Embedder`` backed by sentence-transformers.

    Mirrors ``_LocalEmbedder`` in lumio_wiki/cli.py (kept framework-free at the
    Core SDK layer; the heavy import happens lazily here).
    """

    def __init__(self, model_name: str) -> None:
        from lumio_wiki.records import EmbeddingModelInfo  # type: ignore[import-not-found]

        sentence_transformers = importlib.import_module("sentence_transformers")
        try:
            self._model = sentence_transformers.SentenceTransformer(model_name)
            dimension = getattr(self._model, "get_sentence_embedding_dimension", lambda: 384)()
            self._info = EmbeddingModelInfo(model_name, int(dimension))
        except Exception as exc:
            raise SystemExit(
                f"error: could not load embedding model {model_name!r}: {exc}\n"
                "The model downloads from HuggingFace on first use; check the name "
                "and network access, then retry."
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    @property
    def model_info(self):
        return self._info


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the gold set through the retrieval stages with a real embedding model."
    )
    parser.add_argument(
        "--kb", default=os.environ.get("LUMIO_KB_PATH", str(ROOT / "knowledge-base"))
    )
    parser.add_argument("--gold", default=str(ROOT / "evaluation" / "gold-v1.yaml"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", default=str(ROOT / ".eval" / "semantic-st.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if importlib.util.find_spec("sentence_transformers") is None:
        print(
            "error: sentence-transformers not installed; run:\n"
            "  uv pip install --python .venv/bin/python "
            "'<repo>/packages/lumio-lancedb[embeddings]'",
            file=sys.stderr,
        )
        return 1

    from lumio_wiki import load_knowledge_base, retrieval_eval  # type: ignore[import-not-found]

    kb, report = load_knowledge_base(args.kb)
    if not report.is_valid:
        print(f"error: Knowledge Base at {args.kb} is invalid:\n{report}", file=sys.stderr)
        return 1

    gold_set = retrieval_eval.load_gold_set(args.gold)
    adapter = retrieval_eval.load_lancedb_adapter()
    if adapter is None:
        print(
            "error: lumio-lancedb adapter not importable; run ./bootstrap-lancedb.sh",
            file=sys.stderr,
        )
        return 1

    print(f"embedding model: {args.model} (real, learned — not the hash stand-in)")
    embedder = SentenceTransformersEmbedder(args.model)
    print(f"resolved: {embedder.model_info.name}, dim={embedder.model_info.dimension}")

    result = retrieval_eval.evaluate(kb, gold_set, embedder=embedder, lancedb_adapter=adapter)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

    print()
    print(result.to_table())

    # Per-question semantic/hybrid detail: which expected titles surfaced where.
    ks = result.ks
    k3 = 3 if 3 in ks else ks[-1]
    gold_by_query = {q.query: q for q in gold_set.queries}
    print(f"\nPer-question semantic/hybrid (recall@{k3}):")
    for outcome in result.queries:
        gold_query = gold_by_query.get(outcome.query)
        relevant = sorted(gold_query.relevant) if gold_query else []
        for stage_name in ("lancedb-bm25", "lancedb-semantic", "lancedb-hybrid"):
            stage = outcome.per_stage.get(stage_name)
            if stage is None:
                continue
            recall = stage.recall_by_k.get(k3, 0.0)
            tops = ", ".join(stage.retrieved[:k3]) or "(none)"
            print(f"- {outcome.query[:64]}")
            print(f"    {stage_name}: recall@{k3}={recall:.2f} · top: {tops}")
        if relevant:
            print(f"    expected: {', '.join(relevant)}")

    print(f"\nJSON report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
