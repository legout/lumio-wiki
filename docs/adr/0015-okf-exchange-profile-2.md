---
status: accepted
amends: ADR-0007
---

# ADR-0015: OKF Exchange Profile 2 for OKF v0.2

## Context

Google Cloud's OKF v0.2 keeps the v0.1 bundle model but makes provenance,
trust, lifecycle, freshness, and attested computation first-class. The two
breaking changes are `timestamp` becoming `generated.at` and the conventional
body `# Citations` list moving to structured frontmatter `sources`. Additive
fields include `generated`, `verified`, `status`, `stale_after`, source
credibility signals, and the `Attested Computation` contract.

ADR-0007 correctly pinned Lumio OKF Exchange Profile 1 to OKF v0.1, so the
upstream release does not change Profile 1 behavior. But treating every v0.2
field as an arbitrary producer extension would now be unnecessarily lossy:
OKF `sources` and `status` overlap directly with Lumio's canonical Source and
Lifecycle vocabulary, and OKF may become a durable industry interchange
standard.

Lumio must adopt the standard surface without weakening canonical trust:
foreign `status: stable` or `verified` metadata is not Maintainer approval,
top-level OKF `resource` is not provenance, and an attester or executor
reference must never be executed at the import boundary.

## Decision

Lumio adds **OKF Exchange Profile 2**, pinned to OKF v0.2 at upstream commit
`3fcbb9f828c2f23d109c855ee403c3a4c81f3a96`, alongside unchanged Profile 1.
Profile 2 is selected explicitly with `okf-2`; existing `okf-1` behavior and
bytes remain stable.

Profile 2 export keeps the standard Profile 1 mapping and adds:

- standard OKF `sources` derived from canonical Sources. A Source URL becomes
  `sources[].resource`; an id-only Source becomes a scope descriptor, which
  OKF v0.2 permits;
- standard OKF `status`, derived as `draft`/`review` → `draft`,
  `approved` → `stable`, and `deprecated` → `deprecated`;
- `okf_version: "0.2"` on the bundle-root Navigation Index; and
- the versioned `lumio` extension with exact aliases, Lifecycle, Visibility,
  synthetic status, Sources, and typed Relationships for Lumio-aware round
  trips.

Profile 2 import maps standard `sources[]` into proposed canonical Sources,
plus deterministic provenance for the imported bundle document. It recognizes
`generated`, `verified`, `status`, `stale_after`, source credibility signals,
and Attested Computation fields with explicit diagnostics. Generic imports
still default to `draft` / `internal` / non-synthetic. `verified` and
`status: stable` never auto-approve a page; computation fields are preserved
only as document content metadata and never executed.

The canonical model does not gain OKF `type`, top-level `resource`, trust
events, freshness fields, credibility scores, or computation contracts. Those
require independent Lumio domain decisions if future product needs justify
them. Unknown producer extensions remain previewable and lossy-with-disclosure;
a separate sidecar decision would be required for lossless third-party
round-tripping.

## Considered Options

- **Leave only Profile 1** — rejected: safe, but standardized v0.2 provenance
  and lifecycle would be diagnosed as unknown metadata and lost.
- **Replace Profile 1 in place** — rejected: violates ADR-0007's pinned
  exchange contract and silently changes existing export bytes.
- **Adopt OKF v0.2 as the canonical model** — rejected: OKF trust is advisory
  exchange metadata; Lumio's reviewed Lifecycle, Visibility, typed
  Relationships, and proposal-first publication remain stronger invariants.
- **Auto-map `verified` or `status: stable` to `approved`** — rejected: a
  producer's claim is not Maintainer review.
- **Execute Attested Computation executors or attesters** — rejected: OKF fixes
  an interface, not a sandbox or runtime; import must remain safe and passive.

## Consequences

The Core SDK exposes pinned Profile 2 import/export APIs and the web export
boundary accepts `?profile=okf-2`. The OKF import proposal boundary accepts an
explicit profile selection while defaulting to Profile 1 for backwards
compatibility. Profile 2 tests cover deterministic export, standard Source
mapping, derived status, safe generic import defaults, explicit diagnostics,
and Lumio-aware round trips.

Documentation and the Lumio Knowledge Base must distinguish the historical
Profile 1 contract from the new Profile 2 contract. Future canonical support
for freshness, source credibility, or attested computation should be designed
from Lumio requirements, not inherited automatically from OKF.
