---
status: accepted
related: ADR-0011
---

# ADR-0016: Reviewed Title-Rename with Atomic Reference Repair

## Context

Lumio's identity model is the **Canonical Page Title**: Relationships and
internal links target titles, not paths. Category moves (#80) are therefore
already reference-safe — they change a page's *location* without touching its
*identity*. But a **title rename** changes the identity itself, breaking every
Relationship and body link that targets the old title, with no reviewed repair
path. This is the exact failure AIX's stable `id` was invented to prevent
("I added stable identifiers the week a rename broke two hundred links").

The full AIX answer — stable sub-title page IDs beneath titles, ID-first link
resolution, manifests — is deliberately **not** adopted. It is heavy identity
surgery touching fingerprints, exports, retrieval, and the Discovery Graph.

## Decision

Title rename is a **first-class proposal operation** (issue #140), mirroring
the category move (#80) and the proposed Page Removal (#135) pattern: a
reviewed mutation expressed in Lumio's proposal vocabulary rather than a new
identity substrate.

A rename proposal:

- carries old title → new title as a `rename_from` directive on the renamed
  `ProposedPage`;
- changes the page's `title` frontmatter while preserving its path, Sources,
  aliases, and typed Relationships;
- repairs, **in the same atomic proposal**, every canonical Relationship
  targeting the old title and every exactly-resolved body wikilink so they
  target the new title;
- makes old-title-as-alias an **explicit reviewed choice** (`keep_alias`),
  which preserves lookup-by-old-name without fabricating identity;
- discloses the blast radius: the renamed page and every repaired page;
- blocks on uniqueness (renaming onto an existing title or alias); and
- leaves ambiguous or escaping links as diagnostics, never guessed repairs.

Validation, publication, reserved-artifact regeneration, and immutability of
prior Published Versions behave exactly as for a category move: the renamed
page and its repairs validate together as one candidate Knowledge Base.

### Reference repair scope

Only **exactly-resolved** references are repaired:

- **Canonical Relationships** (`relationships: [{target: "Old Title", ...}]`)
  in other pages' frontmatter are retargeted to the new title.
- **Body wikilinks** (`[[Old Title]]`, `[[Old Title|alias]]`,
  `[[Old Title#section]]`) targeting the old title are retargeted to the new
  title.
- **Path-based Markdown links** (`[label](relative/path.md)`) are left
  unchanged: a title rename does not move the file, so path-based links still
  resolve. This keeps the blast radius minimal — only identity-based
  references break.
- **Ambiguous** links (two pages match) and **escaping** links are never
  guessed; they surface as non-blocking diagnostics for a Maintainer.

## Consequences

- Title rename is safe-by-default: a Maintainer never needs to manually chase
  broken references after approving a rename.
- The file path is preserved (the slug is not regenerated), so relative
  Markdown links are unaffected. A Maintainer who also wants a path change
  uses a category move (#80) as a separate reviewed step.
- The old title is never silently kept or dropped: the alias decision is
  explicit and disclosed in the blast radius.
- The AIX stable-ID alternative remains available if rename-with-repair proves
  insufficient in practice.

## Rejected alternative

**Stable sub-title page IDs / ID-first link resolution (AIX Level 1).** Would
add a persistent `id` beneath the title so renames never break references.
Rejected because it requires deep changes to fingerprints, exports, retrieval,
and the Discovery Graph, and because rename-with-repair solves the immediate
problem within Lumio's existing reviewed-mutation vocabulary.
