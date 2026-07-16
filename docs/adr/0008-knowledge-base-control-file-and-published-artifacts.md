---
status: accepted
---

# ADR-0008: Knowledge Base Control File and Portable Published Artifacts

## Context

Lumio's Knowledge Base is a portable, compiled Markdown tree. ADR-0007
established the Navigation Index as a native reserved derived artifact with an
explicit, versioned `lumio` marker, and adopted the OKF Exchange Profile as a
best-effort interchange boundary. As the Knowledge Base grows beyond one page
per source, three capabilities were missing and are hard to reverse once
shipped in a portable format:

1. A KB-local declaration of the allowed **Content Categories** and the
   **Hot Index** titles a Maintainer curates — portable with the KB, but neither
   a Compiled Page (no body, no provenance, no retrieval) nor an application-only
   setting (it must not depend on the web app to be interpreted).
2. A portable **Activity Log** of successful published Knowledge Base state
   transitions, distinct from the private SQLite audit log and from OKF's
   optional, preview-only `log.md` exchange history.
3. A second regenerated derived artifact — the **Hot Index** — that must be
   excluded from loading, retrieval, and fingerprinting exactly as Navigation
   Indexes are.

Reserving new paths and markers inside every Knowledge Base, and establishing a
portable control file, are difficult to reverse: once published trees carry
these contracts, deployments and local coding agents depend on them.

## Decision

Adopt a **Knowledge Base Control File** at the KB root and extend Lumio's
reserved-artifact marker system with two more marked derived artifacts.

### Knowledge Base Control File (`lumio.yaml`)

A versioned root YAML file declares the KB-local content controls:

- `version` (currently `1`) — the control-file format version.
- `mode` (`categorized`) — distinguishes a categorized Knowledge Base from
  Legacy Flat Mode.
- `categories` — the controlled Content Category catalog. The seeded catalog is
  `concepts`, `entities`, `references`, `procedures`, `tables`, `datasets`, and
  `synthesis`. Category is broad navigation routing; a page's free-form `type`
  remains specific semantics, and no Lumio-wide type taxonomy is introduced.
- `hot_index` — Maintainer-pinned Canonical Page Titles that the Hot Index
  renders.

The Control File is canonical Knowledge Base content: it travels with the KB, is
validated and fingerprinted by the Core SDK, and is neither a Compiled Page nor
an OKF concept. Establishing or migrating it is an explicit, reviewed Maintainer
action — never an automatic upgrade.

### Reserved derived artifacts

Lumio's explicit, versioned `lumio` marker system (ADR-0007) is extended with
two additional reserved artifacts. Each reserved basename is case-insensitive
and requires its matching marker; an unmarked or malformed collision is a
blocking validation error.

| Basename | Marker artifact | Generation |
|---|---|---|
| `index.md` | `navigation-index` | Regenerated wholesale (root + per-directory) |
| `hot.md` | `hot-index` | Regenerated wholesale from Control File pins |
| `log.md` | `activity-log` | Append-only; never regenerated or pruned |

Valid marked artifacts of all three kinds are excluded from Compiled Page
loading, retrieval, Evidence, relationships, and canonical content
fingerprinting — there is no escape hatch from fingerprinting without occupying
the valid reserved artifact role.

### Activity Log contract

The portable Activity Log (`log.md`) records **only successful published
Knowledge Base state transitions**. Publication appends exactly one
grep-friendly dated line (ISO-8601 UTC timestamp, operation token, then a
description) **after** the Knowledge Base state is successfully published.
Because the entry is staged on the publish candidate before the atomic
storage transaction, it persists if and only if the publish commits: a failed
or rolled-back publish leaves the prior history intact and never logs a
transition that did not happen.

The Activity Log **never** carries Reader queries, failed or discarded proposals,
unpublished uploads, or private audit events. Those remain in private
operational stores (SQLite audit events, Git/storage history). This is an
intentional Lumio OKF extension: a marked, append-only portable record,
consistent with ADR-0007's stance rather than strict frontmatter-free OKF
reserved-file syntax. The OKF Exchange Profile (ADR-0007) remains unchanged and
does not generate `log.md` on export.

### Legacy Flat Mode

A Knowledge Base with no Control File loads in **Legacy Flat Mode**: existing
root-level Compiled Pages remain valid, and validation emits a single
non-blocking migration warning. Legacy Flat Mode publishes Navigation Indexes
only; it does not publish a Hot Index or Activity Log until a reviewed
migration establishes the Control File. Migration is always an explicit,
reviewable proposal.

## Considered Options

- **Store the category catalog and Hot Index pins as application settings** —
  rejected: deployments and local coding agents would interpret the same KB
  differently, and the controls would not travel with the KB in Git or shared
  storage.
- **Encode categories and pins as Compiled Page frontmatter or an OKF
  extension** — rejected: the catalog is KB-level navigation control, not page
  content, and OKF vocabulary must not redefine Lumio's domain model
  (ADR-0007).
- **Regenerate `log.md` wholesale like the other artifacts** — rejected: the
  Activity Log is portable history; regenerating it would erase the recorded
  evolution and would have to invent past transitions.
- **Reuse OKF's optional, unmarked `log.md` as the Activity Log** — rejected:
  OKF `log.md` is preview-only exchange history; Lumio's Activity Log is an
  authoritative, marked, append-only record with stricter semantics.
- **Make the Control File Markdown so it shares the Compiled Page loader** —
  rejected: the Control File is not a Compiled Page and must not be parsed,
  retrieved, or cited as one; a separate YAML file keeps the contracts
  disjoint.
- **Gate the Activity Log and Hot Index behind a separate per-KB flag rather
  than the Control File** — rejected: the Control File is the single, portable
  declaration of the categorized-KB contract; deriving artifact behavior from
  it keeps one source of truth.

## Consequences

Publishing and syncing must regenerate Navigation Indexes and the Hot Index
atomically, while loading and fingerprinting must recognize and exclude all
three valid marked artifact kinds. The Activity Log is append-only and must be
written exactly once per successful publish, never by retrieval, ingestion
staging, discard, or Reader activity. Import/export and the OKF Exchange
Profile are unaffected: Profile 1 still does not generate `log.md`, and
generic import still treats unmarked reserved files as blocking collisions.

The cost is a mirroring discipline: `docs/kb-format.md` must record the Control
File, reserved-artifact, Hot Index, and Activity Log contracts alongside the
Compiled Page schema, mirroring `src/lumio/core/knowledge_base.py`. Legacy
Knowledge Bases gain a non-blocking warning until migrated; the migration
itself is deferred to an explicit, reviewed proposal.
