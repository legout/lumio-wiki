---
status: accepted
---

# ADR-0007: Native Navigation Indexes and a Pinned OKF Exchange Profile

## Context

Lumio already uses portable, compiled Markdown as its canonical Knowledge Base and has stronger provenance, lifecycle, visibility, relationship, validation, and citation semantics than the draft Google Open Knowledge Format (OKF). Adopting OKF directly would weaken those invariants, while ignoring it would miss useful filesystem navigation and exchange conventions. The decision is difficult to reverse because it reserves paths inside every Knowledge Base and establishes a public interchange contract.

## Decision

Lumio adopts the **Navigation Index** as a native derived artifact. Every Published Version materializes a reserved, marked `index.md` at the root and in each directory containing Compiled Pages. The root index is an exhaustive Karpathy-style catalog of every Compiled Page, grouped by directory; non-root indexes are shallow OKF-style guides to immediate directories and pages. Navigation Indexes are deterministic, contain titles, relative links, and summaries, and are excluded from Compiled Page loading, retrieval, relationships, and canonical content fingerprinting. An unmarked case-insensitive `index.md` collision is a validation error.

Lumio also defines the optional, best-effort **OKF Exchange Profile 1**, pinned to OKF v0.1 at upstream commit `ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a`. Lumio never claims unqualified OKF compliance and never changes Profile 1 merely because upstream `main` changes. Incompatible upstream changes require a deliberate new Lumio profile.

OKF `type`, `resource`, conventional body sections, and arbitrary producer extensions remain exchange-boundary concerns; they do not expand canonical `CompiledPage` metadata. Export maps title, summary, tags, and body to the corresponding OKF fields, synthesizes `Lumio Compiled Page` or `Lumio Navigation Index` as `type`, and uses a versioned `lumio` extension for aliases, lifecycle, visibility, synthetic status, structured Sources, and typed Relationships. The Markdown body remains prose and is not mined for canonical provenance or relationship semantics.

Generic imports are permissive and reviewable rather than lossless: unknown metadata is previewed and diagnosed but may be dropped on canonical publication. Generic page proposals default to `draft`, `internal`, non-synthetic content with deterministic provenance pointing to the imported OKF document; absent tags remain a blocking Maintainer decision. Imported `index.md` and `log.md` files are exchange artifacts rather than Compiled Pages. Export operates only over an explicitly authorized page set and regenerates Navigation Indexes from that set so excluded titles and summaries cannot leak.

### Consumer-tolerance boundary (addendum, #139)

At the OKF import boundary, an unknown non-empty `type`, arbitrary producer frontmatter key, or unresolved ordinary Markdown body link is not grounds to reject a foreign bundle. The importer returns a previewable proposal and records a dropped-metadata diagnostic or a broken-link warning; body links remain prose and are never promoted to canonical Relationships. The subsequent canonical Proposal Pipeline remains stricter: unresolved typed Relationships in a recognized `lumio` extension are validation errors. Profile 1 deliberately has no opaque extension sidecar, so unknown producer metadata is lossy after canonical publication; the diagnostic is the required disclosure rather than an implied preservation promise.

The detailed Profile 1 mapping, diagnostics, structural rules, and rejected substitutions are recorded in `docs/research/okf-comparison.md`.

## Considered Options

- A single root index alone was rejected because it loses OKF-style progressive disclosure below the root.
- Directory indexes alone were rejected because they lose the Karpathy-style global catalog that agents can read before drilling into pages.
- Canonical `type`, `resource`, and opaque extension fields were rejected because OKF vocabulary should not redefine Lumio's domain model.
- Persistent lossless storage of arbitrary third-party extensions was deferred because it requires a sidecar identity and lifecycle model across page rename, merge, and deletion.
- Tracking the moving OKF v0.1 `main` branch was rejected because draft changes would silently alter Lumio's exchange contract.

## Consequences

Publishing and syncing must regenerate Navigation Indexes atomically, while loading and fingerprinting must recognize and exclude valid generated indexes. Import/export must provide diagnostics, proposal review, path-safety checks, profile-version handling, visibility filtering, and validation of the `lumio` extension. Generic third-party metadata can be lost after canonicalization, but the loss is disclosed before publication and Lumio's canonical trust model remains intact.
