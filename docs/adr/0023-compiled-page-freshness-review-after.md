---
status: accepted
---

# ADR-0023: Compiled Page Freshness via `review_after`

## Context

OKF v0.2 introduced `stale_after` — an absolute instant after which a concept
is stale, chosen over a relative TTL so that staleness is a plain date
comparison any deterministic consumer can make. ADR-0015 adopted OKF v0.2 as an
exchange profile but deliberately kept freshness out of the canonical model,
noting that "future canonical support for freshness [...] should be designed
from Lumio requirements, not inherited automatically from OKF."

Lumio currently has no freshness notion. A Compiled Page can be `approved`
forever while the world changes underneath it; the Dream Cycle reports
structural problems (orphans, broken links, contradictions with `--semantic`)
but cannot answer "what needs re-review?" — the question Maintainers of a
living Knowledge Base actually ask weekly. The 2026-08-24 implementation review
identified this as the single most valuable feature gap found by comparing
Lumio against the OKF v0.2 trust layer.

Lumio requirements that shape the design:

- Freshness is **advisory**: a stale page is not invalid, never blocks
  validation, and is never auto-deprecated. Lifecycle transitions remain
  Maintainer decisions (CONTEXT.md).
- Freshness must be **deterministic and model-free**: the Dream Cycle core and
  `lint` are model-free; freshness evaluation must not change that.
- Freshness must be **portable**: declared in page frontmatter, meaningful to
  any consumer that can read a date, exchangeable through OKF Profile 2.
- Freshness must not become a credibility score. OKF's reasoning applies: a
  computed score is subjective, unportable, and goes stale itself.

## Decision

Compiled Pages gain one optional frontmatter field:

```yaml
review_after: 2027-01-15   # ISO 8601 date; page is due for review on/after this date
```

- A page is **due for review** when `today >= review_after`. Absent field ⇒
  no freshness opinion, ever. This mirrors OKF's "absence carries meaning"
  without importing its vocabulary.
- Name: `review_after`, not `stale_after`. Lumio's canonical vocabulary speaks
  of Maintainer review (Lifecycle: `draft → review → approved → deprecated`);
  the field schedules a *review*, it does not declare content stale.
- Surfacing, all read-only and advisory: `validate`/`lint` report due pages as
  warnings (never failures); the Dream Cycle's deterministic core ranks
  due-for-review pages alongside Link Candidates; `status` shows due counts.
- OKF Profile 2 export maps `review_after` → `stale_after` (lossless).
  Profile 2 import maps `stale_after` → proposed `review_after` with an
  explicit diagnostic, never silently.
- Setting or extending `review_after` on an already-published page is an
  ordinary reviewed page change (proposal-first applies as usual).

## Considered Options

- **Import OKF `stale_after` as the canonical name** — rejected: exchange
  vocabulary should not redefine the canonical model (same argument as
  ADR-0007's rejection of canonical OKF `type`); the mapping is lossless at
  the boundary anyway.
- **Relative TTL (`review_interval: 90d`)** — rejected: OKF's determinism
  argument; a relative interval makes "is it due?" depend on when it was last
  touched and invites silent drift.
- **Blocking validation on due pages** — rejected: staleness is not
  invalidity; blocking would punish living Knowledge Bases and incentivise
  deleting the field.
- **Computed freshness score from source activity** — rejected: unportable,
  subjective, goes stale; sources may carry their own `last_modified` signals
  later without changing this field.
- **Per-Claim freshness** — deferred: page-level covers the Maintainer
  workflow; Claim-level freshness can layer on later if Claim Evidence needs
  it.

## Consequences

- The Compiled Page schema, validation, `lint`, Dream Cycle report, and
  `status` each gain a small, testable addition; no new artifact, no new
  subsystem.
- OKF Profile 2 mapping tables gain one row each direction, with tests
  covering round-trip.
- CONTEXT.md gains a `review_after` entry under identity/metadata.
- Maintainers get a "what needs re-review" answer from `dream` and `lint`
  without enabling the semantic layer.
