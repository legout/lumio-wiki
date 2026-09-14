# PRD-0006: Library Stabilization

_Status: behavior approved. The owner decisions were captured at audit revision `9712f6fd51ac4381a6ca4fc513e2546355fba0d0`; this revision relocates them from `docs/plans/README.md` without changing scope. Capture checkpoint: no new product vocabulary; ADR-0027 owns the workflow decision; the external-consumer inventory remains unresolved for AC4. Plan and release authorization remain separate._

## Goal

Stabilize the public `lumio-wiki` and `lumio-lancedb` libraries without removing supported capabilities.

## Behavioral constraints

- Preserve optional converters, providers, capture clients, S3, and LanceDB. Host-as-Distiller remains the default; providers remain opt-in.
- Serialize cooperating local writers per Knowledge Base and restore prior state after handled failures. Crash atomicity, instant visibility to concurrent readers, and compliance by external editors are not promised.
- Allow disjoint reviewed proposals to publish after intervening disjoint changes. Check affected-path and Control File preconditions, then validate the complete current candidate; do not use one whole-KB digest to reject all outstanding proposals.
- A breaking public API release requires actual consumer inventory and migration, including the separate private Lumio application. Absence of a caller in this repository is not proof that an exported API is dead.

## Acceptance criteria

### AC1: Publication integrity

Validation aggregates authored Claim and ontology problems instead of discarding them or crashing. Candidate destinations cannot overwrite another page through normalization or collision. Proposal and source mutations read durable state under one cooperating-writer boundary, reject stale overlapping state while allowing disjoint changes, and restore the complete pre-mutation state after handled failures.

### AC2: Captured snapshots and retrieval

Pages, fingerprints, graph state, indexes, immutable publication bytes, and private bindings identify the same captured candidate. Lexical-only rebuilds cannot leave accepted stale semantic state. Required rollback validates the complete historical binding set. Retrieval spends its bounded result budget on specific useful passages, including nested sections, and evaluation truthfully reports negatives, passage identity, lifecycle, available stages, and what was not measured.

### AC3: Safe agent workflows

Capture redacts supported credential forms before preview or persistence, and provider failures cross the CLI as bounded safe errors. URL deadlines cover the synchronous network I/O they claim. CLI and SDK ingest share one preparation policy; optional adapter composition has one owner. Executable citation actions retain the effective Knowledge Base Location. Authoring guidance uses the current schema, machine reads are exact and bounded, command examples execute in their documented order, and source/page/transcript/tool content has no operational authority.

### AC4: Simplification and coordinated release

New publications stop producing the retired Activity Log while historical marked logs remain safely recognizable. Public seam removal follows evidence-backed consumer migration, not speculative dead-code inference. Traversal has one bounded owner over captured facts without weakening authorization, topology, or citation contracts. A breaking release is separately approved and certified from built wheels plus real consumer adoption evidence.

## Non-goals

- Removing supported optional capabilities or adding mandatory providers, databases, graph engines, storage frameworks, or agent runtimes.
- Treating graph candidates, citations, or deterministic embeddings as answer entailment or real model-quality proof.
- Publishing, merging, releasing, or changing the private application under this specification alone.

## Evidence

The source findings and qualifications are in [Library stabilization audit evidence](../research/library-stabilization-audit.md). ADRs remain authoritative for their architectural scopes; this PRD does not override them.
