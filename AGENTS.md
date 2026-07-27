# AGENTS.md

## Project Context

This repository contains the Lumio project: deployable chat for trusted knowledge and data. The MVP is a single-tenant, deployable browser agent platform over compiled Markdown knowledge bases.

Before implementation work, read:

- `CONTEXT.md` — the project's ubiquitous language (glossary).
- `docs/prd/0001-knowledge-agent-platform.md` — the approved platform PRD.
- `docs/adr/` — architectural decisions that govern implementation choices.

The intended architecture is an SDK-centered modular monolith: one deployable app for the MVP, with a reusable Knowledge Base Core SDK and thin clients for web, external chat UIs, future CLI, and local coding agents.

## Validation

- Run focused tests serially while developing: `uv run pytest -q <test paths>`.
- Run the full suite with four workers: `uv run pytest -q -n 4`.
- Do not use `-n auto`; a fixed worker count keeps local and CI resource use predictable.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues in `legout/lumio` (uses the `gh` CLI). External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default mattpocock vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` at the repo root + `docs/adr/`. See `docs/agents/domain.md`.

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

1. Author a Compiled Page (YAML frontmatter + Markdown body) in a temp file.
2. `lumio-wiki ingest <file>` — stages a reviewable Ingest Proposal.
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
- `lumio-wiki validate` — exit 0 if valid, 1 otherwise.
- `lumio-wiki lint` — full QA report (superset of validate + structural diagnostics).
