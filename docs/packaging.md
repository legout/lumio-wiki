# Packaging, Ownership, and Migration

This document is the certified packaging contract for the Lumio workspace:
what each distribution owns, how dependencies flow, how optional capabilities
are expressed, how to migrate off the temporary compatibility surface, and
how the assembled product is verified. It is the authoritative companion to
[ADR-0010](adr/0010-uv-workspace-and-progressive-packaging.md) and the
certification in `tests/test_workspace_certification.py` /
`tests/test_adapter_selection_contract.py`.

## Workspace shape

Lumio is a [uv](https://docs.astral.sh/uv/) workspace with three
independently buildable, independently installable members and one shared
lockfile.

| Member | Path | Wheel | Python root |
|---|---|---|---|
| `lumio-wiki` | `packages/lumio-wiki` | `lumio_wiki` | `lumio_wiki` |
| `lumio-lancedb` | `packages/lumio-lancedb` | `lumio_lancedb` | `lumio_lancedb` |
| `lumio` | `packages/lumio` | `lumio` | `lumio` |

The workspace root (`pyproject.toml`) is coordination only — it declares the
workspace, shared dev tooling, and test/lint config. It is **not** a
distributable package: no `[project]` table, `tool.uv.package = false`.

Invariants (certified in `tests/test_workspace_certification.py`):

- Each member has its own `[project]` metadata and hatchling build backend.
- Each wheel ships exactly one top-level Python package root.
- The three roots (`lumio_wiki`, `lumio_lancedb`, `lumio`) are disjoint —
  no two wheels own the same concrete Python module path.
- One shared lockfile (`uv.lock`); no member carries its own.

## Distribution ownership

### `lumio-wiki` — portable Knowledge Base foundation

The standalone, model-free LLM Wiki toolkit. Owns the canonical contracts:

- Compiled Pages, Sources, Content Categories, typed Relationships, Evidence,
  citations, Retrieval Results, Retrieval Traces, validation records.
- Knowledge Base loading, Control File handling, validation, fingerprinting,
  Navigation Indexes, Hot Index, Activity Log, OKF interchange.
- Deterministic metadata/body search, typed-Relationship traversal, graph
  paths, and the always-available zero-index retrieval implementation.
- Knowledge Sources, Source provenance, Proposed Pages, Ingest Proposals,
  blast radius, proposal persistence, review, validation, publication.
- The `lumio-wiki` CLI (`init`, `validate`, `search`, `page`, `related`,
  `paths`, `ingest`, `proposal`, `publish`, `discard`, `health`, `doctor`,
  `skill`).
- A short coding-agent protocol and a packaged Agent Skill, both shipped as
  wheel data and resolvable via `lumio-wiki skill`.

**Dependencies:** `msgpack`, `msgspec[yaml]` only. It does **not** depend on
LanceDB, PyArrow, Stario, Piccolo, OpenAI, LiteParse, MarkItDown, or any
application module. Verified in every release by
`scripts/verify_lumio_wiki_wheel.py` (run from CI in an isolated venv) and
by `packages/lumio-wiki/tests/test_wheel_isolation.py`.

### `lumio-lancedb` — enhanced retrieval adapter

The optional LanceDB retrieval adapter. Depends only on `lumio-wiki` plus
its own retrieval stack. Owns:

- LanceDB index construction, update, freshness, and health.
- BM25 / full-text candidate ranking.
- Vector and semantic ranking when an embedding provider is configured.
- Hybrid and reciprocal-rank-fusion behavior.

It implements `lumio-wiki`'s retrieval interface without changing client
contracts. `lumio-wiki` never imports it, including through a convenience
extra — the dependency graph is one-way.

**Dependencies:** `lumio-wiki>=0.1.1,<0.2.0`, `lancedb`, `pyarrow`,
`msgspec[yaml]`. Optional `embeddings` extra pulls in
`sentence-transformers` (Torch stays out of the base adapter and out of
`lumio-wiki`). Verified in every release by the
`lumio-lancedb-wheel` GitHub Actions workflow, which installs the adapter
beside `lumio-wiki` alone in a fresh venv and asserts Stario/Piccolo/OpenAI/
LiteParse/MarkItDown are absent.

### `lumio` — full deployable application

The existing deployable product and the backward-compatible
`pip install lumio` path. Consumes `lumio-wiki` and `lumio-lancedb` upward
and selects zero-index or LanceDB retrieval through `LUMIO_RETRIEVAL_BACKEND`
and the single public seam `build_retrieval_index` in `lumio.retrieval`.
Owns Stario/Datastar presentation, the Chat Gateway, the Agent Runtime,
authentication and roles, operational SQLite/Piccolo state, storage and
synchronization, background work, provider integration, and browser
ingest/review workflows.

**Dependencies:** `lumio-wiki>=0.1.1,<0.2.0`, `lumio-lancedb>=0.1.1,<0.2.0`,
plus the operational stack (`stario`, `piccolo[sqlite]`, `openai`,
`liteparse`, `markitdown[docx]`, `lancedb`, `pyarrow`, `gitPython`,
`msgspec[yaml]`). Optional `semantic` extra pulls in `sentence-transformers`
for local embeddings.

## Dependency direction

```
lumio-wiki  ←  lumio-lancedb
    ↑              ↑
    └────  lumio ──┘
```

- `lumio-wiki` has no inter-member dependency.
- `lumio-lancedb` depends only on `lumio-wiki`.
- `lumio` consumes both upward.

AST-certified in `tests/test_package_contraction.py`:
`test_dependency_direction_remains_one_way` walks every import in the three
packages and fails if `lumio-wiki` ever names `lumio` / `lumio_lancedb` /
heavyweight deps, or if `lumio-lancedb` ever names `lumio`.

## Optional capabilities

`lumio-wiki` is the only member that exposes optional capability extras
(ADR-0010: ingestion heavyweight implementations must be opt-in so the
standalone package stays lightweight). Each extra is self-contained — none
of them pulls in another `lumio-*` distribution.

| Extra | Adds | Capability |
|---|---|---|
| `lumio-wiki[documents]` | LiteParse, MarkItDown | PDF / scanned-PDF / image / DOCX / HTML ingestion. The base wheel handles text and Markdown. |
| `lumio-wiki[llm]` | `openai` | Unattended Distiller backed by an OpenAI-compatible provider. The base wheel uses the host coding agent as the Distiller (`--distiller passthrough`). |
| `lumio-wiki[all]` | both of the above | Document conversion + unattended distillation together. Still LanceDB-free. |

The LanceDB adapter and the local-embeddings capabilities live on their
respective distributions:

| Extra | Adds | Capability |
|---|---|---|
| `lumio-lancedb` | LanceDB, PyArrow | BM25 / vector / semantic / hybrid retrieval over the same Knowledge Base. |
| `lumio-lancedb[embeddings]` | `sentence-transformers` | Local embeddings for the LanceDB adapter. |
| `lumio[semantic]` | `sentence-transformers` | Local embeddings for the full app's LanceDB backend. |

**Actionable missing-extra errors.** Invoking a capability without its extra
fails with the exact install command. PDF/DOCX/image ingestion from a base
install names `pip install 'lumio-wiki[documents]'`; `--distiller llm` from a
base install names `pip install 'lumio-wiki[llm]'`; `lumio-wiki doctor`
reports each extra's install state and the packaged skill location.

## Capability install matrix (certified)

Every capability is verified in isolation from a built wheel:

| Capability | Test | CI |
|---|---|---|
| `lumio-wiki` base (no extras) | `packages/lumio-wiki/tests/test_wheel_isolation.py` + `scripts/verify_lumio_wiki_wheel.py` | `ci.yml` (`isolated-lumio-wiki-wheel` job) |
| `lumio-wiki[documents]` | `packages/lumio-wiki/tests/test_wheel_isolation_documents.py` | `ci.yml` (slow wheel suite) |
| `lumio-wiki[llm]` | `packages/lumio-wiki/tests/test_wheel_isolation_llm.py` | `ci.yml` (slow wheel suite) |
| `lumio-wiki[all]` | `packages/lumio-wiki/tests/test_wheel_isolation_llm.py::test_all_extra_installs_documents_and_llm_without_lancedb` | `ci.yml` (slow wheel suite) |
| `lumio-lancedb` | `packages/lumio-lancedb/tests/test_lancedb_adapter.py` | `lumio-lancedb-wheel.yml` |

## Adapter selection contract

The full `lumio` application selects retrieval through configuration and
dependency injection — never by importing adapter types in client modules.

- **Single seam.** `lumio.retrieval.build_retrieval_index(kb, index_dir, *,
  backend=None, embedder=None)` is the only sanctioned entry point. It is
  called by `app.py` and `cli.py`.
- **Config.** `LUMIO_RETRIEVAL_BACKEND=zero-index|lancedb` (default
  `lancedb`). Invalid values raise `RetrievalBackendConfigError` with
  migration guidance.
- **Local import.** `build_retrieval_index` imports `LanceDBRetrievalAdapter`
  inside its LanceDB branch, so zero-index deployments never force client
  modules to name `lumio_lancedb` symbols.

Certified in `tests/test_adapter_selection_contract.py` and
`tests/test_retrieval_selection.py`, and AST-locked in
`tests/test_package_contraction.py::test_app_and_cli_select_retrieval_through_factory_not_adapter_types`.

## Versioning and compatibility

- Members share one workspace lockfile for development reproducibility.
- The lockfile is **not** a public compatibility contract. Every published
  inter-member dependency declares a bounded range
  (`lumio-wiki>=0.1.1,<0.2.0`). This is certified at the metadata level
  (`test_published_member_dependencies_declare_compatible_version_ranges`)
  and at the built-wheel METADATA level
  (`test_built_wheels_declare_bounded_inter_member_ranges`).
- Versions may move together initially. Breaking changes to `lumio-wiki`'s
  public contracts require bumping the upper bound in `lumio-lancedb` and
  `lumio` in the same change.

## Agent Skills

`lumio-wiki` ships a short coding-agent protocol and a packaged Agent Skill
as wheel data. Coding agents locate them deterministically without cloning
the Lumio repository:

```bash
lumio-wiki skill --path      # print the absolute path to the packaged SKILL.md
lumio-wiki skill --install <dest>   # copy SKILL.md into a coding-agent skills directory
lumio-wiki doctor            # report version, optionals, and the skill path
```

The protocol and skill are force-included into the wheel via the
`[tool.hatch.build.targets.wheel]` `force-include` map in
`packages/lumio-wiki/pyproject.toml`. Both files also ship under the
importable `lumio_wiki.data` package, so `importlib.resources` resolves them
even under strict build configs.

## Temporary `lumio.core` compatibility

Canonical Knowledge Base behavior lives under `lumio_wiki`. The pre-1.0
`lumio.core` and `lumio.core.<module>` imports are **temporary** public
re-exports so external consumers continue to work during migration.

- **Internal callers** (production `lumio.*` modules and the test suite)
  import final package owners (`lumio_wiki`, `lumio_lancedb`) directly.
  AST-locked in
  `tests/test_package_contraction.py::test_production_modules_do_not_import_compatibility_surfaces`.
- **Compatibility modules** are thin re-exports only — they own no
  duplicate implementation. AST-locked in
  `test_compatibility_core_modules_are_thin_reexports_only`.
- **Removal** is time-bounded to pre-1.0 and will ship only through a
  separately documented migration issue that replaces the re-exports with
  explicit migration guidance.

### Migration cheat sheet

| Old import (pre-1.0) | New owner |
|---|---|
| `lumio.core.knowledge_base` | `lumio_wiki.knowledge_base` |
| `lumio.core.records` | `lumio_wiki.records` |
| `lumio.core.retrieval` | `lumio_wiki.retrieval` |
| `lumio.core.index` | `lumio_lancedb` (adapter) / `lumio_wiki.retrieval` (zero-index contract) |
| `lumio.core.evidence` | `lumio_wiki.evidence` |
| `lumio.core.page_search` | `lumio_wiki.page_search` |
| `lumio.core.fingerprint_store` | `lumio_wiki.fingerprint_store` |
| `lumio.core.okf` | `lumio_wiki.okf` |
| `lumio.core.embeddings` | `lumio_wiki.embeddings` |
| `lumio.proposal_pipeline` | `lumio_wiki.proposal_pipeline` |
| `lumio.source_processor` | `lumio_wiki.source_processor` |

## Build and release

```bash
uv lock --check                              # verify the workspace lockfile is current
uv build --package lumio-wiki --wheel        # build one member wheel
uv build --package lumio-lancedb --wheel
uv build --package lumio --wheel
```

Each member builds from the workspace without its siblings having to be
pre-built — the wheel's METADATA carries the bounded range and the installer
resolves the sibling from PyPI (or from a local wheel index) at install time.

CI certifies every release:

1. `ci.yml` (`workspace` job) — lint + fast suite against the workspace
   environment.
2. `ci.yml` (`isolated-lumio-wiki-wheel` job) — builds the `lumio-wiki` and
   `lumio` wheels, installs `lumio-wiki` alone in a fresh venv, and runs
   `scripts/verify_lumio_wiki_wheel.py` to prove no heavyweight dependency
   leaked into the base install.
3. `lumio-lancedb-wheel.yml` — builds the `lumio-lancedb` and `lumio-wiki`
   wheels, installs them alone in a fresh venv, asserts the app-only deps
   are absent, and runs the adapter smoke.
4. `lumio-assembled-product.yml` (issue #104) — builds all three wheels,
   installs the assembled `lumio` wheel from a local wheel index into a
   fresh venv, and runs the full application suite plus the deployment
   smoke and the view smoke against the wheel-built product.
5. The full application suite runs against the assembled `lumio` member.

## Issue graph certification (issue #104, AC7)

The workspace migration (parent #93) and its contraction (#103) are
certified by this issue (#104) without duplicating any product scope that
lives in the downstream issue graph. The audit was performed against the
live issue tracker; the exact blocker edges verified at certification time
are recorded below so future drift is detectable.

### Closed feature tickets implemented against the workspace

- **#84** (extensible categories) — closed; implemented against
  `lumio_wiki` (Control File + validation).
- **#85** (external-vault import) — closed; reuses the generic OKF import
  path under `lumio_wiki`.
- **#86** (cross-page QA report) — closed; implemented against
  `lumio_wiki` validation + Discovery Graph outcomes.

### Open product tickets and their workspace blockers

| Issue | Blockers | Workspace edge |
|---|---|---|
| #87 (category mapping) | #84, #85 | none (closed-feature blockers only) |
| #88 (navigation regeneration) | #85 | none |
| #89 (import CLI/UI) | #85, **#102** | workspace foundation |
| #90 (link-candidate finder) | #86 | none |
| #91 (lint + cross-linker skills) | #86, #90, **#98** | lumio-wiki CLI/skill |
| #92 (Maintainer QA dashboard) | #86, #91, **#102** | workspace foundation |

Every workspace-coupled blocker points at the foundation tickets (#102
workspace, #98 lumio-wiki CLI/skill) introduced by the migration — never
at #104. #104 owns only certification and documentation.

### Ownership-language note

The prose in #90 and #91 still uses the legacy phrase "Core SDK operation"
rather than naming `lumio_wiki` directly. Updating that prose is owned by
those issues (their scope is the finder/skill behavior), not by this
certification pass. #104's contract is that the **implementation** of those
issues, when it lands, must import from `lumio_wiki` — an invariant locked
at the AST level by
`tests/test_package_contraction.py::test_production_modules_do_not_import_compatibility_surfaces`.

### Automated dedup guardrails

- `tests/test_workspace_certification.py` — structural ownership (one
  Python root per wheel, bounded inter-member ranges).
- `tests/test_package_contraction.py` — no internal caller imports
  `lumio.core.*` compatibility shims; no compatibility module owns a
  duplicate implementation; dependency direction is one-way.

## Out of scope

- A separately published `lumio-web` distribution. The existing `lumio` name
  already represents the deployable web product (ADR-0010).
- Structured dataset query execution. The DuckDB seam remains post-MVP
  (PRD-0001). Ingestion may still create Table or Dataset Descriptors without
  making data queryable.
- Multi-tenant deployments. The MVP is deliberately single-tenant.
