# AGENTS.md

## Project Context

This repository contains the Lumio project: deployable chat for trusted knowledge and data. The MVP is a single-tenant, deployable browser agent platform over compiled Markdown knowledge bases.

Before implementation work, read:

- `CONTEXT.md` — the project's ubiquitous language (glossary).
- `docs/prd/0001-knowledge-agent-platform.md` — the approved platform PRD.
- `docs/adr/` — architectural decisions that govern implementation choices.

The intended architecture is an SDK-centered modular monolith: one deployable app for the MVP, with a reusable Knowledge Base Core SDK and thin clients for web, external chat UIs, future CLI, and local coding agents.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues in `legout/lumio` (uses the `gh` CLI). External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default mattpocock vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` at the repo root + `docs/adr/`. See `docs/agents/domain.md`.
