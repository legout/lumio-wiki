---
status: accepted
amends: ADR-0008
---

# ADR-0022: Retire the Activity Log Reserved Artifact

## Context

ADR-0008 established three reserved derived artifacts: Navigation Indexes,
the Hot Index, and the Activity Log (`log.md` — a portable, append-only record
of successful published state transitions, distinct from the private SQLite
audit log and from OKF's exchange `log.md`).

The 2026-08-24 consolidation review (see issue trail from the implementation
audit) found the Activity Log earns nothing:

- It is written only by the proposal-pipeline deploy path and is **not
  surfaced anywhere**: not in `lint`, `health`, `status`, the web UI, or the
  reader surfaces.
- Its investigative value is already covered by the private audit log
  (ADR-0008 explicitly keeps audit private) and by Git/S3 Published Version
  history.
- It costs a reserved basename, exclusion logic in loading/retrieval/
  fingerprinting, formatting code, and a CONTEXT.md vocabulary entry every
  implementer must learn.

In contrast, the same review confirmed the **Hot Index is load-bearing**: it is
step 0 of the documented agent retrieval ladder (`lumio-wiki hot`), and
Maintainer-pinned curation is a product feature, not bookkeeping. The two
"index" artifacts are sometimes confused for each other, but they serve
different readers (exhaustive catalog vs curated entry points) and both stay.

The decision is hard to reverse because `log.md` is a reserved basename inside
every published Knowledge Base; retiring it changes what future Published
Versions contain.

## Decision

Lumio stops generating and appending to the Activity Log. `log.md` is no
longer a reserved artifact that the Core SDK *produces*.

Backwards compatibility: Knowledge Bases published before this ADR may contain
marked `log.md` files. The loader MUST continue to recognize and exclude a
validly **marked** legacy Activity Log (so old Published Versions still load,
validate, and fingerprint identically), but no new entries are ever written and
no new Published Version contains one. Unmarked `log.md` collisions remain a
blocking validation error, unchanged.

Publish history for portable consumers is served by Published Version
immutability itself (the version directory / Git history is the record);
incident investigation is served by the private audit log, unchanged.

Navigation Indexes and the Hot Index are unaffected and remain the two
reserved navigation artifacts.

## Considered Options

- **Keep the Activity Log and surface it in `status`/UI** — rejected: adds
  product surface to justify an artifact instead of the other way around; the
  audit log already answers "what happened" with more detail.
- **Emit OKF-style `log.md` only at export** — rejected: exchange history is a
  per-export concern; generating it at export time from Published Version
  metadata remains possible later without a reserved in-KB artifact.
- **Also merge Hot Index into Navigation Index** — rejected: the review's
  duplication flag was wrong on inspection; `hot` is the pinned entry surface
  used by the agent retrieval ladder and by Maintainers.
- **Delete legacy `log.md` recognition entirely** — rejected: silently breaks
  loading/fingerprint equality for already-published Knowledge Bases.

## Consequences

- `append_activity_log_entry` and its formatting helpers are deleted;
  `proposal_pipeline` deploy no longer appends.
- Reserved-artifact recognition keeps a legacy `log.md` branch (marked files
  only) with a comment citing this ADR.
- CONTEXT.md loses the Activity Log entry; the Reserved Artifact entry is
  updated to name Navigation Index and Hot Index only, plus legacy `log.md`
  recognition.
- Tests that assert log appends are deleted; tests asserting legacy marked
  `log.md` exclusion are added.
