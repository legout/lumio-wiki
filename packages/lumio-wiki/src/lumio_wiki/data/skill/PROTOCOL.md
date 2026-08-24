# Lumio Wiki Coding-Agent Protocol

A short, agent- and vendor-neutral protocol for interacting with a portable
Lumio Knowledge Base. Every step uses the public `lumio-wiki` CLI (or the
equivalent public `lumio_wiki` Python surface). No web application, no
internal modules, no LanceDB, no OpenAI client required for base behavior.

This protocol invokes **only** public CLI/Python behavior. It never imports
internal application modules and it never parses the private MessagePack
Discovery Graph artifact — graph state is reached through `related`,
`paths`, and `health` alone.

## Roles

- **Host coding agent** — the agent running this protocol. Acts as the default
  **Distiller** for text and Markdown Knowledge Sources.
- **Maintainer** — the human reviewing Ingest Proposals before publication.

## Constants

- The Knowledge Base is a directory of compiled Markdown pages plus a root
  `lumio.yaml` Control File.
- A **Compiled Page** has YAML frontmatter (title, id, entity_types, aliases,
  tags, summary, lifecycle, visibility, sources, claims, synthetic) and a body.
- The **Canonical Page Title** is the unique title a page is known by.
- An accepted **Claim** is a reviewed, evidence-bearing semantic edge between
  Entities (validated against the `lumio.yaml` ontology); an **Extracted
  Reference** is a deterministic non-canonical reference derived from a body
  link (topology only, never Evidence).
- Reserved derived artifacts (`index.md`, `hot.md`, `log.md`) are marked and
  excluded from loading, retrieval, and fingerprinting.

## The retrieval ladder

Find context PROGRESSIVELY, cheapest-first, with no external index. Each step
builds on the last; stop as soon as you have citation-ready Evidence that
supports the question.

0. **Hot Index** — `lumio-wiki hot <kb>`. The Maintainer-pinned entry pages.
   Curated only; read it first, it names the pages the KB considers central.
1. **Navigation Indexes** — `lumio-wiki index <kb> [dir]`. The generated
   catalog of every Compiled Page grouped by directory. Cheap, exhaustive,
   deterministic.
2. **Deterministic search** — `lumio-wiki search <kb> "<query>"`. Zero-index
   lexical search over titles, aliases, tags, summaries, and bodies.
3. **Focused page read** — `lumio-wiki page <kb> "<title>"`. Read one page
   (frontmatter + body) to confirm it supports a claim before citing it.
4. **Related-page lookup** — `lumio-wiki related <kb> "<title>" ...`. Bounded
   neighbor expansion over accepted Claims (plus Extracted References
   with `--scope discovery`).
5. **Bounded paths** — `lumio-wiki paths <kb> "<source>" "<target>" ...`.
   Shortest directed path between two titles, hop-bounded.

Pass `--scope discovery` to traverse accepted Claims PLUS Extracted
References (deterministic body-link topology). Pass `--trace` to `related` or
`paths` for a truthful diagnostic line showing the exact scope, direction,
bounds, outcome, and graph artifact freshness actually used.

## 1. Project setup

```
lumio-wiki setup <path>
lumio-wiki setup <path> --publish-to <s3-uri> [--retrieval zero-index|lancedb] [--source-store <s3-uri>]
lumio-wiki setup --from <s3-uri> [--retrieval zero-index|lancedb] [--source-store <s3-uri>]
```

This is the canonical first-run workflow. It creates or detects the categorized
Knowledge Base, writes project `.env` with `LUMIO_KB_PATH`, and writes/updates
`AGENTS.md` with the portable retrieval and maintenance guardrails. S3 projects
configure through the same command: `--publish-to` records the publication
destination as `LUMIO_PUBLISH_TO` (Maintainer form only), `--retrieval` records
the retrieval backend as `LUMIO_RETRIEVAL_BACKEND` (backend and retrieval mode
are separate settings), and `--source-store` records the private Source
Artifact Store as `LUMIO_SOURCE_STORE`. `--from` configures a read-only
project whose pathless reads resolve the active S3 Published Version. Setup
writes no credentials and never installs optional capabilities; missing
`lumio-wiki[s3]` / `lumio-lancedb[s3]` yield one exact install command. Use
`lumio-wiki init <path>` only when the caller explicitly wants the lower-level
KB directory and seeded Control File without project bootstrap.

## 2. Validate

```
lumio-wiki validate <path>
```

Loads every page, the Control File, links, and reserved artifacts. Exit code
0 means valid; 1 means errors. Run before every publish and after every sync.

## 3. Hot Index and Navigation Indexes (ladder 0 + 1)

```
lumio-wiki hot <kb>
lumio-wiki index <kb> [dir]
```

`hot` renders the Maintainer-pinned Hot Index from the Control File (the
curated entry surface; prints a hint when no pins are configured). `index`
renders the generated Navigation Index — the root exhaustive catalog by
default, or a directory's shallow index. Both are generated in memory, so they
are always current without a prior publish. Read these BEFORE searching.

## 4. Search (ladder 2)

```
lumio-wiki search <kb> "<query>" [--limit N]
```

Deterministic zero-index lexical search over titles, aliases, tags,
summaries, and bodies. No external index. Returns ranked `PageSearchResult`
entries with path, score, matched fields, and a snippet.

## 5. Read a page (ladder 3)

```
lumio-wiki page <kb> "<title>"
```

Reads a Compiled Page by Canonical Page Title (falls back to alias). Prints
the full frontmatter and body. Use this to confirm a page supports an answer
claim — and to copy the exact supporting passage — before citing it.

## 6. Traverse the graph (ladder 4 + 5)

```
lumio-wiki related <kb> "<title>" \
  [--scope canonical|discovery] [--direction outgoing|incoming|both] \
  [--depth N] [--max-edges N] [--max-results N] [--trace]
lumio-wiki paths <kb> "<source>" "<target>" \
  [--scope canonical|discovery] [--direction outgoing|incoming|both] \
  [--max-depth N] [--max-edges N] [--trace]
```

`related` lists bounded Canonical Page Titles connected to a page.
`paths` returns the shortest directed path between two titles, bounded by
`--max-depth` hops. Use `--scope discovery` to include Extracted References
alongside accepted Claims, and `--trace` for a truthful diagnostic of
what the traversal actually used and found.

## 7. Ingest (host agent is the Distiller)

### Managed host-Distiller ingest (preferred — preserves raw-source lineage)

```
lumio-wiki ingest <kb> <original-source> --compiled-page <page.md> --source-id <id>
```

Binds the ORIGINAL raw Knowledge Source (PDF, DOCX, HTML, TXT, or Markdown)
and the host-agent-authored Compiled Page Markdown under ONE stable source
identity, staging a single reviewable proposal. The page MUST declare the
`--source-id` in `sources[].id`. The CLI registers the original bytes in the
private Source Registry under that identity (reusing the Source Version on an
identical retry; rejecting changed bytes until the Maintainer retires and
reactivates), records original filename/content type/converter/hash in
private provenance, and stages the authored page. The converter name is
derived from routing WITHOUT running it, so PDF/DOCX/HTML sources do NOT
require the `[documents]` extra — the host agent already authored the page.
Raw bytes never reach the published KB, Reader retrieval, Citations, exports,
or the canonical fingerprint (ADR-0014). `--compiled-page` and `--source-id`
are required together.

The private Source lifecycle has explicit commands (ADR-0014):
`lumio-wiki source <kb> list` reports identities/status without disclosing
raw bytes; `lumio-wiki source <kb> retire --source-id <id>` and `reactivate
--source-id <id> --file <bytes>` stage ordinary reviewable proposals (the
source stays active/retired until the proposal publishes).

### Plain text/Markdown passthrough (no separate original source)

```
lumio-wiki ingest <kb> <file> [--content-type T]
```

Reads a text or Markdown file, distills it through the model-free
`PassthroughMarkdownDistiller` (the host coding agent authors the Compiled
Page Markdown), and stages a reviewable Ingest Proposal. This does NOT
establish a private Source identity — use the managed mode whenever you have
the original bytes. Prints the proposal id, affected pages, blocked status,
and blast radius. For PDF/DOCX/image sources, install `lumio-wiki[documents]`;
for an unattended OpenAI-compatible Distiller, install `lumio-wiki[llm]`.

## 8. Review proposals

```
lumio-wiki proposal list <kb>
lumio-wiki proposal inspect <kb> <id> [--json]
lumio-wiki proposal validate <kb> <id>
```

`list` shows staged proposals. `inspect` prints metadata, blast radius, and
diff (or full JSON with `--json`). `validate` prints the validation report.

## 9. Publish or discard

```
lumio-wiki publish <kb> <id>
lumio-wiki discard <kb> <id>
```

`publish` applies the proposal's pages to the KB root, regenerates the
reserved Navigation Index and Hot Index, and marks the proposal terminal. It
refuses to publish a blocked proposal (validation always runs first).
`discard` marks a reviewable proposal as discarded.

## 10. Health and graph recovery

```
lumio-wiki health <kb> [--rebuild]
```

Reports page counts, validation status, Discovery Graph health (freshness,
edge count, materialization, fingerprint), and any validation errors or
warnings. A missing, stale, corrupt, or incompatible graph artifact NEVER
blocks zero-index operation — the graph is derived in memory. When the
artifact is not fresh, `health` prints a `graph_recovery:` hint; pass
`--rebuild` to materialize a fresh artifact (actionable recovery).

## 11. Diagnostics and explicit skill management

```
lumio-wiki doctor
lumio-wiki skill path
lumio-wiki skill protocol
lumio-wiki skill install --scope user
lumio-wiki skill status --scope user
lumio-wiki skill update --scope user
```

`doctor` prints the install shape (version, detected optional extras, packaged
skill location). `skill path` / `skill protocol` resolve the canonical bundle
inside the installed wheel. Shared user scope (`~/.agents/skills/lumio-wiki/`)
is preferred for cross-client reuse. Project scope is opt-in for a trusted
repository; review it as part of that repository's instruction surface.
Client-specific `--agent <pi|hermes|codex|claude-code>` targets remain explicit
compatibility fallbacks.

Every installed bundle carries a distribution version and content hash.
`status` is read-only and reports missing/current/stale/corrupt; `update`
performs an explicit atomic refresh from the current wheel. Package installation
and ordinary `setup` never install a skill implicitly. When installation is
requested through setup, use `--skill-scope user|project` or `--agent`. Restart
the agent or start a new session after install/update so discovery runs again.

## 12. Source Artifact inspection (authorized, not Evidence)

```
lumio-wiki source resolve <kb> "<query>" [--published-version <v>] [--json]
lumio-wiki source inspect <kb> --source-id <id> [--published-version <v>]
lumio-wiki source fetch <kb> --source-id <id> [--published-version <v>] --output <path>
lumio-wiki source link <kb> --source-id <id> [--published-version <v>] [--expires 5m]
```

Resolution is identity-oriented (ADR-0020): a local worktree resolves the
registry's current Source Version; an S3 Knowledge Base resolves the active
Published Version once (or the explicit `--published-version`) through its
private Source Binding Manifest. There is no fetch-by-hash and no object-key
interface, and an absent binding NEVER substitutes the latest Source Version.
Raw Source Artifacts are optional (retention is disabled by default) and
private — they never appear in the published Knowledge Base.

`inspect` reports secret-free metadata (safe filename, media type, size,
digest abbreviation, publication binding, verified availability,
authorization outcome). `fetch` writes the byte-exact original — digest and
size re-verified — to an explicit destination; a directory destination
receives the safe filename. `link` explicitly issues a short-lived signed GET
URL for one exact artifact when the store supports signing (5 min default,
1 h maximum); treat it as a bearer secret and never persist or log it.

Agent rules after a fetch: prefer verified `fetch` over `link` (signed URLs
leak through conversation history). Read bounded text directly from
text/CSV/JSON/XML artifacts or use sandboxed document tooling for
PDF/Office/image content. Quote exact original text only with source/version
and stable coordinates; decoded, OCR, or converted output must be labelled
DERIVED. Never execute active content (macros, scripts, active HTML) and
never dump a large private artifact wholesale into model context without an
explicit user request. Inspection supports provenance review; it is not
Evidence and does not promise claim-level passage highlighting.

Access denied, absent binding, unavailable artifact, corruption, and
historical-version mismatch are distinct actionable outcomes. Ordinary output
and errors never disclose credentials, private object keys, or secret-bearing
URLs; KB Reader-only credentials cannot inspect, fetch, or link Source
Artifacts.

## Guardrails

- **Cite paths and passages.** Every domain claim must cite the Canonical
  Page Title, the relative path the CLI prints, AND the supporting passage
  from the Compiled Page body. Read the page (ladder step 3) to copy the
  exact passage; do not cite from memory or from a search snippet alone.
- **Open citations with labelled actions.** `search` and `page` output label
  how to open each cited Compiled Page; reuse the labels verbatim in your
  answer so every citation is actionable (issue #177):
  - `open:` the copyable CLI action, `lumio-wiki page "<title>"`;
  - `web:` a browser Reading Room link — emitted ONLY when a valid
    `LUMIO_READER_BASE_URL` (http(s) origin) is configured; an
    S3/object-store URI is never a document URL;
  - `source-url:` the authored external `sources[].url` when the page
    declares one — visibly distinct from a Compiled Page link;
  - `source-artifact:` the EXPLICIT private-Source action
    (`lumio-wiki source inspect --source-id <id>`). No signed, public, or
    permanent Source Artifact URL is ever emitted implicitly; `source
    fetch`/`source link` run only on explicit request (ADR-0020).
- **Cite or refuse.** Domain claims require a citation to a Compiled Page
  (Canonical Title + path + passage). If the Knowledge Base does not support
  a claim, say "not covered by this knowledge base" — do not fabricate.
- **Connectivity is not support.** An Extracted Reference is topology that
  selects pages to inspect; it is never Evidence and cannot support a claim.
  Graph reachability cannot manufacture support — report "not covered" when
  the selected Evidence is insufficient.
- **Proposal-first.** Validation always runs before publish. Never write
  pages directly to the KB root; always go through `ingest` → `publish`.
- **Source isolation.** Raw Knowledge Sources never appear in the published
  Knowledge Base. The ingest store lives under `<kb>/.lumio/ingest/` and
  stores proposal JSON (not authored Markdown).
- **Public surface only.** This protocol invokes the public CLI and public
  Python surface only. It does not import internal application modules, parse
  the private MessagePack graph, or start the web application.
