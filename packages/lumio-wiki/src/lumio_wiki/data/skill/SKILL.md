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
version: 0.1.1
user-invocable: true
argument-hint: "[init|validate|hot|index|search|page|related|paths|ingest|proposal|publish|discard|health|lint|cross-link|dream|doctor|skill] [args]"
license: Apache 2.0
---

Manage a portable Lumio Knowledge Base from this coding agent. Every operation
below invokes the public `lumio-wiki` CLI, which in turn calls the public
`lumio_wiki` Python surface. No internal application modules, no web server,
no LanceDB, no OpenAI client required for base behavior. This skill invokes
only public CLI/Python behavior and never parses the private MessagePack
Discovery Graph artifact — reach graph state through `related`, `paths`, and
`health` only.

## Setup

1. Confirm `lumio-wiki` is installed: `lumio-wiki --version`. If missing,
   `pip install lumio-wiki` (or `pip install 'lumio-wiki[documents]'` for
   PDF/DOCX/image sources, `pip install 'lumio-wiki[llm]'` for an unattended
   OpenAI-compatible Distiller, `pip install 'lumio-wiki[all]'` for both).
2. Run `lumio-wiki doctor` once per session to see the install shape: version,
   which optional capabilities are present, and where the packaged skill lives.
3. If the user has not yet got a Knowledge Base, run `lumio-wiki init <path>`
   to create one. The Knowledge Base is a directory of compiled Markdown
   pages plus a root `lumio.yaml` Control File.

You are the default **Distiller**. For text and Markdown Knowledge Sources,
the `PassthroughMarkdownDistiller` passes your authored Markdown straight
into a reviewable Ingest Proposal — no second model provider or API key is
needed. Author the proposed Compiled Page Markdown yourself (frontmatter +
body), then ingest it through `lumio-wiki ingest`.

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

The CLI mirrors the public Python surface one-to-one. `<kb>` is the
Knowledge Base root directory in every command below.

| Command | What it does |
|---|---|
| `lumio-wiki init <path>` | Create a categorized KB root with a seeded Control File. |
| `lumio-wiki validate <kb>` | Load and validate every page, Control File, link, and reserved artifact. Exit 1 on errors. |
| `lumio-wiki hot <kb>` | Render the Maintainer-pinned Hot Index (ladder 0). Curated entry pages. |
| `lumio-wiki index <kb> [dir]` | Render the generated Navigation Index (ladder 1). Root catalog, or a directory's shallow index. |
| `lumio-wiki search <kb> <query> [--limit N]` | Deterministic lexical search over titles, aliases, tags, summaries, bodies. Zero-index; no external index. |
| `lumio-wiki page <kb> <title>` | Read a Compiled Page by Canonical Page Title (falls back to alias). Prints frontmatter + body. |
| `lumio-wiki related <kb> <title> [--relationship-type T] [--depth N] [--max-edges N] [--max-results N] [--scope canonical\|discovery] [--direction outgoing\|incoming\|both] [--trace]` | Bounded graph traversal of related Canonical Page Titles. |
| `lumio-wiki paths <kb> <source> <target> [--scope canonical\|discovery] [--direction ...] [--max-depth N] [--max-edges N] [--trace]` | Shortest directed path between two titles, hop-bounded. |
| `lumio-wiki ingest <kb> <file> [--content-type T]` | Read a text/Markdown file, distill it (you are the Distiller), stage a reviewable Ingest Proposal. |
| `lumio-wiki proposal list <kb>` | List staged proposals. |
| `lumio-wiki proposal inspect <kb> <id> [--json]` | Print proposal metadata, blast radius, diff (or full JSON). |
| `lumio-wiki proposal validate <kb> <id>` | Print the proposal's validation report. Exit 1 on errors. |
| `lumio-wiki publish <kb> <id>` | Apply a proposal's pages to the KB root, regenerate reserved artifacts, mark it terminal. |
| `lumio-wiki discard <kb> <id>` | Mark a reviewable proposal as discarded (terminal). |
| `lumio-wiki health <kb> [--rebuild]` | Page counts, validation status, Discovery Graph health + fingerprint. `--rebuild` materializes a fresh graph artifact (actionable recovery); a bad/missing artifact never blocks zero-index operation. |
| `lumio-wiki lint <kb>` | Read-only cross-page QA report: validation, graph health, canonical/discovery structural diagnostics, scope disclosure. Exit 1 when invalid (ADR-0015). |
| `lumio-wiki cross-link <kb> [--limit N] [--stage]` | Missing-link candidates ranked by Discovery Graph impact. `--stage` stages one reviewable repair proposal per top candidate; never direct-writes. |
| `lumio-wiki dream <kb> [--limit N] [--stage]` | The Dream Cycle: read-only reflection (validation + health + structure + ranked candidates); `--stage` stages the top repairs as reviewable Ingest Proposals (ADR-0015). |
| `lumio-wiki doctor` | Version, detected optional extras, and packaged skill location. |
| `lumio-wiki skill path` | Absolute path of the packaged `SKILL.md` inside the installed wheel. |
| `lumio-wiki skill protocol` | Absolute path of the packaged `PROTOCOL.md`. |
| `lumio-wiki skill install --agent <name>` | Copy the skill + protocol into a coding agent's skill directory. Agents: `pi`, `hermes`, `codex`, `claude-code`. |

## Workflow: the retrieval ladder

Find context PROGRESSIVELY, cheapest-first, with no external index. Stop as
soon as you have citation-ready Evidence that supports the question.

0. **Hot Index** — `lumio-wiki hot <kb>`. The curated, Maintainer-pinned entry
   pages. Read this first.
1. **Navigation Indexes** — `lumio-wiki index <kb> [dir]`. The generated
   catalog of every page by directory.
2. **Deterministic search** — `lumio-wiki search <kb> "<query>"`. Zero-index
   lexical search; no external index.
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

1. The user provides a text or Markdown Knowledge Source (a file path, pasted
   text, or a URL you fetch). For PDF/DOCX/image sources, the `[documents]`
   extra must be installed — `lumio-wiki doctor` reports it.
2. Author the proposed Compiled Page Markdown: YAML frontmatter (title,
   aliases, tags, summary, lifecycle, visibility, sources, relationships,
   synthetic) plus a body. Match the existing Knowledge Base's voice and
   structure.
3. Write the Markdown to a temp file, then
   `lumio-wiki ingest <kb> <file>`. The CLI prints the staged proposal id,
   affected pages, blocked status, and blast radius.
4. Inspect the proposal:
   `lumio-wiki proposal inspect <kb> <id>` (metadata + diff) and
   `lumio-wiki proposal validate <kb> <id>` (validation report).
5. If valid and the user approves, `lumio-wiki publish <kb> <id>`. If the
   user rejects it, `lumio-wiki discard <kb> <id>`. Proposal-first is the
   default write mode: validation always runs before publish.

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
4. `lumio-wiki cross-link <kb>` is the focused variant when you only want
   the candidate list (or only link repairs, `--stage`).

The same operations exist on the public Python surface
(`lumio_wiki.run_lint`, `lumio_wiki.run_dream_cycle`,
`lumio_wiki.stage_dream_repairs`, `lumio_wiki.stage_cross_link_proposal`,
`lumio_wiki.stage_relationship_proposal`).

## Optional capabilities

The base install handles text and Markdown. Optional extras are declared but
not installed by default:

- `lumio-wiki[documents]` — LiteParse + MarkItDown for PDF, scanned-PDF, image,
  DOCX, HTML, and other document conversion.
- `lumio-wiki[llm]` — an unattended OpenAI-compatible Distiller (use when you
  do NOT want the host coding agent to be the Distiller).
- `lumio-wiki[all]` — both.
- `lumio-lancedb` — enhanced BM25 / semantic / hybrid retrieval. The base
  zero-index retrieval is always available; clients keep the same
  RetrievalResult / Evidence / citation / Trace contract when the adapter is
  installed.

`lumio-wiki doctor` reports which extras are present and names the exact
install command for any that are missing.

## Install the skill into another agent

```
lumio-wiki skill install --agent pi
lumio-wiki skill install --agent hermes
lumio-wiki skill install --agent codex
lumio-wiki skill install --agent claude-code
```

The command copies `SKILL.md` and `PROTOCOL.md` into the agent's conventional
skill directory and prints the destination. Pass `--dest <dir>` to override
the destination, and `--overwrite` to replace an existing copy.

## Non-goals

- This skill does not start the Lumio web application. The browser app, Chat
  Gateway, Agent Runtime, auth/roles, operational database, and storage sync
  live in the full `lumio` distribution.
- This skill does not implement LanceDB/enhanced retrieval. Install
  `lumio-lancedb` separately if you need BM25, semantic, or hybrid ranking.
- This skill does not bypass validation. Proposal-first is the default write
  mode; validation always runs before publish.
