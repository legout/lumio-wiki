# Lumio

Deployable chat for trusted knowledge and data. The MVP is a single-tenant, deployable browser agent platform that answers questions over a compiled Markdown knowledge base, with the wiki kept portable outside the web app.

## Language

### Product and knowledge artifacts

**Lumio**:
The product. Deployable chat for trusted knowledge and data.
_Avoid_: llm-wiki-agent, the wiki chatbot, the knowledge-base agent.

**Knowledge Base**:
A local filesystem tree of compiled Markdown pages that Lumio loads, validates, indexes, and retrieves from. It is the source-of-truth input, not a database and not an index.
_Avoid_: the wiki (ambiguous — use only when "compiled Markdown artifact" is clearly meant), the corpus, the dataset.

**Compiled Page**:
A Markdown file with YAML frontmatter (structured metadata) and a body (human-readable knowledge). Source-of-truth content.
_Avoid_: document, article, record.

**Synthetic Page**:
A Compiled Page generated from other compiled knowledge rather than a direct raw source. Must declare itself synthetic and may omit provenance.
_Avoid_: generated page, derived page.

**Knowledge Source**:
A raw input that feeds ingestion (PDF, Office doc, HTML, image, etc.). Distinct from a Compiled Page: sources are not part of the published knowledge base.
_Avoid_: input file, attachment.

### Identity and metadata

**Canonical Page Title**:
The unique title a Compiled Page is known by. Relationship targets refer to canonical titles.
_Avoid_: page name, heading.

**Alias**:
An alternate lookup phrase for a Compiled Page. Optional, and unique across the Knowledge Base.
_Avoid_: nickname, shortcut.

**Tag**:
A controlled organization and retrieval label on a Compiled Page. Required and non-empty.
_Avoid_: keyword, category.

**Lifecycle**:
The publication state of a Compiled Page: `draft`, `review`, `approved`, or `deprecated`.
_Avoid_: status (overloaded).

**Visibility**:
The access class of a Compiled Page: `public`, `internal`, or `restricted`. Lumio records it; the runtime enforces it.
_Avoid_: permission, access level.

**Source**:
A provenance reference on a Compiled Page (an identifier, a title, and an optional URL). Non-synthetic pages must have at least one.
_Avoid_: reference, link.

**Relationship**:
A typed, directed edge from one Compiled Page to another, expressed in frontmatter by canonical title.
_Avoid_: link (too generic), connection.

### Retrieval

**Evidence**:
A retrievable unit derived from a Compiled Page. Rebuildable from Markdown; never source-of-truth itself.
_Avoid_: chunk, fragment.

**Citation**:
Metadata that lets an answer point back to its source content: page identity, relative path, optional source reference, and optional line range.
_Avoid_: footnote, reference.

**Retrieval Result**:
A citation-ready object the Core SDK returns: evidence identity, citation, snippet, score, reason, and trace.
_Avoid_: hit, match.

**Retrieval Trace**:
A structured explanation of the retrieval stages (validation, lexical search, ranking) that produced a result. Exposed for admin and power-user inspection.
_Avoid_: debug log, explanation.

**Index Freshness**:
Whether the derived index still reflects the current source Markdown. Lumio detects staleness after a sync or source change and rebuilds from source.
_Avoid_: cache validity, sync state.

### Ingest and publish

**Ingest Proposal**:
A staged set of proposed Markdown changes with provenance, affected pages, and a validation report. Reviewed and approved before publish.
_Avoid_: change set, edit batch.

**Published Version**:
An immutable snapshot of the Knowledge Base produced by a publish action. Identifies a state readers and local agents can sync to.
_Avoid_: release, commit (too VCS-specific).

**Write Mode**:
How ingestion reaches the Knowledge Base. `proposal-first` (default: stage → review → publish) or `direct-write` (trusted: publish immediately, validation still runs).
_Avoid_: edit mode, commit mode.

### Platform roles and guardrails

**Reader / Maintainer / Owner**:
The three Lumio roles. Reader asks questions and views citations. Maintainer runs ingest and reviews staged changes. Owner configures storage, auth, model provider, write mode, secrets, and users.
_Avoid_: user/admin/superuser (use the canonical role names).

**Guardrails**:
The rules the Chat Gateway enforces: domain claims require citation; unsupported questions return "not covered"; out-of-scope questions are rejected or narrowed; external clients cannot bypass retrieval and citation; raw sources stay out of public exports.
_Avoid_: rules, policies.

### Platform capabilities

These named capabilities are part of Lumio's ubiquitous language. Each is a seam, not a module dictate.

**Core SDK**:
The framework-independent boundary that owns loading, validating, indexing, and retrieving over a Knowledge Base. Every client (web, API, future CLI, local coding-agent adapter) calls the same Core SDK behavior.
_Avoid_: the engine, the backend.

**Agent Runtime**:
The Lumio-owned loop that classifies a question, retrieves evidence via the Core SDK, calls the model, synthesizes a cited answer, exposes a trace, and refuses unsupported claims.
_Avoid_: the LLM layer, the brain.

**Chat Gateway**:
The boundary that exposes chat to clients (native web API and a future OpenAI-compatible endpoint), enforcing auth, roles, and guardrails so no client bypasses the Agent Runtime.
_Avoid_: the API, the endpoint.

## Future extension

**Connector**:
A future adapter exposing knowledge or data from a non-Markdown source (table, database, data lake). Not part of the MVP. Retrieval results already allow non-Markdown evidence so connectors can be added without changing client contracts.
_Avoid_: integration, plugin.

**Dataset**:
A future structured data source exposed through a Connector (tables, extracts, Parquet, data-lake artifacts). Query execution is post-MVP.
_Avoid_: database (too narrow), table.
