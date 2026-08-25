# PRD-0005: Graph Exchange Export and Stub Import

_Status: approved for implementation. Governing decision: ADR-0024. Originates from the 2026-08-24 implementation review and the obsidian-wiki wiki-export/wiki-import skill survey._

## Problem Statement

The Knowledge Graph and Discovery Graph are locked inside Lumio: there is no standard-format export for external analysis and visualization tools, and no way to bootstrap a Knowledge Base from another tool's graph export. OKF Profiles 1/2 cover content-bearing exchange, but not the structure-only, tool-interop case that `graph.json` (NetworkX node_link) and GraphML serve.

## Solution

Two deterministic, model-free operations in the Core SDK and `lumio-wiki` CLI:

- `export-graph` — writes `graph.json` (node_link) and `graph.graphml` over the authorized page set. Nodes: page identity, title, Content Category, tags, summary. Edges: typed canonical Relationships plus marked-untyped Extracted References. Visibility filtering enforced exactly as at the OKF export boundary.
- `import-graph` — reads a `graph.json` (Lumio or wiki-export lineage) and stages stub Compiled Pages (frontmatter skeleton + link structure, no bodies) as one ordinary reviewable Ingest Proposal.

Agent skills gain no implementation role; the shipped SKILL.md documents when to use which exchange surface.

## User Stories

1. As a **Maintainer**, I want to export the Discovery Graph to GraphML, so that I can visualize the Knowledge Base in Gephi/yEd.
2. As a **data analyst**, I want `graph.json` in NetworkX node_link format, so that I can load the graph in Python without Lumio code.
3. As a **Maintainer**, I want typed Relationships distinguishable from Extracted References in the export, so that consumers know which edges are reviewed knowledge.
4. As an **Owner**, I want visibility filtering enforced on graph export, so that internal/restricted page titles never leak into an exported artifact.
5. As a **Maintainer**, I want to bootstrap a new Knowledge Base from an external `graph.json` as a reviewable stub proposal, so that structure arrives without unreviewed content.
6. As a **Maintainer**, I want stub import to flow through the normal Proposal Pipeline, so that merge/overwrite decisions are made on a reviewed diff, never by a blind mode flag.
7. As a **local coding agent**, I want the SKILL.md to tell me which exchange command fits my task, so that I don't improvise format conversions.

## Implementation Decisions

- `graph.json` node fields are additive-only once shipped (stability note documented).
- No new dependencies: node_link JSON via stdlib/msgspec; GraphML via stdlib XML.
- Export operates on the in-memory graph the Core SDK already derives; no new graph materialization.
- import-graph validates input shape, maps category by directory convention, and reports unresolvable-node links as proposal diagnostics (broken links tolerated, disclosed).
- Cypher/Postgres/HTML exports are explicitly deferred until a real consumer appears.

## Testing Decisions

- Gold-file tests for both export formats over `eval/fixture_kb` (node/edge counts, typed-vs-untyped edge marking, visibility exclusion).
- Round-trip test: export-graph → import-graph → proposal validates cleanly.
- Import tests: foreign wiki-export graph.json, malformed input, broken-link diagnostics.
- Security test: `restricted` page titles/summaries absent from every export artifact.

## Out of Scope

- Body-content export in graph.json (that is OKF Profile 2's job).
- `cypher.txt`, `postgres.sql`, `graph.html` outputs.
- Interactive visualization inside the web app (Constellation already serves in-product graph viewing).
- Automatic/scheduled exports.
