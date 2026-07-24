---
status: accepted
---

# ADR-0014: Source Versions and Safe Page-Level Invalidation

## Context

Lumio records page-level Sources and source hashes, but a Knowledge Source has
no stable identity: replacing a source's bytes creates an unrelated source, so
no impact analysis happens at all when a source changes, and there is no way to
retire a source without silently abandoning the Compiled Pages it supports.

This ADR originally accepted a claim-level lineage design (stable Claim IDs,
private support sets, a fourth `lumio-lineage` distribution). Before
implementation, that design was reviewed and found disproportionate: Compiled
Pages are topic-scoped and cheap to re-review, source replacement and
retirement are rare events, and the hardest part of claim-level lineage —
reconciling Claim identity across LLM re-distillation — would have turned the
Maintainer into a full-time claim adjudicator. Page-level invalidation with a
stable source identity captures most of the safety value at a fraction of the
cost. The full claim-level design is preserved as a deferred backlog proposal
for the scale where it pays off (thousands of pages, high source churn, team
Maintainers); see [GitHub issue #137](https://github.com/legout/lumio/issues/137).

Safe invalidation must preserve immutable Published Versions, proposal-first
publication, and the portability of the canonical Markdown Knowledge Base.

## Decision

Each Knowledge Source receives a stable private identity in a **Knowledge
Source Registry** owned by `lumio-wiki`. Every ingest of a source's bytes
records an immutable, content-hashed **Source Version** under that identity.
Replacing a source's bytes under an existing identity extends the same source
with a new Source Version; it never creates an unrelated source. Identity is
established explicitly at ingest (Maintainer-assigned or matching a recorded
source key); a filename or path alone never establishes continuing identity.

The registry is private ingest state. It is excluded from Compiled Pages,
Navigation Indexes, the Hot Index, the Activity Log, Knowledge Base exports,
Reader retrieval, Evidence, Citations, Retrieval Traces, and canonical
fingerprints. It is not versioned publication state: no per-Published-Version
snapshots, no prepare-and-activate coupling, no lineage garbage collection.

**Source Retirement** is an explicit Maintainer action; reactivation is
likewise explicit and records a new Source Version. A missing file, an
object-store failure, or a watcher delta may produce a retirement *candidate*
but never deactivates a source automatically.

Replacing or explicitly retiring a Source Version produces an Ingest Proposal
with page-level impacts and a disclosed trigger ("source X version replaced,
hash a→b" or "source X retired"). Each affected Compiled Page is classified:

- `still-supported` — at least one other active Source Version still supports
  the page; informational, no edit required.
- `sole-source-lost` — the page's last active support changed or disappeared;
  the proposal offers re-distillation from the replacement Source Version,
  Maintainer re-review, or an explicit reviewed Page Removal.

The page is the unit of invalidation. There are no Claims, Claim IDs, support
sets, claim-impact states, or model-assisted reconciliation. A Page Removal is
never inferred from an omitted page; it must repair or redirect every canonical
Relationship that would otherwise become invalid in the same proposal, and
body-link repairs remain explicit candidates or diagnostics.

Readers remain bound to the last active Published Version until a reviewed
proposal publishes. A source change in private ingest state never suppresses,
rewrites, or annotates Reader Evidence for an already active Published Version.

## Considered Options

- **Claim-level lineage with a `lumio-lineage` distribution** — *deferred, not
  rejected*. The right design for a thousands-of-pages, high-churn team
  Knowledge Base, but disproportionate to current scale and gated on unsolved
  Claim-identity reconciliation across re-distillation. The registry and
  Source Version vocabulary decided here are deliberately its foundation;
  adopting it later adds precision without reworking identity. Preserved in
  [issue #137](https://github.com/legout/lumio/issues/137).
- **Inline source spans in canonical Compiled Pages** — rejected: weakens
  portability and risks leaking private source locations.
- **A portable lineage/registry artifact** — rejected: source identity is
  private ingest state, not knowledge required by Readers or external agents.
- **Automatic retirement on missing files or storage failures** — rejected:
  storage absence is not proof of retirement.
- **Runtime quarantine or warning overlays on the active Published Version** —
  rejected: unpublished private source state must not mutate Reader Evidence.
- **Deriving source identity from filename or path** — rejected: renames and
  moves would silently sever or merge source histories.

## Consequences

The workspace stays at three distributions (ADR-0010 unchanged); no capability
hooks or transaction seams are added to the Proposal Pipeline. The pipeline
gains a source-change trigger and page-level impact records; existing proposal
review and publication behavior is otherwise unchanged.

`CONTEXT.md` gains the Knowledge Source Registry term and retains Source
Version, Source Retirement, and Page Removal; the Claim, Claim Lineage, and
Lineage Precision terms are removed with the deferred design.

Tests must prove: stable identity across Source Version replacement; immutable
version hashes; explicit retirement and reactivation; candidate-only behavior
for missing files and storage failures; page-level impact classification for
single-source and multi-source pages; explicit Page Removal with Relationship
repair; privacy and export exclusion of the registry; and that Readers remain
bound to the last active Published Version throughout.
