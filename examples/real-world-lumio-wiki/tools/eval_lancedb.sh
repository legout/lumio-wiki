#!/usr/bin/env bash
# "Later" stage comparison (issue #156): run the versioned gold set through the
# public retrieval seam twice — once lexical-only, once with the lumio-lancedb
# adapter layered on — and print a per-question side-by-side report.
#
# The shipped `lumio-wiki eval` is model-free recall@k measurement (issue #138):
# it reports, per retrieval stage, which Canonical Page Titles surface in the
# top-k for each gold query. There is no LLM-as-judge and no answer-quality or
# latency scoring — only retrieval-stage recall. This script turns the two
# machine-readable JSON reports into one human-readable, per-question
# side-by-side so a reviewer can see what the LanceDB stages change.
#
# Prerequisites (from this directory):
#   ./bootstrap.sh                 # base install: lumio-wiki[documents]
#   ./bootstrap-lancedb.sh         # layer lumio-lancedb onto the same venv
#   lumio-wiki setup ./knowledge-base
#   ...ingest + publish the sources (see README end-to-end walkthrough)...
#
# Outputs land in ./.eval/ (gitignored): lexical.json, lancedb.json, and
# comparison.md (also printed to stdout).
set -euo pipefail

EXAMPLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$EXAMPLE_DIR/.venv"
LUMIO="$VENV/bin/lumio-wiki"
GOLD="$EXAMPLE_DIR/evaluation/gold-v1.yaml"
KB="${LUMIO_KB_PATH:-$EXAMPLE_DIR/knowledge-base}"
OUT="${LUMIO_EVAL_OUT:-$EXAMPLE_DIR/.eval}"
# Opt-in: also run the deterministic semantic/hybrid stages (no provider, no
# Torch — `lumio-wiki eval --semantic` uses an offline hash embedder). Default
# off so the default comparison is lexical vs LanceDB BM25.
SEMANTIC="${LUMIO_EVAL_SEMANTIC:-0}"
LEX_JSON="$OUT/lexical.json"
LANCE_JSON="$OUT/lancedb.json"
REPORT="$OUT/comparison.md"

if [[ ! -x "$LUMIO" ]]; then
	echo "error: $LUMIO not found; run ./bootstrap.sh and ./bootstrap-lancedb.sh first" >&2
	exit 1
fi

# The lexical run does not need lancedb, but the whole point of this script is
# the lancedb-backed comparison — fail early with an actionable hint if the
# adapter is absent.
if ! "$VENV/bin/python" - <<'PY'
import importlib.util, sys
ok = all(importlib.util.find_spec(n) is not None for n in ("lumio_lancedb", "lancedb", "pyarrow"))
sys.exit(0 if ok else 1)
PY
then
	echo "error: lumio-lancedb adapter not importable; run ./bootstrap-lancedb.sh first" >&2
	exit 1
fi

if [[ ! -f "$GOLD" ]]; then
	echo "error: gold set not found at $GOLD" >&2
	exit 1
fi

if [[ ! -d "$KB" ]]; then
	echo "error: Knowledge Base not found at $KB; run 'lumio-wiki setup $KB' and publish first" >&2
	exit 1
fi

mkdir -p "$OUT"
export LUMIO_KB_PATH="$KB"

run_eval() {
	# run_eval <label> <json_out> <extra args...>
	local label="$1" json_out="$2"
	shift 2
	printf '\n=== %s ===\n' "$label"
	printf '$ lumio-wiki eval %s\n' "--gold-set $GOLD $*"
	# One run produces the JSON the formatter turns into the per-question and
	# aggregate tables; a second run would just rebuild the LanceDB index.
	"$LUMIO" eval --gold-set "$GOLD" --json "$@" >"$json_out"
}

timed() {
	# timed <start_var> <end_var> -- cmd...
	local start_var="$1" end_var="$2"
	shift 3
	local s e
	s=$(date +%s.%N)
	"$@"
	e=$(date +%s.%N)
	printf -v "$start_var" '%s' "$s"
	printf -v "$end_var" '%s' "$e"
}

lancedb_args=()
if [[ "$SEMANTIC" == "1" ]]; then
	# Demonstrate semantic recall that lexical search misses: collapse a few
	# paraphrases to a shared token for the deterministic hash embedder.
	lancedb_args=(--semantic
		--synonym salary=wages
		--synonym qualify=survey
		--synonym commissioned=installed)
fi

lex_start="" lex_end="" lance_start="" lance_end=""
timed lex_start lex_end -- run_eval "lexical (zero-index + graph, no lancedb)" "$LEX_JSON" --no-lancedb
lance_label="lancedb (BM25)"
if [[ "$SEMANTIC" == "1" ]]; then
	lance_label="lancedb (BM25 + semantic/hybrid)"
fi
timed lance_start lance_end -- run_eval "$lance_label" "$LANCE_JSON" "${lancedb_args[@]}"

lex_secs=$(awk -v s="$lex_start" -v e="$lex_end" 'BEGIN{printf "%.2f", e - s}')
lance_secs=$(awk -v s="$lance_start" -v e="$lance_end" 'BEGIN{printf "%.2f", e - s}')

export LEX_JSON LANCE_JSON REPORT GOLD LEX_SECS="$lex_secs" LANCE_SECS="$lance_secs" SEMANTIC

printf '\n=== Side-by-side comparison ===\n'
"$VENV/bin/python" - <<'PY'
import json
import os
import sys
from pathlib import Path

import msgspec

gold_path = Path(os.environ["GOLD"])
lex_path = Path(os.environ["LEX_JSON"])
lance_path = Path(os.environ["LANCE_JSON"])
report_path = Path(os.environ["REPORT"])
lex_secs = os.environ["LEX_SECS"]
lance_secs = os.environ["LANCE_SECS"]
semantic = os.environ.get("SEMANTIC", "0") == "1"

# Mirror retrieval_eval's gold-set structs so we read EVERY row (including the
# empty-relevant refusal rows load_gold_set drops) with the venv's msgspec.
class _GoldQueryYaml(msgspec.Struct):
	query: str
	relevant: list[str] = msgspec.field(default_factory=list)
	seeds: list[str] | None = None
	note: str = ""


class _GoldSetYaml(msgspec.Struct):
	version: int = 1
	name: str = ""
	description: str = ""
	queries: list[_GoldQueryYaml] = msgspec.field(default_factory=list)


gold = msgspec.yaml.decode(gold_path.read_text(encoding="utf-8"), type=_GoldSetYaml)
lex = json.loads(lex_path.read_text(encoding="utf-8"))
lance = json.loads(lance_path.read_text(encoding="utf-8"))

ks = lex.get("ks", [1, 3, 5])
k3 = 3 if 3 in ks else ks[-1]

lex_by_q = {q["query"]: q for q in lex.get("queries", [])}
lance_by_q = {q["query"]: q for q in lance.get("queries", [])}


def stage_outcome(run, query, stage):
	q = (lance_by_q if run == "lance" else lex_by_q).get(query)
	if q is None:
		return None
	return q.get("per_stage", {}).get(stage)


def fmt_titles(titles, limit=3):
	shown = titles[:limit]
	rest = f" (+{len(titles) - limit})" if len(titles) > limit else ""
	return ", ".join(shown) + rest if titles else "(none)"


def recall3(out):
	return out.get("recall_by_k", {}).get(str(k3)) if out else None


def fmt_recall(v):
	return f"{v:.2f}" if isinstance(v, (int, float)) else "  - "


def missed_titles(out, relevant):
	# Citation correctness at the recall@k level: which expected pages did NOT
	# surface in this stage's top-k3? Empty == every expected citation surfaced.
	if out is None:
		return None
	return [t for t in relevant if t not in out.get("retrieved", [])[:k3]]


lines = []
lines.append(f"# LanceDB Later comparison — {gold.name}")
lines.append("")
lines.append(
	"Side-by-side recall@%d at the Canonical Page Title level. `lex` = zero-index "
	"lexical (the base rung); `bm25` = LanceDB BM25 (the stage lumio-lancedb adds). "
	"`graph` = Discovery Graph expansion (runs for seeded queries)." % k3
)
if semantic:
	lines.append("`sem`/`hyb` = LanceDB semantic/hybrid via the deterministic offline hash embedder.")
lines.append("")
lines.append("## Per question")
lines.append("")

refusals = []
for row in gold.queries:
	q = row.query
	relevant = [r for r in row.relevant if r and r.strip()]
	if not relevant:
		refusals.append(row)
		continue
	lex_lex = stage_outcome("lex", q, "zero-index-lexical")
	lex_graph = stage_outcome("lex", q, "graph-expansion")
	lance_lex = stage_outcome("lance", q, "zero-index-lexical")
	bm25 = stage_outcome("lance", q, "lancedb-bm25")
	sem = stage_outcome("lance", q, "lancedb-semantic")
	hyb = stage_outcome("lance", q, "lancedb-hybrid")

	r_lex = recall3(lex_lex)
	r_bm25 = recall3(bm25)
	if isinstance(r_lex, (int, float)) and isinstance(r_bm25, (int, float)):
		delta = "improved" if r_bm25 > r_lex else ("regressed" if r_bm25 < r_lex else "same")
	else:
		delta = "n/a"
	missed_lex = missed_titles(lex_lex, relevant)
	missed_bm25 = missed_titles(bm25, relevant)

	lines.append(f"### {q}")
	lines.append(f"- expected relevant: {', '.join(relevant)}")
	if row.seeds:
		lines.append(f"- graph seeds: {', '.join(row.seeds)}")
	if row.note:
		lines.append(f"- note: {row.note}")
	lines.append(
		f"- **lex** recall@{k3}={fmt_recall(r_lex)} · top: {fmt_titles(lex_lex.get('retrieved', []) if lex_lex else [])}"
	)
	if lex_graph is not None:
		lines.append(
			f"- **graph** recall@{k3}={fmt_recall(recall3(lex_graph))} · top: {fmt_titles(lex_graph.get('retrieved', []))}"
		)
	lines.append(
		f"- **bm25** recall@{k3}={fmt_recall(r_bm25)} · top: {fmt_titles(bm25.get('retrieved', []) if bm25 else [])}"
	)
	if semantic:
		lines.append(
			f"- **sem** recall@{k3}={fmt_recall(recall3(sem))} · top: {fmt_titles(sem.get('retrieved', []) if sem else [])}"
		)
		lines.append(
			f"- **hyb** recall@{k3}={fmt_recall(recall3(hyb))} · top: {fmt_titles(hyb.get('retrieved', []) if hyb else [])}"
		)
	lines.append(f"- bm25 vs lex: **{delta}**")
	if missed_lex is not None or missed_bm25 is not None:
		lines.append(
			f"- citation missed (top-{k3}): lex={missed_lex or 'none'}, bm25={missed_bm25 or 'none'}"
		)
	lines.append("")

if refusals:
	lines.append("## Refusal questions (not scoreable by recall@k)")
	lines.append("")
	for row in refusals:
		lines.append(f"- **{row.query}**")
		if row.note:
			lines.append(f"  - {row.note}")
	lines.append(
		"\nThese have empty `relevant` in the gold set (no supporting page exists). "
		"`lumio-wiki eval` drops empty-relevant rows, so recall@k cannot score a "
		"correct refusal. A real refusal/answer-quality harness does not exist yet.\n"
	)

lines.append("## Aggregate recall@k per stage")
lines.append("")


def stage_table(run, label):
	lines.append(f"### {label}")
	header = " | ".join(f"recall@{k}" for k in ks)
	lines.append(f"| stage | n | {header} |")
	lines.append("|---|---|" + "---|" * len(ks))
	for s in run.get("stages", []):
		if not s.get("available"):
			continue
		recalls = s.get("mean_recall_by_k", {})
		cells = " | ".join(f"{recalls.get(str(k), 0.0):.3f}" for k in ks)
		lines.append(f"| {s['name']} | {s.get('n', 0)} | {cells} |")
	skipped = [s["name"] for s in run.get("stages", []) if not s.get("available")]
	if skipped:
		lines.append(f"\nSkipped (unavailable): {', '.join(skipped)}")
	lines.append("")


stage_table(lex, "Lexical run (--no-lancedb)")
stage_table(lance, "LanceDB run")

lines.append("## Latency")
lines.append("")
lines.append(
	"Wall-clock for the whole `lumio-wiki eval` run (gold set, all stages), not "
	"per-question — the shipped harness exposes no per-query latency.\n"
)
lines.append(f"- lexical run: **{lex_secs}s**")
lines.append(f"- lancedb run: **{lance_secs}s**")

report = "\n".join(lines) + "\n"
report_path.write_text(report, encoding="utf-8")
sys.stdout.write(report)
PY

printf '\nReports written to %s (lexical.json, lancedb.json, comparison.md)\n' "$OUT"
