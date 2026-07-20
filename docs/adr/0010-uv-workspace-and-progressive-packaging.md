---
status: accepted
---

# ADR-0010: uv Workspace and Progressive Lumio Packaging

## Context

ADR-0001 chose a Python-first modular monolith, one Docker-first deployable
application, a framework-independent Core SDK, LanceDB as the retrieval index,
LiteParse for PDF/OCR ingestion, MarkItDown for broad document conversion, and
uv for packaging and dependency management. Its accepted trade-off was that
LanceDB entered the MVP dependency footprint before semantic retrieval was
strictly required. ADR-0002 later established a Lumio-owned Agent Runtime over
an OpenAI-compatible provider client while leaving the other ADR-0001 choices
standing.

The product now has a second concrete distribution use case beyond the web
application: a user should be able to install a standalone Karpathy-style LLM
Wiki toolkit in any coding agent. That toolkit must load, validate, search,
traverse, ingest, review, and publish a portable Markdown Knowledge Base; expose
citation-ready Evidence and Retrieval Results; provide a CLI; and ship reusable
Agent Skills. It must work without LanceDB, a web framework, an operational
database, an external model provider, or heavyweight document converters.

Two retrieval implementations are also real rather than hypothetical:

1. deterministic zero-index retrieval over Compiled Page metadata and bodies,
   typed Relationships, and graph traversal; and
2. an optional LanceDB implementation for BM25, embeddings, semantic search,
   and hybrid ranking.

Both must return the same citation-ready `RetrievalResult` contract. LanceDB
improves candidate selection; it does not own Compiled Pages, Relationships,
Evidence, citations, Retrieval Traces, or agent behavior. The current Core SDK
retrieval implementation imports LanceDB directly, so the architectural seam
promised by ADR-0001 does not yet produce an independently installable,
dependency-light package.

Ingestion presents the same problem. The current ingestion implementation
combines source conversion, model-driven distillation, proposal records,
validation, blast radius, persistence, and publication. Ingestion is a core LLM
Wiki capability, but requiring LiteParse, MarkItDown, or an OpenAI-compatible
provider in every coding-agent installation would defeat the lightweight
standalone use case.

Distribution boundaries, dependency direction, public contracts, and the
location of the canonical Knowledge Base model are difficult to reverse after
packages are published. They therefore require an explicit decision before the
code is moved.

## Decision

Adopt a **uv workspace** with three progressively enhanced, independently
buildable distributions in one repository and one shared lockfile:

1. `lumio-wiki` — the standalone LLM Wiki foundation;
2. `lumio-lancedb` — the optional enhanced-retrieval adapter; and
3. `lumio` — the existing full deployable web product.

The workspace root coordinates members and is not itself the distributed
application. Each member owns its own project metadata and declares workspace
member dependencies explicitly. The repository remains one codebase, and the
`lumio` member still produces the single Docker-first modular-monolith
deployment required by ADR-0001 and PRD-0001. Package decomposition does not
introduce microservices or multiple runtime deployments.

### `lumio-wiki`: standalone Knowledge Base and ingestion foundation

`lumio-wiki` owns the canonical, framework-independent contracts and behavior:

- Compiled Pages, Sources, Content Categories, typed Relationships, Evidence,
  citations, Retrieval Results, Retrieval Traces, and validation records;
- Knowledge Base loading, Control File handling, validation, fingerprinting,
  Navigation Indexes, the Hot Index, the Activity Log, and OKF interchange;
- deterministic metadata and body search, typed-Relationship traversal, graph
  paths, and the always-available zero-index retrieval implementation;
- Knowledge Sources, Source provenance, Proposed Pages, Ingest Proposals,
  blast radius, proposal persistence, review, validation, and publication;
- a CLI for initializing, validating, searching, reading, traversing,
  inspecting, ingesting, proposing, and publishing a Knowledge Base; and
- a short coding-agent protocol plus reusable Agent Skills that implement the
  Karpathy-style ingest, merge, query, and maintenance workflow.

The public distribution name is `lumio-wiki` because its user-facing promise is
a portable LLM Wiki toolkit. Lumio's internal ubiquitous language remains
unchanged: the canonical artifact is a Knowledge Base containing Compiled
Pages, and ingestion starts from Knowledge Sources. The distribution name does
not introduce a second domain model.

The base installation is lightweight and model-free. It includes text and
Markdown source processing, ingestion interfaces, the proposal pipeline, the
CLI, and Agent Skills. It does not depend on LanceDB, PyArrow, LiteParse,
MarkItDown, OpenAI, Stario, Piccolo, or an operational database.

### Ingestion seams and optional capabilities

Ingestion is decomposed behind three interfaces:

1. **Source Processor** — converts Knowledge Source bytes into normalized text
   and stable sections;
2. **Distiller** — converts normalized source material into proposed Compiled
   Pages; and
3. **Proposal Pipeline** — stages, validates, reviews, and publishes the
   proposed pages.

The base Agent Skill uses the host coding agent as the Distiller, so installing
`lumio-wiki` in a coding agent requires no separate model provider. Optional
unattended and document-processing capabilities are exposed as dependency
extras owned by `lumio-wiki`:

- `lumio-wiki[documents]` adds LiteParse and MarkItDown for PDF, scanned-PDF
  and image OCR, DOCX, HTML, and other supported document conversion;
- `lumio-wiki[llm]` adds an OpenAI-compatible unattended Distiller; and
- `lumio-wiki[all]` includes both optional capability sets.

Missing optional dependencies fail with actionable guidance naming the required
extra. Heavy implementations are loaded only when selected. Structured dataset
query execution remains outside the MVP as established by PRD-0001; ingestion
may still create Table or Dataset Descriptors without making data queryable.

### Retrieval interface and `lumio-lancedb`

`lumio-wiki` owns a small retrieval interface covering index lifecycle,
retrieval, and health. It also owns the zero-index implementation and the
`RetrievalResult`/Evidence/Citation/Trace contracts returned to every client.
Navigation Indexes and the Hot Index remain derived navigation artifacts, not
the canonical search index; Compiled Pages remain the source of truth.

`lumio-lancedb` depends only on `lumio-wiki` plus its retrieval dependencies. It
owns:

- LanceDB index construction, update, freshness, and health;
- BM25/full-text candidate ranking;
- vector and semantic ranking when an embedding provider is configured; and
- hybrid and reciprocal-rank-fusion behavior.

It implements the retrieval interface without changing client contracts.
`lumio-wiki` never imports `lumio-lancedb`, including through a convenience
extra, so the dependency graph remains one-way. Users who want enhanced
retrieval install `lumio-lancedb` explicitly. Optional local embedding
implementations may remain extras of `lumio-lancedb` so Torch is never required
by the base adapter or by `lumio-wiki`.

### Full `lumio` application

The existing `lumio` distribution remains the full deployable product and the
backward-compatible `pip install lumio` path. It depends on `lumio-wiki` and
selects either zero-index retrieval or `lumio-lancedb` through explicit
configuration and dependency injection. The full application owns Stario and
Datastar presentation, the Chat Gateway, the Agent Runtime, authentication and
roles, operational SQLite/Piccolo state, storage and synchronization,
background work, provider integration, and browser ingest/review workflows.

The full application may install the document, provider, and LanceDB capability
sets by default to preserve the current deployable experience. Those choices do
not leak into the `lumio-wiki` base dependency footprint. A separately
published `lumio-web` distribution is not introduced by this decision; the
existing `lumio` name already represents the deployable web product.

### Build, versioning, and verification

The uv workspace shares one lockfile and uses explicit workspace-member sources
for local development. Each distribution remains independently buildable and
publishable. Versions may move together initially, but every dependency between
published members declares a compatible version range; the shared lockfile is
not treated as a public compatibility contract.

CI must build each wheel and install it in an isolated environment. Testing only
inside the shared workspace is insufficient because installed workspace members
can hide undeclared imports. At minimum, verification includes:

- installing and running `lumio-wiki` without LanceDB, PyArrow, Stario,
  Piccolo, OpenAI, LiteParse, or MarkItDown;
- running one retrieval contract suite against zero-index and LanceDB
  implementations and asserting equivalent Evidence/citation contracts plus
  truthful Trace stages;
- testing each optional ingestion extra independently;
- testing the `lumio-wiki` CLI and packaged Agent Skills from the built wheel;
  and
- running the full application suite against the assembled `lumio` member.

No two wheels may own the same concrete Python module path. The exact import
namespace and compatibility-shim strategy are implementation decisions, but
wheel ownership must remain unambiguous and dependencies must point only upward
from adapters and applications to the contracts they implement or consume.

## Considered Options

- **Keep one distribution and use only optional dependency extras** — rejected:
  this can reduce installation size but does not establish an independently
  buildable, independently testable headless product. The coding-agent consumer
  and the second retrieval implementation make the package seam real.
- **Publish `lumio-core`, `lumio-kb`, and `lumio-ui`** — rejected: `core` is too
  vague for the user-facing product, `kb` must not mean "the package that adds
  LanceDB," and the server-rendered Stario/Datastar presentation is coupled to
  the Chat Gateway, runtime, auth, storage, and review workflows rather than
  being a standalone frontend. `lumio-wiki`, `lumio-lancedb`, and the existing
  `lumio` product match the actual seams.
- **Name the standalone package `lumio-core` or `lumio-kb`** — rejected:
  `lumio-core` does not communicate the standalone user capability, while
  `lumio-kb` is accurate domain language but less clearly describes the
  Karpathy-style workflow, CLI, and Agent Skills. `lumio-wiki` is the product
  name; Knowledge Base remains the internal domain term.
- **Put LanceDB in `lumio-wiki` and make only semantic embeddings optional** —
  rejected: it preserves the current dependency leak and prevents a genuinely
  lightweight zero-index installation. LanceDB is an adapter, not the owner of
  the Knowledge Base or retrieval contract.
- **Move ingestion into a separate distribution** — rejected for now:
  ingestion, proposal review, and publication are fundamental to managing an
  LLM Wiki. Source processors and Distillers are real internal seams, while
  dependency extras isolate their heavyweight implementations without creating
  another release surface.
- **Install all converters and the provider client in base `lumio-wiki`** —
  rejected: coding agents already provide a Distiller, and many users need only
  Markdown/text sources. Mandatory OCR, conversion, and provider dependencies
  would undermine the standalone package's principal benefit.
- **Split packages into separate repositories** — rejected: a uv workspace
  preserves locality, one lockfile, atomic cross-package changes, and the
  existing single-product development workflow while still producing isolated
  wheels.
- **uv workspace with progressive packages and optional capabilities** —
  chosen: it gives coding agents a lightweight standalone product, makes the
  retrieval seam enforceable, keeps ingestion in the LLM Wiki product, and
  preserves the one-deployment modular monolith.

## Consequences

This ADR amends ADR-0001 in three specific ways:

1. **Packaging** changes from one Python distribution to a uv workspace with
   independently buildable `lumio-wiki`, `lumio-lancedb`, and `lumio` members.
   Docker-first deployment remains one assembled application.
2. **Retrieval** changes from LanceDB in the base dependency footprint to an
   always-available zero-index implementation owned by `lumio-wiki` plus an
   optional LanceDB adapter. LanceDB remains the chosen enhanced-retrieval
   implementation.
3. **Ingestion dependencies** change from mandatory LiteParse and MarkItDown in
   the full package tree to optional `lumio-wiki` capability extras, while the
   ingestion model and proposal pipeline remain part of the standalone product.

ADR-0002's Lumio-owned Agent Runtime and msgspec decision remain unchanged. The
OpenAI-compatible provider client stays in the full application and in the
optional unattended-Distiller capability, not in base `lumio-wiki`. ADR-0007's
interchange stance, ADR-0008's portable Control File and derived artifacts, and
ADR-0009's per-KB extensible category catalog remain unchanged and become
contracts owned by `lumio-wiki`.

The benefit is progressive enhancement: a coding agent can install a small,
portable LLM Wiki toolkit; users can add document conversion, unattended model
distillation, or LanceDB retrieval independently; and the web application uses
exactly the same Knowledge Base and retrieval contracts. Retrieval adapters can
be added or removed without changing the Knowledge Base format or clients.

The costs are a larger packaging and release surface, explicit inter-package
version compatibility, workspace migration, public-import migration, and
wheel-isolation CI. The current monolithic retrieval and ingestion modules must
be decomposed, and package ownership violations become release-blocking defects.
These costs are accepted because there are now independent consumers and
multiple concrete implementations on both the retrieval and ingestion seams.
