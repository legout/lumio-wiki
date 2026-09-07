# AGENTS.md

## Project Context

This repository contains the open-source Lumio foundation: the portable,
model-free Knowledge Base SDK (`packages/lumio-wiki`) and the optional LanceDB
enhanced-retrieval adapter (`packages/lumio-lancedb`). The deployable Lumio web
application lives in a separate private repository and consumes these published
wheels (ADR-0025).

Before implementation work, read:

- `CONTEXT.md` — the project's ubiquitous language (glossary).
- `docs/prd/0002-core-sdk.md` — the Core SDK PRD.
- `docs/adr/` — architectural decisions that govern implementation choices.
  ADR-0010 defines the workspace and packaging; ADR-0025 records the
  repository split.

## Validation

- Run focused tests serially while developing: `uv run pytest -q <test paths>`.
- Run the full suite with four workers: `uv run pytest -q -n 4`.
- Do not use `-n auto`; a fixed worker count keeps local and CI resource use predictable.

## License

Contributions are licensed Apache-2.0 (see LICENSE).

<!-- lumio-wiki-kb -->
## Lumio Knowledge Base

This project uses a Lumio Knowledge Base for domain knowledge. The host coding
agent IS the default Distiller (no model provider needed for base ingestion).

**KB path:** `/home/volker/coding/lumio/.wiki` (also in `.env` as `LUMIO_KB_PATH`; the CLI reads it
automatically when no `<kb>` argument is given).

### Retrieval ladder (cheapest-first, stop when you have Evidence)

0. `lumio-wiki hot` — Maintainer-pinned entry pages. Read first.
1. `lumio-wiki index [dir]` — generated Navigation Index (all pages by directory).
2. `lumio-wiki search "<query>"` — zero-index lexical search (no external index).
3. `lumio-wiki page "<title>"` — read a page to confirm and cite the exact passage.
4. `lumio-wiki related "<title>" --scope discovery` — related pages (canonical + extracted).
5. `lumio-wiki paths "<src>" "<dst>"` — shortest directed path between two titles.

### Ingest (you are the Distiller)

1. Author a Compiled Page (YAML frontmatter + Markdown body) that declares the
   source identity in `sources[].id`.
2. `lumio-wiki ingest <original-source> --compiled-page <page.md> --source-id <id>`
   — bind the ORIGINAL raw source to your authored page under one stable
   identity and stage a single reviewable Ingest Proposal. No `[documents]`
   extra required (the converter name is derived from routing without running
   it). Plain `lumio-wiki ingest <file>` stays available for text/Markdown
   passthrough but does NOT establish a Source identity.
3. `lumio-wiki proposal list` → `proposal inspect <id>` → `proposal validate <id>`.
4. `lumio-wiki publish <id>` (or `lumio-wiki discard <id>`).

### Maintenance (you are the Maintainer)

- `lumio-wiki lint` — read-only cross-page QA: validation, graph health, and
  canonical/discovery structural diagnostics with scope disclosure. Exit 1 if invalid.
- `lumio-wiki cross-link` — missing-link candidates ranked by Discovery Graph
  impact. Add `--stage` to stage reviewable repair proposals (never direct-writes).
- `lumio-wiki dream` — the Dream Cycle: read-only reflection (validation +
  health + structure + ranked candidates). Add `--stage [--limit N]` to stage
  the top repairs as ordinary Ingest Proposals for review. Add opt-in `--semantic`
  with the `[llm]` extra for semantic findings; it remains proposal-first.

### Guardrails

- **Cite or refuse.** Every domain claim cites a Compiled Page (title + path +
  passage). Unsupported claims return "not covered by this knowledge base."
- **Connectivity is not support.** Graph reachability selects pages to inspect;
  it never manufactures Evidence.
- **Proposal-first.** Validation always runs before publish. Never write `.md`
  files directly to the KB root.

### Diagnostics

- `lumio-wiki doctor` — version, detected extras, skill location.
- `lumio-wiki health` — page counts, validation, Discovery Graph health.
- `lumio-wiki status [<kb> | --json]` — effective configuration and retrieval state.
- `lumio-wiki validate` — exit 0 if valid, 1 otherwise.
- `lumio-wiki lint` — full QA report (superset of validate + structural diagnostics).
<!-- pi-implementation-orchestrator:start -->
## Agent workflow

- Every task declares one test obligation: `new-test`, `existing-check`, or `no-new-test`; focused TDD is required only for `new-test` work.
- Review is adaptive and orchestrator-owned: high-risk or dependency-defining changes are reviewed immediately; low-risk changes may be reviewed cumulatively at a wave boundary.
- Plans and tickets reference exact feature sources; this file defines stable repository-wide scope.
- Source precedence: current owner decision → accepted ADR → approved specification → implementation plan → ticket → existing implementation.
- Stop before implementation when authoritative sources conflict.

### Routing and authority

- Read `docs/agents/issue-tracker.md` and `docs/agents/domain.md` when their scope applies; preserve established project conventions.
- Use `shape-design` for unresolved behavior/design choices, `write-implementation-plan` for approved multi-step work, and `orchestrate-implementation` to execute approved work. Do not turn a trivial edit into a planning exercise.
- Default orchestrated execution to `supervised`: workers may implement and validate, but candidate assembly, integration, and publication retain explicit approval gates.
- Keep one writer per worktree. Use `pi-subagents` for spawned-child lifecycle; named persistent `pi-intercom` peers are read-only advisors, not workers or schedulers.
- Use `systematic-debugging` for unexpected failures and `verification-before-completion` before success claims; match evidence to the exact change and report skipped checks.
- Use `merge-worktree` for target integration and `make-release` for releases. Local integration does not authorize pushing; opening a PR does not authorize merging; release or publication requires its own approved plan.
- Stop on conflicting authoritative sources, unclear ownership, failed required gates, or missing required tooling. Never silently switch execution modes to bypass a blocker.

### Documentation map

- `CONTEXT.md`: canonical domain vocabulary for the whole repository.
- `docs/adr/`: accepted architecture decisions.
- `docs/agents/`: workflow and tracker configuration.
- `docs/specs/` or the configured tracker: feature behavior and acceptance.
- implementation plans/tickets: execution entry points and explicit source references.
<!-- pi-implementation-orchestrator:end -->
