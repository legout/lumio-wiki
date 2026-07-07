# LLM Wiki Prior Art for Lumio

This note distills lessons from four primary-source repositories for Lumio's compiled Markdown Knowledge Base design.

Lumio constraints used for evaluation: Markdown is the durable source of truth; derived indexes are rebuildable; the Core SDK owns loading, validation, indexing, retrieval, citations, graph traversal, and freshness; raw Knowledge Sources are excluded from compiled exports; proposal-first ingest remains the default; unsupported answers must be refused.

## Executive takeaways

1. **Keep Lumio stricter than the agent-skill projects.** The prior-art projects prove the LLM-wiki pattern is useful, but most rely on agent instructions as enforcement. Lumio should encode the invariants in the Core SDK and validation layer: unique canonical titles/aliases, non-empty sources for non-synthetic pages, valid relationship targets, visibility filtering, citation-ready Evidence, and freshness fingerprints.
2. **Adopt a cheap navigation layer before full-text retrieval.** nvk's `_index.md` 3-hop navigation and Ar9av's frontmatter/summary-first query protocol both reduce blind page reads. Lumio already has page summaries and tags; the Core SDK should expose an index/registry view and query trace before BM25 section reads.
3. **Make graph traversal first-class.** All projects converge on links, backlinks, graph views, or path queries. Lumio already has `relationships` and `graph_path`; the next improvement is typed-edge retrieval with edge labels in the Retrieval Trace, not just lexical BM25.
4. **Separate deterministic health from semantic maintenance.** SamurAI explicitly splits fast structural `health.py` from expensive semantic `lint.py`. Lumio should keep deterministic validation in Core and reserve LLM-assisted maintenance for staged Ingest Proposals or Maintainer tools.
5. **Treat capture/ingest as a funnel, not a single upload form.** The strongest systems have inboxes, browser clippers, session capture, feedback capture, collection imports, and external dataset registries. Lumio should keep MVP small but design the ingest boundary around proposal objects that can originate from many capture channels.
6. **Do not import raw-source complexity into the compiled layer.** nvk's dataset registry and collection manifests are a strong warning: large, mutable, binary, or query-native data should be represented by manifests and future Connectors, not flattened into Markdown pages.

## Repo findings

### Ar9av/obsidian-wiki

Primary sources: [README](https://github.com/Ar9av/obsidian-wiki/blob/main/README.md), [`llm-wiki` skill](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/llm-wiki/SKILL.md), [`wiki-ingest`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-ingest/SKILL.md), [`wiki-query`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-query/SKILL.md), [`wiki-status`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-status/SKILL.md), [`cross-linker`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/cross-linker/SKILL.md), [`wiki-dedup`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-dedup/SKILL.md), [`wiki-synthesize`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/wiki-synthesize/SKILL.md), [`tag-taxonomy`](https://github.com/Ar9av/obsidian-wiki/blob/main/.skills/tag-taxonomy/SKILL.md), [`graphrag.py`](https://github.com/Ar9av/obsidian-wiki/blob/main/obsidian_wiki/graphrag.py), [`cli.py`](https://github.com/Ar9av/obsidian-wiki/blob/main/obsidian_wiki/cli.py).

#### Best practices worth borrowing

- **Three-layer language is clear.** The core skill frames raw sources as immutable source code, the wiki as LLM-maintained compiled Markdown, and the schema/skills as the maintenance rules. Lumio already uses the same pattern; keep the product language precise: Knowledge Source → Compiled Page → derived Evidence.
- **Manifest-driven delta ingest.** `.manifest.json` tracks source paths, timestamps/hashes, produced pages, and project mappings; `wiki-status` compares new/modified/touched/deleted sources. Lumio's Ingest Proposal should carry equivalent provenance and affected-page data, but keyed by stable source IDs rather than ad hoc strings.
- **Source content is explicitly untrusted.** `wiki-ingest` says source documents are data, not instructions, and forbids executing commands or changing behavior because a source says so. Lumio's runtime already does this for Evidence; the same rule should be visible in ingest prompts and proposal review.
- **Cheap retrieval ladder.** `wiki-query` starts with `hot.md`, `index.md`, GraphRAG, frontmatter/summary matching, optional semantic search, section reads, then full reads. The important part for Lumio is not QMD; it is the staged retrieval trace and the discipline to escalate only when cheaper evidence is insufficient.
- **GraphRAG pre-pass.** `graphrag.py` builds an in-memory index from frontmatter and wikilinks, scores title/tag/summary matches, returns `should_read`, supports multi-hop path finding, and marks `index_only`. Lumio should expose similar `candidate_pages`, `graph_path`, and `read_sections` stages in Retrieval Trace.
- **Typed relationships in frontmatter.** Cross-linker inserts both body links and `relationships:` entries with types such as `uses`, `extends`, `contradicts`, `derived_from`, and `replaces`. Lumio's relationship type is currently free-form; a controlled starter enum plus validation warnings would make graph queries more reliable.
- **Identity-resolution workflow.** `wiki-dedup` detects duplicate concepts from title/alias/tag similarity, then merges aliases/sources/relationships and leaves redirect stubs. Lumio already forbids duplicate titles/aliases; it should also provide a Maintainer-facing duplicate-candidate report before validation failures become hard blockers.
- **Synthesis is a first-class artifact.** `wiki-synthesize` finds co-occurring concept/entity pairs, writes `synthesis/` pages, requires a cross-cutting insight, records provenance percentages, and forces a “Strongest Objection” section. Lumio's `synthetic: true` field is the right primitive; synthetic pages should have a stricter template and be clearly marked in citations.
- **Controlled tag vocabulary.** `tag-taxonomy` separates domain tags from reserved `visibility/` tags and normalizes aliases. Lumio requires non-empty tags and visibility; add a Core-level taxonomy hook later without blocking MVP.
- **Cross-agent portability.** `cli.py` installs skills into many agent runtimes by symlink/copy and uses a single config resolver. Lumio should not copy that complexity into MVP, but the lesson is that portable Markdown plus a stable Core SDK/API lets many clients share one Knowledge Base.

#### Risks / anti-patterns for Lumio

- **Instructions are not enforcement.** Many invariants are markdown skill instructions. Lumio should keep enforcement in code: validation, publish gates, role checks, visibility filters, raw-source exclusion, and retrieval-before-answer.
- **Vault-wide reads do not scale.** Several maintenance skills still rely on glob/grep/full reads. Lumio should keep derived indexes and graph registries in the Core SDK, with explicit freshness checks.
- **Too many workflows can overwhelm MVP.** Capture, dashboards, synthesize, bridge, dedup, export, history ingest, and daily update are valuable later. MVP should ship the core loop: validate → index → retrieve → cite → proposal ingest → publish.

### SamurAIGPT/llm-wiki-agent

Primary sources: [README](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/README.md), [`AGENTS.md`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/AGENTS.md), [`tools/ingest.py`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/tools/ingest.py), [`tools/query.py`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/tools/query.py), [`tools/build_graph.py`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/tools/build_graph.py), [`tools/lint.py`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/tools/lint.py), [`tools/health.py`](https://github.com/SamurAIGPT/llm-wiki-agent/blob/main/tools/health.py).

#### Best practices worth borrowing

- **Minimal, understandable skeleton.** `raw/`, `wiki/index.md`, `wiki/log.md`, `wiki/overview.md`, `sources/`, `entities/`, `concepts/`, `syntheses/`, and `graph/` are easy to reason about. Lumio's sample Knowledge Base should be similarly small and inspectable.
- **Health vs lint boundary.** `health.py` is deterministic, zero-LLM, and safe every session; `lint.py` handles semantic/content quality periodically. Lumio should mirror this with Core validation as the fast always-on gate and optional LLM maintenance as a separate Maintainer workflow.
- **Post-ingest validation.** `ingest.py` writes pages, then checks broken wikilinks and index coverage. Lumio's proposal validation already runs before publish; keep that as a hard gate and report affected pages.
- **Graph build is two-pass.** `build_graph.py` extracts deterministic wikilink edges, then optionally infers semantic edges with confidence and caches/checkpoints by page hash. Lumio should distinguish `extracted` relationship edges from `inferred`/synthetic edges in metadata and traces.
- **Graph-aware linting.** `lint.py` uses graph degree/community information for hub stubs, fragile bridges, isolated communities, and sparse pages. Lumio can expose admin diagnostics: orphan pages, hub pages with thin summaries, relationship components, and graph gaps.
- **CJK-aware query matching.** `query.py` uses character-level bigram matching for CJK page titles instead of word splitting. Lumio's lexical layer should avoid assuming Latin whitespace tokenization for title/alias matching.
- **Save useful answers back as syntheses.** Query answers can become `syntheses/` pages. Lumio should not auto-write from Reader chat, but a Maintainer action like “create Synthetic Page from answer” is a high-value feature.

#### Risks / anti-patterns for Lumio

- **LLM writes directly to canonical pages.** `ingest.py` writes files after parsing one LLM JSON response. Lumio should keep proposal-first review and never let a Reader-facing chat mutate the Knowledge Base.
- **Query can fall back to LLM page selection from the full index.** Useful for a local tool, but Lumio's Agent Runtime should prefer deterministic retrieval stages and traceable ranking before model-assisted routing.
- **Index file as manual catalog can drift.** SamurAI validates index coverage, but Lumio should treat any human-readable index as derived, not canonical.

### lucasastorian/llmwiki

Primary sources: [README](https://github.com/lucasastorian/llmwiki/blob/master/README.md), [`llmwiki` CLI](https://github.com/lucasastorian/llmwiki/blob/master/llmwiki), [`shared/sqlite_schema.sql`](https://github.com/lucasastorian/llmwiki/blob/master/shared/sqlite_schema.sql), [`mcp/tools/guide.py`](https://github.com/lucasastorian/llmwiki/blob/master/mcp/tools/guide.py), [`mcp/tools/search.py`](https://github.com/lucasastorian/llmwiki/blob/master/mcp/tools/search.py), [`mcp/tools/read.py`](https://github.com/lucasastorian/llmwiki/blob/master/mcp/tools/read.py), [`mcp/tools/write.py`](https://github.com/lucasastorian/llmwiki/blob/master/mcp/tools/write.py), [`api/domain/local_processor.py`](https://github.com/lucasastorian/llmwiki/blob/master/api/domain/local_processor.py), [`api/domain/watcher.py`](https://github.com/lucasastorian/llmwiki/blob/master/api/domain/watcher.py).

#### Best practices worth borrowing

- **Local-first with a derived hidden index.** The CLI creates `wiki/` plus `.llmwiki/index.db` and `.llmwiki/cache`; the README says the index/cache are rebuildable. This matches Lumio's source-of-truth rule; keep derived state hidden and disposable.
- **Storage abstraction seam.** The README's `VaultFS` abstraction gives identical operations over local SQLite/filesystem and hosted Postgres/S3. Lumio's Core SDK should remain framework/storage independent, with app storage modes outside retrieval semantics.
- **Filesystem watcher and freshness.** `watcher.py` tracks content hashes, mtimes, versions, and ignores app-initiated writes to avoid re-index loops. Lumio's sync/publish module should similarly detect external changes and rebuild the index with clear freshness status.
- **FTS plus structured metadata.** `sqlite_schema.sql` stores documents, pages, chunks, references, highlights, content hashes, parser, stale flags, and an FTS5 table. Lumio chose LanceDB/BM25, but the schema shows useful metadata fields for Evidence and admin trace: parser, page count, line/page ranges, content hash, and stale timestamps.
- **Highlights and annotations are first-class retrieval signals.** `read.py` materializes user highlights/notes as a separate appendix and marks them as data, not instructions; `search.py` distinguishes matches in source text vs user notes. Lumio can later treat Maintainer annotations as separate Evidence metadata rather than merging them into source claims.
- **Reference graph on writes.** `write.py` updates references after create/edit/append and returns impact/backlinks. Lumio's Ingest Proposal should show blast radius: pages cited, pages linked, pages now stale, unresolved relationship targets.
- **Append safely around footnotes.** `write.py` appends before trailing footnote definitions and renumbers colliding footnotes. If Lumio generates Markdown with footnote citations, this exact trick prevents broken citation layout.
- **Page-range and image-aware reads.** `read.py` can read page ranges for PDFs/office documents and optionally return images. Lumio's ingestion stack already has LiteParse/MarkItDown; keep page/image provenance available so citations can point to page or line ranges.
- **Guide tool orients the agent.** The MCP `guide` tool tells the model how to use the workspace before it writes. Lumio's external-client/API adapters should expose a compact capability/guardrail descriptor so clients know retrieval/citation rules.

#### Risks / anti-patterns for Lumio

- **Direct write tools are powerful but dangerous.** `create`, `edit`, `append`, and `delete` are appropriate for an MCP workspace; Lumio's public Chat Gateway should not expose equivalent mutation without Maintainer role, proposal review, validation, and audit log.
- **Every page must include a visual element.** This is a product/editorial choice in `guide.py`, not a general Knowledge Base invariant. Lumio should not require visuals in the compiled format.
- **Chunk FTS can drift toward RAG.** Lumio's PRD explicitly says compiled-wiki-first retrieval, not chunk-first RAG. Section/page Evidence is fine; raw-source chunks should not become the answer source for MVP chat unless represented as Compiled Pages or future Connector Evidence.

### nvk/llm-wiki

Primary sources: [README](https://github.com/nvk/llm-wiki/blob/master/README.md), [`wiki-manager/SKILL.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/SKILL.md), [`wiki-structure.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/wiki-structure.md), [`indexing.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/indexing.md), [`ingestion.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/ingestion.md), [`compilation.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/compilation.md), [`research-infrastructure.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/research-infrastructure.md), [`datasets.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/datasets.md), [`sessions.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/sessions.md), [`feedback.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/feedback.md), [`librarian.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/librarian.md), [`linting.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/linting.md), [`hub-resolution.md`](https://github.com/nvk/llm-wiki/blob/master/claude-plugin/skills/wiki-manager/references/hub-resolution.md).

#### Best practices worth borrowing

- **Topic sub-wikis prevent retrieval pollution.** The hub only tracks topic wikis; content lives under `topics/<name>/`. Lumio MVP is single-tenant and single Knowledge Base, but future multi-KB support should isolate indexes per Knowledge Base and only do explicit cross-KB peeks.
- **Derived indexes with stale-on-read rebuild.** `_index.md` files are caches, rebuilt from frontmatter when file counts differ. Lumio should treat any human-readable browsable index as derived and rebuildable; code validation already loads pages as source of truth.
- **3-hop query navigation.** Read root `_index.md`, then category `_index.md`, then matched articles. Lumio can implement the same concept as Core SDK API: registry summary → candidate page summaries → Evidence sections.
- **Raw ingest and compilation are separate phases.** `ingestion.md` turns URLs/files/collections into immutable raw sources; `compilation.md` turns raw sources into synthesized articles. Lumio's `create_proposal` currently converts and distills in one path; keep the proposal object explicit about converted raw text vs proposed Compiled Pages.
- **Collection ingestion uses upstream-native interfaces.** Git repos use `git`, MediaWiki uses dumps/API, message archives use CSV/JSON parsing, Wayback uses CDX. Lumio should avoid uncontrolled crawling and record collection manifests when adding bulk ingestion.
- **Dataset registry boundary.** Large, mutable, remote, sensitive, or query-native data becomes a manifest with schema/sample/query notes; the data itself stays outside the wiki. This maps directly to Lumio's future Connector/Dataset seam.
- **Confidence, volatility, verification.** Compilation assigns confidence from source credibility/corroboration; librarian staleness scores combine source age, verification date, compilation date, source-chain integrity, and volatility half-lives. Lumio's current `lifecycle` enum is simpler; add confidence/freshness as computed overlays before adding required fields.
- **Research quality control.** `research-infrastructure.md` uses multiple agent roles, independent credibility scoring, gap scoring, progress scoring, anti-confirmation thesis mode, and round reflection. Lumio does not need autonomous research in MVP, but should borrow the “quality score before ingest/publish” mindset for future Maintainer workflows.
- **Session and feedback capture stay out of topic content until promoted.** `.sessions/` stores redacted operational digests and feedback candidates; explicit promotion writes a raw note. This is the right privacy boundary for Lumio audit/session data: audit logs and raw session traces should not become public Compiled Pages by accident.
- **Lint is migration.** `linting.md` encodes schema evolution as idempotent lint/fix rules rather than one-off migrations. Lumio can adopt the principle for Knowledge Base format upgrades: validator reports aliases/legacy shapes; a Maintainer tool proposes normalized Markdown patches.
- **Hub path resolution is boring and robust.** Config-first, portable paths, no accidental fallback on permission errors, careful `~` expansion. Lumio storage configuration should follow this level of specificity.

#### Risks / anti-patterns for Lumio

- **Feature surface is huge.** Research, collect, inventory, datasets, archive, sessions, feedback, output, audit, librarian, assess, and lessons learned are a mature power-user suite, not an MVP baseline.
- **Agent-managed files can be inconsistent without code gates.** nvk mitigates with lint, indexes, and rules, but Lumio should make the Core SDK's validator the hard boundary.
- **Provocative mode names and speed-first flows are not Lumio-compatible.** Lumio is a trusted knowledge product; keep names professional and prefer proposal-first review over “ingest aggressively, lint later.”

## Cross-project patterns

### Content model

- **Raw/source and compiled/wiki are distinct.** All four projects separate raw inputs from synthesized wiki pages. Lumio should keep raw Knowledge Sources outside public exports and citations should point to Compiled Pages/Evidence, optionally including source provenance.
- **Every useful page needs frontmatter.** Common fields are title, tags, summary/description, dates, sources, category/type, aliases, and relationship metadata. Lumio's current required fields are a good minimum; summary should probably become required for new pages because cheap retrieval depends on it.
- **Synthetic outputs should be explicit.** SamurAI has syntheses, Ar9av has synthesis pages with inferred provenance, nvk has output/theses, and Lumio already has `synthetic`. Synthetic pages need visible labeling and stricter citation behavior.

### Retrieval and graph

- **Read summaries before bodies.** Index/frontmatter reads are the dominant optimization across Ar9av and nvk. Lumio should add a Core `summarize_index()` or `registry()` API and include registry selection in Retrieval Trace.
- **Graph queries are not lexical queries.** Relationship/path questions should use typed edges and bounded BFS before BM25. Lumio has `graph_path`; expose it in the Agent Runtime when `_classify()` returns relationship/path.
- **Backlinks reveal blast radius.** lucas and SamurAI both use reference/backlink graphs for impact. Lumio should show incoming relationship/backlink impact during proposal review and after publish.
- **Cheap result explanations build trust.** `should_read`, `index_only`, candidate scores, BM25 reasons, and stale annotations are useful. Lumio's Retrieval Result already has `reason` and `trace`; make those user-visible in admin/power-user views.

### Ingest and maintenance

- **Delta ingest needs stable hashes.** Ar9av, lucas, SamurAI graph cache, and Lumio fingerprints all converge on content hashes. Use hashes over mtimes for publish/index freshness and proposal deduplication.
- **Proposal-first is the enterprise-safe variant.** Prior-art tools often write directly because they target local personal wikis. Lumio should keep staged proposals, diffs, validation reports, affected pages, and audit logs.
- **Maintenance should be layered.** Core validation catches broken structure; health/lint catches structural drift; librarian/audit catches quality/trust drift. Lumio MVP needs Core validation and freshness; later admin tools can add quality reports.
- **Controlled vocabularies matter once the wiki grows.** Tags, relationship types, lifecycle, confidence, visibility, source types, and topic guides prevent taxonomy drift.

### Capture channels

- **Useful knowledge arrives from many surfaces.** Browser clipping, uploads, raw inboxes, agent-session histories, chat exports, feedback, meetings, and external datasets all appear in prior art. Lumio should model this as many sources producing the same Ingest Proposal shape.
- **User annotations are not source claims.** lucas distinguishes source text, user highlights, and notes. Lumio should preserve voice/provenance if it later supports annotations.
- **Session/feedback memory needs an explicit promotion boundary.** nvk's `.sessions/` and feedback candidates are the cleanest pattern: capture privately, promote intentionally.

## Prioritized recommendations for Lumio

### MVP-adjacent / high leverage

1. **Require `summary` for new Compiled Pages.** Current docs mark it optional. All retrieval-efficient projects rely on summaries/descriptions. Make this a proposal-generation requirement first; consider validator enforcement after sample data and tests are ready.
2. **Add typed relationship vocabulary.** Keep free-form strings internally if needed, but document and prefer `relates-to`, `uses`, `extends`, `implements`, `contradicts`, `derived-from`, `replaces`. Validate unknown types as warnings first, not hard errors.
3. **Use graph retrieval for relationship questions.** Runtime classification already has `RELATIONSHIP`; route those to `graph_path`/`related_from` and include typed edges in the answer trace before BM25 fallback.
4. **Expose a Knowledge Base registry endpoint.** Return canonical title, aliases, tags, summary, lifecycle, visibility, path, source count, relationship count, and updated/fingerprint metadata. Use it for browse, admin review, and query planning.
5. **Show proposal blast radius.** Ingest Proposal review should list pages created/updated, duplicate title/alias risks, relationship targets added/removed, public/internal/restricted visibility changes, and pages that cite/link to affected pages.
6. **Keep deterministic health separate from LLM review.** `lumio validate` is the always-on gate. Future “lint” can propose patches but should not be required for Reader chat correctness.
7. **Annotate index freshness in UI/API.** Lumio already stores fingerprints. Surface `fresh/stale`, stored digest, current digest, and rebuild action to Owners/Maintainers.

### Post-MVP / strong bets

8. **Synthetic Page workflow from useful answers.** Let a Maintainer promote a cited answer into a `synthetic: true` Compiled Page with sources pointing to cited Compiled Pages, not raw model output.
9. **Dedup candidate report.** Use title/alias/token/tag similarity to report likely identity collisions before publish. Do not auto-merge in MVP.
10. **Cross-link/relationship suggestions.** Suggest missing relationships from co-citation, shared tags, and exact mentions; require Maintainer approval.
11. **Dataset manifest layer.** For future Connector/Dataset work, borrow nvk's manifest boundary: store location, schema, samples, profiles, query recipes, access, checksum, and caveats without copying large data into Markdown.
12. **Annotation-aware Evidence.** If Lumio adds highlights/comments, keep user notes as separate metadata/appendix and label matches as source vs annotation.
13. **Quality/freshness overlays.** Add computed confidence/staleness based on source quality, verification date, page updated date, lifecycle, and volatility rather than immediately expanding required frontmatter.

### Avoid

- Do not let chat mutate canonical Knowledge Base files directly.
- Do not make raw-source chunks a parallel citation source for MVP chat.
- Do not require Obsidian-specific syntax in Lumio's canonical format; support it as input/export compatibility only.
- Do not add autonomous research, session capture, feedback memory, dashboards, or collection imports until the core publish/retrieve/cite loop is boring and tested.
- Do not rely on markdown instructions for security boundaries. Auth, roles, visibility, raw-source exclusion, and retrieval-before-answer belong in code.

## Concrete backlog candidates

- `Core SDK: registry API for page summaries and graph metadata`.
- `Runtime: relationship/path retrieval strategy using typed relationships`.
- `Validation: summary-required warning for proposal-created pages`.
- `Validation: preferred relationship type warnings`.
- `Admin: proposal blast-radius panel`.
- `Admin: deterministic health report separate from validation`.
- `Ingest: source-hash dedupe and affected-page provenance in proposals`.
- `Post-MVP: promote cited answer to Synthetic Page`.
- `Post-MVP: dataset manifest / Connector planning spike`.
