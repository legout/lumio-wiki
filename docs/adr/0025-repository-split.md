---
status: accepted
---

# ADR-0025: Repository Split — open-source foundation, private application

## Context

ADR-0010 established a three-member uv workspace in one repository and explicitly
rejected splitting packages into separate repositories to preserve locality, one
lockfile, and atomic cross-package changes. That trade-off was made when the
web application was the only consumer of the foundation.

The product now has two audiences with different disclosure requirements:

1. The portable Knowledge Base foundation (`lumio-wiki`, `lumio-lancedb`) is a
   general-purpose toolkit for any coding agent; publishing its source invites
   contribution, unblocks `uv tool install lumio-wiki` from public PyPI, and
   matches how comparable tooling is distributed.
2. The deployable web application (`lumio` — Stario UI, auth, storage,
   providers, Agent Runtime) is a product surface that stays proprietary.

The dependency direction certified by ADR-0010 (`lumio-wiki ← lumio-lancedb`,
both consumed by `lumio`, AST-locked in `test_package_contraction.py`) already
supports a clean split: nothing in the foundation names the application.

## Decision

Split the repository along the already-certified package seam:

- **Public repository `legout/lumio-wiki`** — contains `packages/lumio-wiki`,
  `packages/lumio-lancedb`, the wiki-only test suite with shared fixtures,
  `eval/`, the wiki examples and wheel-verification scripts, and the public
  docs. Licensed Apache-2.0. Its `release.yml` publishes the two wheels as one
  lockstep family to public PyPI via trusted publishing.
- **Private repository `legout/lumio`** — contains the web application, its
  test suite, mockups, experiments, Docker packaging, and app docs. It depends
  on the published wheels (`lumio-wiki>=X.Y.Z,<0.N+1.0`, same for
  `lumio-lancedb`) instead of workspace members.

ADR-0010's workspace shape is amended, not reversed: the three wheels, their
ownership boundaries, dependency direction, and lockstep family policy are
unchanged. What changes is repository locality — two repos instead of one —
and the release surface (the public repo releases two wheels; the application
is not published to a public index).

### What each side accepts

- The public repo loses the application-level tests that exercised
  `lumio_wiki` through `lumio.*` re-export shims (they import the private
  app). Tests that could not be cleanly separated stay private; the public
  suite is the wiki-only files plus each package's own tests. Coverage lost
  this way is re-derived on demand, not pre-emptively.
- The private repo resolves the two members from PyPI. Until the first public
  release is published, it may temporarily keep path-based `[tool.uv.sources]`
  pointing at the split-out public checkout.
- The lockstep family now spans two repos for one release (the public pair);
  the private app pins the family with bounded ranges and moves independently
  after that.

## Considered Options

- **Keep the monorepo, publish wheels only** — rejected: the source of the
  foundation stays hidden, which defeats the open-source goal; the "coding
  agent second brain" audience needs to read and trust the code.
- **Filter-repo rewrite of the private repo too (drop foundation paths from
  its history)** — rejected: no benefit (the soon-public code is not a secret
  once public), and it would invalidate every open PR, worktree, and commit
  reference. A normal removal commit suffices.
- **Three repos (one per wheel)** — rejected: `lumio-wiki` and `lumio-lancedb`
  share the retrieval contract, fixtures, and eval gate; splitting them
  doubles release surface for no disclosure benefit.

## Consequences

- `uv tool install lumio-wiki` becomes possible from public PyPI once the
  first public release ships.
- PyPI trusted publishers must be (re)registered for `legout/lumio-wiki`
  (owner action, one-time).
- Cross-repo changes to a foundation contract now require a released wheel
  before the private app can adopt them (or a temporary path source).
- ADR-0010's "Split packages into separate repositories — rejected" option is
  superseded by this decision; everything else in ADR-0010 stands.
