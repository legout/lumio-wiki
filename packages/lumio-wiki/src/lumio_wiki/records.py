"""Domain records for the Lumio Core SDK.

Deterministic serialization: every record in this module is a frozen
``msgspec.Struct`` whose fields serialize in stable declaration order, so
identical record content always produces identical bytes. The Control File
renderer (:func:`lumio_wiki.knowledge_base.write_control_file`) emits the
ontology with sorted keys and stable section order for the same guarantee at
the YAML boundary; unchanged canonical content therefore yields byte-identical
artifacts and stable fingerprints.
"""

import msgspec


class Source(msgspec.Struct, frozen=True):
    """A provenance reference for a Compiled Page."""

    id: str = ""
    title: str = ""
    url: str | None = None


class RegistryEntry(msgspec.Struct, frozen=True):
    """A compact, navigable summary of a Compiled Page for registry views."""

    title: str = ""
    aliases: list[str] = msgspec.field(default_factory=list)
    tags: list[str] = msgspec.field(default_factory=list)
    summary: str = ""
    lifecycle: str = ""
    visibility: str = ""
    path: str = ""
    source_count: int = 0
    relationship_count: int = 0


class Relationship(msgspec.Struct, frozen=True):
    """A derived, title-level view of one canonical graph edge.

    Since ADR-0021, canonical edges are accepted, entity-to-entity Claims.
    This record is the projection consumed by title-oriented surfaces
    (traversal, registry views, OKF exchange); it is no longer a page
    frontmatter input contract.
    """

    target: str = ""
    type: str = ""


# ---------------------------------------------------------------------------
# Entity/Claim ontology records (issue #168, ADR-0021).
#
# Each Compiled Page declares exactly one stable Entity ID and one or more
# controlled Entity Types, and owns its subject Claims. A Claim carries
# exactly one object: another Entity ID or a typed literal. Predicates,
# Entity Types, and Entity redirects are declared by the version-2 Control
# File ontology. Only the three published lifecycle values may appear in an
# active Compiled Page; ``proposed`` and ``rejected`` are Ingest Proposal
# state only.
# ---------------------------------------------------------------------------

#: Published Claim lifecycle values valid in an active Compiled Page.
CLAIM_STATUS_ACCEPTED = "accepted"
CLAIM_STATUS_DISPUTED = "disputed"
CLAIM_STATUS_SUPERSEDED = "superseded"
PUBLISHED_CLAIM_STATUSES = frozenset(
    {CLAIM_STATUS_ACCEPTED, CLAIM_STATUS_DISPUTED, CLAIM_STATUS_SUPERSEDED}
)
#: Lifecycle values that exist only inside Ingest Proposals (ADR-0021).
PROPOSAL_ONLY_CLAIM_STATUSES = frozenset({"proposed", "rejected"})

#: Literal kinds a Predicate may declare for typed-literal Claim objects.
LITERAL_KIND_STRING = "string"
LITERAL_KIND_NUMBER = "number"
LITERAL_KIND_BOOLEAN = "boolean"
LITERAL_KINDS = frozenset({LITERAL_KIND_STRING, LITERAL_KIND_NUMBER, LITERAL_KIND_BOOLEAN})

#: Claim origin metadata distinguishing authored and migrated assertions
#: (model-assisted candidates remain proposal-only future work).
CLAIM_ORIGIN_AUTHORED = "authored"
CLAIM_ORIGIN_MIGRATED = "migrated"
CLAIM_ORIGINS = frozenset({CLAIM_ORIGIN_AUTHORED, CLAIM_ORIGIN_MIGRATED})


class Entity(msgspec.Struct, frozen=True):
    """The identity/type metadata of the Entity one Compiled Page represents.

    A projection of a page's Entity contract (ADR-0021): the stable Entity
    ID, its controlled Entity Types, and the label/path surface. One Entity
    per page; titles and paths are labels, not identity.
    """

    id: str
    entity_types: list[str] = msgspec.field(default_factory=list)
    title: str = ""
    aliases: list[str] = msgspec.field(default_factory=list)
    path: str = ""


class EntityTypeDefinition(msgspec.Struct, frozen=True):
    """A controlled Entity Type declared by the Control File ontology."""

    description: str | None = None


class PredicateDefinition(msgspec.Struct, frozen=True):
    """A controlled Predicate declared by the Control File ontology.

    ``subject_types``/``object_types`` constrain the Entity Types a Claim's
    subject and entity object may carry. A Predicate accepts typed-literal
    objects through ``literal_kind`` instead of ``object_types``. Neither
    constraint list is required: an unconstrained Predicate accepts any
    subject or any entity object.
    """

    subject_types: list[str] = msgspec.field(default_factory=list)
    object_types: list[str] = msgspec.field(default_factory=list)
    literal_kind: str | None = None
    inverse: str | None = None
    synonyms: list[str] = msgspec.field(default_factory=list)
    description: str | None = None


class ClaimEvidence(msgspec.Struct, frozen=True):
    """One Claim Evidence anchor into the owning page's published content.

    An anchor identifies a supporting section heading, a bounded 1-based
    line range within the page body, or both. It never exposes private
    Source Artifact coordinates (ADR-0021).
    """

    section: str | None = None
    line_start: int | None = None
    line_end: int | None = None


class Claim(msgspec.Struct, frozen=True):
    """A stable, reviewed proposition owned by its subject Entity's page.

    Exactly one object is carried: ``object`` (another Entity ID) or
    ``value`` plus ``value_type`` (a typed literal). ``status`` is the
    published lifecycle; proposed/rejected assertions live only inside
    Ingest Proposals. ``valid_from``/``valid_to`` are optional ISO 8601
    valid-time bounds.
    """

    id: str
    predicate: str
    status: str = ""
    object: str | None = None
    value: str | float | int | bool | None = None
    value_type: str | None = None
    confidence: float | None = None
    origin: str = CLAIM_ORIGIN_AUTHORED
    valid_from: str | None = None
    valid_to: str | None = None
    evidence: list[ClaimEvidence] = msgspec.field(default_factory=list)


class EntityRedirect(msgspec.Struct, frozen=True):
    """A retired Entity ID that resolves to a surviving Entity.

    Recorded by a reviewed Entity Merge; the retired ID no longer owns a
    Compiled Page but keeps references resolvable (ADR-0021).
    """

    from_id: str
    to_id: str


class Ontology(msgspec.Struct, frozen=True):
    """The controlled vocabulary declared by the version-2 Control File."""

    entity_types: dict[str, EntityTypeDefinition] = msgspec.field(default_factory=dict)
    predicates: dict[str, PredicateDefinition] = msgspec.field(default_factory=dict)
    redirects: list[EntityRedirect] = msgspec.field(default_factory=list)


# The version of the internal-link extractor that produced an
# ``ExtractedReference`` (ADR-0011, issue #107). Bumped only when the
# extraction algorithm changes in a way that would alter the derived set.
EXTRACTOR_VERSION = "1"


class ExtractedReference(msgspec.Struct, frozen=True):
    """A deterministic, non-canonical directed reference derived from a body link.

    An Extracted Reference is produced when an internal Markdown link or
    unambiguous wikilink in a Compiled Page body resolves to exactly one
    Compiled Page. It records the source and target page identity (by Canonical
    Page Title), the ``markdown-link`` origin, the source path and 1-based line
    range for inspection, and the extractor version. It has only
    reference/navigation meaning: it is never Evidence, never a typed
    Relationship, and is never written back into frontmatter (ADR-0011).
    """

    source_title: str
    target_title: str
    origin: str = "markdown-link"
    source_path: str = ""
    line_start: int = 0
    line_end: int = 0
    extractor_version: str = EXTRACTOR_VERSION


class CompiledPage(msgspec.Struct, frozen=True):
    """A loaded Markdown page from a Knowledge Base.

    Every page in a categorized (Control File v2) Knowledge Base declares
    exactly one stable Entity ``id`` and at least one controlled Entity Type,
    and owns its subject ``claims`` (ADR-0021, issue #168).
    """

    path: str
    title: str = ""
    id: str = ""
    entity_types: list[str] = msgspec.field(default_factory=list)
    aliases: list[str] = msgspec.field(default_factory=list)
    tags: list[str] = msgspec.field(default_factory=list)
    summary: str | None = None
    lifecycle: str | None = None
    visibility: str | None = None
    sources: list[Source] = msgspec.field(default_factory=list)
    claims: list[Claim] = msgspec.field(default_factory=list)
    synthetic: bool = False
    body: str = ""
    body_start_line: int = 1


class ValidationIssue(msgspec.Struct, frozen=True):
    """A single validation problem, naming the file and field involved."""

    file: str
    field: str
    message: str
    severity: str = "error"


class ValidationReport(msgspec.Struct, frozen=True):
    """Aggregated validation result for a Knowledge Base."""

    issues: list[ValidationIssue] = msgspec.field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def __str__(self) -> str:
        lines: list[str] = []
        warnings = [i for i in self.issues if i.severity == "warning"]
        errors = [i for i in self.issues if i.severity != "warning"]
        if not self.issues:
            return "Knowledge base is valid."
        if errors:
            lines.extend(
                f"{issue.file}: {issue.field}: {issue.message}"
                for issue in self.issues
                if issue.severity != "warning"
            )
        if warnings:
            if self.is_valid:
                lines.append("Knowledge base is valid.")
            lines.extend(
                f"WARNING: {issue.file}: {issue.field}: {issue.message}"
                for issue in self.issues
                if issue.severity == "warning"
            )
        if not lines:
            return "Knowledge base is valid."
        return "\n".join(lines)


class SourceFileDigest(msgspec.Struct, frozen=True):
    """A single source file path and its deterministic digest."""

    path: str
    digest: str


class SourceFingerprint(msgspec.Struct, frozen=True):
    """Deterministic digest of a Knowledge Base source tree."""

    digest: str
    sources: list[SourceFileDigest] = msgspec.field(default_factory=list)


class TraceStage(msgspec.Struct, frozen=True):
    """One named stage in a Retrieval Trace."""

    name: str
    detail: str


class RetrievalTrace(msgspec.Struct, frozen=True):
    """Structured explanation of the retrieval stages that produced a result."""

    stages: list[TraceStage] = msgspec.field(default_factory=list)


class EmbeddingModelInfo(msgspec.Struct, frozen=True):
    """Identity/version/dimension of the embedding model backing a vector index.

    Persisted alongside the derived index so the Core SDK can detect when the
    configured embedding model changed and rebuild vectors from source (#75).
    """

    name: str
    dimension: int


class Evidence(msgspec.Struct, frozen=True):
    """A retrievable unit derived from a Compiled Page."""

    id: str
    source_type: str
    page_path: str
    page_title: str
    section_title: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    text: str = ""


class Citation(msgspec.Struct, frozen=True):
    """Metadata that lets an answer point back to its source content.

    ``origin`` distinguishes temporary Chat Context evidence from a published
    Compiled Page without changing the citation contract shared by clients.
    ``page_number`` carries an exact document page when the converter supplies
    one (e.g. a PDF page). It is never invented: when absent, the citation
    falls back to a stable ``section_title`` instead (#35).
    """

    page_title: str
    relative_path: str
    source: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    origin: str = "knowledge_base"
    section_title: str | None = None
    page_number: int | None = None


class RetrievalResult(msgspec.Struct, frozen=True):
    """A citation-ready retrieval result."""

    evidence: Evidence
    citation: Citation
    snippet: str
    score: float
    reason: str
    trace: RetrievalTrace


class PageSearchResult(msgspec.Struct, frozen=True):
    """A deterministic, page-oriented lexical search result."""

    page: CompiledPage
    score: float
    matched_fields: list[str] = msgspec.field(default_factory=list)
    matched_terms: list[str] = msgspec.field(default_factory=list)
    snippet: str = ""


# ---------------------------------------------------------------------------
# Link-candidate finder (issue #90, ADR-0011).
#
# A LinkCandidate is a deterministic, model-free proposal for a MISSING
# authored link: a known Canonical Page Title or Alias mentioned in a Compiled
# Page body without being inside a Markdown link, wikilink, or inline-code
# span. It is strictly non-canonical: it never enters the Discovery Graph and
# never becomes an Extracted Reference until a Maintainer approves it and
# publishes the resulting Markdown proposal (#91). Once published, the link is
# derived as an Extracted Reference by the shared resolver on the next graph
# derivation, without a second approval step.
# ---------------------------------------------------------------------------


class LinkCandidate(msgspec.Struct, frozen=True):
    """A deterministic, non-canonical proposed authored link.

    Emitted when a known Canonical Page Title or Alias appears as an unlinked
    mention in a Compiled Page body. ``term`` is the registered title or alias
    (original casing) that resolved to ``target``; ``line``/``column`` are
    1-based and ``line`` is offset by the page's ``body_start_line`` so it maps
    to the real source file. ``snippet`` carries enough surrounding context to
    review. A candidate is never canonical and never Evidence.
    """

    source_path: str
    source_title: str
    target_path: str
    target_title: str
    term: str
    line: int
    column: int
    snippet: str = ""


class HealthReport(msgspec.Struct, frozen=True):
    """Deterministic, zero-LLM summary of Knowledge Base structural health."""

    stale_index: bool = False
    missing_summaries: list[str] = msgspec.field(default_factory=list)
    broken_relationships: list[str] = msgspec.field(default_factory=list)
    duplicate_aliases: list[str] = msgspec.field(default_factory=list)
    duplicate_titles: list[str] = msgspec.field(default_factory=list)
    invalid_fields: list[str] = msgspec.field(default_factory=list)
    unknown_relationship_types: list[str] = msgspec.field(default_factory=list)
    is_healthy: bool = True


# ---------------------------------------------------------------------------
# Materialized Discovery Graph (issue #108, ADR-0011; issue #170, ADR-0021).
#
# The Discovery Graph adjacency is materialized as a versioned MessagePack
# artifact in the derived index directory so startup can skip re-extraction
# when the Knowledge Base fingerprint and extractor version are unchanged.
# ``GraphState`` is the in-memory representation loaded from the artifact or
# derived directly; ``GraphHealthReport`` is the aggregate observability
# surface. Neither exposes MessagePack layout.
#
# Since issue #170 the graph is keyed by stable Entity IDs, not Canonical
# Page Titles: a title or path change does not alter Entity identity or
# traversal topology. Each edge carries its origin (accepted entity Claim or
# Extracted Reference), Claim ID, Predicate, and — for Extracted References —
# the source path/line and extractor version (ADR-0021).
# ---------------------------------------------------------------------------

#: Edge origin: an accepted, entity-to-entity Claim (canonical Knowledge Graph).
GRAPH_EDGE_ORIGIN_CLAIM = "claim"
#: Edge origin: an Extracted Reference (non-canonical discovery topology).
GRAPH_EDGE_ORIGIN_EXTRACTED = "extracted-reference"
GRAPH_EDGE_ORIGINS = frozenset({GRAPH_EDGE_ORIGIN_CLAIM, GRAPH_EDGE_ORIGIN_EXTRACTED})


class GraphEdge(msgspec.Struct, frozen=True):
    """One directed edge in the materialized graph.

    ``endpoint`` is the neighboring node's graph key: the stable Entity ID,
    or — only for pages in Legacy Flat Mode that declare no Entity ID — the
    Canonical Page Title. ``origin`` discloses whether the edge is an
    accepted entity-to-entity Claim (``predicate``/``claim_id`` set) or a
    non-canonical Extracted Reference (``source_path``/``line_start``/
    ``line_end``/``extractor_version`` provenance set). Extracted References
    never carry a Claim identity and are never promoted to Claims.
    """

    endpoint: str
    predicate: str = ""
    claim_id: str = ""
    origin: str = GRAPH_EDGE_ORIGIN_CLAIM
    source_path: str = ""
    line_start: int = 0
    line_end: int = 0
    extractor_version: str = ""

    @property
    def sort_key(self) -> tuple[str, str, str, str, str, int, int, str]:
        """Deterministic edge ordering key (endpoint, then origin metadata)."""
        return (
            self.endpoint,
            self.predicate,
            self.claim_id,
            self.origin,
            self.source_path,
            self.line_start,
            self.line_end,
            self.extractor_version,
        )


class GraphState(msgspec.Struct, frozen=True):
    """Materialized Discovery Graph adjacency (loaded from artifact or derived).

    Carries the versioned, fingerprint-bound discovery adjacency in both
    outgoing and incoming directions, keyed by stable Entity IDs (Canonical
    Page Titles are graph keys only for Legacy Flat Mode pages without an
    Entity ID). Edges are :class:`GraphEdge` records disclosing Claim ID,
    Predicate, origin, and Extracted Reference provenance; extracted
    references carry an empty predicate and claim ID. Built deterministically
    from a ``_KnowledgeIndex`` and rebuildable byte-identically from unchanged
    Markdown.
    """

    version: int
    fingerprint_digest: str
    extractor_version: str
    outgoing: dict[str, list[GraphEdge]]
    incoming: dict[str, list[GraphEdge]]
    edge_count: int


class GraphHealthReport(msgspec.Struct, frozen=True):
    """Aggregate health + observability signals for the Discovery Graph.

    Reports whether the persisted graph artifact is fresh, whether it is
    materialized, the discovery edge count, the materialized artifact size in
    bytes, an observable graph startup time, and a bounded traversal latency
    signal. Does NOT expose MessagePack layout, raw adjacency, or internal
    artifact keys. Timing fields are observability signals, not correctness.
    """

    graph_fresh: bool = False
    materialized: bool = False
    edge_count: int = 0
    materialized_size_bytes: int | None = None
    startup_ms: int = 0
    traversal_latency_ms: int | None = None
    fingerprint_digest: str = ""


# ---------------------------------------------------------------------------
# Structural graph diagnostics (issue #126, ADR-0011).
#
# DISTINCT from the artifact/runtime observability in ``GraphHealthReport``
# above (graph_fresh, materialization, startup_ms, traversal_latency_ms).
# This surface reports STRUCTURAL TOPOLOGY over the in-memory graph for
# EITHER scope (canonical or discovery): orphan counts, weakly connected
# components, and directed hubs. It is derived, read-only analysis and never
# changes graph ownership, persistence, retrieval semantics, or the Evidence
# contract. No Compiled Page body text is ever embedded.
# ---------------------------------------------------------------------------


class GraphHub(msgspec.Struct, frozen=True):
    """One directed inbound/outbound hub entry in a structural report.

    A human-readable Canonical Page Title paired with its stable Entity ID
    (issue #170) and its directed edge count for the report's scope. Hub
    status is navigational topology, never a Relationship semantic claim: a
    high inbound count means many edges point here, not that the page is
    semantically central.
    """

    title: str
    edge_count: int
    entity_id: str = ""


class UnresolvedReferenceSample(msgspec.Struct, frozen=True):
    """Bounded location detail for one unresolved reference in a group.

    Carries the actionable source location (path + line) so a Maintainer can
    find the link to repair. Full diagnostic detail (the kind text, the raw
    target, surrounding context) remains available through the existing
    ``extraction_diagnostics`` seam; this sample is a bounded, deterministic
    representative subset for aggregate overviews.
    """

    source_path: str
    line_start: int


class UnresolvedReferenceGroup(msgspec.Struct, frozen=True):
    """Aggregate count of one unresolved-reference (outcome, target) pair.

    Groups the existing extraction diagnostics by outcome kind and target so a
    Maintainer can spot REPEATED missing targets (strong page/link-repair
    signals) without scanning raw diagnostics. ``count`` is the number of
    diagnostics with this (outcome, target); ``samples`` is a bounded,
    deterministic representative subset. No universal health threshold is
    applied: a high count is a signal to inspect, not a correctness failure.
    """

    outcome: str
    target: str
    count: int
    samples: tuple[UnresolvedReferenceSample, ...] = ()


class StructuralGraphReport(msgspec.Struct, frozen=True):
    """Deterministic, read-only structural topology diagnostics for one scope.

    Derived over the in-memory graph for EITHER the canonical graph (reviewed
    typed Relationships only) or the Discovery Graph (Relationships PLUS
    Extracted References). This is STRUCTURAL topology analysis, distinct
    from the artifact/runtime observability in :class:`GraphHealthReport`.

    Orphan counts and hubs are DIRECTION-AWARE (inbound vs outbound) and use
    only directed-safe concepts. Weakly connected components and
    largest-component coverage use an UNDIRECTED projection of the directed
    edges: ``undirected_projection_used`` discloses this, and the projection
    is NEVER presented as Relationship semantics.

    Large Knowledge Bases return aggregate counts plus bounded,
    deterministically ordered representative samples (hubs, orphans,
    unresolved references). Callers retain access to complete actionable
    details: unresolved-reference locations through ``extraction_diagnostics``.
    No fixed universal health threshold (e.g. "N edges per page") is encoded
    as a correctness rule. No Compiled Page body text is embedded.
    """

    scope: str
    page_count: int
    edge_count: int
    inbound_orphan_count: int
    outbound_orphan_count: int
    weakly_connected_component_count: int
    largest_component_coverage: float
    undirected_projection_used: bool
    top_inbound_hubs: tuple[GraphHub, ...] = ()
    top_outbound_hubs: tuple[GraphHub, ...] = ()
    unresolved_references: tuple[UnresolvedReferenceGroup, ...] = ()
    inbound_orphan_sample_titles: tuple[str, ...] = ()
    outbound_orphan_sample_titles: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Link-candidate graph-impact ranking (issue #127, ADR-0011).
#
# An advisory, read-only analysis that annotates each EXISTING deterministic
# LinkCandidate with the structural improvement its approved Markdown link
# would make to the Discovery Graph. It evaluates each candidate against the
# current authorized graph WITHOUT mutating Compiled Pages or graph state.
# A candidate never becomes an Extracted Reference, Relationship, Evidence, or
# published change before the existing stage -> validate -> review -> publish
# workflow succeeds. Approved links remain Extracted References with
# navigation meaning only — Canonical Relationship impact is NEVER inferred
# from a Markdown-link proposal (ADR-0011).
# ---------------------------------------------------------------------------

#: Impact signal: the link would give an inbound connection to a page that is
#: currently a Discovery Graph orphan (zero inbound edges).
LINK_IMPACT_KIND_ORPHAN_REPAIR = "orphan_repair"

#: Impact signal: the link would connect two weakly connected components,
#: reducing the WCC count.
LINK_IMPACT_KIND_COMPONENT_JOIN = "component_join"

#: Impact signal: the link would strengthen an existing but indirect (fragile)
#: connection by adding a direct edge where none currently exists.
LINK_IMPACT_KIND_FRAGILE_STRENGTHENING = "fragile_strengthening"


class LinkImpactSignal(msgspec.Struct, frozen=True):
    """One documented structural-impact signal for a proposed link.

    ``kind`` is a stable identifier from the ``LINK_IMPACT_KIND_*`` constants.
    ``detail`` is a human-readable explanation naming the affected topology.
    Signals are advisory structural topology only; they never infer a
    Relationship semantic, never mutate graph state, and never publish a link.
    """

    kind: str
    detail: str


class RankedLinkCandidate(msgspec.Struct, frozen=True):
    """A LinkCandidate annotated with structural Discovery Graph impact.

    Carries the original :class:`LinkCandidate` (which itself holds the source
    path, line, column, snippet, and target identity), the graph scope the
    impact was evaluated against, and the documented impact signals.
    ``impact_score`` is an explicit, stable integer used for deterministic
    ordering (higher = more structural improvement). A candidate with no
    measured structural impact has an empty ``signals`` tuple and
    ``impact_score == 0`` and is still returned so a Maintainer can inspect
    ALL candidates.

    The impact is ADVISORY: it does not mutate Compiled Pages or graph state
    and never infers a Relationship. An approved link becomes an Extracted
    Reference with navigation meaning only (ADR-0011).
    """

    candidate: LinkCandidate
    scope: str
    impact_score: int
    signals: tuple[LinkImpactSignal, ...] = ()


# ---------------------------------------------------------------------------
# Knowledge Base Control File and reserved published artifacts (issue #77).
#
# A categorized Knowledge Base carries a versioned root Control File
# (``lumio.yaml``) that declares KB-local Content Categories and Maintainer-
# pinned Hot Index titles. It is portable with the KB but is neither a Compiled
# Page nor an OKF concept: it is a Lumio-native content control. Publication
# regenerates the reserved Navigation Index hierarchy and Hot Index and appends
# an append-only Activity Log entry. See ADR-0008.
# ---------------------------------------------------------------------------


class ContentCategory(msgspec.Struct, frozen=True):
    """A KB-local navigation category declared by the Control File.

    Category is broad navigation routing; a Compiled Page's free-form ``type``
    is specific semantics. The seeded catalog lives in
    :func:`lumio_wiki.knowledge_base.SEED_CATEGORY_CATALOG`.
    """

    name: str
    description: str | None = None


class HotIndexPin(msgspec.Struct, frozen=True):
    """A Maintainer-pinned Hot Index entry, expressed by Canonical Page Title."""

    title: str
    note: str | None = None


class KnowledgeBaseControlFile(msgspec.Struct, frozen=True):
    """The versioned root Knowledge Base Control File (``lumio.yaml``).

    Carries the controlled Content Category catalog, Maintainer-pinned Hot
    Index titles, and (version 2) the controlled Entity/Claim ontology
    (ADR-0021). Travels with the KB; validated as a KB-local content control,
    not a Compiled Page or an application-only setting. A Knowledge Base with
    no Control File loads in Legacy Flat Mode (ADR-0008).
    """

    version: int
    categories: list[ContentCategory] = msgspec.field(default_factory=list)
    hot_index: list[HotIndexPin] = msgspec.field(default_factory=list)
    mode: str = "categorized"
    path: str = "lumio.yaml"
    ontology: Ontology | None = None


class ActivityLogEntry(msgspec.Struct, frozen=True):
    """One grep-friendly Activity Log line recording a published KB transition.

    The portable Activity Log (reserved ``log.md``) records only successful
    published Knowledge Base state transitions. It never carries Reader
    queries, failed or discarded proposals, unpublished uploads, or private
    audit events (ADR-0008).
    """

    timestamp: str
    operation: str
    description: str
