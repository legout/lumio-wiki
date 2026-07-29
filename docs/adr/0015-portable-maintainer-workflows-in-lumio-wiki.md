---
status: accepted
---

# ADR-0015: Portable Maintainer Workflows in the lumio-wiki CLI

## Context

Issue #91 shipped `lint` and `cross-linker` as role-gated Agent Skills in the
application package (`lumio.skills`, ADR-0012). They are already thin wrappers
over public `lumio_wiki` operations — but a coding agent working from a
project's `AGENTS.md` cannot discover or run them: they have no `lumio-wiki`
CLI verb, the packaged SKILL.md never mentions them, and importing them pulls
in the whole application package just for an `AuthContext` role gate.

At the same time, the Maintainer QA roadmap (PRD #83, prior art in
`docs/research/llm-wiki-prior-art.md`) calls for a periodic **Dream Cycle**:
a composed maintenance pass that runs deterministic health and structural
diagnostics, surfaces missing-link candidates, and stages one reviewable
batch of repairs — the "reflection" loop a living Knowledge Base needs. No
such composed workflow exists in any package.

The portable `lumio-wiki` package is model-free and runs wherever a coding
agent runs. Its operator is, by definition, the Maintainer: there is no
Reader on a local CLI, and every mutating operation already flows through the
proposal-first guardrail (stage → validate → review → publish).

## Decision

1. **Maintenance workflows live in `lumio_wiki`.** A new public module
   `lumio_wiki.maintenance` carries:
   - `run_lint` — the read-only cross-page QA report (validation, canonical
     and Discovery Graph structural diagnostics, Extracted References, and
     the graph-scope disclosure), ported from `lumio.skills.lint`.
   - `stage_cross_link_proposal` / `stage_relationship_proposal` — reviewable
     repair staging over the Proposal Pipeline, ported from
     `lumio.skills.cross_linker`.
   - `run_dream_cycle` / `stage_dream_repairs` — the composed Dream Cycle:
     read-only reflection (validation + health + structural diagnostics for
     both scopes + link candidates ranked by Discovery Graph impact), plus an
     explicit opt-in staging step that turns the top-ranked repairs into
     ordinary reviewable Ingest Proposals.
2. **Three new CLI verbs** expose them: `lumio-wiki lint`, `lumio-wiki
   cross-link`, and `lumio-wiki dream`. `lint` is read-only; `cross-link`
   lists candidates and only stages with `--stage`; `dream` reflects and only
   stages with `--stage`.
3. **No role gate in the portable CLI.** The `AuthContext`/`authorize_skill`
   gate stays in the application layer (`lumio.skills`), which keeps its
   Readers-cannot-run semantics for the deployed app. The portable CLI has no
   identity provider; the proposal-first guardrail is the write protection.
   The legacy `lumio.skills` wrappers remain authoritative in the app and are
   expected to delegate to `lumio_wiki.maintenance` (dedup tracked
   separately).
4. **`lumio-wiki setup` writes the full protocol.** The generated `AGENTS.md`
   section documents the retrieval ladder, ingest, maintenance (`lint`,
   `cross-link`, `dream`), guardrails, and diagnostics, so any coding agent
   in any project gets the complete workflow from first setup.

## Consequences

- Coding agents can lint, find missing links, and run a Dream Cycle from any
  project with zero application-package dependency.
- Every mutation remains proposal-first: `cross-link --stage` and
  `dream --stage` produce `staged` proposals; nothing direct-writes.
- The Dream Cycle is deterministic and model-free. Semantic maintenance
  (contradiction detection, staleness judgment, summary quality) is
  deliberately out of scope and left to a future LLM-assisted Maintainer
  workflow, per the health-vs-lint boundary in the prior-art research.
- `lumio.skills.lint` / `lumio.skills.cross_linker` temporarily duplicate
  logic now in `lumio_wiki.maintenance`; converging them is follow-up work.
