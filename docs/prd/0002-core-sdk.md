# PRD-0002: Lumio Core SDK

_Status: approved behavior. Historical approval predates planning-contract v1; the exact approval reference and capture checkpoint are not recorded, so new execution requires an approved revision or bounded change. This PRD governs the deep loading/validation/retrieval seam. ADR-0010 later placed ingestion and publication in the broader `lumio-wiki` distribution; those capabilities do not expand this Core SDK seam. Stack decisions live in ADR-0001 and ADR-0002; vocabulary lives in `CONTEXT.md`._

## Problem Statement

Every Lumio client — the web chat UI, the admin/review UI, API clients, a future CLI, a future local coding-agent adapter — needs the same capability: load a compiled Markdown knowledge base, validate it, index it, and retrieve citation-ready evidence from it. Without a single shared core, each client re-implements parsing, validation, and retrieval, and they drift apart. The core must also stay free of web, runtime, auth, and model-provider concerns so it can be reused by clients that have nothing to do with the browser app.

## Solution

A deep Core SDK module: a small public seam hiding a large body of behavior. Callers learn one interface — load, validate, build an index, retrieve with citations and a trace, and check freshness — and get parsing, frontmatter validation, exact and graph lookup, lexical retrieval, evidence extraction, citation shaping, and staleness detection behind it. Markdown files are the durable source of truth; every index is derived and rebuildable from source alone.

## User Stories

1. As a client developer, I want to load a Knowledge Base from a filesystem path, so that I can use it without knowing how Markdown is parsed.
2. As a client developer, I want a single validate call that returns a report, so that I see every problem at once instead of crashing on the first.
3. As a client developer, I want to look up pages by canonical title, alias, tag, source, and lifecycle, so that I can answer structured questions without re-implementing indexing.
4. As a client developer, I want to traverse Relationships and resolve multi-hop graph paths, so that I can answer relationship questions.
5. As a client developer, I want to retrieve citation-ready Evidence for a free-text query, so that an answer can cite its sources.
6. As an operator, I want the SDK to detect a stale index after a source page changes, so that I know when to rebuild.
7. As an operator, I want to rebuild any index from source Markdown alone, so that no derived state is irreplaceable.
8. As a client developer, I want to use the SDK with no model provider available, so that retrieval and validation work offline.
9. As a future Connector author, I want Retrieval Results to allow non-Markdown Evidence, so that I can add dataset/table sources without changing the client contract.

## Implementation Decisions

These are the durable, seam-level decisions. Specific public symbol names and the internal file split are decided during implementation and are deliberately not fixed here.

- **Deep module, small seam.** Callers learn a small interface and get a large amount of behavior. The internal split — parsing, validation, exact lookup, graph traversal, lexical retrieval, freshness — is private to the implementation, not part of the interface. _Depth is a property of the interface, not the implementation: the SDK can be internally composed of small, swappable parts that simply are not part of the public seam._
- **The interface is the test surface.** Tests cross the same seam callers do. Any test that has to reach past the public interface is a signal the module is the wrong shape.
- **Framework-independent.** The Core SDK must not import the app framework, the frontend interactivity layer, the metadata ORM or its tables, the runtime/gateway, the web UI, auth/role modules, or any model-provider client. (Specific technology in ADR-0001; struct and validation layer in ADR-0002.)
- **One adapter means a hypothetical seam; two means a real one.** Internal seams used only by the SDK's own tests are fine; they are not exposed through the public interface just because tests use them.
- **Markdown is the durable source of truth; every index is derived and rebuildable.** Deleting an index and rebuilding from Markdown must reproduce it.
- **Validation collects, not fails fast.** A single validate call returns a report aggregating: malformed frontmatter, missing required fields, invalid lifecycle or visibility values, empty tags, non-synthetic pages with empty sources, duplicate canonical titles, duplicate aliases, and unresolved Relationship targets. Each issue identifies its file and field.
- **Retrieval results must not assume all Evidence is Markdown.** Evidence carries a source type (MVP: `compiled_markdown`); future Connectors can introduce `dataset`, `table`, or `database` evidence without changing the Retrieval Result contract.
- **Citation and Retrieval Trace are first-class** in every Retrieval Result — page identity, relative path, optional source reference, optional line range, and a structured explanation of the retrieval stages.
- **Freshness** is decided by a deterministic digest over source Markdown paths and bytes; a stale index is rebuilt from source.

## Acceptance and lean assurance

- Validate through the public seam and assert public Evidence, diagnostics, trace, and freshness behavior; private index layout is not a contract.
- One representative valid fixture and the smallest invalid cases needed for distinct diagnostic classes are sufficient. Do not create one fixture or test per field when one public report journey proves aggregation.
- A factual query returns cited Evidence; a known-missing fact returns no result. A source change marks derived state stale and rebuild restores it from Markdown.
- No model provider, network, or app framework participates in this seam's required checks.
- Related implementation tasks may share one validation unit. Add a new focused test only when existing public-seam coverage cannot expose the named failure.

## Out of Scope

- Model-provider calls, answer synthesis, chat request/response handling.
- OpenAI-compatible runtime logic.
- Auth or role enforcement (the SDK records Visibility; it does not enforce it).
- App-framework or frontend-interactivity behavior.
- Metadata ORM tables and migrations.
- Git/shared-storage publishing.
- Raw-source ingestion or distillation within this Core SDK seam. ADR-0010 assigns those capabilities to the broader `lumio-wiki` distribution.
- Structured dataset query execution (Connector and Dataset are future seams only).

## Further Notes

Implementation work uses the smallest coherent vertical slices through load → validate → index → retrieve. Use a plan only when dependencies or cross-session coordination need one, and create GitHub Issues only when tracker coordination is useful. Several related slices may share one validation unit; a new test is required only when a named reachable failure lacks meaningful existing coverage. The forbidden-dependency list keeps the SDK reusable; the public seam keeps it deep.
