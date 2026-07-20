---
status: accepted
---

# ADR-0009: Extensible Knowledge Base Category Catalog

## Context

ADR-0008 established the Knowledge Base Control File (`lumio.yaml`) and seeded a
fixed Content Category catalog — `concepts`, `entities`, `references`,
`procedures`, `tables`, `datasets`, and `synthesis`. ADR-0008 is explicit that
"Category is broad navigation routing; a page's free-form `type` remains
specific semantics, and no Lumio-wide type taxonomy is introduced," and that
establishing or migrating the Control File is "an explicit, reviewed Maintainer
action — never an automatic upgrade." The fixed seed was deliberate: it keeps
the category axis predictable and resists taxonomy sprawl.

A forthcoming capability — one-way import of external compiled-Markdown
Knowledge Bases into Lumio's canonical format (a deferred, not-yet-filed PRD; obsidian-wiki is
the first intended validation target, not a special case) — presses on that fixed seed.
External vaults carry category structures Lumio does not seed (for example
`projects/`, `journal/`, `skills/`). ADR-0007 already permits lossy
canonicalization of imported metadata as long as the loss is disclosed before
publish, and generic OKF-bundle import as an Ingest Proposal already exists.
But forcing external categories into Lumio's fixed taxonomy, or discarding
whole directories with disclosure, loses real navigation structure and sharply
weakens the import's value: the whole point of importing a compiled brain is to
keep it navigable, not to flatten it.

The decision is difficult to reverse. Once published Knowledge Bases may carry
declared categories beyond the seed, the Core SDK's loading, validation,
Navigation Index generation, Hot Index interaction, retrieval routing, and
migration semantics must all treat the catalog as data rather than a fixed
enum, and deployments plus local coding agents come to depend on the declared
set traveling with the KB.

## Decision

Make the Content Category catalog **extensible per Knowledge Base**: a KB's
Control File may declare additional Content Categories beyond the ADR-0008
seeded defaults. The seeded catalog remains the default and the recommended set.
This amends ADR-0008's "controlled Content Category catalog" from a fixed seed
to an extensible catalog with a seeded default; it does not alter ADR-0008's
Control File structure, reserved derived artifacts, or Activity Log contract.

### Extensible catalog declaration

A KB's `categories` entry declares the complete Content Category catalog for
that Knowledge Base. When the entry is absent or empty, the Core SDK applies
the seeded default (`concepts`, `entities`, `references`, `procedures`,
`tables`, `datasets`, `synthesis`). A Maintainer may extend the list with
additional slug-valid categories via a reviewed Control File change. Declared
categories are first-class for navigation, validation, and retrieval, and —
consistent with ADR-0008 — carry no Lumio-wide type semantics: a page's
free-form `type` remains specific meaning, and no taxonomy is imposed on the
new categories.

### Validation and reserved-name safety

Declared categories must be valid slugs (lowercase, ASCII letters, digits, and
hyphens; beginning with a letter; bounded length), unique within the catalog,
and must not collide with reserved basenames or markers from ADR-0007 and
ADR-0008 (`index`, `hot`, `log`, and the `lumio` marker system). As today, a
Compiled Page's `category` frontmatter must resolve to an entry in the KB's
declared catalog or validation fails. Categories are never generated
automatically from content, from frontmatter, or from import; they exist only
when a Maintainer declares them.

### Effect on reserved artifacts and retrieval

Navigation Indexes (ADR-0007) render declared categories as first-class
directories exactly like seeded ones. The Hot Index is unaffected because it is
pin-based (Control File `hot_index`), not category-based. Retrieval routing
treats declared categories as navigation scope indistinguishable from seeded
ones; the compiled-wiki-first, whole-page-evidence retrieval model
(PRD-0001) is unchanged. Legacy Flat Mode (ADR-0008) is unaffected: a Knowledge
Base with no Control File has no declared categories and loads exactly as
before.

### Maintainer gate and import relationship

Adding, renaming, or removing a declared category is an explicit, reviewed
Maintainer action expressed as a Control File change — never automatic, never
inferred, and never created silently by import. External-vault import MAY
propose adding declared categories to preserve imported structure, but that
proposal is reviewed like any Control File change. If a Maintainer rejects a
proposed category, the corresponding imported content is dropped with the
pre-publish disclosure already required by ADR-0007. The gate is the
anti-sprawl guardrail: extensibility is available, but every new category is a
visible, reviewable decision.

## Considered Options

- **Drop unmapped categories with disclosure on import** — rejected: it honors
  ADR-0007 and ADR-0008 with no change, but it discards real navigation
  structure and reduces external-vault import to a flattened ingest, defeating
  its purpose.
- **Expand the seeded catalog to a fixed superset (add `projects`, `journal`,
  `skills`, etc.)** — rejected: it bakes assumptions about specific source
  formats into Lumio's universal seed, contradicting the stance that
  obsidian-wiki (or any single source) is not special; it still fails for
  arbitrary external vaults; and the seed would grow every time a new source
  format appears.
- **Make `type` carry category-like semantics instead of extending categories**
  — rejected: ADR-0008 deliberately separates broad navigation routing
  (category) from specific semantics (free-form `type`); overloading `type`
  would blur that seam and still not produce navigable directory structure.
- **Fully per-KB extensible catalog, Maintainer-gated via the Control File** —
  chosen: it preserves arbitrary external structure, keeps the seeded catalog as
  the recommended default, and gates proliferation behind reviewed Control File
  changes so extensibility does not become taxonomy sprawl.

## Consequences

The Core SDK's loading, validation, Navigation Index generation, retrieval
routing, and migration semantics must treat the category catalog as data, not a
fixed enum. `docs/kb-format.md` must record the extensible-catalog contract and
the seeded default alongside the existing Control File, reserved-artifact, Hot
Index, and Activity Log contracts, mirroring
`src/lumio/core/knowledge_base.py`.

The Hot Index, Activity Log, and OKF Exchange Profile (ADR-0007) are
unaffected. Trust, citation, visibility, and provenance invariants are not
weakened: category extensibility is navigation structure, not semantics or
trust, and lossy canonicalization still applies to all non-category imported
metadata. Legacy Knowledge Bases are unaffected until a Maintainer chooses to
declare categories.

The cost is a cross-KB consistency trade-off: because each Knowledge Base
declares its own catalog, category names are not guaranteed to mean the same
thing across KBs. This is acceptable because categories are KB-local
navigation, not a Lumio-wide taxonomy (ADR-0008), and the Maintainer gate plus
the seeded-default recommendation contain sprawl. A forthcoming external-vault
import capability will depend on this decision being recorded (its PRD is
deferred and not yet filed); the import does not depend on any weakening of
ADR-0007's interchange or trust stance.
