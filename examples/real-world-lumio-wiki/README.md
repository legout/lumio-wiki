# Real-world `lumio-wiki` coding-agent trial

This folder is a staged, realistic test of Lumio's progressive product layers:

1. **Now:** `lumio-wiki[documents]` only — portable Knowledge Base setup, mixed-format ingestion, proposal review, publication, validation, and zero-index retrieval through a coding agent.
2. **Later:** add `lumio-lancedb` and compare enhanced retrieval against the same published Knowledge Base. Tracked in #156.
3. **Finally:** add the full `lumio` application and exercise the same knowledge through Lumio's browser UI. Tracked in #157.

The corpus describes **Atlas Heatworks**, a fictional heat-pump installer. It is synthetic, non-sensitive, and deliberately shaped like a small operational corpus: company context, product data, support policy, an installation procedure, an annual impact report, and an internal office-format pricing reference. Facts overlap across documents so the coding agent must reconcile entities, provenance, and Relationships rather than merely copying isolated files.

## Folder layout

```text
.
├── .venv/                 # isolated install; generated and ignored
├── knowledge-base/        # create this through the coding agent; ignored
├── sources/               # raw Knowledge Sources to ingest
│   ├── company-overview.md
│   ├── customer-support-policy.txt
│   ├── product-catalog.html
│   ├── installation-handbook.docx
│   ├── 2025-impact-report.pdf
│   ├── aster-pricing-deck.pptx
│   └── aster-careplus-matrix.xlsx
├── evaluation/
│   ├── questions.md       # questions to ask after publication; do not ingest
│   └── gold-v1.yaml       # "Later" recall@k gold set (query -> relevant titles)
├── tools/
│   ├── generate_binary_sources.py
│   ├── validate_sources.py
│   ├── run_maintenance.sh
│   ├── stage_dream_repair.sh
│   ├── install_skill.sh
│   └── eval_lancedb.sh    # "Later" lexical-vs-lancedb comparison (#156)
├── bootstrap.sh
└── bootstrap-lancedb.sh   # "Later" stage: layer lumio-lancedb onto the venv
```

## Installation

From this directory:

```bash
./bootstrap.sh
source .venv/bin/activate
lumio-wiki doctor
python tools/validate_sources.py
bash tools/run_maintenance.sh
```

`bootstrap.sh` installs the current repository's `lumio-wiki[documents]` package into this folder's isolated virtual environment. It does **not** install `lumio-lancedb` or the full `lumio` application. The `documents` extra remains part of the `lumio-wiki` distribution and is needed for PDF, DOCX, PPTX, XLSX, and HTML conversion. Issue #154 / ADR-0018 routes office formats through AnyDoc (`firecrawl-anydoc==0.1.3`); the base `lumio-wiki` wheel stays AnyDoc-free and the extra is the only place AnyDoc is imported.

## Start the coding-agent trial

Start Pi, Codex, Claude Code, or another coding agent **from this directory**.
First run the canonical first-run command yourself (or have the agent run it):

```bash
lumio-wiki setup ./knowledge-base
```

`setup` is the canonical first run: it creates the Knowledge Base at
`./knowledge-base`, writes `.env` (`LUMIO_KB_PATH`), and writes/updates
`AGENTS.md` with the retrieval-ladder protocol so a restarted or new session
can locate, retrieve from, cite, ingest into, and maintain the Knowledge Base.
`lumio-wiki init ./knowledge-base` is the lower-level KB-only operation and
skips that project wiring. Skill installation stays explicit — run
`bash tools/install_skill.sh` (or pass an explicit agent, e.g.
`bash tools/install_skill.sh claude-code`) to copy the packaged workflow
into the target agent's conventional skill directory, then **restart the
agent or start a new session** so the skill is discovered.

Then ask the agent:

> Ingest every file in `./sources` into the Lumio Knowledge Base at `./knowledge-base` using the installed `lumio-wiki` CLI and its coding-agent workflow. Treat me as the Maintainer: keep every mutation proposal-first, show me each proposal's validation and blast radius, and wait for my approval before publishing. Do not install `lumio-lancedb` or the full `lumio` application.

**Restart / new-session check:** after a restart, the agent should resolve the
Knowledge Base with no path argument — `lumio-wiki validate` (no `<kb>`)
succeeds because the CLI reads `LUMIO_KB_PATH` from `.env`. If you installed a
skill, confirm discovery ran in the new session before relying on it.

This repository already provides the Lumio project instructions to agents started inside it. For a truly standalone test outside this repository, use the installed CLI's `skill path`, `skill protocol`, or `bash tools/install_skill.sh` to expose the packaged workflow to the chosen agent.

## Maintenance drill

After the Knowledge Base is published, the example ships a maintenance tool
belt that exercises the read-only and proposal-first surfaces without
mutating anything by default:

```bash
bash tools/run_maintenance.sh        # validate, health, lint, cross-link, dream, source list
bash tools/stage_dream_repair.sh     # dream --stage --limit 3, prints proposal IDs to review
DREAM_LIMIT=5 bash tools/stage_dream_repair.sh
```

`run_maintenance.sh` is the always-safe read-only pass — run it after any
ingest. `stage_dream_repair.sh` is the proposal-first drill: it stages up to
`$DREAM_LIMIT` (default 3) reviewable repairs and prints the proposal IDs the
agent should walk through `proposal inspect` → `proposal validate` →
`publish` (or `discard`). These scripts are the manual stand-in for the
coding agent's daily `lumio-wiki dream --stage --limit 3` workflow described
in `AGENTS.md`.

## End-to-end walkthrough (manual)

The canonical first-run + ingest + maintenance flow, all commands run from
this directory. Anything that touches the Knowledge Base is proposal-first.

```bash
# 1. Install into an isolated venv (installs lumio-wiki[documents]).
./bootstrap.sh
source .venv/bin/activate

# 2. Confirm install + AnyDoc wiring.
lumio-wiki doctor
python tools/validate_sources.py

# 3. First run: create the Knowledge Base, write .env and AGENTS.md.
lumio-wiki setup ./knowledge-base

# 4. (Optional) install the packaged Agent Skill for the chosen harness.
bash tools/install_skill.sh            # defaults to pi
bash tools/install_skill.sh claude-code

# 5. (Optional) ingest every committed fixture.
# Each ingest stages a Compiled Page bound to a Source identity; review,
# validate, and publish each proposal in turn. See "Compiled Page schema"
# below for the required frontmatter.
for src in sources/*; do
  base=$(basename "$src")
  sid="${base%.*}"
  page="/tmp/${sid}.md"
  cat > "$page" <<EOF
---
lumio:
  artifact: compiled-page
  version: 1
title: "$sid"
visibility: "public"
category: "references"
type: "$sid"
lifecycle: "approved"
aliases: ["$sid"]
tags: ["atlas-heatworks"]
sources:
  - id: "$sid"
summary: "Atlas Heatworks — $sid compiled page (issue #154 fixture)."
durability_rationale: "Fictional reference: durable until the next controlled revision."
relationships:
  - target: "company-overview"
    type: "relates-to"
synthetic: true
---

# $sid

Compiled from $base. Marker text intentionally minimal; the example tests
routing, validation, and retrieval, not content authoring.
EOF
  lumio-wiki ingest "$src" --compiled-page "$page" --source-id "$sid"
done

# 6. Walk staged proposals.
lumio-wiki proposal list
lumio-wiki proposal validate <id>
lumio-wiki publish <id>             # or: lumio-wiki discard <id>

# 7. Read-only maintenance pass.
bash tools/run_maintenance.sh

# 8. Proposal-first repair drill.
bash tools/stage_dream_repair.sh
```

### Compiled Page schema (categorized Knowledge Base)

A categorized Knowledge Base rejects any proposal that is missing one of the
required fields below. The Distiller (coding agent) must populate every field
when authoring a Compiled Page; placeholder text is acceptable for the
example, but the field itself must be present.

| Field                 | Required | Notes                                                                                                                                       |
|-----------------------|----------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `lumio.artifact`      | yes      | Must be `compiled-page` and `lumio.version: 1`.                                                                                             |
| `title`               | yes      | Canonical Page Title; used as the file name and the page identity.                                                                          |
| `visibility`          | yes      | `public` or `internal`; the example uses `public`.                                                                                          |
| `category`            | yes      | Must be one of the configured Content Categories in `knowledge-base/lumio.yaml` (`references`, `procedures`, `entities`, `concepts`, ...). |
| `type`                | yes      | Free-form short label; cannot be empty.                                                                                                     |
| `lifecycle`           | yes      | `approved` for published content; `draft` for work-in-progress proposals.                                                                   |
| `aliases`             | yes      | List of alternate titles used by retrieval.                                                                                                 |
| `tags`                | yes      | List of taxonomy tags.                                                                                                                      |
| `sources`             | yes      | List of `id` entries; each `id` must match a registered Knowledge Source.                                                                   |
| `summary`             | yes      | One-paragraph description; surfaced in `lumio-wiki search` results.                                                                        |
| `durability_rationale`| yes      | Why this page is durable (long-lived, controlled, etc.). The Distiller never fabricates one.                                                |
| `relationships`       | optional | List of `{target, type}` entries; allowed `type` values are `contradicts`, `derived-from`, `extends`, `implements`, `relates-to`, `replaces`, `uses`. Unknown types emit a warning but do not block. |
| `synthetic`           | optional | `true` for example / fixture content; required for pages that are not extracted from a source.                                             |

`tools/install_skill.sh` is idempotent. The packaged skill discovers the KB
through `LUMIO_KB_PATH`; after restart it can answer evaluation questions
without any path argument.

## Later comparison: `lumio-lancedb` retrieval (#156)

After the Knowledge Base is published with `lumio-wiki[documents]`, the
"Later" stage layers the optional `lumio-lancedb` adapter onto the **same**
venv and compares enhanced retrieval against the same published Knowledge Base.
`lumio-lancedb` is a **separate package** (not a `lumio-wiki[lancedb]` extra):
the base `lumio-wiki` install stays model- and LanceDB-free, and
`bootstrap-lancedb.sh` layers the adapter on top without recreating the venv.

```bash
# 1. Base install first (creates .venv with lumio-wiki[documents]).
./bootstrap.sh

# 2. Layer lumio-lancedb onto the same venv; refuses to run if the full
#    `lumio` application is installed. Prints `lumio-wiki doctor` so
#    extra[lancedb] is visible as "installed".
./bootstrap-lancedb.sh

# 3. Publish the Knowledge Base (see the end-to-end walkthrough above), then:
bash tools/eval_lancedb.sh
```

### What it measures

`tools/eval_lancedb.sh` runs `lumio-wiki eval` twice against the versioned gold
set `evaluation/gold-v1.yaml` and turns the two machine-readable reports into one
human-readable, per-question side-by-side:

- **lexical run** — `lumio-wiki eval --gold-set evaluation/gold-v1.yaml
  --no-lancedb` (zero-index lexical + Discovery Graph expansion only);
- **lancedb run** — `lumio-wiki eval --gold-set evaluation/gold-v1.yaml` with the
  adapter present, which adds the LanceDB BM25 stage.

The shipped `eval` harness is **model-free recall\@k measurement** (issue #138):
for each gold query it reports, per retrieval stage, which Canonical Page Titles
surface in the top-k. There is **no** LLM-as-judge and **no** answer-quality or
per-question latency scoring — only retrieval-stage recall at the page-title
level. The script reports whole-run wall-clock latency (the harness exposes no
per-query timing) and is honest about that limit.

### What a reviewer should look for

- the per-question **lex vs bm25 recall\@3** and the `improved` / `regressed` /
  `same` delta — does the LanceDB stage surface the expected relevant page where
  lexical missed it (or vice versa)?
- the **aggregate recall\@k per stage** tables (the headline evidence that
  enhanced retrieval changes results on the same questions);
- **graph expansion** recall on the seeded relationship questions;
- the **refusal questions** section — these are recorded in the gold set with
  empty `relevant` (no supporting page exists) and are **not scoreable** by the
  recall\@k harness (it drops empty-relevant rows). A correct refusal therefore
  cannot be measured here yet — see "product findings" below.

### Opt-in semantic/hybrid stages

By default the comparison is lexical vs LanceDB BM25 (no embedder, no Torch).
Set `LUMIO_EVAL_SEMANTIC=1` to also run the LanceDB semantic and hybrid stages
using the harness's deterministic, offline hash embedder (no provider, no
network — `lumio-wiki eval --semantic`), with a few `--synonym` paraphrase
collapses to demonstrate semantic recall lexical search misses:

```bash
LUMIO_EVAL_SEMANTIC=1 bash tools/eval_lancedb.sh
```

For real (non-deterministic) embeddings, install the adapter's `[embeddings]`
extra (`LUMIO_LANCEDB_EMBEDDINGS=1 ./bootstrap-lancedb.sh`) and point the CLI at
a provider or local model via `LUMIO_PROVIDER_*` / `--model`. The example does
not do this by default to keep the venv Torch-free.

### Where outputs land

`tools/eval_lancedb.sh` writes to `./.eval/` (gitignored) and prints the same
report to stdout:

- `.eval/lexical.json`, `.eval/lancedb.json` — the raw `lumio-wiki eval --json`
  reports;
- `.eval/comparison.md` — the rendered side-by-side.

### Gold set format and anchoring

`evaluation/gold-v1.yaml` is the format the shipped `lumio-wiki eval --gold-set`
command consumes: a YAML document mapping each query to its expected relevant
Canonical Page Titles (plus optional graph seeds and a note), with one row per
question in `evaluation/questions.md`. The `relevant` titles are anchored to the
**source-stem page titles** the walkthrough's step-5 recipe produces
(`company-overview`, `customer-support-policy`, …). If your coding agent
authored different page titles, update `relevant` in `gold-v1.yaml` before
running the comparison — recall is matched by exact Canonical Page Title.

### Product findings (deviations from the literal issue scope)

Issue #156 was written before/as the eval harness landed, so the scripts adapt
to the shipped CLI rather than the issue's literal command shapes (the issue
hedges twice: "or the lexical stage appropriate to current CLI" and "or
lumio-lancedb directly, whichever the packaging layout requires"):

- the issue's `gold-v1.jsonl` ("expected citation path or refusal marker") is
  realized as **`gold-v1.yaml`** because `lumio-wiki eval --gold-set` decodes a
  YAML query\->titles document; a flat JSONL fixture would not load;
- the issue's `--stage keyword` lexical run is **`--no-lancedb`** (zero-index
  lexical + graph expansion), and the lancedb-backed run is the default
  `lumio-wiki eval` (which adds LanceDB BM25 when the adapter is present);
- the issue's per-question "answer snippet / citation correctness / latency"
  columns map to **recall\@k at the page-title level** plus whole-run latency,
  because the harness is model-free and exposes no answers or per-query timing;
- a **refusal/answer-quality harness does not exist yet** — refusal questions
  are carried in the gold set but cannot be scored by recall\@k.

## Trial boundaries

- Do not ingest `README.md`, `evaluation/`, or `tools/`; only `sources/` contains Knowledge Sources.
- Do not let the agent use the source files directly when answering evaluation questions; answers should come from the published Knowledge Base.
- Keep proposal-first publication enabled.
- Record any confusing command, missing guidance, incorrect conversion, weak proposal, or unsupported claim. These are product findings, not test setup failures to hide.
- The Atlas Heatworks organization, people, products, suppliers, and measurements are fictional.

## Regenerating the binary fixtures

The committed DOCX, PDF, PPTX, and XLSX are reproducible content fixtures.
Regenerate the full set, or a subset, after editing the generator with:

```bash
uv run --isolated --with python-docx --with reportlab --with python-pptx --with openpyxl \
    python tools/generate_binary_sources.py
uv run --isolated --with python-docx --with reportlab --with python-pptx --with openpyxl \
    python tools/generate_binary_sources.py --targets pptx xlsx
```

Supported targets: `docx`, `pdf`, `pptx`, `xlsx`. The default run regenerates all four.
