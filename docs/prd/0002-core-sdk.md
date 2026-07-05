# PRD-0002: Lumio Core SDK

_Status: approved. Originates from the 2026-07-04 Core SDK TDD plan, the architecture contracts, and the module-boundaries doc. Absorbs both: their durable content lives in the sections below; their file-path/type-signature detail is intentionally omitted (it goes stale) and is decided at implementation time. Stack decisions in ADR-0001 and ADR-0002; ubiquitous language in `CONTEXT.md`._

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

These are the durable, seam-level decisions. Specific public symbol names and the internal file split are decided at implementation time (TDD, one vertical slice at a time) and are deliberately not fixed here.

- **Deep module, small seam.** Callers learn a small interface and get a large amount of behavior. The internal split — parsing, validation, exact lookup, graph traversal, lexical retrieval, freshness — is private to the implementation, not part of the interface. _Depth is a property of the interface, not the implementation: the SDK can be internally composed of small, swappable parts that simply are not part of the public seam._
- **The interface is the test surface.** Tests cross the same seam callers do. Any test that has to reach past the public interface is a signal the module is the wrong shape.
- **Framework-independent.** The Core SDK must not import the app framework, the frontend interactivity layer, the metadata ORM or its tables, the runtime/gateway, the web UI, auth/role modules, or any model-provider client. (Specific technology in ADR-0001; struct and validation layer in ADR-0002.)
- **One adapter means a hypothetical seam; two means a real one.** Internal seams used only by the SDK's own tests are fine; they are not exposed through the public interface just because tests use them.
- **Markdown is the durable source of truth; every index is derived and rebuildable.** Deleting an index and rebuilding from Markdown must reproduce it.
- **Validation collects, not fails fast.** A single validate call returns a report aggregating: malformed frontmatter, missing required fields, invalid lifecycle or visibility values, empty tags, non-synthetic pages with empty sources, duplicate canonical titles, duplicate aliases, and unresolved Relationship targets. Each issue identifies its file and field.
- **Retrieval results must not assume all Evidence is Markdown.** Evidence carries a source type (MVP: `compiled_markdown`); future Connectors can introduce `dataset`, `table`, or `database` evidence without changing the Retrieval Result contract.
- **Citation and Retrieval Trace are first-class** in every Retrieval Result — page identity, relative path, optional source reference, optional line range, and a structured explanation of the retrieval stages.
- **Freshness** is decided by a deterministic digest over source Markdown paths and bytes; a stale index is rebuilt from source.

## Testing Decisions

- **Test only at the public seam.** Tests must not inspect private index files or depend on internal storage layout. They assert public Evidence fields returned by retrieval.
- **Valid fixture** — pages with full frontmatter (canonical title, aliases, tags, summary, lifecycle, visibility, sources, relationships).
- **Invalid fixtures** — one per error class: broken frontmatter, missing required field, broken relationship target, duplicate alias. Each proves the report names the file and the field.
- **Retrieval** returns at least one cited result for a factual query against the valid fixture; a known-missing fact returns no result rather than a fabricated one.
- **Freshness** — a source change flips the index to stale; a rebuild from source restores it.
- **No model provider, network, or app framework** is in scope for this module's tests.

## Out of Scope

- Model-provider calls, answer synthesis, chat request/response handling.
- OpenAI-compatible runtime logic.
- Auth or role enforcement (the SDK records Visibility; it does not enforce it).
- App-framework or frontend-interactivity behavior.
- Metadata ORM tables and migrations.
- Git/shared-storage publishing.
- Raw-source ingestion or distillation.
- Structured dataset query execution (Connector and Dataset are future seams only).

## Further Notes

The Core SDK is the first implementation workstream after this PRD. It should be broken into independently-grabbable issues via `/to-issues`, each a vertical slice that cuts through load → validate → index → retrieve with its own test. The forbidden-dependency list above is the contract that keeps the SDK reusable; the public seam is the contract that keeps it deep.
