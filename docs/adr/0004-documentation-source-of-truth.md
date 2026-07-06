---
status: accepted
---

# ADR-0004: Documentation source-of-truth policy

## Context

As Lumio's README grew to serve developers, sys admins, and users, three
questions had no recorded answer: does the README describe the **as-built**
system or the approved MVP **vision**? Where does the canonical Compiled Page
frontmatter schema live (it was defined only in code)? And does the README
duplicate the glossary/vision or defer to existing docs? Without a recorded
policy the README risks drifting into restating unbuilt features as if they
work, and the frontmatter schema risks having no human-readable source of
truth.

## Decision

1. **The README documents as-built reality.** It describes only what runs
   today, plus a clearly-flagged Roadmap section. The vision lives in PRDs;
   decisions live in ADRs; the ubiquitous language lives in `CONTEXT.md`
   (glossary-only). The README defers depth via links rather than duplicating
   these.
2. **`docs/kb-format.md` is the canonical human-readable Compiled Page
   frontmatter reference**, mirrored from `src/lumio/core/records.py` (record
   types) and `src/lumio/core/knowledge_base.py` (validation rules). **When the
   two disagree, the code is correct** and the doc must be updated. It is kept
   out of `CONTEXT.md`, which is a glossary, not a spec.

## Considered Options

- **README documents the full MVP vision** — rejected: it would describe
  unbuilt features (semantic search, Connectors, Datasets) as if they work,
  misleading operators. PRDs already own the vision.
- **Frontmatter schema inline in the README** — rejected: makes the README the
  de facto schema source of truth while it also tries to be a landing page, and
  bloats it.
- **Frontmatter schema in `CONTEXT.md`** — rejected: violates the glossary-only
  rule; the schema is a spec, not ubiquitous language.

## Consequences

Each concern has one owner: reality → README, vision → PRDs, decisions → ADRs,
language → `CONTEXT.md`, schema → `docs/kb-format.md`. The README stays
scannable and honest.

The cost is a mirroring discipline: `docs/kb-format.md` must be updated whenever
records.py or the validator changes. That discipline is owned by this ADR;
#13 tracked its introduction and was closed in favor of this record.
