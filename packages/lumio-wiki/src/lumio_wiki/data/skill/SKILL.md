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
  "related pages", and "validate the Knowledge Base".
version: 0.1.1
user-invocable: true
argument-hint: "[init|validate|search|page|related|paths|ingest|proposal|publish|discard|health|doctor|skill] [args]"
license: Apache 2.0
---

Manage a portable Lumio Knowledge Base from this coding agent. Every operation
below invokes the public `lumio-wiki` CLI, which in turn calls the public
`lumio_wiki` Python surface. No internal application modules, no web server,
no LanceDB, no OpenAI client required for base behavior.

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
| `lumio-wiki search <kb> <query> [--limit N]` | Deterministic lexical search over titles, aliases, tags, summaries, bodies. Zero-index; no external index. |
| `lumio-wiki page <kb> <title>` | Read a Compiled Page by Canonical Page Title (falls back to alias). Prints frontmatter + body. |
| `lumio-wiki related <kb> <title> [--relationship-type T] [--depth N] [--scope canonical\|discovery] [--direction outgoing\|incoming\|both]` | Bounded graph traversal of related Canonical Page Titles. |
| `lumio-wiki paths <kb> <source> <target> [--scope ...] [--direction ...]` | Shortest typed path between two titles. |
| `lumio-wiki ingest <kb> <file> [--content-type T]` | Read a text/Markdown file, distill it (you are the Distiller), stage a reviewable Ingest Proposal. |
| `lumio-wiki proposal list <kb>` | List staged proposals. |
| `lumio-wiki proposal inspect <kb> <id> [--json]` | Print proposal metadata, blast radius, diff (or full JSON). |
| `lumio-wiki proposal validate <kb> <id>` | Print the proposal's validation report. Exit 1 on errors. |
| `lumio-wiki publish <kb> <id>` | Apply a proposal's pages to the KB root, regenerate reserved artifacts, mark it terminal. |
| `lumio-wiki discard <kb> <id>` | Mark a reviewable proposal as discarded (terminal). |
| `lumio-wiki health <kb>` | Page counts, validation status, Discovery Graph health + fingerprint. |
| `lumio-wiki doctor` | Version, detected optional extras, and packaged skill location. |
| `lumio-wiki skill path` | Absolute path of the packaged `SKILL.md` inside the installed wheel. |
| `lumio-wiki skill protocol` | Absolute path of the packaged `PROTOCOL.md`. |
| `lumio-wiki skill install --agent <name>` | Copy the skill + protocol into a coding agent's skill directory. Agents: `pi`, `hermes`, `codex`, `claude-code`. |

## Workflow: search and cite

When the user asks a question answerable from the Knowledge Base:

1. Run `lumio-wiki search <kb> "<query>"` to find candidate pages.
2. Read each candidate with `lumio-wiki page <kb> "<title>"` to confirm it
   supports the answer.
3. For multi-hop questions, use `lumio-wiki paths <kb> <source> <target>` and
   `lumio-wiki related <kb> "<title>"` to trace typed Relationships.
4. Cite the Canonical Page Title (and the relative path the CLI prints) for
   every claim. If the Knowledge Base does not support a claim, say so — do
   not fabricate. This mirrors the Lumio guardrail: domain claims require
   citation; unsupported questions return "not covered".

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
