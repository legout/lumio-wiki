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
| `summary` | no | string | Human-readable one-line summary. |
| `sources` | conditional | list\[mapping\] | **Required unless `synthetic: true`.** Each source is a mapping (see below). A non-synthetic page must declare at least one. |
| `relationships` | no | list\[mapping\] | Typed, directed edges to other pages (see below). Each `target` must resolve to an existing canonical title. |
| `synthetic` | no | boolean | Default `false`. Declares the page is generated from other compiled knowledge; a synthetic page may omit `sources`. |

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
| `type` | no | string | Edge type (e.g. `relates-to`). Free-form string. |

## Validation rules (the contract)

Lumio collects every issue across the whole Knowledge Base rather than failing
on the first one. A Knowledge Base is **valid** only when the report is empty.

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

## Checking a Knowledge Base

```bash
uv run lumio validate path/to/kb
```

Exit code `0` means valid; `1` means one or more issues were found, each
reported as `file: field: message`.
