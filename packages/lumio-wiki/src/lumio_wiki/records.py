"""Domain records for the Lumio Core SDK."""

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
    """A typed, directed edge to another Compiled Page."""

    target: str = ""
    type: str = ""


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
    """A loaded Markdown page from a Knowledge Base."""

    path: str
    title: str = ""
    aliases: list[str] = msgspec.field(default_factory=list)
    tags: list[str] = msgspec.field(default_factory=list)
    summary: str | None = None
    lifecycle: str | None = None
    visibility: str | None = None
    sources: list[Source] = msgspec.field(default_factory=list)
    relationships: list[Relationship] = msgspec.field(default_factory=list)
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
# Materialized Discovery Graph (issue #108, ADR-0011).
#
# The Discovery Graph adjacency is materialized as a versioned MessagePack
# artifact in the derived index directory so startup can skip re-extraction
# when the Knowledge Base fingerprint and extractor version are unchanged.
# ``GraphState`` is the in-memory representation loaded from the artifact or
# derived directly; ``GraphHealthReport`` is the aggregate observability
# surface. Neither exposes MessagePack layout.
# ---------------------------------------------------------------------------


class GraphState(msgspec.Struct, frozen=True):
    """Materialized Discovery Graph adjacency (loaded from artifact or derived).

    Carries the versioned, fingerprint-bound discovery adjacency in both
    outgoing and incoming directions. Edges are ``(endpoint, type)`` tuples;
    extracted references carry an empty type. Built deterministically from a
    ``_KnowledgeIndex`` and rebuildable byte-identically from unchanged
    Markdown.
    """

    version: int
    fingerprint_digest: str
    extractor_version: str
    outgoing: dict[str, list[tuple[str, str]]]
    incoming: dict[str, list[tuple[str, str]]]
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

    A Canonical Page Title paired with its directed edge count for the
    report's scope. Hub status is navigational topology, never a
    Relationship semantic claim: a high inbound count means many edges point
    here, not that the page is semantically central.
    """

    title: str
    edge_count: int


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
    samples: list[UnresolvedReferenceSample] = msgspec.field(default_factory=list)


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
    top_inbound_hubs: list[GraphHub] = msgspec.field(default_factory=list)
    top_outbound_hubs: list[GraphHub] = msgspec.field(default_factory=list)
    unresolved_references: list[UnresolvedReferenceGroup] = msgspec.field(default_factory=list)
    inbound_orphan_sample_titles: list[str] = msgspec.field(default_factory=list)
    outbound_orphan_sample_titles: list[str] = msgspec.field(default_factory=list)

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

    Carries the controlled Content Category catalog and Maintainer-pinned Hot
    Index titles. Travels with the KB; validated as a KB-local content control,
    not a Compiled Page or an application-only setting. A Knowledge Base with
    no Control File loads in Legacy Flat Mode (ADR-0008).
    """

    version: int
    categories: list[ContentCategory] = msgspec.field(default_factory=list)
    hot_index: list[HotIndexPin] = msgspec.field(default_factory=list)
    mode: str = "categorized"
    path: str = "lumio.yaml"


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
