# myKG fit for Lumio

## Tracking

- **Status:** focused experiment proposed; no adoption decision has been made.
- **Experiment:** [GitHub issue #146](https://github.com/legout/lumio/issues/146).
- **Experiment harness:** [`experiments/mykg/`](../../experiments/mykg/) — candidate converter, runbook, and report template (Path B: harness only; the run-dependent metrics, audit, and recommendation stay deferred pending the maintainer inputs).
- **Engineering documents:** an ADR and productization spec are deferred until the experiment produces an `adopt`, `defer`, or `reject` recommendation.

## Verdict

myKG is a much better fit for Lumio than for selayer, but it is not a drop-in dependency.

Its strongest role is an optional upstream, corpus-level extractor that produces review material for Lumio's existing proposal pipeline. It should not replace Lumio's Source Processors, Distiller, canonical Compiled Pages, Discovery Graph, or Reader retrieval.

The practical recommendation is:

1. use myKG as a separate process for a small extraction experiment;
2. translate its nodes and edges into a reviewable Lumio import proposal;
3. keep myKG output private until a Maintainer approves it;
4. publish only canonical Compiled Pages through Lumio's normal validation and proposal flow.

## Why the fit is promising

Lumio and myKG both turn raw documents into navigable knowledge, but they are optimized for different stages.

Lumio already owns:

- document conversion into stable normalized sections;
- per-source distillation into proposed Compiled Pages;
- stable Knowledge Source identity and immutable Source Versions;
- proposal review, validation, blast-radius analysis, and atomic publication;
- canonical Markdown pages with provenance, lifecycle, visibility, and reviewed Relationships;
- a derived Discovery Graph;
- citation-ready Evidence and refusal when support is missing;
- OKF and external compiled-Markdown import and export.

myKG adds capabilities Lumio does not currently have:

- corpus-wide RDFS/OWL schema induction;
- cross-document entity extraction and name normalization;
- typed relationship proposals across documents;
- confidence on nodes, edges, and attributes;
- source-chunk evidence for extracted entities and relationships;
- orphan detection and relationship repair proposals;
- resumable extraction sessions and selective re-extraction after schema growth.

Those additions could improve Lumio's Maintainer workflow for large, messy source collections. They are most useful before knowledge becomes canonical.

Primary myKG sources:

- [README](https://github.com/SenolIsci/mykg/blob/main/README.md)
- [Architecture](https://github.com/SenolIsci/mykg/blob/main/docs/architecture.md)
- [MCP server](https://github.com/SenolIsci/mykg/blob/main/src/mykg/mcp_server.py)
- [Query implementation](https://github.com/SenolIsci/mykg/blob/main/src/mykg/query.py)
- [Identifier generation](https://github.com/SenolIsci/mykg/blob/main/src/mykg/ids.py)
- [Schema validation](https://github.com/SenolIsci/mykg/blob/main/src/mykg/schema_validator.py)

## Where myKG overlaps with Lumio

A large part of myKG would duplicate existing Lumio modules.

| Capability | Lumio owner | myKG overlap |
| --- | --- | --- |
| PDF, Office, HTML, and image conversion | `lumio_wiki.source_processor` | myKG preprocessing and MinerU conversion |
| LLM document distillation | `Distiller` and `OpenAIDistiller` | myKG Pass 2 extraction |
| source hashing and incremental updates | `SourceRegistry` and Source Versions | myKG sessions and append mode |
| graph traversal | Core SDK canonical and Discovery Graph scopes | myKG NetworkX graph and MCP tools |
| Markdown knowledge export | canonical Compiled Pages and OKF profiles | myKG Obsidian vault |
| search and path queries | Core SDK retrieval and graph paths | myKG lexical search and BFS/DFS |
| semantic maintenance | Dream Cycle and proposal staging | orphan connection and schema growth |

Adopting myKG for these overlapping capabilities would create two ingestion models, two identity systems, two graph stores, and two freshness models. Lumio should keep one canonical pipeline.

## The authority mismatch

The main integration problem is not technical. It is semantic authority.

Lumio's accepted decisions are explicit:

- Compiled Markdown is canonical.
- A canonical Relationship is a reviewed semantic claim.
- An Extracted Reference supports navigation only.
- Discovery Graph reachability selects pages to inspect; it never constitutes Evidence.
- raw Knowledge Sources and private registry state do not enter Reader retrieval or public exports;
- every mutation is proposal-first and validated before publication.

See `CONTEXT.md`, `docs/prd/0001-knowledge-agent-platform.md`, `docs/adr/0011-derived-reference-graph-and-progressive-storage.md`, and `docs/adr/0014-source-versions-and-page-level-invalidation.md`.

myKG produces LLM-extracted nodes and edges after schema review. Confidence and source files make those edges inspectable, but they do not make them reviewed Lumio Relationships. A `0.95` extraction score is not equivalent to Lumio lifecycle `approved`.

Therefore:

- myKG edges must enter Lumio as candidates, never canonical Relationships;
- myKG ontology classes may suggest a page's free-form `type`, but must not create Content Categories automatically;
- myKG source paths and chunks may support Maintainer review, but must remain private;
- myKG IDs must not become Canonical Page Titles or Knowledge Source IDs without explicit mapping;
- omitted myKG nodes must never imply Page Removal or Source Retirement.

## Where it should integrate

### Not the Distiller interface

Lumio's `Distiller` is deliberately small:

```text
distill(NormalizedSource, categories?) -> proposed Compiled Page Markdown
```

myKG is corpus-oriented. It induces a shared ontology, normalizes identities across files, and extracts cross-document relationships. Wrapping a complete myKG session around one `NormalizedSource` would lose its main advantage and make the Distiller unexpectedly expensive and stateful.

A `MyKgDistiller` would therefore be the wrong module shape.

### Not Reader retrieval

myKG's MCP server can return graph nodes, paths, and raw source chunks. Exposing that directly to Lumio's Agent Runtime would bypass canonical Compiled Pages, visibility filtering, Citation shaping, and the rule that raw sources stay outside Reader retrieval.

MCP may be useful for a Maintainer-only research tool, but it should not implement Lumio's `RetrievalAdapter`.

### Best fit: external extraction followed by import proposal

The best initial flow is:

```text
private source corpus
  -> myKG extraction session
  -> conversion to proposed Compiled Markdown
  -> Lumio external import / IngestProposal
  -> validation and blast-radius report
  -> Maintainer review
  -> canonical publication
```

Lumio already has most of the receiving path:

- `import_external_compiled_markdown()` parses an external Markdown tree;
- `propose_external_import()` maps categories and stages diagnostics;
- `ProposalPipeline.assemble()` builds the diff, validation report, and blast radius;
- `ProposalPipeline.publish()` performs authoritative candidate validation before writing;
- `SourceRegistry` keeps raw-source identity and Source Versions private.

A small converter can turn myKG `nodes.jsonl`, `edges.jsonl`, and source metadata into temporary Compiled Page Markdown. This avoids importing myKG into `lumio-wiki` and keeps all canonicalization inside Lumio.

## Mapping rules

A safe converter should follow these rules.

### Nodes

- Map a selected myKG node to one proposed Compiled Page.
- Derive a candidate Canonical Page Title, then detect collisions against titles and aliases.
- Preserve myKG aliases as proposed aliases only after collision checks.
- Map ontology class to free-form page `type`, not Content Category.
- Route to an existing category. A new category requires an explicit Control File proposal and Maintainer approval.
- Default lifecycle to `draft` or `review`, never `approved`.
- Default visibility conservatively, usually `internal`.
- Require at least one Lumio `Source` for every non-synthetic page.

### Edges

- Do not write myKG edges directly to frontmatter Relationships.
- Present relation type, confidence, source files, and supporting chunks in proposal review metadata.
- Only accepted edges become canonical Relationships.
- If a relation merely means that one page mentions another, render a reviewed body link or leave it as a candidate. Lumio can derive an Extracted Reference after publication.
- Map relation types to Lumio's preferred vocabulary only when the meaning is exact. Do not collapse arbitrary ontology properties into `relates-to` without disclosure.

### Provenance and confidence

- Map stable corpus/source identity into Lumio's private Knowledge Source Registry.
- Preserve source hashes and versions there, rather than treating filenames as identity.
- Keep per-attribute and per-edge confidence in private proposal diagnostics.
- Never map confidence to lifecycle, visibility, or publication status.
- Make supporting source chunks available to Maintainers, but exclude them from public Knowledge Base export unless the Maintainer deliberately writes a cited summary into the Compiled Page.

## Packaging and ownership

Do not add myKG to the `lumio-wiki` distribution. `lumio-wiki` is intentionally model-free and has a small dependency set. myKG brings RDF, NetworkX, MCP, multiple model clients, document tooling, and pipeline orchestration.

For the first experiment, use a standalone script or Agent Skill that:

1. invokes the `mykg` CLI;
2. reads the session outputs;
3. writes a temporary external Markdown tree;
4. calls Lumio's existing import/proposal path.

If the experiment succeeds, the subprocess orchestration belongs in the full `lumio` application or an optional external package. The portable `lumio-wiki` package should only consume the resulting Markdown proposal.

Do not introduce a general extractor-provider interface until a second corpus extractor exists. One adapter would be a hypothetical seam.

## What Lumio should borrow even without integration

Several myKG design choices are worth studying directly:

- a schema review gate before corpus-wide extraction;
- locked base ontology plus explicit proposed additions;
- per-file extraction shards and resumable stages;
- source-chunk evidence for relation proposals;
- orphan diagnostics separated from automatic repair;
- schema history and merge logs;
- bounded re-extraction when the schema grows.

Lumio should not copy myKG's confidence merge policy or identity slugs. Confidence is not trust, and normalized type/name slugs can collide.

## Experiment

Use one small domain corpus with 5 to 10 non-sensitive documents.

1. Run Lumio's current per-source Distiller to establish a baseline proposal.
2. Run myKG with schema review enabled on the same corpus.
3. Convert selected myKG nodes into proposed Compiled Pages.
4. Present edges as review candidates with supporting chunks.
5. Stage the result through `propose_external_import()` or `ProposalPipeline`.
6. Compare the proposals before publishing anything.

Measure:

- duplicate entities and title collisions;
- unsupported or hallucinated attributes;
- relationship acceptance, modification, and rejection rates;
- source-passage correctness;
- number of pages accepted versus discarded;
- Maintainer review time;
- extraction latency and model cost;
- whether raw source paths, source text, or restricted data leak into Compiled Pages or exports.

Success requires:

- every accepted page has valid Lumio provenance;
- no myKG edge becomes canonical without review;
- at least 85 percent of accepted relationship candidates need no semantic correction;
- entity duplication is lower than the current per-source Distiller baseline;
- the resulting Knowledge Base passes normal candidate validation and lint;
- Maintainer review effort is lower than manually reconciling the baseline proposals;
- no raw source material leaks into Reader-visible artifacts.

Stop if the relationship precision is poor, one-entity-per-page output floods the Knowledge Base with low-value pages, identity collisions require extensive manual cleanup, or the review burden exceeds direct Lumio distillation.

## Conclusion

myKG is a good candidate for Lumio's pre-publication ingestion workflow, especially for multi-document corpora where cross-document entity resolution matters. It is not a good candidate for Lumio's runtime or canonical data model.

The fit is best described as:

> myKG discovers and proposes; Lumio reviews, canonicalizes, publishes, retrieves, and cites.

That is strong enough to justify a focused experiment, but not yet a dependency or product integration.
