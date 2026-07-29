---
title: "Google Open Knowledge Format (OKF) comparison"
status: "research"
sources:
  - "https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf"
  - "https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md"
  - "https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/src/reference_agent/bundle/document.py"
- "https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/3fcbb9f828c2f23d109c855ee403c3a4c81f3a96/okf/SPEC.md"
---

# Google Open Knowledge Format (OKF) comparison

## Scope

Compared Google Cloud's OKF v0.1 draft and reference implementation with Lumio's current compiled-Markdown Knowledge Base model.

## OKF v0.2 update (2026-07-28)

OKF v0.2 keeps the v0.1 bundle structure, reserved `index.md` / `log.md`, required `type`, recommended `title` / `description` / `resource` / `tags`, Markdown-link graph, and permissive consumer rules. It then makes provenance, trust, lifecycle, freshness, and attested computation first-class:

- **Breaking:** `timestamp` is superseded by `generated: { by, at }`; consumers may use it as a legacy fallback.
- **Breaking:** the conventional body `# Citations` list is superseded by structured frontmatter `sources`; consumers may parse the legacy section for old bundles.
- **Additive:** `sources[]` with `resource`, optional `id` / `title`, and credibility signals (`author`, `usage_count`, `last_modified`, `usage_window`).
- **Additive:** `generated`, `verified`, actor conventions, derived trust tiers, `status`, and `stale_after`.
- **Additive:** the `Attested Computation` concept with `runtime`, `parameters`, `computation`, `executor`, and `attester`.
- The reference implementation now matches the specification: only `type` is always required. The v0.1 implementation/spec mismatch documented below is resolved upstream.

Lumio's response is **OKF Exchange Profile 2** (ADR-0015), pinned to OKF v0.2 at upstream commit `3fcbb9f828c2f23d109c855ee403c3a4c81f3a96`. Profile 1 remains pinned and unchanged. Profile 2 maps standard `sources` and derived `status`, recognizes trust/freshness/computation fields with explicit diagnostics, and still refuses to treat foreign trust metadata as Lumio Maintainer approval or to execute computation contracts.

## What OKF contributes

- A vendor-neutral exchange unit: a directory of Markdown files with YAML frontmatter, designed to be readable, diffable, portable, and consumable without a proprietary SDK.
- A minimal concept vocabulary: `type`, `title`, `description`, optional `resource`, `tags`, and `timestamp`, with producer-defined extensions.
- Progressive disclosure through generated `index.md` files at bundle and subdirectory levels.
- Explicit cross-links between concepts and conventional `# Schema`, `# Examples`, and `# Citations` sections.
- Optional `log.md` files for human-readable update history.
- Permissive consumers: unknown types and extension keys should not make a bundle unusable; broken links are tolerated.

## Alignment with Lumio

Lumio already has the core OKF strengths: a portable Markdown filesystem Knowledge Base, YAML metadata, Git/storage portability, summaries, tags, provenance sources, typed relationships, lifecycle/visibility, synthetic pages, validation, and derived indexes. Lumio's typed `relationships` frontmatter and citation-ready line ranges are more explicit than OKF's generic Markdown-link graph.

## Recommended adoption

### Decision: adopt the Navigation Index as a native derived artifact

A **Navigation Index** is a generated Markdown guide to the Compiled Pages beneath a Knowledge Base directory. It supports progressive disclosure for people and agents but is neither canonical knowledge nor a Synthetic Page. It is a Lumio-native artifact rather than an OKF-only export feature; the OKF Exchange Profile may map Navigation Indexes to OKF `index.md` files.

Navigation Indexes are materialized alongside Compiled Pages in every Published Version so filesystem readers and coding agents can use them without Lumio. Publish and sync regenerate them from canonical content. Page loading and canonical content fingerprinting exclude them, and direct edits never become source-of-truth knowledge.

The case-insensitive basename `index.md` is reserved in every Knowledge Base directory for Navigation Indexes. Each generated file carries a machine-readable Lumio marker. An existing reserved path without a valid marker is a validation error rather than being ignored or overwritten; a marked index may be regenerated atomically.

Lumio uses a hybrid content contract. The root `index.md` is an exhaustive catalog of every Compiled Page, organized by directory, following the Karpathy LLM Wiki access pattern. Each non-root `index.md` is a shallow local guide listing immediate child directories and immediate Compiled Pages, following OKF-style progressive disclosure. Page entries contain Canonical Page Title, relative Markdown link, and `summary`; indexes omit exchange-only metadata, provenance, relationships, tags, and generated timestamps. Directories precede pages, empty sections are omitted, and deterministic path ordering makes unchanged inputs byte-identical.

### Decision: keep `type` inside the OKF Exchange Profile

Do not add `type` or a page-kind equivalent to canonical `CompiledPage` metadata. The exchange layer accepts any non-empty imported type and surfaces it in preview and diagnostics, but canonical publication drops it. Export synthesizes the fixed values `Lumio Compiled Page` for Compiled Pages and `Lumio Navigation Index` for Navigation Indexes. These values do not change Lumio's canonical model. If a future Lumio use case independently needs page classification, define it from Lumio's domain requirements rather than inheriting OKF's vocabulary.

### Decision: keep `resource` inside the OKF Exchange Profile

Do not add OKF `resource` to canonical `CompiledPage` metadata and do not map it to Lumio `sources`: a described asset and supporting provenance are different concepts. The exchange layer may preserve `resource` during a direct OKF-to-OKF conversion, but canonicalization does not retain it. If Connector-backed assets later need stable external identity, define that Lumio-native concept from Connector requirements and map it to OKF at the boundary.

### Decision: tolerate extensions without promising lossless canonical round trips

Import accepts unknown frontmatter keys and unknown `type` values, surfaces them in preview and diagnostics, and reports which values canonicalization will drop. A direct OKF-to-OKF conversion may preserve them, but an OKF import → canonical Lumio publish → later OKF export is intentionally lossy: only explicitly mapped Lumio semantics survive. The initial profile does not add opaque extension state or a metadata sidecar to the canonical Knowledge Base. Persistent lossless round-tripping is deferred until a concrete interoperability use case justifies its identity and lifecycle complexity.

### Decision: use a minimal standard-field mapping

Import maps OKF `title` to Canonical Page Title, `description` to `summary`, `tags` to canonical tags, and the Markdown body to the proposed Compiled Page body. Export performs the inverse mapping. `resource`, `timestamp`, and unknown keys are previewed but dropped on canonical publication and omitted from canonical export. Export omits `timestamp` rather than misrepresenting export or Git time as OKF's “last meaningful change.”

When `title` is absent, import proposes a reviewable title from the first level-one heading or, failing that, a humanized filename stem, and emits a diagnostic identifying the fallback. Normal canonical title and collision validation still gates publication.

### Decision: preserve Lumio semantics in a versioned extension

Export includes a namespaced `lumio` object with `profile_version: 1` plus aliases, lifecycle, visibility, synthetic status, structured sources, and typed relationships. Generic OKF consumers may ignore it; a Lumio-aware importer maps a recognized version back into a reviewable proposal and applies normal canonical validation. Unknown future `lumio` keys produce diagnostics and are dropped on canonical publication. Standard `description` and `tags` carry summary and tags without duplication inside the extension, and canonical sources never map to OKF `resource`.

### Decision: treat the Markdown body as prose

Import preserves the Markdown body unchanged. Export preserves it unless a surviving typed Relationship has no corresponding body link, in which case export appends an ordinary `See also` link to the target for OKF-only consumers; canonical pages are never mutated. Conventional `# Schema`, `# Examples`, and `# Citations` sections remain prose. Import does not infer typed relationships or canonical sources from body links, and export does not synthesize conventional sections. Structured provenance and typed relationships travel through the validated `lumio` extension; reviewed semantic extraction from third-party prose is deferred to a separate future conversion workflow.

### Decision: export only an explicitly authorized visibility scope

The serializer receives an already-authorized set of Compiled Pages from the Core SDK and cannot widen it. Root and directory Navigation Indexes are regenerated from only included pages, so omitted titles and summaries do not leak. Structured relationships to excluded targets are removed with diagnostics; body links remain unchanged but broken links introduced by filtering are reported. Included pages retain their `lumio.visibility`, and audit events record the selected visibility scope and page count. A full export occurs only when the authorization layer explicitly supplies all visibility classes.

### Decision: use safe defaults for generic OKF imports

Without a recognized `lumio` extension, import proposes `lifecycle: draft`, `visibility: internal`, `synthetic: false`, and a deterministic Source whose identity combines bundle identity with the document's relative path. The Source URL is the bundle origin only when known; OKF `resource` never substitutes for provenance. Imported tags are retained, while missing or empty tags leave the proposal blocked for Maintainer classification. Missing descriptions remain summary warnings. A recognized, validated `lumio.profile_version` overrides these defaults with its aliases, lifecycle, visibility, synthetic status, sources, and relationships.

### Decision: classify conventional bundle artifacts by reserved path

On OKF import, every case-insensitive `index.md` is a navigation artifact: it may be checked for malformed or broken links but is neither authoritative nor proposed as a Compiled Page. Every root or directory `log.md` is optional exchange history: it is previewed but does not create audit events or a Compiled Page. Canonicalization drops both, and publication regenerates Navigation Indexes from accepted pages; direct OKF-to-OKF conversion may preserve them.

Other Markdown files become page candidates. Broken prose links are warnings, while unresolved typed relationships in a recognized `lumio` extension remain canonical validation errors. Unsupported non-Markdown files are listed and skipped. Unsafe paths, traversal, escaping symlinks, duplicate case-folded paths, and colliding archive entries are rejected before proposals are produced.

### Decision: pin OKF Exchange Profile 1

**OKF Exchange Profile 1** targets OKF v0.1 at upstream commit [`ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a`](https://github.com/GoogleCloudPlatform/knowledge-catalog/commit/ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a), dated 2026-06-12. `lumio.profile_version: 1` is independent of the upstream version. Profile 1 behavior never changes merely because upstream `main` changes; incompatible upstream changes require a deliberate new Lumio profile. Import may parse an unidentified profile permissively for preview, but canonical publication requires explicit profile selection or approval. Exports are described as “Lumio OKF Exchange Profile 1,” never as unqualified OKF compliance.

### Do not adopt as canonical replacements

- Do not replace Lumio's typed `relationships` with untyped Markdown links. Keep typed, validated relationships as the graph source of truth; Markdown links can remain useful in prose and exports.
- Do not replace `summary` with a second independently edited `description`. If OKF export is needed, map Lumio's summary to OKF's description at the boundary to avoid metadata drift.
- Do not use `log.md` as the authoritative audit trail or generate one in Profile 1. Lumio's SQLite audit events and Git/publish history provide stronger machine-readable and operational provenance; imported logs are preview-only exchange history.
- Do not copy the OKF reference agent's required-field behavior blindly. The v0.1 specification says only `type` is required and other fields are recommended, while `reference_agent/bundle/document.py` currently defines `type`, `title`, `description`, and `timestamp` as required. Lumio should follow its own trust and validation invariants.

## Conclusion

OKF is an optional, best-effort interchange and presentation profile, not Lumio's canonical domain model. Lumio adopts native hybrid Navigation Indexes: one exhaustive Karpathy-style root catalog plus shallow OKF-style directory indexes, materialized in Published Versions but excluded from canonical page semantics and fingerprinting. OKF `type`, `resource`, conventional body sections, and unknown extensions remain exchange-boundary concerns. Profile 1 provides explicit standard-field mappings, a versioned `lumio` extension for Lumio-owned trust semantics, safe generic-import defaults, visibility-filtered export, and transparent diagnostics without promising lossless third-party round trips. Lumio's Compiled Pages, provenance, lifecycle, visibility, typed relationships, and citation model remain canonical.

OKF v0.2 does not change that boundary, but it does make structured provenance and lifecycle standard rather than producer-specific. Lumio therefore supports v0.2 through a separately pinned Profile 2: standard `sources` and derived `status` are adopted at the exchange boundary, while trust events, freshness, source credibility, and Attested Computation remain disclosed exchange metadata unless Lumio independently promotes them into the canonical model.
