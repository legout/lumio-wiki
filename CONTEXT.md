# Lumio

Deployable chat for trusted knowledge and data. The MVP is a single-tenant, deployable browser agent platform that answers questions over a compiled Markdown knowledge base, with the wiki kept portable outside the web app.

## Language

### Product and knowledge artifacts

**Lumio**:
The product. Deployable chat for trusted knowledge and data.
_Avoid_: llm-wiki-agent, the wiki chatbot, the knowledge-base agent.

**Knowledge Base**:
A portable, versioned tree of Compiled Pages that Lumio loads from a local
filesystem or configured object storage location. It is the source-of-truth
input, not a database and not an index.
_Avoid_: the wiki (ambiguous — use only when "compiled Markdown artifact" is
clearly meant), the corpus, the dataset.

**Knowledge Base Location**:
A deployment or CLI configuration that resolves a Knowledge Base's Published
Versions from a local filesystem or object storage. Its credentials are
deployment configuration, never portable Knowledge Base content.
_Avoid_: Knowledge Source (raw ingest material), working copy, bucket (too
specific).

**Shared-directory Storage**:
A storage mode that copies Published Versions between an app's filesystem
working copy and a directory shared by its consumers. It is neither Git
synchronization nor object storage.
_Avoid_: local storage (ambiguous), S3 storage, shared Knowledge Base.

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
A typed, directed edge from one Compiled Page to another, expressed in frontmatter by canonical title. It is a reviewed semantic claim, not an ordinary body link.
_Avoid_: link (too generic), connection.

**Extracted Reference**:
A deterministic, non-canonical directed reference derived from an internal Markdown link in a Compiled Page body. It supports navigation and context discovery but does not assert Relationship semantics.
_Avoid_: inferred relationship, automatic relationship, link edge.

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
A structured explanation of the retrieval stages (validation, lexical search, graph expansion, ranking) that produced a result. Exposed for admin and power-user inspection.
_Avoid_: debug log, explanation.

**Discovery Graph**:
The derived graph used to find context, containing canonical Relationships plus Extracted References. Its topology selects Evidence to inspect; an Extracted Reference is never Evidence and cannot support an answer claim by itself.
_Avoid_: knowledge graph (ambiguous), assertion graph, link graph.

**Index Freshness**:
Whether the derived index still reflects the current source Markdown. Lumio detects staleness after a sync or source change and rebuilds from source.
_Avoid_: cache validity, sync state.

### Ingest and publish

**Source Version**:
An immutable, content-hashed version of a privately identified Knowledge
Source. A new Source Version extends the same source lineage; it is not
automatically a different Knowledge Source.
_Avoid_: source revision, file version, upload.

**Source Retirement**:
An explicit Maintainer action that makes a Knowledge Source's current Source
Version ineligible to support future published knowledge. A missing or
unavailable source is only a retirement candidate.
_Avoid_: source deletion, missing file.

**Knowledge Source Registry**:
Private ingest state giving each Knowledge Source a stable identity, its
immutable content-hashed Source Versions, and retirement status. Excluded from
Compiled Pages, exports, Reader retrieval, and canonical fingerprints.
_Avoid_: lineage database, source index, manifest.

**Ingest Proposal**:
A staged set of proposed Markdown changes or explicit Page Removals with
provenance, affected pages, and a validation report. Reviewed and approved
before publish.
_Avoid_: change set, edit batch.

**Page Removal**:
An explicit, reviewed Ingest Proposal mutation that excludes a Compiled Page
from the next Published Version and repairs invalid canonical Relationships in
the same proposal. It is never inferred from an omitted page.
_Avoid_: missing page, automatic deletion.

**Published Version**:
An immutable snapshot of the Knowledge Base produced by a publish action. Identifies a state readers and local agents can sync to.
_Avoid_: release, commit (too VCS-specific).

**Write Mode**:
How ingestion reaches the Knowledge Base. `proposal-first` (default: stage → review → publish) or `direct-write` (trusted: publish immediately, validation still runs).
_Avoid_: edit mode, commit mode.

### Knowledge Base controls and published artifacts

**Knowledge Base Control File**:
A versioned root `lumio.yaml` that declares a categorized Knowledge Base's Content Category catalog and Hot Index pins. It is KB-local content control: portable with the Knowledge Base, validated and fingerprinted by the Core SDK, but neither a Compiled Page nor an OKF concept.
_Avoid_: config file, settings file, kb.json.

**Content Category**:
A broad navigation-routing label declared by the Control File (the seeded catalog is concepts, entities, references, procedures, tables, datasets, synthesis). Distinct from a Compiled Page's free-form `type`, which is specific semantics. No Lumio-wide type taxonomy exists.
_Avoid_: folder, section, classification (too generic).

**Hot Index**:
A regenerated, Maintainer-pinned navigation surface (`hot.md`) listing exactly the Compiled Pages whose Canonical Titles the Control File pins. Curated only; citation/access-frequency ranking is deferred.
_Avoid_: favorites, popular pages, recommendations.

**Activity Log**:
An append-only portable record (`log.md`) of successful published Knowledge Base state transitions. One grep-friendly entry per publish, appended only after the KB state succeeds. Excludes Reader queries, failed/discarded proposals, unpublished uploads, and private audit events. Distinct from the SQLite audit log and OKF's preview-only exchange history.
_Avoid_: changelog, audit log (the audit log is private), history file.

**Legacy Flat Mode**:
The mode a Knowledge Base with no Control File loads in: existing root-level Compiled Pages remain valid with a non-blocking migration warning, and only Navigation Indexes are published until a reviewed migration establishes the Control File.
_Avoid_: old mode, unmanaged mode.

**Reserved Artifact**:
A marked, reserved derived Markdown file the Core SDK recognizes by basename and `lumio` marker (Navigation Index `index.md`, Hot Index `hot.md`, Activity Log `log.md`). Valid marked artifacts are excluded from Compiled Page loading, retrieval, and fingerprinting; an unmarked or malformed collision is a blocking error.
_Avoid_: generated file, cache file.

### Platform roles and guardrails

**Reader / Maintainer / Owner**:
The three Lumio roles. Reader asks questions and views citations. Maintainer runs ingest, reviews staged changes, and administers Reader accounts only (cannot see or target Owner/Maintainer accounts, themselves, or any role assignment). Owner configures storage, auth, model provider, write mode, secrets, and all accounts and roles. Owner is a first-run role created only through setup; the account-administration surface can never create, promote to, update, or delete an Owner.
_Avoid_: user/admin/superuser (use the canonical role names).

**Guardrails**:
The rules the Chat Gateway enforces: domain claims require citation; unsupported questions return "not covered"; out-of-scope questions are rejected or narrowed; external clients cannot bypass retrieval and citation; raw sources stay out of public exports.
_Avoid_: rules, policies.

### Web UI interaction model

**Chat + Citation Workspace**:
The Reader's default surface: a chat-first workspace that keeps Citations,
Compiled Pages, and Retrieval Trace access close to the conversation. Chat is
the entrypoint; evidence remains first-class.
_Avoid_: chatbot screen, ask page, Q&A panel.

**Chat Context**:
A temporary, Reader-owned conversation scope retained for the active retention
window. It carries chat state and any attached Conversation Source; it is not a
published Knowledge Base artifact.

**Conversation Source**:
A temporary private text or Markdown source attached to one Chat Context. Its
converted text and stable sections are retrievable only by its owning Reader
within that context and are removed independently of the published Knowledge
Base. It is distinct from both a raw Knowledge Source used by ingest and a
published Compiled Page.

**Reading Room**:
The unified Reader surface for browsing, searching, and reading published
Compiled Pages, presented three ways from one shared document-rendering module:
a persistent chat-side evidence column opened from a Citation (it keeps the
chat thread available while promoting source text, cited line ranges, and
page- or passage-grounded follow-up questions), a focus-managed responsive
sheet over chat when the content area cannot fit two readable columns, and a
standalone chat-free destination for browse, deterministic lexical search, and
full-width reading. Selecting a Citation, asking another question, and the
column's URL/history state are all explicit Reader actions.
_Avoid_: document viewer, article view, citation popup, Library (the retired
Reader browse/reading concept; stable `/kb` routes remain for compatibility).

**Constellation**:
A contextual Relationship lens seeded from an answer, Citation, Compiled Page,
or Maintainer/Owner diagnostic task. It explains how Compiled Pages relate; it
is not the Reader home.
_Avoid_: graph homepage, mind map, knowledge graph app.

**Workshop**:
The Maintainer surface for ingest, validation, Ingest Proposal review, and
publish decisions.
_Avoid_: admin panel, CMS, moderation queue.

**Progressive Console**:
The Maintainer interaction model where ordinary Workshop tasks remain readable
and approachable, while ingest, validation, sync, proposal review, and power
navigation reveal Console affordances such as panes, operation logs, status
lines, and a command palette.
_Avoid_: terminal mode, developer console, power-user-only UI.

**Operational Disclosure**:
The Owner interaction model where settings and administration remain calm by
default, while audit, diagnostics, risky changes, and troubleshooting reveal
deeper operational detail only when needed.
_Avoid_: operations cockpit, control center, debug dashboard.

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
