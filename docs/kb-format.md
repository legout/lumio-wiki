# Compiled Page Format

The canonical reference for a Lumio **Compiled Page**: the Markdown + YAML
frontmatter shape the Core SDK loads, validates, indexes, and retrieves from.

This document mirrors the validation rules enforced in
`src/lumio/core/knowledge_base.py` and the record types in
`src/lumio/core/records.py`. When they disagree, **the code is correct** —
update this page to match. For the *language* behind these terms (what a
Compiled Page, Source, or Relationship *means*), see
[`CONTEXT.md`](../CONTEXT.md).

## File shape

Every Compiled Page is one `.md` file with a YAML frontmatter block delimited
by `---`, followed by a Markdown body:

```markdown
---
title: "Architecture"
aliases:
  - "System Architecture"
tags:
  - "architecture"
  - "system"
summary: "How Lumio is structured internally."
lifecycle: "approved"
visibility: "internal"
sources:
  - id: "architecture-doc"
    title: "Architecture decision records"
relationships:
  - target: "Lumio Overview"
    type: "relates-to"
synthetic: false
---

# Architecture

Lumio is built as a modular monolith with a framework-independent Core SDK.
```

A Knowledge Base is a directory tree of such files. Lumio loads every `.md`
file under the root path and validates them together.

## Frontmatter fields

| Field | Required | Type | Rules |
|---|---|---|---|
| `title` | yes | string | Non-empty. The **Canonical Page Title**; unique across the Knowledge Base. Relationship targets refer to this. |
| `tags` | yes | list\[string\] | Non-empty. Controlled organization labels. |
| `lifecycle` | yes | string | One of `draft`, `review`, `approved`, `deprecated`. |
| `visibility` | yes | string | One of `public`, `internal`, `restricted`. |
| `aliases` | no | list\[string\] | Alternate lookup phrases. Each must be **unique across the Knowledge Base**. |
| `summary` | no | string | Human-readable one-line summary. **Recommended for new pages** — cheap retrieval depends on it. |
| `sources` | conditional | list\[mapping\] | **Required unless `synthetic: true`.** Each source is a mapping (see below). A non-synthetic page must declare at least one. |
| `relationships` | no | list\[mapping\] | Typed, directed edges to other pages (see below). Each `target` must resolve to an existing canonical title. |
| `synthetic` | no | boolean | Default `false`. Declares the page is generated from other compiled knowledge; a synthetic page may omit `sources`. Synthetic pages promoted from cited answers include a `retrieval_trace` key recording the Retrieval Trace stages that produced the original answer. |

### `sources` entries

| Key | Required | Type | Notes |
|---|---|---|---|
| `id` | no | string | Stable provenance identifier. |
| `title` | no | string | Human-readable provenance title. |
| `url` | no | string | Optional URL. |

### `relationships` entries

| Key | Required | Type | Notes |
|---|---|---|---|
| `target` | no | string | Canonical title of the target page. **Must resolve** to an existing page title, or validation fails. |
| `type` | no | string | Edge type. The preferred vocabulary is: `relates-to`, `uses`, `extends`, `implements`, `contradicts`, `derived-from`, `replaces`. Unknown types still load for backward compatibility, but they produce a validation warning suggesting the preferred vocabulary. |

## Validation rules (the contract)

Lumio collects every issue across the whole Knowledge Base rather than failing
on the first one. A Knowledge Base is **valid** when the report contains no
error-severity issues. Warnings (for example, an unknown relationship type) do
not block loading, indexing, or publishing.

Per page:

1. `title` is present and non-empty.
2. `lifecycle` is present and one of the allowed values.
3. `visibility` is present and one of the allowed values.
4. `tags` is present, a list, and non-empty.
5. If `aliases` is present, it is a list.
6. If the page is **not** synthetic, `sources` is present with at least one
   entry (and each entry is a mapping).
7. If `relationships` is present, it is a list of mappings.

Across pages:

8. **Canonical titles are unique** — no two pages may share a `title`.
9. **Aliases are unique** — no alias may repeat across the Knowledge Base.
10. **Relationship targets resolve** — every `relationships[].target` must
    equal an existing page's canonical title.

## Navigation Indexes (reserved derived artifacts)

The case-insensitive basename `index.md` is reserved in every Knowledge Base
directory for a **Navigation Index**: a derived guide to the Compiled Pages
beneath that directory. It is **not** a Compiled Page.

A Navigation Index declares its reserved role with a `lumio` marker:

```yaml
---
lumio:
  artifact: navigation-index
  version: 1
---
```

Publishing regenerates one root `index.md` — an exhaustive catalog of every
Compiled Page grouped by directory — and one `index.md` per directory
containing Compiled Pages, listing immediate child directories followed by
immediate pages. Each page entry carries the Canonical Page Title, a portable
relative Markdown link, and summary. Generated indexes are deterministic
(byte-identical for unchanged content), declare format version 1, and omit
timestamps, tags, provenance, relationships, and exchange-only metadata.

The Core SDK excludes valid marked Navigation Indexes from Compiled Page
loading, validation, retrieval, Evidence, and the canonical content
fingerprint. An `index.md` (or `INDEX.md`) without the valid marker is a
blocking validation error — authored Markdown cannot claim the reserved
basename without occupying the derived-artifact role. Direct edits to a marked
index are overwritten on the next publish. See
[ADR-0007](adr/0007-navigation-indexes-and-okf-exchange-profile.md).

## Checking a Knowledge Base

```bash
uv run lumio validate path/to/kb
```

Exit code `0` means valid; `1` means one or more issues were found, each
reported as `file: field: message`.

## Knowledge Base Control File (categorized Knowledge Bases)

A categorized Knowledge Base carries a versioned root **Control File** named
`lumio.yaml`. It is canonical Knowledge Base content — it travels with the KB
in Git or shared storage, is validated and fingerprinted by the Core SDK, and is
**neither a Compiled Page nor an OKF concept**. It declares the KB-local
content controls:

```yaml
version: 1
mode: "categorized"
categories:
  - name: concepts
    description: "Core ideas, definitions, and mental models."
  - name: entities
hot_index:
  - title: "Lumio Overview"
  - title: "Acme Corp"
    note: "Pinned for visibility"
```

| Field | Required | Type | Rules |
|---|---|---|---|
| `version` | yes | integer | Currently `1`. Unsupported versions are a blocking error. |
| `mode` | no | string | `categorized` (default). |
| `categories` | yes | list\[mapping\|string\] | Non-empty. Each entry is a mapping with a non-empty `name` (and optional `description`) or a bare name string. Names must be unique. The seeded catalog is `concepts`, `entities`, `references`, `procedures`, `tables`, `datasets`, `synthesis`. |
| `hot_index` | no | list\[mapping\|string\] | Each entry is a mapping with a non-empty `title` (and optional `note`) or a bare title string. Every pinned `title` must resolve to an existing Canonical Page Title, or validation fails (analogous to Relationship targets). |

Category is broad navigation routing; a page's free-form `type` remains specific
semantics and no Lumio-wide type taxonomy is introduced. Establishing or
migrating the Control File is an explicit, reviewed Maintainer action — never an
automatic upgrade.

A Knowledge Base with **no Control File** loads in **Legacy Flat Mode**: existing
root-level Compiled Pages remain valid, and validation emits a single
non-blocking migration warning. Legacy Flat Mode publishes Navigation Indexes
only; it does not publish a Hot Index or Activity Log until a reviewed migration
establishes the Control File. See
[ADR-0008](adr/0008-knowledge-base-control-file-and-published-artifacts.md).

## Reserved published artifacts

A Published Version carries marked reserved derived artifacts alongside Compiled
Pages. Each reserved basename is case-insensitive and must carry its matching
`lumio` marker; an unmarked or malformed collision is a blocking validation
error — authored Markdown cannot claim a reserved basename without occupying the
derived-artifact role. Valid marked artifacts are excluded from Compiled Page
loading, validation, retrieval, Evidence, relationships, and the canonical
content fingerprint. Direct edits to a regenerated artifact are overwritten on
the next publish; the Activity Log is never overwritten.

| Basename | Marker | Kind | Generated by |
|---|---|---|---|
| `index.md` | `artifact: navigation-index` | Navigation Index | Regenerated wholesale (root + per directory with Compiled Pages) |
| `hot.md` | `artifact: hot-index` | Hot Index | Regenerated wholesale from the Control File's pinned titles |
| `log.md` | `artifact: activity-log` | Activity Log | Append-only; one entry per successful publish |

```yaml
---
lumio:
  artifact: navigation-index   # or hot-index, activity-log
  version: 1
---
```

**Navigation Indexes** (`index.md`) were introduced in ADR-0007: an exhaustive
Karpathy-style root catalog plus shallow OKF-style per-directory guides,
deterministic (byte-identical for unchanged content), listing titles, portable
relative links, and summaries.

**Hot Index** (`hot.md`) is the Maintainer-pinned navigation surface. It lists
exactly the Compiled Pages whose Canonical Page Titles the Control File pins, in
pin order, and is regenerated on every publish/sync. It exists only in a
categorized Knowledge Base with at least one pin; when the last pin is removed,
publication prunes it. Hot Index ranking is Maintainer-curated only — citation
or access frequency ranking is deferred (ADR-0008).

**Activity Log** (`log.md`) is the append-only portable history of successful
published Knowledge Base state transitions. Each entry is one grep-friendly
line:

```
2026-07-16T17:13:25Z publish: Published pages: concepts/overview.md
```

Publication appends exactly one entry **after** the Knowledge Base state is
successfully published. It **never** records Reader queries, failed or discarded
proposals, unpublished uploads, or private audit events — those remain in
private operational stores. It is distinct from both the SQLite audit log and
from OKF's optional, preview-only `log.md` exchange history. The OKF Exchange
Profile does not generate `log.md` on export (ADR-0007, ADR-0008).
