---
status: accepted
amends: ADR-0008, ADR-0011, ADR-0013, ADR-0014, ADR-0016
---

# ADR-0021: Entity-Claim Ontology and Progressive Graph Materialization

## Context

Lumio's reviewed graph currently consists of typed `Relationship(target, type)`
records between Compiled Pages identified by Canonical Page Title. The Discovery
Graph adds deterministic Extracted References from body links, persists a
fingerprint-bound MessagePack adjacency cache for zero-index use, and lets
optional LanceDB retrieval rank Evidence after graph expansion.

This model is sufficient for page navigation but not for a trustworthy knowledge
graph. It cannot give entities stable identity across title changes, represent
literal-valued propositions, attach evidence to individual semantic assertions,
validate predicate domain and range, distinguish disputed or superseded
assertions, or support reviewed entity resolution. Sage Wiki demonstrates the
value of these capabilities, but adopting its database and runtime architecture
would add complexity that conflicts with Lumio's portable compiled-Markdown
foundation.

Lumio has not been used in production, so preserving the title-based graph format
is not a requirement. The design should take the direct path to a small,
inspectable entity-claim model while retaining proposal-first publication,
portable canonical content, zero-index operation, immutable Published Versions,
and optional enhanced retrieval. The implementation specification and task map
are tracked in [GitHub issue #167](https://github.com/legout/lumio/issues/167).

## Decision

### Make each Compiled Page one Entity

Every Compiled Page represents exactly one **Entity** and declares a stable,
Knowledge-Base-wide **Entity ID** plus one or more controlled **Entity Types**.
The Entity ID is independent of the page's Canonical Page Title and path. A title
rename changes a label, not identity; ADR-0016's title-repair operation remains
useful for human-authored body links but no longer defines semantic identity.

The one-Entity-per-page rule is deliberate. An Entity important enough to enter
the reviewed Knowledge Graph receives a human-readable Compiled Page. Lumio will
not introduce hidden entity manifests or allow several primary Entities to share
one page until real corpora prove that the restriction is inadequate.

### Replace canonical Relationships with evidence-bearing Claims

A **Claim** is a stable, reviewed proposition owned by its subject Entity's
Compiled Page. It contains:

- a stable Claim ID;
- the subject Entity ID, implied by the owning page;
- a controlled Predicate ID;
- exactly one object: another Entity ID or a typed literal;
- one or more Claim Evidence anchors into published Compiled Page content;
- a publication lifecycle of `accepted`, `disputed`, or `superseded`;
- optional confidence and valid-time metadata; and
- extraction/origin metadata sufficient to distinguish authored, migrated, and
  future model-assisted candidates.

A proposed or rejected assertion exists only inside an Ingest Proposal and is
not part of an active Published Version. Only `accepted` entity-to-entity Claims
enter the canonical traversal projection. Disputed and superseded Claims remain
inspectable but do not silently select supporting context. A Claim and its graph
edge are not Evidence; answers remain grounded in citation-ready Evidence from
Compiled Pages.

Claim Evidence identifies a supporting section or bounded line range in a
Compiled Page. It does not expose private Source Artifact coordinates and does
not implement the raw-source Claim Lineage deferred in issue #137 and ADR-0014.

### Declare a small ontology in the Control File

The Knowledge Base Control File gains a versioned ontology section containing:

- controlled Entity Type definitions; and
- Predicate definitions with canonical ID, optional synonyms, optional inverse,
  allowed subject Entity Types, and allowed object Entity Types or literal kind.

Publication validation blocks duplicate or dangling Entity and Claim IDs,
unknown types or Predicates, domain/range violations, invalid literal kinds,
missing Claim Evidence, invalid evidence anchors, and merge cycles. Lumio does
not add OWL/RDF reasoning, type inheritance, ontology namespaces, cardinality
rules, or automatic ontology induction in this phase.

Entity aliases remain page lookup labels. A reviewed **Entity Merge** selects a
surviving Entity ID, repairs Claims and exactly resolved links in the same
proposal, records the retired ID as a redirect, and discloses the blast radius.
Automatic entity merging is forbidden.

### Preserve canonical and discovery graph scopes

The **Knowledge Graph** is the canonical graph of accepted Claims. The
**Discovery Graph** is the Knowledge Graph plus deterministic Extracted
References from internal Markdown links. Extracted References remain
non-canonical navigation topology, never become Claims automatically, and never
support an answer by themselves.

Traversal continues to apply authorization before expansion and remains bounded
by depth, edge, and result limits. Lumio loads the active version's accepted edge
projection into an in-memory `GraphState` and performs traversal there rather
than issuing one storage query per hop.

### Keep two progressive derived materializations

Canonical knowledge remains Compiled Markdown plus `lumio.yaml`. Both graph
materializations are disposable and fingerprint-bound:

1. `lumio-wiki` retains the MessagePack adjacency artifact as the zero-index
   cache. A missing, stale, or corrupt cache is rebuilt from canonical content.
2. `lumio-lancedb` adds `entities` and `graph_edges` tables beside its Evidence
   tables for scalar, lexical, semantic, and hybrid candidate retrieval. Loading
   a Published Version projects accepted edges into the same in-memory
   `GraphState` used by zero-index operation.

LanceDB is the only enhanced persisted graph projection. Lumio will not add
SQLite, PGlite, PostgreSQL, a graph database, or a generic `GraphStore`
hierarchy now. LanceDB does not own canonical knowledge, perform recursive graph
queries, or replace Lumio's outer Published Version protocol. Local and S3
publication build every requested artifact under one inactive immutable version,
health-check it, write completion metadata, and activate it through the existing
conditional pointer update.

### Deliver deterministic trust before model automation

The first implementation is model-free after a Maintainer or host Distiller has
authored candidate pages. It covers records, ontology validation, proposals,
reviewed entity resolution, graph materialization, traversal, retrieval metadata,
and publication. Claims never auto-publish.

Model-assisted entity/Claim extraction, evidence-span grounding, entailment,
independent-source consensus, contradiction detection, and staleness transitions
require a separate specification after the deterministic foundation is proven.

## Considered Options

- **Keep title-based Relationships and add metadata** — rejected because title
  identity, entity resolution, literal values, and stable Claim identity remain
  unsolved.
- **Use separate entity and Claim manifests** — rejected because hidden graph
  records weaken inspectability and create another canonical artifact model.
- **Store each Claim as its own file** — rejected because it makes ordinary wiki
  review and navigation noisy without current scale evidence.
- **Use LanceDB as canonical knowledge** — rejected because the Knowledge Base
  would stop being portable, diffable, and rebuildable without an index.
- **Replace MessagePack with LanceDB** — rejected for now because zero-index and
  S3 readers benefit from a dependency-free cache and the Owner explicitly
  accepts two progressive derived formats.
- **Adopt SQLite/PostgreSQL or PGlite/PostgreSQL now** — rejected because their
  constraints, transactions, and recursive SQL do not yet justify dual-engine
  schemas, migrations, parity testing, or another runtime component.
- **Implement model extraction and Sage-style trust consensus in the first
  milestone** — rejected because provider variance would obscure whether the
  canonical model and validation rules are correct.

## Consequences

`CompiledPage`, the Control File schema, validation, proposal review, title
rename, removal, graph state, retrieval traces, CLI output, OKF exchange, and
LanceDB publication must adopt Entity IDs and Claims. Existing unshipped
Knowledge Bases and tests may be updated directly; no compatibility loader or
migration command is required.

The MessagePack cache and LanceDB tables must produce behaviorally identical
accepted traversal topology from the same fingerprinted Published Version.
LanceDB may additionally support entity resolution candidates and Claim search,
but cannot promote candidates or change canonical content.

The design intentionally accepts two ceilings: every graph Entity requires a
Compiled Page, and traversal loads accepted adjacency into memory. Revisit the
first only when meaningful entities repeatedly fail to justify pages. Revisit
storage only when measured startup, memory, traversal, concurrent mutation, or
server-side authorization requirements exceed the current design.

Tests must prove deterministic IDs and rebuilds, ontology validation, evidence
anchor validation, entity merge and redirect behavior, Claim lifecycle
filtering, canonical-versus-discovery scope, authorization-before-expansion,
MessagePack/LanceDB parity, stale artifact rejection, local/S3 publication
atomicity, and identical citation contracts with and without `lumio-lancedb`.
