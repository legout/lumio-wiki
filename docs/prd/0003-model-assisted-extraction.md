# PRD-0003: Model-Assisted Extraction and Grounding (Deferred)

_Status: deferred future specification. Recorded by issue #173 so the deferred
model-assisted work has an explicit home instead of hiding inside the
entity-claim ontology delivery. Nothing in this PRD is implemented, approved
for implementation, or scheduled; ADR-0021 defers it until the deterministic
foundation is proven._

## Problem Statement

The entity-claim ontology (#167–#173) is deliberately model-free after a
Maintainer or host Distiller authors candidate pages: deterministic records,
ontology validation, proposal-first publication, reviewed Entity Merge, graph
materialization, traversal, and retrieval metadata all work without a model
provider. That determinism was a prerequisite, not a terminal state.
Maintainers still author every Entity, Claim, and Evidence anchor by hand,
which caps corpus growth and leaves no assistance for evidence-span
verification.

## Scope (to be decided when this PRD is picked up)

The deferred capabilities ADR-0021 names, each of which requires its own
review before any is scheduled:

- Model-assisted Entity and Claim **extraction** (candidates only — never
  auto-published; the proposal-first gate and `proposed` lifecycle already
  exist for this).
- Model-assisted evidence-**span grounding** (suggesting Claim Evidence
  anchors into published page content for review).
- **Entailment** and contradiction detection over accepted Claims.
- Independent-source **consensus** and staleness transitions.

## Non-Goals

- Changing the canonical format, the one-Entity-per-page rule, or the
  in-memory traversal ceilings (ADR-0021).
- Any model provider becoming a `lumio-wiki` dependency; assisted flows
  remain optional extras behind existing seams (`[llm]`, provider config).
- Auto-publication of anything a model produces.

## Open Questions

- Evaluation methodology for extraction quality that stays honest to the
  #173 disclosure rules (measured metrics only, corpus/mode/warm-up/fallback
  disclosed, no answer-quality claims).
- Trust thresholds for surfacing candidates without drowning Maintainers.
- Whether grounding suggestions reuse the Retrieval Trace contract.
