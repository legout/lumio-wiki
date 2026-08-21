---
name: lumio-wiki
description: >-
  Use when the user wants to build, search, ingest into, review, publish, or
  diagnose a portable Lumio Knowledge Base from a coding agent. Covers
  initializing a compiled Markdown Knowledge Base, lexical page search, reading Compiled
  Pages by Canonical Page Title, typed Relationship traversal and shortest-path
  lookup, text/Markdown ingestion into reviewable Ingest Proposals, proposal
  inspection/validation, publication and discard, Discovery Graph health, and
  install diagnostics. The host coding agent is the default Distiller — no
  model provider is required for base text/Markdown ingestion. Triggers
  include "lumio", "knowledge base", "wiki", "compiled page", "ingest",
  "proposal", "publish", "navigation index", "hot index", "graph path",
  "related pages", "retrieval ladder", and "validate the Knowledge Base".
license: Apache 2.0
metadata:
  distribution: lumio-wiki
  version: "0.1.1"
---

Manage a portable Lumio Knowledge Base from this coding agent. Every operation
below invokes the public `lumio-wiki` CLI, which in turn calls the public
`lumio_wiki` Python surface. No internal application modules, no web server,
no LanceDB, no OpenAI client required for base behavior. This skill invokes
only public CLI/Python behavior and never parses the private MessagePack
Discovery Graph artifact — reach graph state through `related`, `paths`, and
`health` only. Read the [detailed coding-agent protocol](PROTOCOL.md) only when
the concise workflow below is insufficient.

## Setup

1. Confirm `lumio-wiki` is installed: `lumio-wiki --version`. If missing,
   `pip install lumio-wiki` (or `pip install 'lumio-wiki[documents]'` for
   PDF/image/office/HTML sources, `pip install 'lumio-wiki[llm]'` for an
   unattended OpenAI-compatible Distiller, `pip install 'lumio-wiki[all]'` for both).
2. Run `lumio-wiki doctor` once per session to see the install shape: version,
   which optional capabilities are present, and where the packaged skill lives.
3. For a project's first run, use `lumio-wiki setup <path>`. It creates or
   detects the Knowledge Base and writes project `.env` plus `AGENTS.md` for
   restarted and cross-client sessions. `lumio-wiki init <path>` is the
   lower-level KB-only operation. S3 projects configure through the same
   command: `lumio-wiki setup <kb> --publish-to <s3-uri>` records the
   publication destination (`LUMIO_PUBLISH_TO`), the retrieval backend
   (`LUMIO_RETRIEVAL_BACKEND`), and the private source artifact store
   (`LUMIO_SOURCE_STORE`) in `.env`; `lumio-wiki setup --from <s3-uri>`
   configures a read-only project. Setup writes no credentials.
4. Skill installation is explicit. Prefer `lumio-wiki skill install --scope
   user` for a shared cross-client copy; use project scope only for a trusted
   repository. Check drift with `skill status` and refresh with `skill update`.

You are the default **Distiller**. For text and Markdown Knowledge Sources,
the `PassthroughMarkdownDistiller` passes your authored Markdown straight
into a reviewable Ingest Proposal — no second model provider or API key is
needed. Author the proposed Compiled Page Markdown yourself (frontmatter +
body), then bind it to the original source with
`lumio-wiki ingest <kb> <source> --compiled-page <page> --source-id <id>`.

## Ubiquitous language

The Knowledge Base is a local filesystem tree of compiled Markdown pages.
A **Compiled Page** has YAML frontmatter (title, aliases, tags, summary,
lifecycle, visibility, sources, relationships, synthetic) and a body. The
**Canonical Page Title** is the unique title a page is known by. A typed
**Relationship** is a reviewed semantic edge; an **Extracted Reference** is a
deterministic non-canonical reference derived from a body link. An **Ingest
Proposal** is a staged, reviewable set of proposed changes. Publication
produces a **Published Version** and regenerates the reserved Navigation
Index (`index.md`) and Hot Index (`hot.md`). See `CONTEXT.md` in the Lumio
repository for the full glossary.

## Commands

The CLI is the portable public operating surface. `<kb>` is the Knowledge Base
root directory in every command below.

| Command | What it does |
|---|---|
| `lumio-wiki setup <path> \| --from <s3-uri> [--publish-to <s3-uri>] [--retrieval zero-index\|lancedb] [--source-store <s3-uri>] [--skill-scope user\|project\|--agent <name>]` | Canonical first run: create/detect the KB (or bind a read-only S3 Location) and write project `.env` plus `AGENTS.md`; S3 configuration keys are recorded in `.env`; skill installation remains explicit. |
| `lumio-wiki init <path>` | Lower-level KB-only operation: create a categorized root with a seeded Control File. |
| `lumio-wiki validate <kb>` | Load and validate every page, Control File, link, and reserved artifact. Exit 1 on errors. |
| `lumio-wiki hot <kb>` | Render the Maintainer-pinned Hot Index (ladder 0). Curated entry pages. |
| `lumio-wiki index <kb> [dir]` | Render the generated Navigation Index (ladder 1). Root catalog, or a directory's shallow index. |
| `lumio-wiki search <kb> <query> [--limit N] [--mode lexical\|semantic\|hybrid] [--model M] [--index-dir D]` | Retrieve citation-ready Evidence. Default `--mode lexical` is deterministic zero-index (no index/model). `--mode semantic\|hybrid` add embedding-based retrieval; need `lumio-lancedb` + an embedder (`lumio-lancedb[embeddings]` or `LUMIO_PROVIDER_*`). |
| `lumio-wiki page <kb> <title>` | Read a Compiled Page by Canonical Page Title (falls back to alias). Prints frontmatter + body. |
| `lumio-wiki related <kb> <title> [--relationship-type T] [--depth N] [--max-edges N] [--max-results N] [--scope canonical\|discovery] [--direction outgoing\|incoming\|both] [--trace]` | Bounded graph traversal of related Canonical Page Titles. |
| `lumio-wiki paths <kb> <source> <target> [--scope canonical\|discovery] [--direction ...] [--max-depth N] [--max-edges N] [--trace]` | Shortest directed path between two titles, hop-bounded. |
| `lumio-wiki ingest <kb> <file> [--content-type T]` | Read a text/Markdown file, distill it (you are the Distiller), stage a reviewable Ingest Proposal. No private Source identity is established. |
| `lumio-wiki ingest <kb> <source> --compiled-page <page.md> --source-id <id>` | Managed host-Distiller ingest (issue #149): bind the ORIGINAL raw source to your authored Compiled Page under one stable identity; registers the source, stages the authored page as one proposal. No `[documents]` extra required. |
| `lumio-wiki proposal list <kb>` | List staged proposals. |
| `lumio-wiki proposal inspect <kb> <id> [--json]` | Print proposal metadata, blast radius, diff (or full JSON). |
| `lumio-wiki proposal validate <kb> <id>` | Print the proposal's validation report. Exit 1 on errors. |
| `lumio-wiki publish <kb> <id>` | Apply a proposal's pages to the KB root, regenerate reserved artifacts, mark it terminal. |
| `lumio-wiki discard <kb> <id>` | Mark a reviewable proposal as discarded (terminal). |
| `lumio-wiki health <kb> [--rebuild]` | Page counts, validation status, Discovery Graph health + fingerprint. `--rebuild` materializes a fresh graph artifact (actionable recovery); a bad/missing artifact never blocks zero-index operation. |
| `lumio-wiki lint <kb>` | Read-only cross-page QA report: validation, graph health, canonical/discovery structural diagnostics, scope disclosure. Exit 1 when invalid (ADR-0015). |
| `lumio-wiki cross-link <kb> [--limit N] [--stage]` | Missing-link candidates ranked by Discovery Graph impact. `--stage` stages one reviewable repair proposal per top candidate; never direct-writes. |
| `lumio-wiki relationship stage <kb> <source> <target> --type T` | Stage a typed canonical Relationship proposal (e.g. `--type uses`). Distinct from `cross-link --stage` (authored Markdown links / Extracted References); reviewed through the same proposal pipeline. |
| `lumio-wiki source <kb> <register\|list\|retire\|reactivate> --source-id <id>` | Manage private Knowledge Source lifecycle state (ADR-0014). Explicit `retire`/`reactivate` stage ordinary reviewable proposals; `list` reports identities/status without disclosing raw bytes. |
| `lumio-wiki source inspect <kb> --source-id <id> [--published-version <v>]` | Secret-free metadata for the ONE exact Source Version bound to the id: safe filename, media type, size, digest abbreviation, publication binding, verified availability, authorization outcome (ADR-0020). Local worktrees resolve the registry's current version; S3 KBs / `--published-version` resolve the private Source Binding Manifest — never a silent fallback to the latest version. |
| `lumio-wiki source fetch <kb> --source-id <id> [--published-version <v>] --output <path>` | Byte-exact original Source Artifact to an explicit destination, digest and size re-verified (ADR-0020). A directory destination receives the safe filename. Content is not rendered or converted here. |
| `lumio-wiki source link <kb> --source-id <id> [--published-version <v>] [--expires 5m]` | Explicit short-lived signed GET URL for ONE exact artifact when the store supports signing (S3 adapter). 5 min default, 1 h max; the URL is a bearer secret — never persist or log it. Prefer verified `fetch` (signed URLs can leak through conversation history). |
| `lumio-wiki dream <kb> [--limit N] [--stage] [--semantic]` | Deterministic Dream Cycle reflection plus optional semantic review; `--semantic` requires the `[llm]` extra and remains proposal-first. |
| `lumio-wiki doctor` | Version, detected optional extras, and packaged skill location. |
| `lumio-wiki skill path` | Absolute path of the packaged `SKILL.md` inside the installed wheel. |
| `lumio-wiki skill protocol` | Absolute path of the packaged `PROTOCOL.md`. |
| `lumio-wiki skill install --scope user\|project` | Explicitly install the canonical bundle into shared cross-client scope. User is preferred; project requires a trusted repository. |
| `lumio-wiki skill install --agent <name>` | Explicit compatibility fallback for `pi`, `hermes`, `codex`, or `claude-code`. |
| `lumio-wiki skill status [--scope user\|project\|--agent <name>]` | Read-only missing/current/stale/corrupt drift report. Defaults to shared user scope. |
| `lumio-wiki skill update [--scope user\|project\|--agent <name>]` | Atomically refresh an installed copy from the current wheel. |

## Workflow: the retrieval ladder

Find context PROGRESSIVELY, cheapest-first, with no external index. Stop as
soon as you have citation-ready Evidence that supports the question.

0. **Hot Index** — `lumio-wiki hot <kb>`. The curated, Maintainer-pinned entry
   pages. Read this first.
1. **Navigation Indexes** — `lumio-wiki index <kb> [dir]`. The generated
   catalog of every page by directory.
2. **Deterministic search** — `lumio-wiki search <kb> "<query>"`. Zero-index
   lexical search; no external index. Add `--mode semantic` or `--mode hybrid`
   (needs `lumio-lancedb` + an embedder) as an escalation rung when lexical
   surface matching is insufficient.
3. **Focused page read** — `lumio-wiki page <kb> "<title>"`. Read one page to
   confirm it supports a claim and copy the exact passage.
4. **Related-page lookup** — `lumio-wiki related <kb> "<title>" [--scope
   discovery] [--direction both] [--depth N] [--trace]`. Bounded neighbors;
   `--scope discovery` adds deterministic body-link Extracted References.
5. **Bounded paths** — `lumio-wiki paths <kb> "<src>" "<tgt>" [--max-depth N]
   [--trace]`. Shortest directed path, hop-bounded.

Use `--scope discovery` to include Extracted References (deterministic body
links) alongside canonical Relationships, and `--trace` on `related`/`paths`
for a truthful diagnostic of the scope, direction, bounds, and outcome
actually used.

**Cite paths AND passages.** Every claim must cite the Canonical Page Title,
the relative path the CLI prints, AND the supporting passage from the body.
If the selected Evidence does not support the question, report "not covered by
this knowledge base" — do not fabricate, and do not let graph connectivity
manufacture support (an Extracted Reference is topology, never Evidence).

## Workflow: ingest (you are the Distiller)

The managed host-Distiller mode binds the ORIGINAL raw Knowledge Source and
your authored Compiled Page under one stable source identity, so provenance
records the real source (not a throwaway temp file). Prefer it whenever you
have the original source file.

1. The user provides the original Knowledge Source (a PDF, DOCX, HTML, TXT, or
   Markdown file). You do NOT convert it yourself — no `[documents]` extra is
   required for managed ingest.
2. Author the proposed Compiled Page Markdown: YAML frontmatter (title,
   aliases, tags, summary, lifecycle, visibility, sources, relationships,
   synthetic) plus a body. Match the existing Knowledge Base's voice and
   structure. **The page MUST declare the source identity in `sources[].id`.**
3. Choose a stable, lowercase `--source-id` for the original source (e.g.
   `annual-impact-report`) and bind both in ONE command:
   `lumio-wiki ingest <kb> <original-source> --compiled-page <page.md>
   --source-id <id>`. The CLI registers the original bytes under that identity
   (reusing the Source Version on an identical retry; rejecting changed bytes
   until you retire/reactivate), derives the converter name from routing
   WITHOUT running it, stages the authored page as one proposal, and prints
   the proposal id, source id/hash, affected pages, and blocked status.
4. Inspect the proposal:
   `lumio-wiki proposal inspect <kb> <id>` (provenance + diff) and
   `lumio-wiki proposal validate <kb> <id>` (validation report).
5. If valid and the user approves, `lumio-wiki publish <kb> <id>`. If the
   user rejects it, `lumio-wiki discard <kb> <id>`. Proposal-first is the
   default write mode: validation always runs before publish. A discarded
   proposal leaves the source identity active-but-unsupported; an identical
   retry reuses it safely.

Plain text/Markdown passthrough (no separate original source) still works:
`lumio-wiki ingest <kb> <file>` distills the file and stages it, but it does
NOT establish a private Source identity — use the managed mode whenever you
have the original bytes to preserve lineage.

## Workflow: maintenance (you are the Maintainer)

Run periodically or after large ingests — the Dream Cycle keeps a living
Knowledge Base connected:

1. `lumio-wiki lint <kb>` — read-only QA. Check `valid`, validation
   errors/warnings, and the structural diagnostics for BOTH scopes
   (canonical = reviewed Relationships; discovery = Relationships plus
   Extracted References). Exit 1 means fix pages before anything else.
2. `lumio-wiki dream <kb>` — the reflection report: health, structure, and
   the missing-link candidates ranked by Discovery Graph impact (orphan
   repair, component join, fragile-connection strengthening).
3. `lumio-wiki dream <kb> --stage [--limit N]` — stage the top repairs as
   ordinary reviewable Ingest Proposals. Nothing direct-writes: review with
   `proposal inspect`, then `publish` or `discard` as usual.
   Add opt-in `--semantic` (requires the `[llm]` extra) to stage semantic
   findings through the same proposal-first path.
4. `lumio-wiki cross-link <kb>` is the focused variant when you only want
   the candidate list (or only link repairs, `--stage`). `cross-link --stage`
   only adds authored Markdown links (Extracted References, discovery-graph
   topology) — it never creates a typed canonical Relationship.
5. `lumio-wiki relationship stage <kb> <source> <target> --type T` promotes a
   typed canonical Relationship (e.g. `--type uses`) through the same
   proposal-first pipeline. It is the canonical-graph counterpart to
   `cross-link --stage`; the type is never inferred. Preferred types:
   `contradicts`, `derived-from`, `extends`, `implements`, `relates-to`,
   `replaces`, `uses` (a non-preferred type stages as a warning-level
   generic edge).

The same operations exist on the public Python surface
(`lumio_wiki.run_lint`, `lumio_wiki.run_dream_cycle`,
`lumio_wiki.stage_dream_repairs`, `lumio_wiki.stage_cross_link_proposal`,
`lumio_wiki.stage_relationship_proposal`).

## Optional capabilities

The base install handles text and Markdown. Optional extras are declared but
not installed by default:

- `lumio-wiki[documents]` — document conversion, routed per format
  (ADR-0018): PDF / scanned-PDF / image → LiteParse (OCR + real page numbers);
  office formats (Word/PowerPoint/Excel/OpenDocument/RTF/EPUB/CSV) → AnyDoc;
  HTML and other broad formats → MarkItDown.
- `lumio-wiki[llm]` — an unattended OpenAI-compatible Distiller (use when you
  do NOT want the host coding agent to be the Distiller).
- `lumio-wiki[all]` — both.
- `lumio-lancedb` — enhanced BM25 / semantic / hybrid retrieval. The base
  zero-index retrieval is always available; clients keep the same
  RetrievalResult / Evidence / citation / Trace contract when the adapter is
  installed. Once installed, `lumio-wiki search --mode semantic|hybrid`
  retrieves through it from the CLI (needs an embedder:
  `lumio-lancedb[embeddings]` or `LUMIO_PROVIDER_*`).

`lumio-wiki doctor` reports which extras are present and names the exact
install command for any that are missing.

## Install and maintain the skill

```
lumio-wiki skill install --scope user
lumio-wiki skill status --scope user
lumio-wiki skill update --scope user
```

Shared user scope (`~/.agents/skills/lumio-wiki/`) is preferred for cross-client
reuse. Project scope is opt-in for trusted repositories. Compatibility targets
remain available through `--agent pi|hermes|codex|claude-code`. Every copy has a
version/hash manifest; status is read-only and update is explicit and atomic.
Restart the agent or start a new session after install/update.

## Non-goals

- This skill does not start the Lumio web application. The browser app, Chat
  Gateway, Agent Runtime, auth/roles, operational database, and storage sync
  live in the full `lumio` distribution.
- This skill does not implement LanceDB/enhanced retrieval itself, but
  `lumio-wiki search --mode semantic|hybrid` exposes it when `lumio-lancedb`
  (+ an embedder) is installed. Install `lumio-lancedb` (or
  `lumio-lancedb[embeddings]`) to enable BM25, semantic, or hybrid ranking.
- This skill does not bypass validation. Proposal-first is the default write
  mode; validation always runs before publish.
te
  mode; validation always runs before publish.
