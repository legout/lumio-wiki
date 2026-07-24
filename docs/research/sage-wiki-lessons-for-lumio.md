# Sage-Wiki lessons for Lumio's compiled knowledge pipeline

_Date: 2026-07-22. Scope: architectural research only; no decision or
implementation commitment._

## Research question

Which lessons from the published Sage-Wiki retrospective and the current
Sage-Wiki implementation should Lumio investigate, and which decisions must be
resolved before creating ADRs, a product spec, or implementation issues?

This report is the evidence input to the claim-lineage `grill-with-docs`
decisions recorded in ADR-0014. It is not itself an architecture decision.

## Executive findings

1. **Sage-Wiki validates Lumio's compiled-artifact premise.** Both systems treat
   maintained Markdown knowledge as the durable artifact and search indexes as
   supporting infrastructure rather than canonical knowledge.
2. **The most relevant gap is the semantic compilation boundary.** Lumio has
   strong Source Processor, Distiller, and Proposal Pipeline seams, but its
   unattended Distiller currently asks one model call to turn a normalized
   source directly into final proposed Compiled Pages. Sage-Wiki separates
   summarization, concept extraction, and article writing.
3. **Incrementality is incomplete without retraction semantics.** File hashes
   and affected-page lists optimize additions and modifications, but reliable
   deletion requires an inspectable dependency chain from Knowledge Source to
   source section or Claim to Compiled Page.
4. **Lumio's trust boundary should be preserved.** Sage-Wiki's ontology and
   learning database are useful implementation evidence, but Lumio's reviewed
   typed Relationships, proposal-first publication, portable Compiled Pages,
   and distinction between canonical Relationships and derived Extracted
   References are stronger safeguards.
5. **Several lessons are already implemented in Lumio.** Deterministic health
   checks, graph diagnostics, blast radius, hybrid retrieval, derived graph
   materialization, lightweight standalone tooling, and proposal-first edits
   must not be rediscovered as new projects.
6. **The retrospective is not a stable Sage-Wiki specification.** At the
   inspected repository commit, Sage-Wiki describes a tiered compiler and 17
   MCP tools, while the retrospective describes five passes and 14 tools. Its
   implementation is evidence about trade-offs, not a design to copy.

## Evidence baseline

### Karpathy's original pattern

Karpathy's idea file establishes three durable principles:

- raw sources remain available;
- an LLM incrementally integrates their knowledge into an interlinked Markdown
  wiki; and
- useful interactions improve the persistent artifact instead of leaving their
  result only in chat.

The original document deliberately leaves implementation choices to the agent
and user. It does not require SQLite, embeddings, MCP, a fixed compiler pass
count, or a canonical ontology database.

### Current Sage-Wiki implementation

The Sage-Wiki repository was inspected at commit
`16c8c344cddb71de92d13e5a6e813c0181dae9f4`.

The current implementation contains more machinery than the retrospective's
five-pass description:

- `runFullPipeline` performs Pass 1 summarization, Pass 2 concept extraction,
  and Pass 3 article writing.
- The outer compiler handles diffing, tier selection, indexing, embeddings,
  image work, quality scoring, manifest updates, optional pruning, and trust
  checks.
- Source type can be configured explicitly or detected from extension and
  content signals.
- Progress reports phases, item starts/completions/errors, discovered concepts,
  and a compile summary.
- Verified outputs are demoted when hashes of supporting sources change.
- Search combines BM25 and vector ranks with RRF and can apply tag and recency
  adjustments.
- Learnings are stored in SQLite with deduplication, a 500-entry bound, and a
  180-day TTL.

This demonstrates the operational value of decomposition, but also shows that
"the five-pass compiler" is a conceptual summary rather than a durable public
contract.

### Lumio's current baseline

Lumio already establishes these relevant contracts:

- A Knowledge Base is a portable, versioned tree of Compiled Pages and remains
  the source of truth (`CONTEXT.md`, PRD-0001).
- Source processing, distillation, and proposal publication are separate seams
  (ADR-0010).
- A `NormalizedSource` contains normalized text, stable sections, conversion
  identity, source hash, filename, and content type.
- The unattended `OpenAIDistiller` currently sends the filename and full
  normalized text to one generic Compiled Page authoring prompt.
- The Proposal Pipeline computes proposed pages, diff, validation, affected
  pages, and blast radius before publication.
- Typed Relationships remain reviewed canonical claims, while body links yield
  derived Extracted References used only for discovery (ADR-0011).
- Deterministic health analysis already covers validation, graph freshness,
  orphan conditions, unresolved references, deprecated pages, contradiction
  Relationships, and structural graph diagnostics. Issue #126 delivered
  scope-aware diagnostics; issue #127 extends them for cross-link priority.
- Zero-index retrieval and optional LanceDB return the same Evidence, Citation,
  and Retrieval Trace contracts (ADR-0010).

## Candidate lesson 1: semantic intermediate representation

### Intermediate-representation evidence

Sage-Wiki uses source summaries as an intermediate representation before
concept extraction and article writing. Lumio's `NormalizedSource` is a strong
conversion boundary, but it is not a semantic representation: it contains
source text and sections, not extracted Claims, resolved page identities,
contradictions, or proposed removals.

### Intermediate-representation potential value

A semantic intermediate representation could make these independently
observable and testable:

- source interpretation;
- Claim extraction with source spans;
- concept and alias candidates;
- candidate typed Relationships;
- contradictions and uncertainty;
- target Compiled Page resolution;
- additions, revisions, and removals; and
- the reason each page is affected.

It could also permit different models or deterministic logic per stage without
changing the Proposal Pipeline.

### Intermediate-representation risks and unresolved choices

- A transient value is cheap to change; a persisted or public SDK record becomes
  a compatibility contract.
- Calling it a "Knowledge Delta" may conflict with proposal diff and source
  delta unless its domain meaning is precise.
- Claim extraction creates false precision unless source spans and uncertainty
  are retained.
- A rigid universal IR may handle papers well but distort procedures, meetings,
  datasets, or code repositories.

## Candidate lesson 2: source-genre-specific distillation

### Source-genre evidence

Sage-Wiki distinguishes source types and supports prompts such as
`summarize-paper.md`; its implementation permits explicit type selection or
automatic detection. Lumio routes formats to Source Processors, but its
unattended semantic prompt is generic after normalization.

### Source-genre potential value

Distinguishing **format** from **epistemic genre** would allow a PDF research
paper, PDF policy, and PDF meeting export to receive different extraction
contracts. Candidate genres include research paper, reference documentation,
procedure, policy, meeting transcript, incident report, dataset descriptor, and
code repository.

### Source-genre risks and unresolved choices

- Automatic classification can silently select the wrong extraction contract.
- Genre must not be conflated with Content Category or free-form page `type`.
- Maintainers may require an override and disclosure of the selected profile.
- A global fixed genre taxonomy may be too rigid for per-Knowledge Base needs.

## Candidate lesson 3: dependency-aware invalidation and retraction

### Invalidation evidence

Sage-Wiki hashes sources, records source-to-concept information in a manifest,
supports pruning when deleted sources leave an article orphaned, and demotes
verified outputs when supporting-source hashes change. Karpathy's pattern
requires the maintained wiki to reflect changed and contradictory sources, not
merely accumulate additions.

Lumio records source hashes and affected pages for an Ingest Proposal, but
canonical Compiled Page `sources` are page-level provenance. They do not
identify which source section supports which material Claim.

### Invalidation potential value

A dependency chain such as:

```text
Knowledge Source → Source Version → stable section → Claim → Compiled Page
```

could support precise stale-impact reporting, removal proposals, and review of
Claims that have lost all support.

### Invalidation risks and unresolved choices

- Claim metadata in canonical Markdown could make pages hard for humans and
  external agents to edit.
- Private state preserves the Markdown contract but is not portable with the
  Knowledge Base.
- Source retirement does not imply Claim removal when another independent
  Source Version still supports it.
- Modified prose may preserve a Claim while changing its range, requiring stable
  source and Claim identity rather than line numbers alone.
- Synthetic Pages need lineage through Compiled Pages and Retrieval Traces, not
  a fictional direct raw-source relationship.

The subsequent grill selected this candidate as the first architecture slice.
ADR-0014 records the resolved privacy, package, support-set, invalidation,
publication, and retention decisions.

## Candidate lesson 4: compiler events and per-stage model policy

### Compiler-event evidence

Sage-Wiki exposes phases and per-item progress and supports separate model and
cost behavior across compiler work. Lumio exposes operational progress in
several application workflows, but standalone semantic distillation is one
model operation at the Distiller seam.

### Compiler-event potential value

A stable internal event vocabulary could power CLI progress, Workshop status,
retry boundaries, metrics, and cost disclosure. Separating semantic stages
would permit deterministic processing or cheaper models where stronger models
are unnecessary.

### Compiler-event risks and unresolved choices

- UI strings should not accidentally become a permanent SDK event protocol.
- Provider token and cache metrics must not leak into the Knowledge Base.
- Retryable work needs idempotency boundaries so replay does not imply duplicate
  publication.

## Candidate lesson 5: answer plus reviewable knowledge update

### Answer-promotion evidence

Karpathy's compounding rule and the Sage-Wiki retrospective argue that a useful
query should improve the wiki. Lumio already defines Synthetic Pages and
requires a promoted cited answer to retain its Retrieval Trace, while the
Proposal Pipeline supplies the review boundary.

### Safe Lumio interpretation

The safe Lumio form is not "every query writes the Knowledge Base." It is:

> An authorized user may turn a useful cited answer or discovered connection
> into an optional Ingest Proposal that creates or revises Compiled Pages.

The answer remains immediate output. The proposed update remains staged until
validation and publication succeed.

### Answer-promotion risks and unresolved choices

- Reader, Maintainer, and Owner capabilities must remain distinct.
- Conversation text can contain prompt injection, private Conversation Sources,
  or information outside the published visibility scope.
- A cited answer may be useful without being durable knowledge.
- Repeated promotion can create duplicates or circular synthesis unless identity
  and merge behavior are explicit.

## Candidate lesson 6: model-assisted linting

### Model-assisted-lint evidence

Sage-Wiki combines deterministic and model-assisted maintenance. Lumio already
has deterministic validation and graph-health analysis, including issue #126
structural diagnostics and issue #127's proposal-first cross-link work.

### Model-assisted-lint potential value

Advisory passes could identify:

- concepts repeatedly mentioned without a Compiled Page;
- likely aliases or duplicate Canonical Page identities;
- Claims apparently superseded by newer Sources;
- candidate semantic Relationships; and
- pages that may need synthesis or decomposition.

### Boundary to preserve

A finding is not a Relationship, Source, Evidence item, or published change.
Any content change must become an Ingest Proposal. Deterministic checks should
remain separately identifiable from model suggestions.

## Candidate lesson 7: governed compiler learning

### Compiler-learning evidence

Sage-Wiki stores typed learnings in SQLite and surfaces them during later
linting. The inspected implementation bounds state by count and age. Its current
documented types are `convention`, `gotcha`, `correction`, `error-fix`, and
`api-drift`, which differs from the retrospective's list including preferences.

### Compiler-learning potential value

Remembered corrections could prevent repeated source-classification,
identity-resolution, or formatting mistakes.

### Compiler-learning risks and unresolved choices

- Automatically injecting prior text into prompts creates persistent prompt
  injection and policy drift.
- Operational SQLite state is not portable Knowledge Base content.
- A convention may apply to one Knowledge Base, source genre, compiler version,
  or organization; global scope is unsafe.
- Rules need provenance, review, version compatibility, regression examples,
  revocation, and conflict handling.
- Domain truth differs from a compiler rule; combining them obscures ownership.

## Lessons not recommended as Lumio premises

### A single Go binary

The transferable requirement is low operational friction, not a language or
binary format. ADR-0010 already gives Lumio lightweight and progressively
enhanced packages plus one assembled Docker-first application. A Go rewrite is
not supported by the evidence.

### SQLite as canonical ontology or universal retrieval architecture

SQLite is an effective Sage-Wiki choice. It should not replace Lumio's canonical
Markdown Relationships or retrieval-adapter boundary without new requirements.
Lumio keeps the Discovery Graph rebuildable from Compiled Pages and can add
storage adapters when measurement justifies them.

### MCP as a core requirement

MCP may become a client adapter, but Lumio's Core SDK, CLI, and Agent Skills
already provide a protocol surface. An MCP server must not bypass roles,
proposal-first writes, Visibility, or Citation guarantees. MCP means **Model
Context Protocol**; the retrospective's "Multipurpose Communication Protocol"
expansion is incorrect.

### Recency as truth

Recency may help ranking, but a newer source is not necessarily more accurate or
authoritative. Any future recency feature must remain a disclosed ranking signal
and must not substitute for provenance, lifecycle, or evidence quality.

## Decision handoff

The research produced seven candidate lessons. The grill selected
**claim-level source invalidation and retraction** as the first focused slice and
deferred the other six. The resolved architecture is recorded in:

- `docs/adr/0014-source-versions-and-page-level-invalidation.md` (accepted
  first in its claim-level form, later descoped to page-level invalidation
  with the claim-level design deferred to a backlog issue); and
- [GitHub issue #129](https://github.com/legout/lumio/issues/129), the
  claim-lineage PRD.

No implementation issues should be created until that PRD is deliberately
decomposed.

## Sources

### Primary external sources

- Andrej Karpathy, [LLM Wiki idea file][karpathy-llm-wiki], pinned gist revision
  `ac46de1ad27f92b28ac95459c782c07f6b8c964a`.
- Sage-Wiki repository at commit
  [`16c8c344cddb71de92d13e5a6e813c0181dae9f4`][sage-commit].
- Sage-Wiki [README and architecture summary][sage-readme].
- Sage-Wiki [full semantic compiler pipeline][sage-pipeline].
- Sage-Wiki [source-type detection][sage-types].
- Sage-Wiki [compiler progress reporting][sage-progress].
- Sage-Wiki [source-change trust invalidation][sage-invalidation].
- Sage-Wiki [stored linter learnings][sage-learning].
- Sage-Wiki [hybrid RRF search][sage-search].
- Sage-Wiki [compound MCP tools][sage-mcp].

### Lumio sources

- `CONTEXT.md`
- `docs/prd/0001-knowledge-agent-platform.md`
- `docs/adr/0007-navigation-indexes-and-okf-exchange-profile.md`
- `docs/adr/0010-uv-workspace-and-progressive-packaging.md`
- `docs/adr/0011-derived-reference-graph-and-progressive-storage.md`
- `docs/kb-format.md`
- `packages/lumio-wiki/src/lumio_wiki/source_processor.py`
- `packages/lumio-wiki/src/lumio_wiki/distiller.py`
- `packages/lumio-wiki/src/lumio_wiki/proposal_pipeline.py`
- `packages/lumio-wiki/src/lumio_wiki/knowledge_base.py`
- GitHub issues #126 and #127 in `legout/lumio`

[karpathy-llm-wiki]: https://gist.githubusercontent.com/karpathy/442a6bf555914893e9891c11519de94f/raw/ac46de1ad27f92b28ac95459c782c07f6b8c964a/llm-wiki.md
[sage-commit]: https://github.com/xoai/sage-wiki/tree/16c8c344cddb71de92d13e5a6e813c0181dae9f4
[sage-invalidation]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/trust/source_check.go
[sage-learning]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/linter/learning.go
[sage-mcp]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/mcp/tools_compound.go
[sage-pipeline]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/compiler/fullpipeline.go
[sage-progress]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/compiler/progress.go
[sage-readme]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/README.md
[sage-search]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/hybrid/search.go
[sage-types]: https://github.com/xoai/sage-wiki/blob/16c8c344cddb71de92d13e5a6e813c0181dae9f4/internal/compiler/typedetect.go
