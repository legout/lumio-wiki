# Compiled Page Format

The canonical reference for a Lumio **Compiled Page**: the Markdown + YAML
frontmatter shape the Core SDK loads, validates, indexes, and retrieves from.

This document mirrors the validation rules enforced in
`packages/lumio-wiki/src/lumio_wiki/knowledge_base.py` and the record types in
`packages/lumio-wiki/src/lumio_wiki/records.py`. When they disagree, **the code
is correct** — update this page to match.
For the *language* behind these terms (what a
Compiled Page, Source, or Claim *means*), see
[`CONTEXT.md`](../CONTEXT.md) and
[ADR-0021](adr/0021-entity-claim-ontology-and-progressive-graph-materialization.md).

## File shape

Every Compiled Page is one `.md` file with a YAML frontmatter block delimited
by `---`, followed by a Markdown body. In a categorized Knowledge Base
(Control File v2) every page declares **exactly one stable Entity** and owns
its subject **Claims**:

```markdown
---
id: "entity:lumio"
title: "Lumio"
entity_types:
  - software-system
aliases:
  - "Lumio Knowledge Platform"
tags:
  - "product"
summary: "Deployable chat for trusted knowledge and data."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "lumio-product"
    title: "Lumio product overview"
claims:
  - id: "claim:lumio-uses-lancedb"
    predicate: "uses"
    object: "entity:lancedb"
    status: "accepted"
    evidence:
      - section: "Retrieval"
  - id: "claim:lumio-first-released"
    predicate: "first-released"
    value: 2025
    value_type: number
    status: "accepted"
    evidence:
      - lines: [3, 4]
---

# Lumio

Lumio is a deployable chat platform for trusted knowledge and data.

## Retrieval

Lumio uses LanceDB for optional enhanced retrieval.
```

A Knowledge Base is a directory tree of such files. Lumio loads every `.md`
file under the root path and validates them together.

The **Entity ID** (`id`) is the semantic identity: stable, Knowledge-Base-wide,
and independent of the page's title and path. A title rename changes a label,
never identity. The **Canonical Page Title** remains the human-readable lookup
surface (with `aliases`); traversal results are reported as titles.

## Frontmatter fields

| Field | Required | Type | Rules |
|---|---|---|---|
| `id` | yes (v2) | string | Stable Entity ID (conventionally `entity:<slug>`). Unique across the Knowledge Base. Titles and paths are not identity. |
| `entity_types` | yes (v2) | list\[string\] | Non-empty; every type must be declared in the Control File's `ontology.entity_types`. |
| `title` | yes | string | Non-empty. The **Canonical Page Title**; unique across the Knowledge Base. |
| `tags` | yes | list\[string\] | Non-empty. Controlled organization labels. |
| `lifecycle` | yes | string | One of `draft`, `review`, `approved`, `deprecated`. |
| `visibility` | yes | string | One of `public`, `internal`, `restricted`. |
| `review_after` | no | date (ISO 8601) | Optional review schedule (ADR-0023). Due when `today >= review_after`; surfaced as advisory warnings in `validate`/`lint`/`dream`/`status`, never blocking. Absent means no freshness opinion. |
| `aliases` | no | list\[string\] | Alternate lookup phrases. Each must be **unique across the Knowledge Base**. |
| `summary` | no | string | Human-readable one-line summary. **Recommended for new pages** — cheap retrieval depends on it. |
| `sources` | conditional | list\[mapping\] | **Required unless `synthetic: true`.** A non-synthetic page must declare at least one. |
| `claims` | no | list\[mapping\] | The subject Claims this page owns (see below). |
| `synthetic` | no | boolean | Default `false`. Declares the page is generated from other compiled knowledge; a synthetic page may omit `sources`. |

### `sources` entries

| Key | Required | Type | Notes |
|---|---|---|---|
| `id` | no | string | Stable provenance identifier. |
| `title` | no | string | Human-readable provenance title. |
| `url` | no | string | Optional URL. |

### `claims` entries

A **Claim** is a stable, reviewed proposition owned by its subject Entity's
page. The page's Entity ID is the subject; the Claim carries **exactly one
object**: another Entity ID (`object`) **or** a typed literal (`value` plus
`value_type`). Both, or neither, is a validation error.

| Key | Required | Type | Notes |
|---|---|---|---|
| `id` | yes | string | Stable Claim ID (conventionally `claim:<slug>`). Unique across the Knowledge Base. |
| `predicate` | yes | string | A Predicate ID declared in the Control File's `ontology.predicates`. |
| `object` | conditional | string | The object Entity ID. Must reference a live page's Entity. Forbidden when the Predicate declares `literal_kind`. |
| `value` + `value_type` | conditional | scalar + string | A typed literal object. `value_type` must equal the Predicate's declared `literal_kind` (`string`, `number`, or `boolean`) and match the value's kind. |
| `status` | yes | string | One of `accepted`, `disputed`, `superseded`. `proposed`/`rejected` exist only inside Ingest Proposals and are blocked on active pages. |
| `confidence` | no | float | Optional reviewer confidence. |
| `origin` | no | string | `authored` (default) or `migrated`. Model-assisted origins remain future proposal-only work. |
| `valid_from` / `valid_to` | no | string | Optional ISO 8601 valid-time bounds. |
| `evidence` | yes | list\[mapping\] | At least one **Claim Evidence** anchor into this page's published content (see below). |

### Claim Evidence anchors

An anchor identifies a supporting section heading, a bounded 1-based line
range within the page body, or both:

```yaml
evidence:
  - section: "Retrieval"
  - lines: [3, 4]
```

A Claim and its graph edge are **not Evidence by themselves** — answers cite
page Evidence. Evidence anchors never expose private Source Artifact
coordinates (ADR-0014, ADR-0021).

## Validation rules (the contract)

Lumio collects every issue across the whole Knowledge Base rather than failing
on the first one. A Knowledge Base is **valid** when the report contains no
error-severity issues.

Per page:

1. `title` is present and non-empty.
2. `id` is present (v2) and `entity_types` is a non-empty list of declared types.
3. `lifecycle` and `visibility` are present and allowed.
4. `tags` is present, a list, and non-empty.
5. If the page is **not** synthetic, `sources` is present with at least one entry.
6. `claims` entries are mappings with non-empty `id` and `predicate`.

Across pages:

1. **Entity IDs are unique** — no two pages may declare the same `id`.
2. **Canonical titles are unique**; **aliases are unique** across the Knowledge Base.
3. **Claim IDs are unique** across the Knowledge Base.
4. **Claim objects resolve** — an `object` must equal a live page's Entity ID
    (a *dangling object entity* is a blocking error).

Ontology conformance (validated against the Control File):

1. **Unknown Entity Types and Predicates are blocked** — every used type and
    predicate must be declared.
2. **Domain/range** — a Claim's subject page must carry one of the
    Predicate's `subject_types`; an entity object must carry one of its
    `object_types` (violations are blocking errors).
3. **Literal kinds** — a `literal_kind` Predicate accepts only `value`+
    `value_type` objects of that kind; an entity-object Predicate accepts only
    `object` objects.
4. **Claim Evidence is present and anchored** — every Claim has at least one
    anchor; a `section` must exist in the page body; `lines` must be a bounded
    2-list inside the body.
5. **Published lifecycle only** — `accepted`, `disputed`, or `superseded` on
    active pages.
6. **Redirects** — every `ontology.redirects` source must be a retired
    (page-less) Entity ID, its chain must terminate at a live Entity, and no
    cycle is allowed (merge-cycle failures block publication).

Only `accepted` entity-to-entity Claims enter the canonical traversal
projection. Disputed and superseded Claims stay inspectable but never select
supporting context. Extracted References (deterministic internal body links)
remain non-canonical Discovery Graph edges and never become Claims.

## Knowledge Base Control File (v2)

A categorized Knowledge Base carries a versioned root **Control File** named
`lumio.yaml`. It is canonical Knowledge Base content — it travels with the KB,
is validated and fingerprinted, and declares the KB-local content controls
**including the ontology**:

```yaml
version: 2
mode: "categorized"
categories:
  - name: concepts
    description: "Core ideas, definitions, and mental models."
  - name: entities
hot_index:
  - title: "Lumio"
ontology:
  entity_types:
    software-system:
      description: "A deployed software product or platform."
    library: {}
    concept: {}
  predicates:
    uses:
      subject_types: [software-system]
      object_types: [library, software-system]
      inverse: used-by
      synonyms: [depends-on]
    described-as:
      literal_kind: string
  redirects:
    entity:legacy-wiki: entity:sage-wiki
```

| Field | Required | Type | Rules |
|---|---|---|---|
| `version` | yes | integer | Currently `2`. Unsupported versions are a blocking error (v1 is unsupported — no compatibility loader exists). |
| `mode` | no | string | `categorized` (default). |
| `categories` | no | list\[mapping\|string\] | The complete Content Category catalog (see ADR-0009). Absent/empty applies the seeded default. |
| `hot_index` | no | list\[mapping\|string\] | Pinned titles; every `title` must resolve to an existing Canonical Page Title. |
| `ontology` | no | mapping | The controlled vocabulary (below). Present in every v2 Knowledge Base that models Claims. |

### `ontology.entity_types`

A mapping of Entity Type ID to an optional definition (`description`). Pages
declare these IDs in `entity_types`. There is no type inheritance.

A freshly initialized Knowledge Base (`lumio-wiki setup`/`init`) seeds a
**valid-empty ontology**: the version-2 Control File is present with the
category catalog, but `entity_types` and `predicates` are empty. That state is
fully valid and sufficient **until the first page declares `entity_types` or
`claims`** — from that moment validation requires every used type and
predicate to be declared, because the vocabulary is KB-local content a
Maintainer declares deliberately. A tested starter ontology (entity types +
an inverse predicate pair + a literal predicate) lives in
[`docs/quickstart.md`](quickstart.md) (issue #180).

### `ontology.predicates`

A mapping of Predicate ID to a definition:

| Key | Type | Notes |
|---|---|---|
| `subject_types` | list\[string\] | Entity Types the subject page may carry. Empty/absent = unconstrained. |
| `object_types` | list\[string\] | Entity Types an entity object may carry. Empty/absent = unconstrained. Mutually exclusive with `literal_kind`. |
| `literal_kind` | string | `string`, `number`, or `boolean` — the Predicate accepts typed-literal objects instead of entities. |
| `inverse` | string | Optional inverse Predicate ID; **must itself be a declared predicate**. Both directions are authored as ordinary Claims. |
| `synonyms` | list\[string\] | Alternate labels for retrieval. |
| `description` | string | Optional. |

The format does **not** support type inheritance, OWL/RDF reasoning,
namespaces, cardinality rules, or automatic ontology induction.

### `ontology.redirects`

Retired Entity IDs from reviewed **Entity Merges**, mapping each retired ID to
its surviving Entity (`from_id -> to_id`). A redirect keeps old references
resolvable (`lumio-wiki entity entity:legacy-wiki` resolves through the chain
to the survivor) while validation guarantees acyclicity and that only retired
IDs redirect. Merges are proposal-first: a merge proposal retargets Claims and
exactly resolved links, records the redirect, and discloses the blast radius
before publication.

### Extensible Content Category catalog (ADR-0009)

The Content Category catalog remains **extensible per Knowledge Base** beyond
the seeded defaults (`concepts`, `entities`, `references`, `procedures`,
`tables`, `datasets`, `synthesis`). A Compiled Page's category is the first
path segment and must be declared. Category is navigation routing only —
semantic typing lives in `entity_types`.

### Legacy Flat Mode

A Knowledge Base with **no Control File** loads in **Legacy Flat Mode**: the
Entity contract (`id`, `entity_types`, `claims`) is optional and no ontology
validation runs. Legacy Flat Mode publishes Navigation Indexes only. There is
no v1→v2 migration command — existing Knowledge Bases update their pages
directly (ADR-0021).

## Known ceilings (deliberate)

ADR-0021 accepts two ceilings explicitly:

1. **One Entity per page.** An Entity important enough to enter the reviewed
   Knowledge Graph has exactly one Compiled Page; there are no hidden entity
   manifests. Revisit only when meaningful entities repeatedly fail to justify
   a page.
2. **In-memory traversal.** Traversal loads the active version's accepted-edge
   adjacency into an in-memory `GraphState` (zero-index MessagePack cache or
   LanceDB projection) and never issues one storage query per hop. Revisit
   only when measured scale exceeds the design.

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
containing Compiled Pages. Each page entry carries the Canonical Page Title, a
portable relative Markdown link, and summary. Generated indexes are
deterministic (byte-identical for unchanged content), declare format version
1, and omit timestamps, tags, provenance, claims, and exchange-only metadata.

The Core SDK excludes valid marked Navigation Indexes from Compiled Page
loading, validation, retrieval, Evidence, and the canonical content
fingerprint. An `index.md` (or `INDEX.md`) without the valid marker is a
blocking validation error. Direct edits to a marked index are overwritten on
the next publish. See
[ADR-0007](adr/0007-navigation-indexes-and-okf-exchange-profile.md).

## Checking a Knowledge Base

```bash
uv run lumio-wiki validate path/to/kb
```

Exit code `0` means valid; `1` means one or more issues were found, each
reported as `file: field: message`.

## Reserved published artifacts

A Published Version carries marked reserved derived artifacts alongside
Compiled Pages. Each reserved basename is case-insensitive and must carry its
matching `lumio` marker; an unmarked or malformed collision is a blocking
validation error. Valid marked artifacts are excluded from Compiled Page
loading, validation, retrieval, Evidence, Claims, and the canonical content
fingerprint. Direct edits to a regenerated artifact are overwritten on the
next publish; the Activity Log is never overwritten.

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
exactly the Compiled Pages whose Canonical Page Titles the Control File pins,
in pin order, and is regenerated on every publish/sync. It exists only in a
categorized Knowledge Base with at least one pin; when the last pin is removed,
publication prunes it. Hot Index ranking is Maintainer-curated only (ADR-0008).

**Activity Log** (`log.md`) is the append-only portable history of successful
published Knowledge Base state transitions. Each entry is one grep-friendly
line:

```
2026-07-16T17:13:25Z publish: Published pages: concepts/overview.md
```

Publication appends exactly one entry **after** the Knowledge Base state is
successfully published. It **never** records Reader queries, failed or
discarded proposals, unpublished uploads, or private audit events. The OKF
Exchange Profile does not generate `log.md` on export (ADR-0007, ADR-0008).
