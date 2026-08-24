<!-- lumio-wiki-kb -->
## Lumio Knowledge Base

This project uses a Lumio Knowledge Base for domain knowledge. The host coding
agent IS the default Distiller (no model provider needed for base ingestion).

**KB path:** `/home/volker/coding/lumio/examples/real-world-lumio-wiki/knowledge-base` (also in `.env` as `LUMIO_KB_PATH`; the CLI reads it
automatically when no `<kb>` argument is given).

### Retrieval ladder (cheapest-first, stop when you have Evidence)

0. `lumio-wiki hot` — Maintainer-pinned entry pages. Read first.
1. `lumio-wiki index [dir]` — generated Navigation Index (all pages by directory).
2. `lumio-wiki search "<query>"` — zero-index lexical search (no external index).
3. `lumio-wiki page "<title>"` — read a page to confirm and cite the exact passage.
4. `lumio-wiki related "<title>" --scope discovery` — related pages (canonical + extracted).
5. `lumio-wiki paths "<src>" "<dst>"` — shortest directed path between two titles.

### Ingest (you are the Distiller)

1. Author a Compiled Page (YAML frontmatter + Markdown body) that declares the
   source identity in `sources[].id`.
2. `lumio-wiki ingest <original-source> --compiled-page <page.md> --source-id <id>`
   — bind the ORIGINAL raw source to your authored page under one stable
   identity and stage a single reviewable Ingest Proposal. No `[documents]`
   extra required (the converter name is derived from routing without running
   it). Plain `lumio-wiki ingest <file>` stays available for text/Markdown
   passthrough but does NOT establish a Source identity.
3. `lumio-wiki proposal list` → `proposal inspect <id>` → `proposal validate <id>`.
4. `lumio-wiki publish <id>` (or `lumio-wiki discard <id>`).

### Maintenance (you are the Maintainer)

- `lumio-wiki lint` — read-only cross-page QA: validation, graph health, and
  canonical/discovery structural diagnostics with scope disclosure. Exit 1 if invalid.
- `lumio-wiki cross-link` — missing-link candidates ranked by Discovery Graph
  impact. `--stage` repairs candidates as authored Markdown links (Extracted
  References, Discovery Graph only); it never creates typed Claims.
- Claim authoring — canonical edges are accepted, evidence-bearing Claims
  authored directly in Compiled Page `claims:` frontmatter (predicate +
  entity object or typed literal, validated against the `lumio.yaml`
  ontology). Entity Merge is proposal-first: `lumio-wiki entity merge`
  stages the reviewed merge with its full blast radius.
- `lumio-wiki dream` — the Dream Cycle: read-only reflection (validation +
  health + structure + ranked candidates). Add `--stage [--limit N]` to stage
  the top repairs as ordinary Ingest Proposals for review. Add opt-in `--semantic`
  with the `[llm]` extra for semantic findings; it remains proposal-first.

### Guardrails

- **Cite or refuse.** Every domain claim cites a Compiled Page (title + path +
  passage). Unsupported claims return "not covered by this knowledge base."
- **Connectivity is not support.** Graph reachability selects pages to inspect;
  it never manufactures Evidence.
- **Proposal-first.** Validation always runs before publish. Never write `.md`
  files directly to the KB root.

### Diagnostics

- `lumio-wiki doctor` — version, detected extras, skill location.
- `lumio-wiki health` — page counts, validation, Discovery Graph health.
- `lumio-wiki validate` — exit 0 if valid, 1 otherwise.
- `lumio-wiki lint` — full QA report (superset of validate + structural diagnostics).
