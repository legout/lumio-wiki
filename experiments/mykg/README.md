# myKG → Lumio experiment harness

Experiment-only harness for **GitHub issue #146** — *Spike: evaluate myKG as a
private corpus extractor feeding Lumio Ingest Proposals*. It implements the
focused experiment recommended by [`docs/research/mykg-fit.md`](../../docs/research/mykg-fit.md).

> **This is an experiment, not a product integration.** Nothing here is a
> `lumio-wiki` dependency. The converter is a standalone, standard-library-only
> script kept outside the package graph. myKG is invoked out of process; Lumio
> only consumes the resulting candidate Markdown through its existing,
> proposal-first external-import path.

## Status of this kickoff (Path B)

Issue #146 is labeled `needs-info`. Four maintainer preconditions gate the full
experiment (see **Missing inputs** below) and are **not yet provided**, so this
kickoff delivers the experiment **harness** only:

| #146 deliverable | status |
| --- | --- |
| Experiment-only converter / script (outside `lumio-wiki` deps) | ✅ `convert_mykg.py` |
| Reproducible run instructions + pinned myKG version/configuration | ✅ this runbook (version/config to be pinned at run time) |
| Baseline-vs-myKG metrics report | ⏳ **deferred** — requires the run |
| Privacy/export audit results | ⏳ **deferred** — requires the run |
| Final `adopt` / `defer` / `reject` recommendation | ⏳ **deferred** — requires the run |

The deferred items need the missing inputs and an actual myKG run; producing
them now would mean fabricating results. Use [`report-template.md`](report-template.md)
to capture them once the run happens.

## Missing inputs (maintainer must provide before the run)

These are the unchecked preconditions from issue #146. Fill them in here, then
run the experiment.

- [ ] **Corpus:** 5–10 non-sensitive documents — path: `<TODO>`
- [ ] **myKG model / provider configuration:** `<TODO>` (provider, model, schema-review on)
- [ ] **Cost + elapsed-time budget:** `<TODO>`
- [ ] **Experiment-artifact storage location + retention:** `<TODO>` (must be private; not the published KB)

## What the converter does

`convert_mykg.py` reads a myKG session and writes a candidate Lumio Compiled
Page tree plus a **private** relationship-candidate sidecar.

```text
private source corpus
  -> myKG extraction session (out of process, with schema review)
  -> convert_mykg.py                      # THIS SCRIPT
  -> temporary external Compiled Markdown tree + private sidecar
  -> import_external_compiled_markdown()  # Lumio parses the tree
  -> propose_external_import()            # Lumio stages an Ingest Proposal
  -> ProposalPipeline.stage()             # review queue
  -> Maintainer review + candidate validation + (optional) publish
```

### myKG session layout (input)

The converter mirrors myKG's own `load_session` layout:

```text
<session_root>/
  output/
    nodes.jsonl      # one node per line
    edges.jsonl      # one edge per line
  intermediate/
    schema.json      # optional for conversion (context only)
```

**Node** fields: `id`, `type` (ontology class), `confidence`, optional
`aliases`, `attributes` (each `{name: {value, confidence?}}`; the node *name* is
`attributes.name.value`), optional `source_files`.

**Edge** fields: `id`, `from`, `to`, `type` (relation), `confidence`, optional
`method`, optional `source_files`, optional `attributes`.

A tiny, valid sample session lives at [`sample/`](sample/) and is used by the
test suite.

### Mapping rules (issue #146 + mykg-fit.md)

| myKG | Lumio candidate page | notes |
| --- | --- | --- |
| node name (`attributes.name.value`) | `title` (Canonical Page Title) | collision-checked within the batch and (optionally) an existing KB; disambiguated on collision |
| node `aliases` | `aliases` | dropped if they collide with a title/alias; never duplicated |
| node `type` (ontology class) | `type` (free-form) | **never** a Content Category; routed under a seeded category directory |
| node `type` | `tags` (one, type-derived; fallback `entity`) | tags are required and non-empty |
| — | `lifecycle: review` (or `draft`) | **never** `approved` |
| — | `visibility: internal` | conservative default |
| node `source_files` | `sources` (opaque, stable ids) | raw paths stay private; node with no `source_files` → `synthetic: true` |
| node `confidence`, raw chunks, method | **nowhere** in the page | private only |

**Edges are never written as Relationships.** Every myKG edge becomes a row in
`relationship-candidates.jsonl` (disposition `pending`), carrying `relation_type`,
`confidence`, `method`, and `source_files` for Maintainer review. Only edges a
Maintainer accepts may later be promoted to canonical Relationships through a
reviewed proposal — confidence never becomes Lifecycle/Visibility/publication
status.

### Boundaries: what the converter does NOT own

- **No Knowledge Source Registry identity or content hashes.** The converter
  emits **opaque, stable source ids** as a correlation handle (plus a private
  id→raw-file map in `conversion-report.json`). Establishing Lumio Knowledge
  Source Registry identities and content-hashed Source Versions is Lumio's job
  at ingest time (`propose_external_import`), not the converter's. The converter
  does not read the raw corpus files (by design), so it cannot hash them.
- **No supporting source chunks.** myKG's session JSONL (`nodes.jsonl` /
  `edges.jsonl`) carries file-level `source_files`, `method`, `confidence`, and
  per-attribute values — **not** chunk-level source text. The sidecar records
  everything myKG exports at this level; capturing supporting CHUNKS would
  require reading myKG's intermediate chunk store (a future enhancement), and
  any such chunks must stay private to Maintainer review.
- **Within-batch uniqueness is guaranteed; cross-KB collisions are Lumio's gate.**
  The converter guarantees unique candidate titles/slugs within a run and drops
  colliding aliases. Collisions against an *existing* Knowledge Base are
  authoritatively caught by Lumio's `propose_external_import()` validation;
  `--kb-root` only adds an optional, best-effort early warning (it scans
  block-style frontmatter and may miss inline/flow-style YAML aliases).

## Reproducible run

### 1. Pin myKG, then run it (out of process)

The run is **not reproducible** until you record the exact myKG version and
model/provider configuration in **Missing inputs** above and in the report's
Run metadata. Capture them first, and use the same version + config for the
baseline and the myKG-assisted run:

```bash
mykg --version                                        # record this
mykg extract --input <CORPUS_DIR> --session <SESSION_ROOT> --schema-review \
  --model <MODEL> --provider <PROVIDER>
```

### 2. Convert the session to a candidate Markdown tree

```bash
uv run python experiments/mykg/convert_mykg.py \
  --session <SESSION_ROOT> \
  --out <EXPERIMENT_TREE_DIR> \
  --category entities \
  --default-lifecycle review \
  --default-visibility internal
```

Options: `--default-tag` (fallback tag), `--source-id-prefix` (default `mykg`),
`--kb-root <KB>` (best-effort collision warnings against an existing KB),
`--default-visibility {public,internal,restricted}`, `--default-lifecycle {draft,review}`.

This writes:

- `<EXPERIMENT_TREE_DIR>/<category>/*.md` — candidate Compiled Pages;
- `<EXPERIMENT_TREE_DIR>/relationship-candidates.jsonl` — **private** edge candidates;
- `<EXPERIMENT_TREE_DIR>/conversion-report.json` — **private** summary, collisions, warnings, skipped nodes, and an opaque-source-id → raw-file map (`private_provenance`).

### 3. Try the sample (no myKG needed)

```bash
uv run python experiments/mykg/convert_mykg.py \
  --session experiments/mykg/sample \
  --out /tmp/mykg-sample-tree
cat /tmp/mykg-sample-tree/entities/acme.md
cat /tmp/mykg-sample-tree/relationship-candidates.jsonl
```

### 4. Stage through Lumio's external-import proposal path

Using the Core SDK (the exact entry points the issue names):

```python
from lumio_wiki import (
    import_external_compiled_markdown,
    propose_external_import,
    SourceProvenance,   # provenance for the staged proposal
)

parsed = import_external_compiled_markdown("<EXPERIMENT_TREE_DIR>")
proposal = propose_external_import(parsed, kb, SourceProvenance(...))
# Then drive review/stage/publish through ProposalPipeline / the Workshop / the
# lumio-wiki CLI proposal+publish commands. Publishing is OPTIONAL for the
# experiment — comparison happens before any publish.
```

Review the private `relationship-candidates.jsonl` alongside the staged
proposal. Accept edges separately, through a reviewed proposal, before any may
become canonical Relationships.

## Architecture guardrails (issue #146)

- myKG runs **out of process** and stays out of the `lumio-wiki` dependency graph.
- myKG nodes/attributes/ontology classes/aliases/edges are **candidates only**.
- **No myKG edge becomes canonical without Maintainer review** — edges never enter
  page frontmatter `relationships:`.
- **Confidence never maps to Lifecycle, Visibility, or publication status.**
- Raw paths, source chunks, method, and extraction metadata stay in the private
  sidecar / conversion report — never in a Compiled Page or a public export.
- No general extractor-provider interface is introduced; this single adapter is a
  hypothetical seam only.

## Measurements

Capture the experiment's results in [`report-template.md`](report-template.md)
(duplicate entities/collisions, hallucinated attributes, relationship
acceptance rates, source-passage correctness, pages accepted vs discarded,
review time, latency/cost, leakage check, success criteria, and the final
adopt/defer/reject decision).
