---
status: accepted
---

# ADR-0011: Derived Reference Graph and Progressive Graph Storage

## Context

Lumio's canonical graph currently contains only typed Relationships authored in
Compiled Page frontmatter and resolved by Canonical Page Title. ADR-0007 also
keeps ordinary Markdown body links as prose rather than mining them for
canonical provenance or Relationship semantics. That protects Lumio's trust
model, but it leaves useful, explicit navigation structure unavailable to the
zero-index retrieval path and makes large imports depend on Maintainers
reviewing every graph connection before agents can use it to discover context.

The standalone `lumio-wiki` direction in ADR-0010 requires deterministic search,
relationship traversal, graph paths, a coding-agent protocol, and reusable Agent
Skills without LanceDB. LanceDB remains an optional adapter for BM25, semantic,
and hybrid Evidence retrieval. The graph therefore needs a lightweight
materialization that works with or without LanceDB, remains rebuildable from
Markdown, and can later scale beyond an in-memory representation without
changing client behavior.

## Decision

### Distinguish canonical Relationships from Extracted References

A **Relationship** remains a typed, directed, canonical claim expressed in
frontmatter by Canonical Page Title. This ADR does not weaken its validation or
review requirements.

An ordinary internal Markdown link in a Compiled Page body deterministically
produces an **Extracted Reference** after its destination resolves to exactly one
Compiled Page. An Extracted Reference records source and target page identity,
its `markdown-link` origin, source path and line range, and extractor version. It
has only reference/navigation meaning: it does not infer a domain-specific
Relationship type such as `uses`, `implements`, or `works-at`, and it is not
written back into frontmatter.

This narrowly amends ADR-0007: the Markdown body is still not mined for
canonical provenance or Relationship semantics, but explicit internal links may
be materialized as non-canonical retrieval topology. Valid marked Reserved
Artifacts remain excluded from Compiled Page loading and therefore never
contribute Extracted References.

Lumio exposes two graph scopes internally:

- the **canonical graph**, containing reviewed typed Relationships; and
- the **Discovery Graph**, containing canonical Relationships plus Extracted
  References.

The Discovery Graph selects context to inspect. An Extracted Reference is never
Evidence and cannot by itself support an answer claim. The Agent Runtime must
still answer from citation-ready Evidence derived from the selected Compiled
Pages. Retrieval Traces disclose graph scope, edge origin, and source location.

### Derive automatically; review exceptions and semantic promotion

Resolved Extracted References are deterministic derived state and do not require
individual Maintainer approval. Broken, escaping, ambiguous, duplicate, or
visibility-ineligible targets are excluded and surfaced as actionable validation
or health diagnostics. Traversal applies authorization before endpoint
resolution and expansion, and uses bounded depth, edge, and result limits.

Adding a missing Markdown link, inferring a typed semantic Relationship from
prose, or promoting an Extracted Reference into a canonical Relationship remains
a proposal-first change. Deterministic and model-assisted finders may emit
candidates with provenance and confidence, but no inferred semantic type becomes
canonical automatically. Large imports are reviewed by exception and aggregate
quality signals rather than by requiring approval of every resolved reference.

### Store a rebuildable MessagePack graph beside optional LanceDB indexes

Markdown remains the source of truth. The preferred persisted graph
materialization is a versioned MessagePack artifact in the configured derived
index directory. It stores adjacency in both outgoing and incoming directions
and carries the Knowledge Base fingerprint and extractor version. A missing,
stale, corrupt, or incompatible artifact is discarded and rebuilt from
Compiled Pages. Zero-index operation may derive the same graph directly in
memory when no persisted graph has been built.

When `lumio-lancedb` is installed, its Evidence, BM25, vector, and hybrid tables
live beside the MessagePack graph under the same logical index directory and
share the same Knowledge Base fingerprint. LanceDB does not own or canonically
store the graph. Graph expansion selects page identities; the configured
retrieval implementation then ranks citation-ready Evidence from those pages.

If measured graph startup time, memory use, or traversal latency outgrows the
MessagePack representation, Lumio may materialize the same derived edges in
SQLite for an embedded Python deployment or PostgreSQL for a shared server
deployment. PGLite may be evaluated where its runtime integration is
appropriate. The public Knowledge Base and retrieval interfaces must not expose
which graph materialization is used. A storage-adapter seam is introduced only
when a second implementation is actually required.

## Considered Options

- **Treat every Markdown link as a canonical Relationship** — rejected because a
  link proves reference/navigation, not a domain-specific semantic claim, and
  would silently weaken proposal-first trust.
- **Require Maintainer approval for every extracted link edge** — rejected
  because deterministic references in large imports would create an
  unreviewable queue without improving semantic correctness.
- **Keep only canonical Relationships in the graph** — rejected because it
  discards explicit topology already present in Markdown and weakens zero-index
  agent navigation.
- **Store the graph only in LanceDB** — rejected because `lumio-wiki` must work
  without LanceDB and LanceDB is optimized for Evidence retrieval rather than
  recursive graph traversal.
- **Store the graph only in memory** — rejected as the sole strategy because
  large Knowledge Bases would pay repeated extraction and startup costs.
- **Adopt SQLite, PostgreSQL, or a graph database immediately** — rejected until
  measurements justify the operational dependency; the MessagePack
  materialization preserves a migration path without premature infrastructure.

## Consequences

The Core SDK must add deterministic internal-link resolution, incoming and
outgoing adjacency, graph-scope-aware traversal, truthful Retrieval Trace stages,
and graph freshness checks. The coding-agent CLI and Agent Skill can use Hot and
Navigation Indexes, zero-index search, page reads, related-page lookup, and graph
paths without LanceDB; `lumio-lancedb` remains a progressive retrieval
enhancement over the same Evidence contract.

Maintainer QA work in #83, #86, #90, and #91 remains proposal-first when it
changes Markdown or promotes semantic Relationships. Those issues must treat
already-present, exactly resolved Markdown links as derived Extracted References
rather than requiring a second approval step. Workspace issues #94, #96, #98,
and #99 must keep graph ownership in `lumio-wiki`, add the MessagePack
materialization to the zero-index capability, and keep LanceDB limited to its
retrieval adapter role.

The cost is a stricter distinction between topology used to discover Evidence
and semantic claims safe to present as Relationships. Tests must prove
deterministic rebuilds, stale-cache rejection, path and alias resolution,
authorization-before-traversal, bounded expansion, exception diagnostics, and
identical citation contracts with and without LanceDB.
