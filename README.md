# lumio-wiki

> Portable, model-free Knowledge Base foundation for
> [Lumio](CONTEXT.md) — a compiled Markdown knowledge base your coding agents
> can load, validate, search, traverse, ingest, and publish, with citations
> and proposal-first review. The deployable Lumio web application (chat UI,
> auth, roles, providers) lives in a separate private repository and consumes
> these published wheels.

**Status:** pre-1.0 MVP software, licensed Apache-2.0.

For the *why* and the vision, read the domain docs — this README documents
**what runs today**:

> **Two trust models, one Knowledge Base.** The **`lumio-wiki` CLI** (and the
> library behind it) has no accounts or roles: a coding agent with filesystem
> access *is* the Distiller and Maintainer — it authors Compiled Pages, stages
> Ingest Proposals, validates them deterministically, and publishes, with no
> human step enforced. A web app can add human governance over the same
> artifact; proposal-first is the shared seam.

- [`CONTEXT.md`](CONTEXT.md) — ubiquitous language (glossary).
- [`docs/prd/0002-core-sdk.md`](docs/prd/0002-core-sdk.md) — Core SDK PRD.
- [`docs/adr/`](docs/adr/) — architectural decisions (ADR-0025 records the
  repository split).
- [`docs/kb-format.md`](docs/kb-format.md) — canonical Compiled Page frontmatter reference.
- [`docs/quickstart.md`](docs/quickstart.md) — canonical onboarding journey
  (MinIO → AWS), runnable via [`examples/onboarding-journey/`](examples/onboarding-journey/).

## Install

`lumio-wiki` ships as two progressively enhanced wheels (ADR-0010):

| Distribution | Installs | Use when |
|---|---|---|
| `lumio-wiki` | `msgspec[yaml]`, `msgpack`, plus the `lumio-wiki` CLI and packaged Agent Skill | You want a portable, model-free Knowledge Base from any coding agent (Pi, Hermes, Codex, Claude Code, …). |
| `lumio-lancedb` | `lumio-wiki` + LanceDB + PyArrow | You want BM25 / semantic / hybrid retrieval over the same Knowledge Base. |

```bash
pip install lumio-wiki                       # foundation: load, validate, search, traverse, ingest, publish
pip install 'lumio-wiki[documents]'          # + LiteParse/MarkItDown/AnyDoc for PDF/image/office/HTML ingestion
pip install 'lumio-wiki[llm]'                # + an unattended OpenAI-compatible Distiller
pip install 'lumio-wiki[all]'                # documents + llm together (still no LanceDB)
pip install lumio-lancedb                    # + enhanced BM25/semantic/hybrid retrieval
```

The base wheel depends only on `msgspec[yaml]` and `msgpack`; it does not
install LanceDB/PyArrow or any model provider. Optional capabilities:

| Extra | Brings | Capability |
|---|---|---|
| `lumio-wiki[documents]` | LiteParse, MarkItDown, AnyDoc | PDF / scanned-PDF / image (OCR + page numbers), office formats, HTML. The base wheel handles text and Markdown. |
| `lumio-wiki[llm]` | `openai` | Unattended Distiller backed by an OpenAI-compatible provider. The base wheel uses the host coding agent as the Distiller. |
| `lumio-wiki[all]` | both | Document conversion + unattended distillation together. |
| `lumio-lancedb` | LanceDB, PyArrow | BM25 / vector / semantic / hybrid retrieval. `lumio-wiki` never imports it. |
| `lumio-lancedb[embeddings]` | `sentence-transformers` | Local embeddings for the LanceDB adapter. Torch stays out of the base adapter and out of `lumio-wiki`. |
| `lumio-lancedb[s3]` | `obstore` | Remote (S3) index support. |

**Adapter selection.** `lumio-wiki` provides the always-available zero-index
retrieval; installing `lumio-lancedb` adds BM25 / semantic / hybrid retrieval
behind the same `RetrievalResult` contract. `lumio-wiki` never imports the
adapter — the dependency graph is one-way.

## CLI reference

`uv run lumio-wiki <command>` (or just `lumio-wiki` once installed). Works
from any coding agent with no model provider and no LanceDB.

```bash
lumio-wiki --version
lumio-wiki init <path>                           # scaffold a categorized Knowledge Base
lumio-wiki validate <kb-path>                    # exit 0 if valid, 1 otherwise
lumio-wiki search <kb-path> "<query>"            # lexical search over titles, aliases, tags, summaries, bodies
lumio-wiki page <kb-path> "<title>"              # read a Compiled Page by Canonical Title or alias
lumio-wiki related <kb-path> "<title>"           # list pages related to a title
lumio-wiki paths <kb-path> "<src>" "<dst>"       # shortest typed path between two titles
lumio-wiki ingest <kb-path> <source> [--distiller passthrough|llm]   # stage a Knowledge Source as a proposal
lumio-wiki ingest-url <kb-path> <url> --compiled-page <page.md> --source-id <id>  # safe URL ingestion (HTTPS-only, bounded, SSRF-safe)
lumio-wiki ingest-research <kb-path> <report.md> --manifest <manifest.yaml> --source-id <id>  # research bundle (consulted URLs = provenance)
lumio-wiki proposal list|inspect|validate <kb-path> [id]      # review staged proposals
lumio-wiki publish <kb-path> <id>                # publish a reviewed proposal
lumio-wiki source inspect <kb> --source-id <id> [--published-version <v>]  # secret-free metadata for the exact bound Source Version
lumio-wiki source fetch <kb> --source-id <id> [--published-version <v>] --output <path>  # byte-exact original, digest re-verified
lumio-wiki source link <kb> --source-id <id> [--published-version <v>] [--expires 5m]   # short-lived signed GET URL (max 1h; bearer secret)
lumio-wiki health <kb-path>                      # Knowledge Base health and diagnostics
lumio-wiki doctor                                # install shape: version, optionals, packaged skill location
lumio-wiki skill [--install|--path]              # locate or install the packaged Agent Skill
```

`ingest --distiller llm` requires `lumio-wiki[llm]`; PDF/DOCX/image sources
require `lumio-wiki[documents]`. Both fail with actionable guidance if the
extra is missing.

## Knowledge Base format

A Knowledge Base is a directory of compiled Markdown pages (YAML frontmatter +
body). Minimal example:

```markdown
---
title: "Technology Stack"
tags: ["technology"]
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "stack-doc"
    title: "Stack decision"
---

# Technology Stack

Python-first: msgspec, LanceDB (optional adapter).
```

For the full field table, see **[`docs/kb-format.md`](docs/kb-format.md)**. A
categorized Knowledge Base additionally carries a versioned root Control File
(`lumio.yaml`) and marked reserved artifacts (Navigation Index `index.md`, Hot
Index `hot.md`, Activity Log `log.md`); see
[ADR-0008](docs/adr/0008-knowledge-base-control-file-and-published-artifacts.md).

## Development

Requirements: **Python ≥ 3.14** and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                       # install both members
uv lock --check                               # verify the lockfile is current
uv run pytest -q -n 4                         # full suite (four parallel workers)
uv run pytest -q packages/lumio-wiki/tests    # focused tests stay serial
uv run ruff check .                           # lint
uv run lumio-wiki validate tests/fixtures/valid   # sanity check the sample KB
```

### Codebase layout

```
packages/lumio-wiki/src/lumio_wiki/       # Canonical portable Knowledge Base owner
packages/lumio-lancedb/src/lumio_lancedb/ # Optional LanceDB enhanced-retrieval adapter
```

## Packaging, ownership, and migration

Quick reference (full contract in [`docs/packaging.md`](docs/packaging.md)):

- **`lumio-wiki`** owns the canonical Knowledge Base model, loading,
  validation, Navigation/Hot Indexes, deterministic zero-index retrieval, typed
  traversal, Evidence/citations, ingestion interfaces, the proposal pipeline,
  the `lumio-wiki` CLI, and the packaged Agent Skill. No downward dependency.
- **`lumio-lancedb`** depends only on `lumio-wiki`. It owns LanceDB index
  lifecycle and BM25/semantic/hybrid ranking, returning the same
  `RetrievalResult` contract.

The wheels release as one lockstep family from tagged commits. The web
application lives in a separate private repository (ADR-0025).

## What works today

- **Core SDK** — load, validate (entity-claim ontology against the Control
  File), lexical/frontmatter/graph index, retrieve with citations + retrieval
  trace, source-fingerprint freshness, Markdown export.
- **Entity-claim ontology** — one stable Entity per Compiled Page;
  evidence-bearing Claims; proposal-first Entity Merge with redirects;
  deterministic entity resolution; model-free evaluation (`lumio-wiki eval`,
  `lumio-wiki eval-ontology`).
- **Ingest & publish** — managed ingest with private Source Registry and
  Artifact Store (S3/MinIO), proposal pipeline, Page Removal with claim and
  body-link repair, Published Versions with binding manifests, OKF
  interchange.
- **Maintenance** — lint, cross-link candidates, Dream Cycle diagnostics
  (link, duplicate, synthesis, transitive impact), S3 published-version diff.

## License

Apache-2.0 — see [LICENSE](LICENSE).
