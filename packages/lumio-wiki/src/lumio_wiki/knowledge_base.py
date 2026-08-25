"""Knowledge Base loading, validation, indexing, and retrieval."""

import hashlib
import heapq
import os
import re
import tempfile
import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from datetime import UTC, date as _date_cls, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote

import msgspec
import msgspec.yaml as yaml

from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    Embedder,
    RetrievalMode,
)
from lumio_wiki.fingerprint_store import load_stored_fingerprint
from lumio_wiki.graph_state import (
    GRAPH_ARTIFACT_FILENAME,
    build_graph_state,
    load_graph_artifact,
    write_graph_artifact,
)
from lumio_wiki.page_search import search_pages as search_pages_over
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    CLAIM_ORIGIN_AUTHORED,
    CLAIM_ORIGINS,
    EXTRACTOR_VERSION,
    LITERAL_KINDS,
    LITERAL_KIND_BOOLEAN,
    LITERAL_KIND_NUMBER,
    LITERAL_KIND_STRING,
    LINK_IMPACT_KIND_COMPONENT_JOIN,
    LINK_IMPACT_KIND_FRAGILE_STRENGTHENING,
    LINK_IMPACT_KIND_ORPHAN_REPAIR,
    PROPOSAL_ONLY_CLAIM_STATUSES,
    PUBLISHED_CLAIM_STATUSES,
    ActivityLogEntry,
    Claim,
    ClaimEvidence,
    CompiledPage,
    ContentCategory,
    ENTITY_MATCH_ALIAS,
    ENTITY_MATCH_CANONICAL_TITLE,
    ENTITY_MATCH_ENTITY_ID,
    ENTITY_MATCH_REDIRECT,
    Entity,
    EntityRedirect,
    EntityResolution,
    EntityTypeDefinition,
    ExtractedReference,
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    GRAPH_EDGE_SCOPE_CANONICAL,
    GRAPH_EDGE_SCOPE_DISCOVERY,
    GraphEdge,
    GraphHealthReport,
    GraphHub,
    GraphState,
    HealthReport,
    HotIndexPin,
    KnowledgeBaseControlFile,
    LinkCandidate,
    LinkImpactSignal,
    Ontology,
    PageSearchResult,
    PredicateDefinition,
    RankedLinkCandidate,
    RegistryEntry,
    Relationship,
    RetrievalResult,
    RetrievalTrace,
    Source,
    SourceFileDigest,
    SourceFingerprint,
    StructuralGraphReport,
    TraceStage,
    UnresolvedReferenceGroup,
    UnresolvedReferenceSample,
    ValidationIssue,
    ValidationReport,
)
from lumio_wiki.retrieval import RetrievalAdapter, default_retrieval_adapter

VALID_LIFECYCLES = {"draft", "review", "approved", "deprecated"}
VALID_VISIBILITIES = {"public", "internal", "restricted"}
# Entity/Claim identity contracts (ADR-0021, issue #168). Entity IDs and
# Claim IDs are ``entity:``/``claim:`` prefixed slugs; titles and paths are
# not semantic identity.
ENTITY_ID_PREFIX = "entity:"
CLAIM_ID_PREFIX = "claim:"
IDENTIFIER_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")
# The canonical graph vocabulary is Control-File ontology data since
# ADR-0021; title-based Relationship frontmatter input is removed.

# ---------------------------------------------------------------------------
# Public graph traversal seam (issue #106, ADR-0011) and Discovery Graph
# (issue #107).
#
# The canonical graph contains only reviewed, typed Relationships resolved by
# Canonical Page Title. The Discovery Graph additionally contains Extracted
# References: deterministic, non-canonical directed references derived from
# exactly-resolved internal Markdown links and unambiguous wikilinks in
# Compiled Page bodies (ADR-0011). Traversal is direction-aware, cycle-safe,
# bounded by explicit depth / expanded-edge / result limits, and restricted to
# a caller-supplied authorized candidate page set so endpoint resolution and
# expansion can never surface a page the caller has no visibility to. The
# ``scope`` parameter selects ``canonical`` (Relationships only) or
# ``discovery`` (Relationships plus Extracted References).
# ---------------------------------------------------------------------------
GRAPH_SCOPE_CANONICAL = "canonical"
GRAPH_SCOPE_DISCOVERY = "discovery"
GRAPH_SCOPE_VALUES = frozenset({GRAPH_SCOPE_CANONICAL, GRAPH_SCOPE_DISCOVERY})
GRAPH_DIRECTION_OUTGOING = "outgoing"
GRAPH_DIRECTION_INCOMING = "incoming"
GRAPH_DIRECTION_BOTH = "both"
GRAPH_DIRECTIONS = frozenset(
    {GRAPH_DIRECTION_OUTGOING, GRAPH_DIRECTION_INCOMING, GRAPH_DIRECTION_BOTH}
)
# Default bounds keep traversal deterministic and terminating without caller
# tuning. Depth is measured in Relationship hops from the start title.
DEFAULT_GRAPH_MAX_DEPTH = 5
DEFAULT_GRAPH_MAX_EDGES = 1000
DEFAULT_GRAPH_MAX_RESULTS = 50
# Default bounds for structural-diagnostic REPRESENTATIVE SAMPLES (issue
# #126). These cap payload size for large Knowledge Bases; they are NOT
# health thresholds. Callers retain access to complete actionable details
# via ``extraction_diagnostics``.
DEFAULT_GRAPH_HUB_SAMPLE = 10
DEFAULT_GRAPH_ORPHAN_SAMPLE = 25
DEFAULT_GRAPH_UNRESOLVED_SAMPLE = 5
# Link-candidate graph-impact score weights (issue #127, ADR-0011).
# Explicit, documented, stable integers. The total impact score for a
# candidate is the sum of applicable signal weights. Higher = more structural
# improvement. Ordering within the same score is lexicographic on the
# candidate's identity fields (stable tie-break).
LINK_IMPACT_WEIGHT_COMPONENT_JOIN = 100
LINK_IMPACT_WEIGHT_ORPHAN_REPAIR = 50
LINK_IMPACT_WEIGHT_FRAGILE_STRENGTHENING = 10


def _require_graph_scope(scope: str) -> None:
    """Validate the graph scope.

    Both scopes are implemented (ADR-0011, issue #107):
    ``canonical`` contains only reviewed typed Relationships, and
    ``discovery`` contains canonical Relationships plus Extracted References
    derived from exactly-resolved internal links. An unknown value raises
    ``ValueError``.
    """
    if scope not in GRAPH_SCOPE_VALUES:
        raise ValueError(
            f"unknown graph scope {scope!r}; expected one of {sorted(GRAPH_SCOPE_VALUES)}"
        )


def _require_graph_direction(direction: str) -> None:
    """Validate the traversal direction value."""
    if direction not in GRAPH_DIRECTIONS:
        raise ValueError(
            f"unknown graph direction {direction!r}; expected one of {sorted(GRAPH_DIRECTIONS)}"
        )


def _require_non_negative_limit(name: str, value: int) -> None:
    """Validate that a graph traversal limit is a non-negative integer."""
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value}")


class _GraphTopology(NamedTuple):
    """Authorized directed-graph topology snapshot for structural analysis.

    Computed once over the authorized node set and shared by
    ``graph_diagnostics`` and ``rank_link_candidates_by_graph_impact`` so the
    degree, undirected-projection, and WCC logic has a single source of
    truth. Self-loops carry no structural connectivity and are excluded.
    """

    in_degree: dict[str, int]
    out_degree: dict[str, int]
    out_endpoints: dict[str, set[str]]
    edge_count: int
    wcc_id: dict[str, int]
    wcc_count: int
    largest_wcc_size: int


class _GraphExpansion(NamedTuple):
    """Graph-expansion output for retrieval: eligible pages + trace metadata.

    Issue #172: alongside the eligible Compiled Pages the expansion records
    the claim-aware metadata a truthful Retrieval Trace discloses — resolved
    seed Entity IDs, unresolved seed names, traversed Claim IDs/Predicates,
    and the edge origins actually followed (accepted Claims and/or Extracted
    References). The lifecycle filter is implicit and disclosed as
    ``accepted``-only: disputed/superseded Claims never enter adjacency.
    """

    pages: list
    resolved_seed_keys: list[str]
    unresolved_seeds: list[str]
    traversed_claim_ids: list[str]
    traversed_predicates: list[str]
    traversed_origins: list[str]


def _compute_graph_topology(
    outgoing: dict[str, list[GraphEdge]],
    nodes: frozenset[str],
) -> _GraphTopology:
    """Compute directed degrees, undirected projection, and WCC assignment.

    The single shared implementation for both ``graph_diagnostics`` and
    ``rank_link_candidates_by_graph_impact``. Iterates authorized nodes in
    sorted order for deterministic results. Nodes and endpoints are graph
    keys (stable Entity IDs; issue #170).
    """
    in_degree: dict[str, int] = {t: 0 for t in nodes}
    out_degree: dict[str, int] = {t: 0 for t in nodes}
    out_endpoints: dict[str, set[str]] = {t: set() for t in nodes}
    undirected: dict[str, set[str]] = {t: set() for t in nodes}
    edge_count = 0
    for src in sorted(nodes):
        for edge in outgoing.get(src, ()):
            endpoint = edge.endpoint
            if endpoint == src or endpoint not in nodes:
                continue
            edge_count += 1
            out_degree[src] += 1
            out_endpoints[src].add(endpoint)
            in_degree[endpoint] += 1
            undirected[src].add(endpoint)
            undirected[endpoint].add(src)

    wcc_id: dict[str, int] = {}
    wcc_count = 0
    largest = 0
    for start in sorted(nodes):
        if start in wcc_id:
            continue
        wcc_id[start] = wcc_count
        size = 0
        queue: deque[str] = deque([start])
        while queue:
            node = queue.popleft()
            size += 1
            for neighbour in undirected[node]:
                if neighbour not in wcc_id:
                    wcc_id[neighbour] = wcc_count
                    queue.append(neighbour)
        wcc_count += 1
        if size > largest:
            largest = size

    return _GraphTopology(
        in_degree=in_degree,
        out_degree=out_degree,
        out_endpoints=out_endpoints,
        edge_count=edge_count,
        wcc_id=wcc_id,
        wcc_count=wcc_count,
        largest_wcc_size=largest,
    )


# Navigation Index reserved derived-artifact semantics (issue #64).
#
# The case-insensitive basename ``index.md`` is reserved in every Knowledge Base
# directory. A file with that basename is a Navigation Index only when its
# frontmatter declares a valid marker:
#
#     lumio:
#       artifact: navigation-index
#       version: 1
#
# A valid marked Navigation Index is a derived artifact: it is excluded from the
# loaded Compiled Page collection, registry, graph, retrieval, Evidence, and the
# canonical content fingerprint. An unmarked (or malformed-marker) collision on
# the reserved basename is a blocking validation error rather than being silently
# ignored or overwritten.
NAV_INDEX_BASENAME = "index.md"
NAV_INDEX_ARTIFACT = "navigation-index"
# The format version Navigation Index generation emits (issue #65). Kept in
# ``SUPPORTED_NAV_INDEX_VERSIONS`` so generated indexes are always recognized.
NAV_INDEX_VERSION = 1
SUPPORTED_NAV_INDEX_VERSIONS = frozenset({NAV_INDEX_VERSION})

# ---------------------------------------------------------------------------
# Knowledge Base Control File and reserved published artifacts (issue #77).
#
# A categorized Knowledge Base carries a versioned root Control File
# (``lumio.yaml``) declaring KB-local Content Categories and Maintainer-pinned
# Hot Index titles. Publication regenerates the Navigation Index hierarchy and
# the Hot Index, and appends an append-only Activity Log entry recording the
# successful transition. These are intentional Lumio OKF extensions: marked
# reserved artifacts, consistent with the Navigation Index marker system, rather
# than strict frontmatter-free OKF reserved-file syntax. See ADR-0008.
# ---------------------------------------------------------------------------

# The root Control File. It is portable with the KB but is neither a Compiled
# Page (not Markdown) nor an OKF concept. It is canonical KB content and is
# fingerprinted alongside Compiled Pages.
CONTROL_FILE_BASENAME = "lumio.yaml"
CONTROL_FILE_VERSION = 2
SUPPORTED_CONTROL_FILE_VERSIONS = frozenset({CONTROL_FILE_VERSION})
KB_MODE_CATEGORIZED = "categorized"
KB_MODE_LEGACY = "legacy-flat"
# Mode values a *present* Control File may declare. Legacy Flat Mode is
# represented by the absence of a Control File (see ``KB_MODE_LEGACY``), so a
# present Control File must declare ``categorized``; declaring ``legacy-flat``
# is contradictory and is rejected as a blocking error (ADR-0008, issue #77).
SUPPORTED_CONTROL_FILE_MODES = frozenset({KB_MODE_CATEGORIZED})

# The seeded Content Category catalog a new categorized Knowledge Base adopts.
# The distiller may route only to configured categories; Maintainers propose
# catalog changes through the normal review-and-publish workflow (issue #76).
SEED_CATEGORY_CATALOG: list[ContentCategory] = [
    ContentCategory(name="concepts", description="Core ideas, definitions, and mental models."),
    ContentCategory(
        name="entities",
        description="Named people, organizations, products, tools, projects, and standards.",
    ),
    ContentCategory(
        name="references",
        description="Reference landing pages for independently useful Knowledge Sources.",
    ),
    ContentCategory(name="procedures", description="Runbooks, methods, and checklists."),
    ContentCategory(
        name="tables", description="Documentation for database and warehouse table relations."
    ),
    ContentCategory(
        name="datasets",
        description="Documentation for file, extract, collection, and data-lake assets.",
    ),
    ContentCategory(
        name="synthesis", description="Cross-source conclusions and synthesized knowledge."
    ),
]

# ---------------------------------------------------------------------------
# Extensible Content Category catalog (ADR-0009, issue #84).
#
# The category catalog is data, not a fixed enum: a KB's Control File may
# declare additional slug-valid categories beyond the seed. When the
# declaration is absent or empty the Core SDK applies ``SEED_CATEGORY_CATALOG``
# as the default. Declared category names must:
#
#   * be valid slugs — lowercase ASCII letters, digits, and hyphens;
#     beginning with a letter; 1..64 chars (``CATEGORY_SLUG_RE``);
#   * be unique within the catalog (checked in the loader); and
#   * not collide with reserved basenames/markers (``RESERVED_CATEGORY_NAMES``).
#
# Adding, renaming, or removing a category is an explicit, reviewed Maintainer
# action expressed as a Control File change — never automatic (ADR-0009).
# ---------------------------------------------------------------------------
CATEGORY_MAX_LENGTH = 64
CATEGORY_SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
# Category names that collide with reserved derived-artifact basenames
# (``index``, ``hot``, ``log``) or the ``lumio`` marker system. A category
# with one of these names would shadow a reserved path or marker and is
# rejected (ADR-0007 and ADR-0009).
RESERVED_CATEGORY_NAMES = frozenset({"index", "hot", "log", "lumio"})

# Reserved Activity Log artifact: append-only portable history of successful
# published Knowledge Base state transitions. Never regenerated or pruned; only
# appended to after a publish succeeds. Excluded from Compiled Page loading,
# retrieval, and canonical fingerprinting like other marked derived artifacts.
ACTIVITY_LOG_BASENAME = "log.md"
ACTIVITY_LOG_ARTIFACT = "activity-log"
ACTIVITY_LOG_VERSION = 1
SUPPORTED_ACTIVITY_LOG_VERSIONS = frozenset({ACTIVITY_LOG_VERSION})

# Reserved Hot Index artifact: a regenerated, Maintainer-pinned navigation
# surface. Generated from the Control File's pinned titles and the current
# Compiled Pages on every publish/sync. Excluded from loading, retrieval, and
# fingerprinting like other marked derived artifacts.
HOT_INDEX_BASENAME = "hot.md"
HOT_INDEX_ARTIFACT = "hot-index"
HOT_INDEX_VERSION = 1
SUPPORTED_HOT_INDEX_VERSIONS = frozenset({HOT_INDEX_VERSION})

# Maps each reserved derived-artifact basename to the marker artifact it must
# carry. The loader, validator, and fingerprint use this single registry so the
# reserved-artifact contract has one source of truth (ADR-0007 and ADR-0008).
RESERVED_ARTIFACT_MARKERS: dict[str, str] = {
    NAV_INDEX_BASENAME: NAV_INDEX_ARTIFACT,
    ACTIVITY_LOG_BASENAME: ACTIVITY_LOG_ARTIFACT,
    HOT_INDEX_BASENAME: HOT_INDEX_ARTIFACT,
}
# Supported marker versions per artifact kind.
RESERVED_ARTIFACT_VERSIONS: dict[str, frozenset[int]] = {
    NAV_INDEX_ARTIFACT: SUPPORTED_NAV_INDEX_VERSIONS,
    ACTIVITY_LOG_ARTIFACT: SUPPORTED_ACTIVITY_LOG_VERSIONS,
    HOT_INDEX_ARTIFACT: SUPPORTED_HOT_INDEX_VERSIONS,
}
# Human-readable labels for diagnostics, keyed by marker artifact value.
RESERVED_ARTIFACT_LABELS: dict[str, str] = {
    NAV_INDEX_ARTIFACT: "Navigation Index",
    ACTIVITY_LOG_ARTIFACT: "Activity Log",
    HOT_INDEX_ARTIFACT: "Hot Index",
}


class KnowledgeBase(msgspec.Struct, frozen=True):
    """A loaded Knowledge Base."""

    root: Path
    pages: list[CompiledPage] = msgspec.field(default_factory=list)
    index_dir: Path | None = None
    control: KnowledgeBaseControlFile | None = None
    retrieval: Any | None = None

    def _knowledge_index(self) -> _KnowledgeIndex:  # noqa: F821
        return _KnowledgeIndex(self.pages)

    def _retrieval_adapter(self) -> RetrievalAdapter:
        """Return the bound adapter, or the always-available zero-index default."""
        if self.retrieval is None:
            return default_retrieval_adapter()
        return self.retrieval

    def lookup_by_title(self, title: str) -> list[CompiledPage]:
        """Return pages whose Canonical Page Title matches ``title``."""
        return self._knowledge_index().by_title.get(title, [])

    def lookup_by_alias(self, alias: str) -> list[CompiledPage]:
        """Return pages that declare ``alias`` as an alternate lookup phrase."""
        return self._knowledge_index().by_alias.get(alias, [])

    def lookup_by_tag(self, tag: str) -> list[CompiledPage]:
        """Return pages tagged with ``tag``."""
        return self._knowledge_index().by_tag.get(tag, [])

    def lookup_by_source(self, source_id: str) -> list[CompiledPage]:
        """Return pages whose first Source id matches ``source_id``."""
        return self._knowledge_index().by_source.get(source_id, [])

    def lookup_by_lifecycle(self, lifecycle: str) -> list[CompiledPage]:
        """Return pages in the given Lifecycle state."""
        return self._knowledge_index().by_lifecycle.get(lifecycle, [])

    def public_pages(self) -> list[CompiledPage]:
        """Return the public Compiled Pages (the public export authorization scope).

        This is the Core SDK authorization selection for the OKF Exchange
        Profile 1 public scope. The export serializer receives this
        already-authorized set and cannot widen it: a full export occurs only
        when the caller explicitly supplies every visibility class it intends
        to export. See ADR-0007 and ``docs/research/okf-comparison.md``.
        """
        return [page for page in self.pages if (page.visibility or "") == "public"]

    def search_pages(
        self,
        query: str,
        limit: int = 20,
        pages: Sequence[CompiledPage] | None = None,
    ) -> list[PageSearchResult]:
        """Search page metadata and body text with deterministic lexical ranking.

        ``pages`` is an optional already-authorized candidate set. Clients that
        enforce Visibility should filter before calling this method so an
        unauthorized page cannot influence result counts, ranking, or snippets.
        """
        candidates = self.pages if pages is None else list(pages)
        return search_pages_over(candidates, query, limit)

    def entities(self) -> list[Entity]:
        """Return the Entity identity/type projection of every page (ADR-0021).

        One Entity per Compiled Page, in page order; pages without an Entity
        contract (Legacy Flat Mode) are skipped.
        """
        return [
            Entity(
                id=page.id,
                entity_types=list(page.entity_types),
                title=page.title,
                aliases=list(page.aliases),
                path=page.path,
            )
            for page in self.pages
            if page.id
        ]

    def resolve_entity(self, name: str) -> EntityResolution:
        """Resolve one stable Entity from an exact ID, title, or alias (#172).

        Deterministic priority: exact Entity ID, then exact Canonical Page
        Title, then exact alias. A name claimed by several pages (a shared
        alias) returns a truthful ambiguity — ``entity`` is ``None`` and
        ``candidates`` carries every claimant — never a guess. Unknown names
        and Legacy Flat Mode pages (no Entity contract) resolve to nothing.
        Resolution is read-only: it never authors, merges, accepts,
        disputes, or supersedes a Claim.
        """
        index = self._knowledge_index()

        def entity_of(page: CompiledPage) -> Entity:
            return Entity(
                id=page.id,
                entity_types=list(page.entity_types),
                title=page.title,
                aliases=list(page.aliases),
                path=page.path,
            )

        key = name.strip()
        if not key:
            return EntityResolution()

        page = index.by_entity_id.get(key)
        if page is not None:
            return EntityResolution(entity=entity_of(page), matched_by=ENTITY_MATCH_ENTITY_ID)

        # Retired Entity IDs stay resolvable through ontology redirects
        # (ADR-0021): a reviewed Entity Merge records the retired ID as a
        # redirect, so it resolves to the surviving Entity rather than
        # "not found". Validation guarantees acyclicity; the seen-set is a
        # cheap defense against invalid hand-edited Control Files.
        ontology = self.control.ontology if self.control is not None else None
        redirect_map = {r.from_id: r.to_id for r in (ontology.redirects if ontology else ())}
        current = key
        seen = {key}
        while current in redirect_map and redirect_map[current] not in seen:
            current = redirect_map[current]
            seen.add(current)
        if current != key:
            surviving = index.by_entity_id.get(current)
            if surviving is not None:
                return EntityResolution(
                    entity=entity_of(surviving), matched_by=ENTITY_MATCH_REDIRECT
                )

        pages = index.by_title.get(key) or []
        pages = [p for p in pages if p.id]
        if len(pages) == 1:
            return EntityResolution(
                entity=entity_of(pages[0]), matched_by=ENTITY_MATCH_CANONICAL_TITLE
            )
        if len(pages) > 1:
            # Duplicate titles are invalid in categorized mode; report the
            # ambiguity truthfully instead of picking a winner.
            return EntityResolution(
                matched_by=ENTITY_MATCH_CANONICAL_TITLE,
                candidates=[entity_of(p) for p in pages],
            )

        alias_pages = [p for p in (index.by_alias.get(key) or []) if p.id]
        if len(alias_pages) == 1:
            return EntityResolution(entity=entity_of(alias_pages[0]), matched_by=ENTITY_MATCH_ALIAS)
        if len(alias_pages) > 1:
            return EntityResolution(
                matched_by=ENTITY_MATCH_ALIAS,
                candidates=[entity_of(p) for p in alias_pages],
            )
        return EntityResolution()

    def entity_id_for_title(self, title: str) -> str | None:
        """Stable Entity ID owning ``title``, or ``None`` (issue #172).

        The ID-bearing SDK seam for title-returning operations
        (:meth:`related_pages`, :meth:`shortest_path`): map each returned
        Canonical Page Title to its machine-stable Entity ID through this
        public method. ``None`` for unknown titles, ambiguous duplicate
        titles (invalid input, never guessed), and Legacy Flat Mode pages
        (the title is the fallback graph key there).
        """
        resolution = self.resolve_entity(title)
        entity = resolution.entity
        return entity.id if entity is not None else None

    def related_from(self, title: str, relationship_type: str | None = None) -> list[Relationship]:
        """Return canonical edges whose source page has the given Canonical Title.

        Each returned :class:`Relationship` is a derived, title-level view of
        one accepted entity-to-entity Claim (ADR-0021): ``type`` is the Claim
        predicate and ``target`` the object entity's Canonical Page Title.
        """
        relationships: list[Relationship] = []
        seen: set[tuple[str, str]] = set()
        index = self._knowledge_index()
        for page in self.lookup_by_title(title):
            for claim in page.claims:
                if claim.status != "accepted" or claim.object is None:
                    continue
                target = index.by_entity_id.get(claim.object)
                if target is None:
                    continue
                if relationship_type is not None and claim.predicate != relationship_type:
                    continue
                edge = (target.title, claim.predicate)
                if edge in seen:
                    continue
                seen.add(edge)
                relationships.append(Relationship(target=edge[0], type=edge[1]))
        return relationships

    def graph_path(self, source_title: str, target_title: str) -> list[str] | None:
        """Return the shortest directed path from ``source_title`` to ``target_title``.

        Titles are resolved to stable Entity IDs at the module edge
        (issue #170); the search runs over Entity-ID adjacency and the path is
        returned in human-readable Canonical Page Titles. Returns ``None`` if
        no path exists.
        """
        if source_title == target_title:
            return [source_title]

        index = self._knowledge_index()
        source_key = index.graph_key_by_title.get(source_title)
        target_key = index.graph_key_by_title.get(target_title)
        if source_key is None or target_key is None:
            return None
        adjacency = index.adjacency
        if source_key not in adjacency:
            return None

        queue: deque[tuple[str, list[str]]] = deque([(source_key, [source_key])])
        visited = {source_key}

        while queue:
            current, path = queue.popleft()
            for edge in adjacency.get(current, []):
                endpoint = edge.endpoint
                if endpoint == target_key:
                    keys = path + [endpoint]
                    return [index.title_by_graph_key.get(k, k) for k in keys]
                if endpoint not in visited:
                    visited.add(endpoint)
                    queue.append((endpoint, path + [endpoint]))

        return None

    # ------------------------------------------------------------------
    # Public graph traversal seam (issue #106, ADR-0011).
    #
    # ``related_pages`` and ``shortest_path`` expose canonical-graph traversal
    # that is direction-aware, cycle-safe, bounded, and restricted to a
    # caller-supplied authorized candidate page set. They are the seam a later
    # task populates with Extracted References (``scope="discovery"``); the
    # legacy ``graph_path`` and ``related_from`` keep their existing behavior.
    # ------------------------------------------------------------------

    def related_pages(
        self,
        title: str,
        *,
        direction: str = GRAPH_DIRECTION_OUTGOING,
        scope: str = GRAPH_SCOPE_CANONICAL,
        candidate_titles: Iterable[str] | None = None,
        relationship_type: str | None = None,
        max_depth: int = 1,
        max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
        max_results: int = DEFAULT_GRAPH_MAX_RESULTS,
    ) -> list[str]:
        """Return the bounded, authorized Canonical Page Titles related to ``title``.

        ``title`` (a Canonical Page Title or Alias) is resolved to stable
        Entity IDs at the module edge; traversal runs over Entity-ID adjacency
        and results are returned as human-readable Canonical Page Titles —
        titles are labels, never identity (issue #170, ADR-0021).

        Expands graph neighbors breadth-first up to ``max_depth`` hops,
        following ``direction`` (``"outgoing"``, ``"incoming"``, or
        ``"both"``). ``scope`` selects the graph: ``"canonical"`` (default)
        traverses only accepted entity-to-entity Claims (Claim Predicate IDs
        as the relationship type); ``"discovery"`` traverses canonical Claims
        PLUS Extracted References derived from exactly-resolved internal links
        in Compiled Page bodies (ADR-0011, issue #107). In discovery scope,
        equivalent edges (same source -> target) are deduplicated to a single
        endpoint.

        Traversal operates ONLY over ``candidate_titles`` — the
        already-authorized Canonical Page Titles the caller has visibility to.
        Authorization is checked BEFORE graph resolution/expansion: an
        unauthorized page cannot influence result counts, and endpoints are
        only resolved for pages inside the authorized set. When
        ``candidate_titles`` is ``None`` every loaded Compiled Page title is
        the authorized universe. ``title`` itself is never included in the
        result.

        ``max_edges`` bounds the number of edges inspected/expanded and
        ``max_results`` bounds the returned title count; both truncate
        deterministically (results are sorted by Canonical Page Title).
        ``max_depth``, ``max_edges``, and ``max_results`` must all be
        non-negative. When the resolved page is not in the authorized
        candidate set the result is empty.
        """
        _require_graph_scope(scope)
        _require_graph_direction(direction)
        _require_non_negative_limit("max_depth", max_depth)
        _require_non_negative_limit("max_edges", max_edges)
        _require_non_negative_limit("max_results", max_results)
        candidate = self._graph_candidate(candidate_titles)
        seeds = self._authorized_seed_keys(title, candidate)
        if not seeds:
            return []
        allowed = self._graph_allowed_keys(candidate)
        neighbors = self._graph_neighbor_fn(direction, scope)
        title_of = self._knowledge_index().title_by_graph_key

        visited = set(seeds)
        found: set[str] = set()
        frontier: deque[tuple[str, int]] = deque((seed, 0) for seed in sorted(seeds))
        edges_expanded = 0
        while frontier:
            current, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            # Iterate raw graph edges in deterministic order and debit the
            # expanded-edge budget per ELIGIBLE edge inspected (after the
            # relationship-type and candidate-set filters), not per unique
            # endpoint. Parallel edges to the same endpoint each consume the
            # budget; non-candidate / wrong-type / self edges are filtered in
            # O(1) without expanding the graph.
            for edge in neighbors(current):
                if relationship_type is not None and edge.predicate != relationship_type:
                    continue
                if edge.endpoint == current or edge.endpoint not in allowed:
                    continue
                edges_expanded += 1
                if edges_expanded > max_edges:
                    frontier.clear()
                    break
                if edge.endpoint in visited:
                    continue
                visited.add(edge.endpoint)
                found.add(edge.endpoint)
                frontier.append((edge.endpoint, depth + 1))

        # Deterministic truncation: sort all discovered titles, then cap.
        # max_results is validated non-negative above, so the slice always binds.
        return sorted(title_of.get(k, k) for k in found)[:max_results]

    def shortest_path(
        self,
        source_title: str,
        target_title: str,
        *,
        direction: str = GRAPH_DIRECTION_OUTGOING,
        scope: str = GRAPH_SCOPE_CANONICAL,
        candidate_titles: Iterable[str] | None = None,
        max_depth: int = DEFAULT_GRAPH_MAX_DEPTH,
        max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
    ) -> list[str] | None:
        """Return the directed shortest path between two Canonical Page Titles.

        Performs a cycle-safe breadth-first search over the graph, following
        ``direction`` (``"outgoing"``, ``"incoming"``, or ``"both"``).
        ``"outgoing"`` follows source->target edges; ``"incoming"`` follows
        them in reverse; ``"both"`` treats them as undirected. ``scope``
        selects the graph: ``"canonical"`` (default) uses only reviewed typed
        Relationships; ``"discovery"`` uses canonical Relationships PLUS
        Extracted References (ADR-0011, issue #107).

        Traversal operates ONLY over ``candidate_titles``: both endpoints and
        every intermediate title must be in the authorized set, or ``None`` is
        returned. When ``candidate_titles`` is ``None`` every loaded Compiled
        Page title is authorized. The path is bounded by ``max_depth`` hops and
        ``max_edges`` expanded edges; ``[source_title]`` is returned when source
        equals target (and is authorized). Tie-breaking is deterministic
        (lexicographic neighbor order), so repeated loads of unchanged Compiled
        Pages yield identical paths.
        """
        _require_graph_scope(scope)
        _require_graph_direction(direction)
        _require_non_negative_limit("max_depth", max_depth)
        _require_non_negative_limit("max_edges", max_edges)
        candidate = self._graph_candidate(candidate_titles)
        if source_title == target_title:
            resolved = self._authorized_seed_keys(source_title, candidate)
            return [source_title] if resolved else None
        source_keys = self._authorized_seed_keys(source_title, candidate)
        target_keys = self._authorized_seed_keys(target_title, candidate)
        if not source_keys or not target_keys:
            return None
        allowed = self._graph_allowed_keys(candidate)
        neighbors = self._graph_neighbor_fn(direction, scope)
        title_of = self._knowledge_index().title_by_graph_key

        visited = set(source_keys)
        queue: deque[tuple[str, list[str]]] = deque((seed, [seed]) for seed in sorted(source_keys))
        edges_expanded = 0
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            # Debit the expanded-edge budget per ELIGIBLE edge inspected (after
            # the candidate-set filter), not per unique endpoint.
            # ``shortest_path`` does not filter by relationship type, so every
            # candidate edge counts.
            for edge in neighbors(current):
                endpoint = edge.endpoint
                if endpoint == current or endpoint not in allowed:
                    continue
                edges_expanded += 1
                if edges_expanded > max_edges:
                    return None
                if endpoint in target_keys:
                    keys = path + [endpoint]
                    return [title_of.get(k, k) for k in keys]
                if endpoint not in visited:
                    visited.add(endpoint)
                    queue.append((endpoint, path + [endpoint]))
        return None

    def _graph_candidate(self, candidate_titles: Iterable[str] | None) -> frozenset[str]:
        """Return the authorized candidate title set, defaulting to all titles."""
        if candidate_titles is None:
            return frozenset(page.title for page in self.pages if page.title)
        return frozenset(candidate_titles)

    def _graph_allowed_keys(self, candidate: frozenset[str]) -> frozenset[str]:
        """Map authorized Canonical Page Titles to authorized graph keys.

        Graph keys are stable Entity IDs (Canonical Page Title fallback for
        Legacy Flat Mode pages without an Entity ID — issue #170). Titles not
        present in the loaded Knowledge Base authorize nothing.
        """
        index = self._knowledge_index()
        return frozenset(
            index.graph_key_by_title[title]
            for title in candidate
            if title in index.graph_key_by_title
        )

    def _authorized_seed_keys(self, title_or_alias: str, candidate: frozenset[str]) -> list[str]:
        """Resolve a Canonical Page Title or Alias to authorized seed keys.

        Resolution is a local lookup, not graph expansion. Authorization is
        applied BEFORE the resolved identity can seed any traversal: only
        pages whose Canonical Page Title is in the authorized ``candidate``
        set contribute keys (issue #170: resolve titles/aliases to Entity IDs
        at the module edge, without making titles identity).
        """
        index = self._knowledge_index()
        pages = index.by_title.get(title_or_alias) or index.by_alias.get(title_or_alias, [])
        keys: list[str] = []
        for page in pages:
            if page.title and page.title in candidate:
                key = index.graph_key_by_title.get(page.title)
                if key is not None and key not in keys:
                    keys.append(key)
        return keys

    def _graph_neighbor_fn(self, direction: str, scope: str = GRAPH_SCOPE_CANONICAL):
        """Return a ``graph key -> iterable[GraphEdge]`` closure.

        Yields the RAW edges reachable from a graph key (stable Entity ID) per
        ``direction`` in deterministic order, WITHOUT materializing,
        de-duplicating, or sorting a per-node neighbor list. The caller debits
        its expanded-edge budget per eligible edge as it inspects them,
        applying the relationship-type, candidate-set, and self-loop filters
        itself (those filters do not expand the graph).

        ``scope`` selects the graph: ``canonical`` uses only accepted
        entity-to-entity Claims; ``discovery`` uses canonical Claims PLUS
        Extracted References (equivalent edges deduplicated to a single
        endpoint). Extracted edges carry an empty predicate and claim ID.

        Determinism comes from the adjacency being stored pre-sorted in
        ``_KnowledgeIndex``; ``both`` direction lazily merges the two
        pre-sorted streams via ``heapq.merge``.
        """
        index = self._knowledge_index()
        if scope == GRAPH_SCOPE_DISCOVERY:
            outgoing = index.discovery_adjacency
            incoming = index.discovery_incoming
        else:
            outgoing = index.adjacency
            incoming = index.incoming

        if direction == GRAPH_DIRECTION_OUTGOING:

            def neighbors(key: str):
                return outgoing.get(key, ())

        elif direction == GRAPH_DIRECTION_INCOMING:

            def neighbors(key: str):
                return incoming.get(key, ())

        else:  # GRAPH_DIRECTION_BOTH

            def neighbors(key: str):
                return heapq.merge(
                    outgoing.get(key, ()),
                    incoming.get(key, ()),
                    key=_graph_edge_sort_key,
                )

        return neighbors

    # ------------------------------------------------------------------
    # Discovery Graph inspection (issue #107, ADR-0011).
    # ------------------------------------------------------------------

    def extracted_references(self, title: str) -> list[ExtractedReference]:
        """Return the deduplicated Extracted References whose source is ``title``.

        Each Extracted Reference carries the source path and the FIRST source
        occurrence line range as provenance for inspection. Equivalent edges
        (same source_title -> target_title) appear once. The list is sorted by
        ``(target_title, line_start)`` for deterministic inspection. Returns an
        empty list when ``title`` has no extracted outgoing references.
        """
        refs = self._knowledge_index().extracted_references
        owned = [ref for ref in refs if ref.source_title == title]
        return sorted(owned, key=lambda r: (r.target_title, r.line_start, r.source_path))

    def extraction_diagnostics(self) -> list[ExtractionDiagnostic]:  # noqa: F821
        """Return non-blocking diagnostics from Extracted Reference resolution.

        Broken and ambiguous link targets are excluded from the traversable
        Discovery Graph and surfaced here as non-blocking diagnostics so a
        Maintainer can fix the Markdown. External URLs, escaping paths, and
        reserved-artifact targets are expected, correct exclusions and are
        intentionally silent. These are derived-state diagnostics, not
        blocking validation errors: a resolved link is deterministic and
        does not require Maintainer approval (ADR-0011).
        """
        return list(self._knowledge_index().extraction_diagnostics)

    # ------------------------------------------------------------------
    # Graph materialization (issue #108, ADR-0011).
    #
    # The Discovery Graph adjacency is materialized as a versioned
    # MessagePack artifact in the derived index directory so startup can
    # skip re-extraction when the Knowledge Base fingerprint and extractor
    # version are unchanged. A missing, stale, corrupt, partial, or
    # incompatible artifact is ignored and the graph is rebuilt
    # deterministically in memory. The artifact lives outside the Knowledge
    # Base source tree and is never a Compiled Page, Reserved Artifact,
    # fingerprint input, or OKF export entry. Works without LanceDB or an
    # operational database.
    # ------------------------------------------------------------------

    def materialize_graph(self, index_dir: str | Path) -> Path:
        """Materialize the Discovery Graph as a MessagePack artifact.

        Serializes the current Discovery Graph adjacency (canonical
        Relationships plus Extracted References, both outgoing and incoming)
        alongside the Knowledge Base fingerprint and extractor version, and
        writes it atomically into ``index_dir``. Returns the artifact path.
        The artifact is rebuildable: deleting it and re-running this method
        over unchanged Markdown reproduces byte-identical adjacency and
        identical public traversal results.
        """
        fingerprint = fingerprint_sources(self.root)
        index = self._knowledge_index()
        return write_graph_artifact(
            index_dir,
            index=index,
            fingerprint=fingerprint,
            extractor_version=EXTRACTOR_VERSION,
        )

    def load_or_derive_graph(self, index_dir: str | Path) -> GraphState:
        """Return the Discovery Graph state, loading the artifact when fresh.

        Loads the persisted MessagePack artifact when it exists and its
        fingerprint and extractor version match the current Knowledge Base;
        otherwise derives the same adjacency deterministically in memory.
        Never raises on a bad artifact: stale, corrupt, partial, or
        incompatible artifacts fall back to in-memory derivation.
        """
        fingerprint = fingerprint_sources(self.root)
        state = load_graph_artifact(
            index_dir,
            fingerprint=fingerprint,
            extractor_version=EXTRACTOR_VERSION,
        )
        if state is not None:
            return state
        return build_graph_state(
            self._knowledge_index(),
            fingerprint=fingerprint,
            extractor_version=EXTRACTOR_VERSION,
        )

    def graph_health(self, index_dir: str | Path) -> GraphHealthReport:
        """Return aggregate health + observability signals for the Discovery Graph.

        Reports whether the persisted artifact is fresh (fingerprint and
        extractor version match), whether it is materialized, the discovery
        edge count, the materialized artifact size in bytes, an observable
        graph startup time, and a bounded traversal latency signal. Does NOT
        expose MessagePack layout, raw adjacency, or internal artifact keys.
        Timing fields are observability signals, not correctness invariants.
        """
        import time

        index_dir_path = Path(index_dir)
        fingerprint = fingerprint_sources(self.root)
        artifact_path = index_dir_path / GRAPH_ARTIFACT_FILENAME
        materialized = artifact_path.is_file()
        materialized_size_bytes = artifact_path.stat().st_size if materialized else None

        start = time.perf_counter()
        state = load_graph_artifact(
            index_dir_path,
            fingerprint=fingerprint,
            extractor_version=EXTRACTOR_VERSION,
        )
        graph_fresh = state is not None
        if state is None:
            state = build_graph_state(
                self._knowledge_index(),
                fingerprint=fingerprint,
                extractor_version=EXTRACTOR_VERSION,
            )
        startup_ms = int((time.perf_counter() - start) * 1000)

        return GraphHealthReport(
            graph_fresh=graph_fresh,
            materialized=materialized,
            edge_count=state.edge_count,
            materialized_size_bytes=materialized_size_bytes,
            startup_ms=startup_ms,
            traversal_latency_ms=self._graph_traversal_latency_ms(),
            fingerprint_digest=fingerprint.digest,
        )

    def _graph_traversal_latency_ms(self) -> int | None:
        """Return a tiny bounded discovery traversal latency in ms, or ``None``.

        Measures one single-hop ``related_pages`` expansion over the in-memory
        Discovery Graph so the signal reflects the traversal cost an operator
        would observe, not the artifact decode cost.
        """
        import time

        titles = [page.title for page in self.pages if page.title]
        if not titles:
            return None
        start = time.perf_counter()
        self.related_pages(
            titles[0],
            scope=GRAPH_SCOPE_DISCOVERY,
            max_depth=1,
            max_results=1,
        )
        return int((time.perf_counter() - start) * 1000)

    def graph_diagnostics(
        self,
        *,
        scope: str = GRAPH_SCOPE_CANONICAL,
        candidate_titles: Iterable[str] | None = None,
        max_hub_sample: int = DEFAULT_GRAPH_HUB_SAMPLE,
        max_orphan_sample: int = DEFAULT_GRAPH_ORPHAN_SAMPLE,
        max_unresolved_sample: int = DEFAULT_GRAPH_UNRESOLVED_SAMPLE,
    ) -> StructuralGraphReport:
        """Return deterministic structural topology diagnostics for one scope.

        Computes a read-only :class:`StructuralGraphReport` over the in-memory
        graph for EITHER scope (``canonical``: reviewed typed Relationships
        only; ``discovery``: Relationships PLUS Extracted References). This is
        STRUCTURAL topology analysis — orphan counts, weakly connected
        components, directed hubs — and is distinct from the artifact/runtime
        observability in :meth:`graph_health` (freshness, materialization,
        latency). It never changes graph ownership, persistence, retrieval
        semantics, or the Evidence contract (ADR-0011).

        Orphan counts and hubs are DIRECTION-AWARE and directed-safe. Weakly
        connected components and largest-component coverage use an UNDIRECTED
        projection of the directed edges; this is disclosed via
        ``undirected_projection_used`` and is never presented as Relationship
        semantics. Self-loops (a page relating/linking only to itself) carry
        no structural connectivity and are excluded from all counts.

        Traversal operates ONLY over ``candidate_titles`` — the
        already-authorized Canonical Page Titles the caller has visibility to.
        When ``candidate_titles`` is ``None`` every loaded Compiled Page title
        is the authorized universe; edges whose source or endpoint falls
        outside the candidate set are excluded. Nodes are stable Entity IDs
        (issue #170): hub and orphan identity is Entity-based, so a title
        change does not alter the report, while report fields stay
        human-readable titles with the Entity ID disclosed alongside hubs.
        ``max_hub_sample``, ``max_orphan_sample``, and
        ``max_unresolved_sample`` bound the representative samples (NOT health
        thresholds); callers retain access to complete actionable
        unresolved-reference locations through :meth:`extraction_diagnostics`.
        """
        _require_graph_scope(scope)
        _require_non_negative_limit("max_hub_sample", max_hub_sample)
        _require_non_negative_limit("max_orphan_sample", max_orphan_sample)
        _require_non_negative_limit("max_unresolved_sample", max_unresolved_sample)

        index = self._knowledge_index()
        if scope == GRAPH_SCOPE_DISCOVERY:
            outgoing = index.discovery_adjacency
        else:
            outgoing = index.adjacency

        candidate = self._graph_candidate(candidate_titles)
        all_titles = frozenset(page.title for page in self.pages if page.title)
        nodes = self._graph_allowed_keys(candidate & all_titles)
        title_of = index.title_by_graph_key

        topo = _compute_graph_topology(outgoing, nodes)
        in_degree = topo.in_degree
        out_degree = topo.out_degree
        edge_count = topo.edge_count
        wcc_count = topo.wcc_count
        largest = topo.largest_wcc_size

        page_count = len(nodes)
        inbound_orphans = sorted(
            (k for k in nodes if in_degree[k] == 0), key=lambda k: title_of.get(k, k)
        )
        outbound_orphans = sorted(
            (k for k in nodes if out_degree[k] == 0), key=lambda k: title_of.get(k, k)
        )
        coverage = (largest / page_count) if page_count else 0.0

        top_inbound_hubs = self._top_hubs(nodes, in_degree, max_hub_sample)
        top_outbound_hubs = self._top_hubs(nodes, out_degree, max_hub_sample)

        unresolved = (
            self._aggregate_unresolved_references(candidate, max_unresolved_sample)
            if scope == GRAPH_SCOPE_DISCOVERY
            else ()
        )

        return StructuralGraphReport(
            scope=scope,
            page_count=page_count,
            edge_count=edge_count,
            inbound_orphan_count=len(inbound_orphans),
            outbound_orphan_count=len(outbound_orphans),
            weakly_connected_component_count=wcc_count,
            largest_component_coverage=coverage,
            undirected_projection_used=True,
            top_inbound_hubs=top_inbound_hubs,
            top_outbound_hubs=top_outbound_hubs,
            unresolved_references=unresolved,
            inbound_orphan_sample_titles=tuple(
                title_of.get(k, k) for k in inbound_orphans[:max_orphan_sample]
            ),
            outbound_orphan_sample_titles=tuple(
                title_of.get(k, k) for k in outbound_orphans[:max_orphan_sample]
            ),
            inbound_orphan_sample_entity_ids=tuple(inbound_orphans[:max_orphan_sample]),
            outbound_orphan_sample_entity_ids=tuple(outbound_orphans[:max_orphan_sample]),
        )

    def _top_hubs(
        self,
        nodes: frozenset[str],
        degree: dict[str, int],
        max_sample: int,
    ) -> tuple[GraphHub, ...]:
        """Return deterministic top hubs by directed degree, bounded.

        Ranks graph keys (stable Entity IDs) by descending degree with
        lexicographic ``(title, entity id)`` tie-break, excludes zero-degree
        nodes, and caps at ``max_sample``. Each hub discloses its Entity ID
        alongside the human-readable title. Hub status is navigational
        topology, never a Relationship semantic claim.
        """
        index = self._knowledge_index()
        title_of = index.title_by_graph_key
        return tuple(
            GraphHub(
                title=title_of.get(k, k),
                edge_count=degree[k],
                entity_id=k,
            )
            for k in sorted(nodes, key=lambda x: (-degree[x], title_of.get(x, x), x))
            if degree[k] > 0
        )[:max_sample]

    def _aggregate_unresolved_references(
        self,
        candidate: frozenset[str],
        max_sample: int,
    ) -> tuple[UnresolvedReferenceGroup, ...]:
        """Aggregate extraction diagnostics by (outcome, target).

        Extracted-reference outcomes (broken, ambiguous) are Discovery Graph
        derivation diagnostics (ADR-0011). Repeated missing targets are strong
        page/link-repair signals. Only diagnostics whose source page is in the
        authorized ``candidate`` set are included, preserving visibility. Full
        diagnostic detail (kind text, raw target, context) remains available
        through :meth:`extraction_diagnostics`; each group carries a bounded,
        deterministic representative subset of source locations.
        """
        path_to_title = {page.path: page.title for page in self.pages if page.title}
        groups: dict[tuple[str, str], list[ExtractionDiagnostic]] = {}
        for diag in self._knowledge_index().extraction_diagnostics:
            src_title = path_to_title.get(diag.source_path)
            if src_title is None or src_title not in candidate:
                continue
            groups.setdefault((diag.kind, diag.target), []).append(diag)

        result: list[UnresolvedReferenceGroup] = []
        for outcome, target in sorted(groups):
            ordered = sorted(
                groups[(outcome, target)],
                key=lambda d: (d.source_path, d.line_start, d.target),
            )
            result.append(
                UnresolvedReferenceGroup(
                    outcome=outcome,
                    target=target,
                    count=len(ordered),
                    samples=tuple(
                        UnresolvedReferenceSample(
                            source_path=d.source_path,
                            line_start=d.line_start,
                        )
                        for d in ordered[:max_sample]
                    ),
                )
            )
        return tuple(result)

    def rank_link_candidates_by_graph_impact(
        self,
        candidates: Iterable[LinkCandidate],
        *,
        scope: str = GRAPH_SCOPE_DISCOVERY,
        candidate_titles: Iterable[str] | None = None,
    ) -> list[RankedLinkCandidate]:
        """Annotate and deterministically prioritize LinkCandidates by graph impact.

        Evaluates each EXISTING deterministic :class:`LinkCandidate` against the
        current authorized Discovery Graph WITHOUT mutating Compiled Pages
        or graph state. For each candidate, computes the structural impact of
        the link IF it were published and derived as an Extracted Reference:

        * **Orphan repair** (``LINK_IMPACT_KIND_ORPHAN_REPAIR``): the link
          would give an inbound connection to a page that is currently a graph
          orphan (zero inbound edges).
        * **Component joining** (``LINK_IMPACT_KIND_COMPONENT_JOIN``): the
          link would connect two weakly connected components (reducing WCC
          count).
        * **Fragile-connection strengthening**
          (``LINK_IMPACT_KIND_FRAGILE_STRENGTHENING``): source and target are
          in the same weakly connected component but no direct edge exists
          between them; adding a direct edge strengthens the existing indirect
          (fragile) connection. The fragile topology is identified through the
          same structural diagnostics seam (#126): directed inbound degrees,
          undirected WCC projection, and direct-edge presence — the topology
          ``graph_diagnostics`` reports.

        Candidates are ordered by descending ``impact_score`` with stable
        lexical tie-breaking on ``(source_path, line, column, target_title,
        term)``. Repeated runs over unchanged Markdown are identical. A
        candidate with no measured structural impact is still returned
        (Maintainers can inspect ALL candidates).

        Authorization is applied BEFORE graph impact is calculated: the graph
        topology is computed over ``candidate_titles`` only (default: every
        loaded Compiled Page title). A candidate whose source or target falls
        outside the authorized set receives score 0 and no signals, and the
        inaccessible page identity does not affect any other candidate's
        scores, counts, reasons, or output ordering.

        Only ``GRAPH_SCOPE_DISCOVERY`` is valid (the default): a published
        Markdown link becomes an Extracted Reference, which participates in
        the Discovery Graph only. Canonical-scope impact is rejected because
        it would imply inferring a typed Relationship from a Markdown-link
        proposal (ADR-0011).

        The impact is ADVISORY: it never infers a typed Relationship from a
        Markdown-link proposal. An approved link remains an Extracted
        Reference with navigation meaning only (ADR-0011).
        """
        _require_graph_scope(scope)
        if scope != GRAPH_SCOPE_DISCOVERY:
            raise ValueError(
                "rank_link_candidates_by_graph_impact only supports the "
                "discovery scope; a published Markdown link becomes an "
                "Extracted Reference (discovery-only), never a canonical "
                "Relationship (ADR-0011)"
            )

        index = self._knowledge_index()
        outgoing = index.discovery_adjacency

        candidate_set = self._graph_candidate(candidate_titles)
        all_titles = frozenset(page.title for page in self.pages if page.title)
        # Authorized topology over stable Entity-ID graph keys (issue #170);
        # LinkCandidate fields stay title-oriented, so map at this edge.
        nodes = self._graph_allowed_keys(candidate_set & all_titles)
        key_of = index.graph_key_by_title

        # Compute the authorized topology ONCE via the shared structural
        # helper (same source of truth as graph_diagnostics). Each candidate
        # is then evaluated against this snapshot without mutation.
        topo = _compute_graph_topology(outgoing, nodes)
        in_degree = topo.in_degree
        out_endpoints = topo.out_endpoints
        wcc_id = topo.wcc_id

        ranked: list[RankedLinkCandidate] = []
        for cand in candidates:
            src_title = cand.source_title
            tgt_title = cand.target_title
            src_key = key_of.get(src_title)
            tgt_key = key_of.get(tgt_title)

            signals: list[LinkImpactSignal] = []
            score = 0

            # Authorization gate: inaccessible page identities do not affect
            # scores, counts, reasons, or output ordering.
            if (
                src_key in nodes
                and tgt_key in nodes
                and src_key is not None
                and tgt_key is not None
                and src_key != tgt_key
            ):
                # Orphan repair: target has zero inbound edges.
                if in_degree[tgt_key] == 0:
                    score += LINK_IMPACT_WEIGHT_ORPHAN_REPAIR
                    signals.append(
                        LinkImpactSignal(
                            kind=LINK_IMPACT_KIND_ORPHAN_REPAIR,
                            detail=(
                                f"gives '{tgt_title}' its first inbound "
                                f"{scope} connection (currently an inbound orphan)"
                            ),
                        )
                    )

                # Component joining: source and target in different WCCs.
                if wcc_id[src_key] != wcc_id[tgt_key]:
                    score += LINK_IMPACT_WEIGHT_COMPONENT_JOIN
                    signals.append(
                        LinkImpactSignal(
                            kind=LINK_IMPACT_KIND_COMPONENT_JOIN,
                            detail=(
                                "joins two weakly connected components (reduces WCC count by 1)"
                            ),
                        )
                    )

                # Fragile strengthening: same WCC, no direct edge exists.
                # Adding a direct edge strengthens the indirect connection.
                elif tgt_key not in out_endpoints[src_key]:
                    score += LINK_IMPACT_WEIGHT_FRAGILE_STRENGTHENING
                    signals.append(
                        LinkImpactSignal(
                            kind=LINK_IMPACT_KIND_FRAGILE_STRENGTHENING,
                            detail=(
                                "strengthens an indirect connection: source and "
                                "target share a weakly connected component but no "
                                "direct edge currently exists"
                            ),
                        )
                    )

            ranked.append(
                RankedLinkCandidate(
                    candidate=cand,
                    scope=scope,
                    impact_score=score,
                    signals=tuple(signals),
                )
            )

        # Deterministic ordering: highest impact first, then stable lexical
        # tie-break on the candidate's identity fields.
        ranked.sort(
            key=lambda r: (
                -r.impact_score,
                r.candidate.source_path,
                r.candidate.line,
                r.candidate.column,
                r.candidate.target_title,
                r.candidate.term,
            )
        )
        return ranked

    def build_index(
        self,
        index_dir: str | Path | None = None,
        *,
        embedder: Embedder | None = None,
        retrieval: RetrievalAdapter | None = None,
    ) -> KnowledgeBase:  # noqa: F821
        """Build a derived index at ``index_dir`` and return a new KnowledgeBase.

        Routes the build through the retrieval adapter slot: ``retrieval`` (or
        the bound adapter, or the always-available zero-index default) owns the
        derived index. The returned Knowledge Base binds the chosen adapter so a
        later :meth:`retrieve` reuses it. ``embedder`` is forwarded to the
        adapter so a semantic-capable adapter (e.g. LanceDB, wired in a later
        task) can build the citation-ready vector index; the zero-index default
        ignores it.
        """
        chosen_dir = Path(index_dir) if index_dir is not None else self.index_dir
        if chosen_dir is None:
            raise KnowledgeBaseError(
                "index_dir is required to build an index; pass one to build_index()"
            )
        chosen_dir = chosen_dir.resolve()
        adapter: RetrievalAdapter = (
            retrieval if retrieval is not None else self._retrieval_adapter()
        )
        adapter.build_index(
            self.pages,
            chosen_dir,
            embedder=embedder,
            fingerprint=fingerprint_sources(self.root),
        )
        return KnowledgeBase(
            root=self.root,
            pages=self.pages,
            index_dir=chosen_dir,
            control=self.control,
            retrieval=adapter,
        )

    def _eligible_pages_for_graph(
        self,
        seed_titles: Sequence[str],
        *,
        scope: str,
        direction: str,
        max_depth: int,
    ) -> "_GraphExpansion":
        """Return the graph-expanded eligible Compiled Pages plus trace metadata.

        Discovery Graph expansion (issue #112, ADR-0011): for each seed title,
        resolve authorized seed keys, expand authorized neighbors (scope /
        direction / depth bounded) breadth-first, and resolve to the loaded
        Compiled Pages. Authorization is the set of loaded page titles, so an
        unloaded page can never enter the eligible set. The graph selects
        page identities; the retrieval adapter ranks citation-ready Evidence
        from them and never imports graph code.

        Since issue #172 the expansion also records claim-aware metadata for
        a truthful Retrieval Trace: resolved seed Entity IDs, unresolved seed
        names, traversed Claim IDs/Predicates, and edge origins. Disputed and
        superseded Claims never enter traversal (only accepted Claims are
        adjacency), so they can never masquerade as traversed support.
        """
        authorized = {page.title for page in self.pages if page.title}
        candidate = self._graph_candidate(authorized)
        resolved: list[str] = []
        unresolved: list[str] = []
        for seed in seed_titles:
            keys = self._authorized_seed_keys(seed, candidate)
            if keys:
                resolved.extend(key for key in keys if key not in resolved)
            else:
                unresolved.append(seed)
        allowed = self._graph_allowed_keys(candidate)
        neighbors = self._graph_neighbor_fn(direction, scope)
        index = self._knowledge_index()

        visited = set(resolved)
        found: set[str] = set()
        claim_ids: set[str] = set()
        predicates: set[str] = set()
        origins: set[str] = set()
        frontier: deque[tuple[str, int]] = deque((seed, 0) for seed in sorted(resolved))
        edges_expanded = 0
        while frontier:
            current, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            for edge in neighbors(current):
                if edge.endpoint == current or edge.endpoint not in allowed:
                    continue
                edges_expanded += 1
                if edges_expanded > DEFAULT_GRAPH_MAX_EDGES:
                    frontier.clear()
                    break
                origins.add(edge.origin)
                if edge.claim_id:
                    claim_ids.add(edge.claim_id)
                if edge.predicate:
                    predicates.add(edge.predicate)
                if edge.endpoint in visited:
                    continue
                visited.add(edge.endpoint)
                found.add(edge.endpoint)
                frontier.append((edge.endpoint, depth + 1))

        eligible_keys = visited
        pages = [
            page
            for page in self.pages
            if page.title and index.graph_key_by_title.get(page.title) in eligible_keys
        ]
        return _GraphExpansion(
            pages=pages,
            resolved_seed_keys=resolved,
            unresolved_seeds=unresolved,
            traversed_claim_ids=sorted(claim_ids),
            traversed_predicates=sorted(predicates),
            traversed_origins=sorted(origins),
        )

    @staticmethod
    def _with_prepended_stages(
        result: RetrievalResult, stages: list[TraceStage]
    ) -> RetrievalResult:
        """Return ``result`` with ``stages`` prepended to its Retrieval Trace.

        Retrieval traces are built inside the adapter (frozen records); the
        Knowledge Base annotates the graph-expansion / fingerprint stages it
        owns on top of the adapter's lexical / semantic / fusion stages so the
        composed trace stays truthful without the adapter knowing about graphs.
        """
        if not stages:
            return result
        return RetrievalResult(
            evidence=result.evidence,
            citation=result.citation,
            snippet=result.snippet,
            score=result.score,
            reason=result.reason,
            trace=RetrievalTrace(stages=[*stages, *result.trace.stages]),
        )

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        index_dir: str | Path | None = None,
        hints: Sequence[str] | None = None,
        *,
        mode: RetrievalMode = "lexical",
        embedder: Embedder | None = None,
        score_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
        graph_seed_titles: Sequence[str] | None = None,
        graph_scope: str = GRAPH_SCOPE_DISCOVERY,
        graph_direction: str = GRAPH_DIRECTION_OUTGOING,
        graph_max_depth: int = 2,
    ) -> list[RetrievalResult]:
        """Retrieve citation-ready Evidence for ``query``.

        Delegates to the bound retrieval adapter, defaulting to the
        always-available zero-index lexical adapter. The default lexical path
        works over the loaded Compiled Pages and needs no ``index_dir``; the
        LanceDB BM25 / semantic / hybrid paths require the LanceDB retrieval
        adapter to be bound via :meth:`build_index` (``retrieval=...``).

        This is fresh retrieval over Compiled Pages / Knowledge Base Evidence.
        It is distinct from Conversation Recall (#59): no persisted Reader
        questions, chat context, or shared recall records are involved.

        ``hints`` is an optional, bounded list of sanitized additional query
        terms (e.g. recall-derived significant terms) that shape the lexical
        search *before* it runs. They are appended to the query so the
        Knowledge Base's own retrieval ranks them, never substituted for it;
        the original ``query`` always participates. Callers must keep the hint
        set bounded and sanitized (significant tokens only) so it cannot be
        abused as a second free-form query (issue #55).

        Graph expansion (issue #112, ADR-0011): when ``graph_seed_titles`` is
        provided, the Discovery Graph selects eligible page identities (canonical
        Relationships plus Extracted References) BEFORE the adapter ranks
        Evidence. Only eligible pages are passed to the adapter; an Extracted
        Reference is never Evidence. An empty eligible set returns ``[]`` with
        a truthful trace (no fallback to all pages). ``graph_seed_titles=None``
        preserves the exact pre-expansion behavior. Before composing the
        in-memory graph with a built adapter index, the stored index fingerprint
        is checked against the current source fingerprint; a mismatch triggers a
        rebuild so graph and Evidence never disagree.
        """
        chosen_dir = Path(index_dir) if index_dir is not None else self.index_dir
        effective_query = query
        if hints:
            extra = " ".join(hint for hint in hints if hint)
            if extra:
                effective_query = f"{query} {extra}".strip()
        resolved = chosen_dir.resolve() if chosen_dir is not None else None

        graph_active = graph_seed_titles is not None
        eligible_pages: Sequence[CompiledPage] | None = None
        prepended_stages: list[TraceStage] = []

        if graph_active:
            _require_graph_scope(graph_scope)
            _require_graph_direction(graph_direction)
            _require_non_negative_limit("graph_max_depth", graph_max_depth)
            seeds = list(graph_seed_titles)
            expansion = self._eligible_pages_for_graph(
                seeds,
                scope=graph_scope,
                direction=graph_direction,
                max_depth=graph_max_depth,
            )
            eligible_pages = expansion.pages
            # Truthful seed resolution (issue #172): resolved seed Entity IDs
            # plus — as its own stage — any seed that authorized nothing.
            seed_note = ", ".join(expansion.resolved_seed_keys) or "(none resolved)"
            if expansion.unresolved_seeds:
                seed_note += (
                    f"; unresolved (no loaded authorized page): "
                    f"{', '.join(expansion.unresolved_seeds)}"
                )
            prepended_stages.append(TraceStage("graph-seeds", seed_note))
            # Claim-aware expansion disclosure (issue #172): scope, counts,
            # accepted-only lifecycle filter, followed edge origins, traversed
            # Claim IDs/Predicates (bounded), and the artifact source.
            detail = (
                f"{graph_scope} scope; {len(eligible_pages)} eligible pages "
                f"from {len(seeds)} seeds; lifecycle=accepted "
                f"(disputed/superseded never traverse); "
                f"source=in-memory Entity-ID graph"
            )
            if expansion.traversed_origins:
                detail += f"; origins={', '.join(expansion.traversed_origins)}"
            if expansion.traversed_claim_ids:
                ids = expansion.traversed_claim_ids
                shown = ", ".join(ids[:8]) + (f" (+{len(ids) - 8} more)" if len(ids) > 8 else "")
                detail += f"; claims={shown}"
            if expansion.traversed_predicates:
                preds = expansion.traversed_predicates
                shown = ", ".join(preds[:8]) + (
                    f" (+{len(preds) - 8} more)" if len(preds) > 8 else ""
                )
                detail += f"; predicates={shown}"
            prepended_stages.append(TraceStage("graph-expansion", detail))
            # Fingerprint gate: the in-memory graph is inherently current; a
            # built adapter index may be stale. Before composing them, assert the
            # stored fingerprint matches the current source. A mismatch (or a
            # missing fingerprint) is stale and triggers a rebuild so graph and
            # Evidence never disagree (issue #112, ADR-0011).
            if resolved is not None:
                current_fp = fingerprint_sources(self.root)
                stored_fp = load_stored_fingerprint(resolved)
                if stored_fp is None or stored_fp.digest != current_fp.digest:
                    self._retrieval_adapter().build_index(
                        self.pages,
                        resolved,
                        embedder=embedder,
                        fingerprint=current_fp,
                    )
                    prepended_stages.append(
                        TraceStage(
                            "fingerprint-check",
                            "stale derived index rebuilt from current source before compose",
                        )
                    )

        results = self._retrieval_adapter().retrieve(
            self.pages,
            effective_query,
            limit=limit,
            index_dir=resolved,
            mode=mode,
            embedder=embedder,
            score_threshold=score_threshold,
            eligible_pages=eligible_pages,
        )

        if prepended_stages:
            results = [
                KnowledgeBase._with_prepended_stages(result, prepended_stages) for result in results
            ]
        return results

    def fingerprint(self) -> SourceFingerprint:
        """Return the current source fingerprint for this Knowledge Base."""
        return fingerprint_sources(self.root)

    def stored_fingerprint(self) -> SourceFingerprint | None:
        """Return the source fingerprint stored with the last index build, if any."""
        if self.index_dir is None:
            return None
        return load_stored_fingerprint(self.index_dir)

    def registry(self) -> list[RegistryEntry]:
        """Return a compact registry entry for every Compiled Page."""
        return [
            RegistryEntry(
                title=page.title,
                aliases=list(page.aliases),
                tags=list(page.tags),
                summary=page.summary or "",
                lifecycle=page.lifecycle or "",
                visibility=page.visibility or "",
                path=page.path,
                source_count=len(page.sources),
                relationship_count=sum(
                    1
                    for claim in page.claims
                    if claim.status == "accepted" and claim.object is not None
                ),
            )
            for page in self.pages
        ]

    def is_fresh(self) -> bool:
        """Return whether the derived index still matches the current Markdown source."""
        stored = self.stored_fingerprint()
        if stored is None:
            return False
        return self.fingerprint().digest == stored.digest

    def health_report(self) -> HealthReport:
        """Return a deterministic, zero-LLM health report for this Knowledge Base.

        The report scans the registry, validation issues, and freshness state to
        surface structural problems for maintenance awareness. It does not call
        an LLM and does not replace the publish validation gate.
        """
        missing_summaries = sorted(entry.path for entry in self.registry() if not entry.summary)

        validation = validate(self.root)

        broken_relationships: set[str] = set()
        unknown_relationship_types: set[str] = set()
        invalid_fields: set[str] = set()
        for issue in validation.issues:
            message = issue.message
            if ": dangling object entity: " in message:
                broken_relationships.add(issue.file)
            elif ": unknown predicate: " in message or message.startswith("unknown entity type: "):
                unknown_relationship_types.add(issue.file)
            elif issue.severity == "error":
                if not (
                    message.startswith("duplicate alias: ")
                    or message.startswith("duplicate canonical title: ")
                ):
                    invalid_fields.add(issue.file)

        alias_locations: dict[str, list[str]] = {}
        for page in self.pages:
            for alias in page.aliases:
                if alias:
                    alias_locations.setdefault(alias, []).append(page.path)
        duplicate_aliases = sorted(
            alias for alias, paths in alias_locations.items() if len(paths) > 1
        )

        title_locations: dict[str, list[str]] = {}
        for page in self.pages:
            if page.title:
                title_locations.setdefault(page.title, []).append(page.path)
        duplicate_titles = sorted(
            title for title, paths in title_locations.items() if len(paths) > 1
        )

        stale_index = self.index_dir is not None and not self.is_fresh()

        is_healthy = (
            not stale_index
            and not missing_summaries
            and not broken_relationships
            and not duplicate_aliases
            and not duplicate_titles
            and not invalid_fields
            and not unknown_relationship_types
        )

        return HealthReport(
            stale_index=stale_index,
            missing_summaries=missing_summaries,
            broken_relationships=sorted(broken_relationships),
            duplicate_aliases=duplicate_aliases,
            duplicate_titles=duplicate_titles,
            invalid_fields=sorted(invalid_fields),
            unknown_relationship_types=sorted(unknown_relationship_types),
            is_healthy=is_healthy,
        )


def export_bundle(kb: KnowledgeBase) -> str:
    """Return a Markdown bundle of the compiled Knowledge Base.

    The bundle contains only public Compiled Pages from the published knowledge
    base. Raw Knowledge Sources are stored separately and are deliberately
    excluded, and internal/restricted pages are withheld from the public export.
    """
    parts: list[str] = []
    for page in sorted(kb.pages, key=lambda page: page.path):
        if page.visibility != "public":
            continue
        page_path = kb.root / page.path
        parts.append(f"<!-- Page: {page.path} -->\n")
        parts.append(page_path.read_text(encoding="utf-8"))
        parts.append("\n---\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Discovery Graph: Extracted References from internal links (issue #107,
# ADR-0011).
#
# An Extracted Reference is a deterministic, non-canonical directed reference
# derived from an internal Markdown link or unambiguous wikilink in a Compiled
# Page body. It is produced ONLY when the link target resolves to exactly one
# Compiled Page (by canonical title first, then alias). External URLs, escaping
# paths, broken targets, ambiguous targets, duplicates, and valid Reserved
# Artifacts are excluded and surfaced as non-blocking diagnostics. Extraction
# never mutates the page body or frontmatter and never promotes a reference
# into a typed Relationship. An Extracted Reference is never Evidence.
# ---------------------------------------------------------------------------


class ExtractionDiagnostic(msgspec.Struct, frozen=True):
    """A non-blocking diagnostic from Extracted Reference resolution.

    Broken, ambiguous, escaping, external, duplicate, or reserved-artifact
    link targets are dropped from the traversable Discovery Graph and surfaced
    here so Maintainers can act on them through existing health/validation
    contracts. Diagnostics are never blocking validation errors: a resolved
    link is derived state, not a semantic claim (ADR-0011).
    """

    source_path: str
    line_start: int
    target: str
    kind: str
    detail: str


# Markdown link syntax: ``[label](destination)``. The ``!`` prefix (images) is
# excluded explicitly so ``![alt](x.md)`` is not treated as a reference. The
# destination is captured without the fragment/query so resolution is by path.
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!\!)\[(?P<label>[^\]]*)\]\((?P<dest>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
)
# Wikilink syntax: ``[[Target]]`` or ``[[Target|alias]]``.
_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^|\]]+)(?:\|[^\]]*)?\]\]")
# Schemes that mark a destination as external (non-navigational).
_EXTERNAL_SCHEMES = frozenset({"http", "https", "mailto", "ftp", "ftps", "tel"})


def _is_external_destination(dest: str) -> bool:
    """Return whether ``dest`` is an external URL or anchor-only reference."""
    if not dest:
        return True
    if dest.startswith("#"):
        return True
    lower = dest.lower()
    for scheme in _EXTERNAL_SCHEMES:
        if lower.startswith(scheme + ":"):
            return True
    # Protocol-relative URL (``//host``).
    if dest.startswith("//"):
        return True
    return False


def _is_escaping_destination(dest: str) -> bool:
    """Return whether ``dest`` would leave the Knowledge Base root.

    Any path containing a ``..`` segment is treated as escaping: the Discovery
    Graph only resolves links within the Knowledge Base, and a parent
    traversal has no defined target page. (A ``..`` that net-stays-in-root by
    path arithmetic would still be ambiguous across reloads, so it is excluded
    deterministically.)
    """
    parts = dest.replace("\\", "/").split("/")
    return any(part == ".." for part in parts)


def _destination_basename(dest: str) -> str:
    """Return the case-insensitive basename of ``dest`` (for reserved checks).

    The fragment and query are stripped first so ``page.md#section`` resolves
    by its path basename. Returns the empty string for an empty/bare dest.
    """
    cleaned = dest.split("#", 1)[0].split("?", 1)[0].strip()
    if not cleaned:
        return ""
    tail = cleaned.rstrip("/").split("/")[-1]
    return tail.lower()


def _destination_stem(dest: str) -> str:
    """Return ``dest`` with any ``./`` prefix and ``.md`` suffix removed.

    Used to match a link destination against a Compiled Page ``path``. Both
    sides are compared case-insensitively after normalization so a link to
    ``Beta.md`` resolves to a page whose path is ``beta.md``.
    """
    cleaned = dest.split("#", 1)[0].split("?", 1)[0].strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[: -len(".md")]
    return cleaned


def _resolve_link_target(
    dest: str,
    by_title: dict[str, list[CompiledPage]],
    by_alias: dict[str, list[CompiledPage]],
    by_path: dict[str, CompiledPage],
    source_dir: str = "",
) -> tuple[CompiledPage | None, str]:
    """Resolve a link destination to exactly one Compiled Page.

    Returns ``(page, reason)`` where ``reason`` is ``"ok"`` on a unique match,
    or one of ``"broken"``, ``"ambiguous"``, ``"reserved"`` describing why no
    traversable target was produced. Canonical title is tried first, then
    alias, then path-stem match. Exactly one match across all tried keys is
    required; zero or more-than-one is dropped.
    """
    if _is_external_destination(dest):
        return None, "external"
    if _is_escaping_destination(dest):
        return None, "escaping"

    basename = _destination_basename(dest)
    if basename in RESERVED_ARTIFACT_MARKERS:
        return None, "reserved"

    stem = _destination_stem(dest)

    # 1. Canonical title match (case-insensitive on the stem when it looks
    #    like a title rather than a path).
    title_matches: list[CompiledPage] = []
    for key, pages in by_title.items():
        if key.casefold() == stem.casefold():
            title_matches.extend(pages)
    if len(title_matches) == 1:
        return title_matches[0], "ok"
    if len(title_matches) > 1:
        return None, "ambiguous"

    # 2. Alias match.
    alias_matches: list[CompiledPage] = []
    for key, pages in by_alias.items():
        if key.casefold() == stem.casefold():
            alias_matches.extend(pages)
    if len(alias_matches) == 1:
        return alias_matches[0], "ok"
    if len(alias_matches) > 1:
        return None, "ambiguous"

    # 3. Path-stem match. Relative links resolve against the source page's
    #    directory first (so ``[x](b.md)`` in ``guide/a.md`` finds
    #    ``guide/b.md``), then fall back to the stem as-is. Root-absolute
    #    links (``/guide/b.md``) match after stripping the leading slash.
    folded = stem.casefold().lstrip("/")
    if source_dir:
        path_match = by_path.get(f"{source_dir}/{folded}")
        if path_match is not None:
            return path_match, "ok"
    path_match = by_path.get(folded)
    if path_match is not None:
        return path_match, "ok"

    return None, "broken"


def _resolve_wikilink_target(
    target: str,
    by_title: dict[str, list[CompiledPage]],
    by_alias: dict[str, list[CompiledPage]],
) -> tuple[CompiledPage | None, str]:
    """Resolve a wikilink target to exactly one Compiled Page.

    Wikilinks target by canonical title or alias (never by file path). The
    same single-match collision rule as other Knowledge Base lookup applies.
    """
    folded = target.strip().casefold()
    if not folded:
        return None, "broken"

    title_matches: list[CompiledPage] = []
    for key, pages in by_title.items():
        if key.casefold() == folded:
            title_matches.extend(pages)
    if len(title_matches) == 1:
        return title_matches[0], "ok"
    if len(title_matches) > 1:
        return None, "ambiguous"

    alias_matches: list[CompiledPage] = []
    for key, pages in by_alias.items():
        if key.casefold() == folded:
            alias_matches.extend(pages)
    if len(alias_matches) == 1:
        return alias_matches[0], "ok"
    if len(alias_matches) > 1:
        return None, "ambiguous"

    return None, "broken"


def _scan_body_links(body: str, body_start_line: int) -> list[tuple[str, int, int]]:
    """Return ``(destination_or_target, line_start, line_end)`` for each link.

    Scans for Markdown links ``[label](dest)`` (excluding images) and
    wikilinks ``[[Target]]`` / ``[[Target|alias]]``. Line numbers are 1-based
    and offset by ``body_start_line`` so they map to real source file lines.
    Links whose opening bracket is inside an inline-code span (backticks) are
    skipped: code is not navigational.
    """
    # Build a set of character spans covered by inline code so we can skip
    # links whose opening bracket lands inside code. A single-backtick span
    # is the common case; we treat backtick runs of any length as code fences.
    code_spans: list[tuple[int, int]] = []
    in_code = False
    fence_len = 0
    code_start = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "`":
            run = 0
            j = i
            while j < len(body) and body[j] == "`":
                run += 1
                j += 1
            if not in_code:
                in_code = True
                fence_len = run
                code_start = i
            elif run == fence_len:
                in_code = False
                code_spans.append((code_start, j))
                fence_len = 0
            i = j
        else:
            i += 1
    if in_code:
        code_spans.append((code_start, len(body)))

    def in_code_span(pos: int) -> bool:
        for start, end in code_spans:
            if start <= pos < end:
                return True
        return False

    # Precompute the file-line of each body character for range mapping.
    line_starts: list[int] = [0]
    for idx, ch in enumerate(body):
        if ch == "\n":
            line_starts.append(idx + 1)

    def char_to_file_line(pos: int) -> int:
        # 1-based body line of pos, then offset by body_start_line.
        import bisect

        body_line = bisect.bisect_right(line_starts, pos)
        return body_start_line + body_line - 1

    found: list[tuple[str, int, int]] = []

    for match in _MARKDOWN_LINK_RE.finditer(body):
        if in_code_span(match.start()):
            continue
        dest = match.group("dest")
        start_line = char_to_file_line(match.start())
        end_line = char_to_file_line(match.end() - 1)
        found.append((dest, start_line, max(start_line, end_line)))

    for match in _WIKILINK_RE.finditer(body):
        if in_code_span(match.start()):
            continue
        target = match.group("target")
        start_line = char_to_file_line(match.start())
        end_line = char_to_file_line(match.end() - 1)
        found.append((target, start_line, max(start_line, end_line)))

    return found


def _extract_references(
    pages: Sequence[CompiledPage],
) -> tuple[list[ExtractedReference], list[ExtractionDiagnostic]]:
    """Derive deduplicated Extracted References from Compiled Page bodies.

    Returns ``(references, diagnostics)``. References are deduplicated by
    ``(source_title, target_title)``: the FIRST source occurrence (by file
    line) is retained as provenance; later equivalents are dropped. Both
    lists are returned in deterministic order (references sorted by
    ``(source_title, target_title, line_start, source_path)``; diagnostics
    sorted by ``(source_path, line_start, target)``).

    Extraction is pure: it never mutates a page body or frontmatter and never
    promotes a reference into a typed Relationship.
    """
    by_title: dict[str, list[CompiledPage]] = {}
    by_alias: dict[str, list[CompiledPage]] = {}
    by_path: dict[str, CompiledPage] = {}
    for page in pages:
        if page.title:
            by_title.setdefault(page.title, []).append(page)
        for alias in page.aliases:
            if alias:
                by_alias.setdefault(alias, []).append(page)
        if page.path:
            stem = page.path
            if stem.lower().endswith(".md"):
                stem = stem[: -len(".md")]
            by_path.setdefault(stem.casefold(), page)

    raw: list[ExtractedReference] = []
    diagnostics: list[ExtractionDiagnostic] = []

    # Iterate pages in a stable order (by path then title) so diagnostics and
    # first-occurrence provenance are deterministic regardless of insertion
    # order. This mirrors the canonical adjacency determinism contract.
    ordered = sorted(pages, key=lambda p: (p.path, p.title))
    for page in ordered:
        if not page.body or not page.title:
            continue
        source_dir = _page_dir(page.path)
        for dest, line_start, line_end in _scan_body_links(page.body, page.body_start_line):
            # Try Markdown-link resolution first (path-aware), then wikilink
            # resolution (title/alias only). A bare ``[[Target]]`` has no path
            # so it is resolved as a wikilink; a ``[label](path.md)`` is
            # resolved as a Markdown link.
            if dest and (":" in dest or dest.startswith("/") or dest.startswith(".")):
                target_page, reason = _resolve_link_target(
                    dest, by_title, by_alias, by_path, source_dir
                )
            else:
                # Could be a wikilink target or a bare Markdown destination.
                target_page, reason = _resolve_link_target(
                    dest, by_title, by_alias, by_path, source_dir
                )
                if reason == "broken":
                    # Fall back to wikilink-style title/alias resolution.
                    target_page, reason = _resolve_wikilink_target(dest, by_title, by_alias)

            if target_page is not None:
                if target_page.title == page.title:
                    # Self-links produce no traversable edge.
                    continue
                raw.append(
                    ExtractedReference(
                        source_title=page.title,
                        target_title=target_page.title,
                        origin="markdown-link",
                        source_path=page.path,
                        line_start=line_start,
                        line_end=line_end,
                        extractor_version=EXTRACTOR_VERSION,
                    )
                )
            elif reason in {"broken", "ambiguous"}:
                diagnostics.append(
                    ExtractionDiagnostic(
                        source_path=page.path,
                        line_start=line_start,
                        target=dest,
                        kind=reason,
                        detail=(f"{reason} extracted-link target: {dest}"),
                    )
                )
            # ``external``, ``escaping``, ``reserved`` are intentionally
            # silent: they are expected, non-actionable exclusions.

    # Deduplicate by (source_title, target_title), keeping the first occurrence
    # by (line_start, source_path) for stable provenance.
    seen: set[tuple[str, str]] = set()
    deduped: list[ExtractedReference] = []
    for ref in sorted(
        raw,
        key=lambda r: (r.source_title, r.target_title, r.line_start, r.source_path),
    ):
        key = (ref.source_title, ref.target_title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)

    diagnostics.sort(key=lambda d: (d.source_path, d.line_start, d.target))
    return deduped, diagnostics


def extract_references(pages: Sequence[CompiledPage]) -> list[ExtractedReference]:
    """Public extraction entrypoint: return deduplicated Extracted References.

    Equivalent to ``_extract_references(pages)[0]``. Exposed for inspection and
    so callers can derive references without constructing a full KnowledgeBase.
    """
    return _extract_references(pages)[0]


class _KnowledgeIndex:
    """In-memory exact-lookup and graph indexes derived from a list of pages.

    Since issue #170 (ADR-0021) the graph adjacency is keyed by stable Entity
    IDs, not Canonical Page Titles: a title or path change does not alter
    Entity identity or traversal topology. Pages in Legacy Flat Mode without
    an Entity ID fall back to their Canonical Page Title as the graph key, so
    legacy Knowledge Bases keep working; identity remains the Entity ID
    wherever one is declared.
    """

    def __init__(self, pages: list[CompiledPage]) -> None:
        self.by_title: dict[str, list[CompiledPage]] = {}
        self.by_alias: dict[str, list[CompiledPage]] = {}
        self.by_tag: dict[str, list[CompiledPage]] = {}
        self.by_source: dict[str, list[CompiledPage]] = {}
        self.by_lifecycle: dict[str, list[CompiledPage]] = {}
        self.by_entity_id: dict[str, CompiledPage] = {}
        # Graph keys: stable Entity IDs (Canonical Page Title fallback for
        # Legacy Flat Mode pages without an Entity ID) — issue #170.
        self.graph_key_by_title: dict[str, str] = {}
        self.title_by_graph_key: dict[str, str] = {}
        self.adjacency: dict[str, list[GraphEdge]] = {}
        # Reverse adjacency: target graph key -> [GraphEdge(source)].
        # Built alongside the outgoing adjacency so incoming traversal and
        # ``both`` direction are derived from the same accepted-Claim set
        # without a second pass (issue #106).
        self.incoming: dict[str, list[GraphEdge]] = {}

        for page in pages:
            self.by_title.setdefault(page.title, []).append(page)
            if page.id:
                self.by_entity_id[page.id] = page
            if page.title:
                # Duplicated titles share the fallback key; canonical mode
                # validates duplicates as errors, so this only affects
                # already-invalid legacy input.
                self.graph_key_by_title[page.title] = page.id or page.title
                self.title_by_graph_key[page.id or page.title] = page.title
            for alias in page.aliases:
                self.by_alias.setdefault(alias, []).append(page)
            for tag in page.tags:
                self.by_tag.setdefault(tag, []).append(page)
            for source in page.sources:
                self.by_source.setdefault(source.id, []).append(page)
            self.by_lifecycle.setdefault(page.lifecycle or "", []).append(page)
        # Canonical adjacency (ADR-0021, issue #170): accepted entity-to-entity
        # Claims projected onto stable Entity IDs. Disputed and superseded
        # Claims stay inspectable but never enter traversal; literal Claims
        # are not graph edges.
        for page in pages:
            if not page.title:
                continue
            source_key = self.graph_key_by_title[page.title]
            for claim in page.claims:
                if claim.status != "accepted" or claim.object is None:
                    continue
                target = self.by_entity_id.get(claim.object)
                if target is None or not target.title:
                    continue
                edge = GraphEdge(
                    endpoint=self.graph_key_by_title[target.title],
                    predicate=claim.predicate,
                    claim_id=claim.id,
                    origin=GRAPH_EDGE_ORIGIN_CLAIM,
                    scope=GRAPH_EDGE_SCOPE_CANONICAL,
                )
                self.adjacency.setdefault(source_key, []).append(edge)
                self.incoming.setdefault(edge.endpoint, []).append(edge.reversed(source_key))
        # Canonical adjacency is stored pre-sorted so graph traversal
        # iterates canonical edges deterministically without per-node
        # materialization/sorting (ADR-0011, issue #106).
        for edges in self.adjacency.values():
            edges.sort(key=_graph_edge_sort_key)
        for edges in self.incoming.values():
            edges.sort(key=_graph_edge_sort_key)

        # Discovery Graph adjacency (issue #107, ADR-0011; issue #170):
        # EVERY canonical accepted Claim edge PLUS EVERY exactly-resolved
        # Extracted Reference, each retained with its full origin and
        # provenance metadata. Edges to the same endpoint are NOT merged
        # here: an Extracted Reference beside a Claim to the same target
        # keeps its own provenance in the graph state (issue #170), while
        # TRAVERSAL deduplicates endpoints through its visited set so a
        # related-page expansion still surfaces each page once. Extracted
        # edges carry no predicate/claim identity — they are never canonical
        # and are never promoted to Claims.
        extracted, diags = _extract_references(pages)
        self.extracted_references = extracted
        self.extraction_diagnostics = diags
        self.discovery_adjacency: dict[str, list[GraphEdge]] = {
            source_key: list(edges) for source_key, edges in self.adjacency.items()
        }
        for ref in extracted:
            src_key = self.graph_key_by_title.get(ref.source_title)
            tgt_key = self.graph_key_by_title.get(ref.target_title)
            if src_key is None or tgt_key is None:
                continue
            self.discovery_adjacency.setdefault(src_key, []).append(
                GraphEdge(
                    endpoint=tgt_key,
                    origin=GRAPH_EDGE_ORIGIN_EXTRACTED,
                    source_path=ref.source_path,
                    line_start=ref.line_start,
                    line_end=ref.line_end,
                    extractor_version=ref.extractor_version,
                    scope=GRAPH_EDGE_SCOPE_DISCOVERY,
                )
            )
        self.discovery_incoming: dict[str, list[GraphEdge]] = {
            source_key: list(edges) for source_key, edges in self.incoming.items()
        }
        for ref in extracted:
            src_key = self.graph_key_by_title.get(ref.source_title)
            tgt_key = self.graph_key_by_title.get(ref.target_title)
            if src_key is None or tgt_key is None:
                continue
            self.discovery_incoming.setdefault(tgt_key, []).append(
                GraphEdge(
                    endpoint=src_key,
                    origin=GRAPH_EDGE_ORIGIN_EXTRACTED,
                    source_path=ref.source_path,
                    line_start=ref.line_start,
                    line_end=ref.line_end,
                    extractor_version=ref.extractor_version,
                    scope=GRAPH_EDGE_SCOPE_DISCOVERY,
                )
            )
        for edges in self.discovery_adjacency.values():
            edges.sort(key=_graph_edge_sort_key)
        for edges in self.discovery_incoming.values():
            edges.sort(key=_graph_edge_sort_key)


def _graph_edge_sort_key(
    edge: GraphEdge,
) -> tuple[str, str, str, str, str, int, int, str, str]:
    """Deterministic total order over edges (issue #170)."""
    return edge.sort_key


class FrontmatterError(Exception):
    """Raised when a page's YAML frontmatter cannot be parsed."""


class KnowledgeBaseError(Exception):
    """Raised when a Knowledge Base cannot be loaded."""


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str, int]:
    """Split and decode YAML frontmatter from a Markdown page body.

    Returns the decoded frontmatter mapping, the body text, and the 1-indexed file
    line number of the first character of the body.
    """
    if not text.startswith("---"):
        raise FrontmatterError("page is missing frontmatter")

    end = text.find("\n---", 3)
    if end == -1:
        raise FrontmatterError("frontmatter is not closed")

    raw_yaml = text[3:end].strip()
    body_start_offset = end + 4
    body = text[body_start_offset:]
    body_start_line = text[:body_start_offset].count("\n") + 1

    try:
        data = yaml.decode(raw_yaml) if raw_yaml else {}
    except Exception as exc:
        raise FrontmatterError(f"frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise FrontmatterError("frontmatter is not a YAML mapping")

    return data, body, body_start_line


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _as_review_after(value: Any, relative: str) -> tuple[str | None, list[ValidationIssue]]:
    """Decode the optional ``review_after`` frontmatter field (ADR-0023).

    YAML decodes a bare ISO 8601 date to a ``datetime.date``; a QUOTED date
    (as rendered by OKF import and other double-quoting writers) stays a
    string and is accepted when it parses as one. Anything else (a malformed
    string, an integer, a full timestamp) is a blocking validation error.
    Returns the canonical ``YYYY-MM-DD`` string (or ``None`` when the field
    is absent) plus the structural issues for a malformed value.
    """
    if value is None:
        return None, []
    iso: str | None = None
    if isinstance(value, _date_cls) and not isinstance(value, datetime):
        iso = value.isoformat()
    elif isinstance(value, str):
        try:
            iso = _date_cls.fromisoformat(value.strip()).isoformat()
        except ValueError:
            iso = None
    if iso is not None:
        return iso, []
    return None, [
        ValidationIssue(
            file=relative,
            field="review_after",
            message=f"review_after must be an ISO 8601 date (YYYY-MM-DD), got {value!r}",
        )
    ]


def _as_sources(value: Any) -> list[Source]:
    if not isinstance(value, list):
        return []
    sources: list[Source] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        sources.append(
            Source(
                id=str(item.get("id", "")),
                title=str(item.get("title", "")),
                url=str(item.get("url")) if item.get("url") is not None else None,
            )
        )
    return sources


def _as_claims(value: Any, relative: str) -> tuple[list[Claim], list[ValidationIssue]]:
    """Build ``Claim`` records from raw frontmatter, reporting structural issues.

    A claim entry must be a mapping with a non-empty ``id`` and ``predicate``.
    ``evidence`` entries may carry ``section``, ``lines`` (a 2-list of 1-based
    body line numbers), or both. Ontology conformance is validated later
    against the Control File; this parser enforces only shape.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [ValidationIssue(file=relative, field="claims", message="claims must be a list")]
    claims: list[Claim] = []
    issues: list[ValidationIssue] = []
    for item in value:
        if not isinstance(item, dict):
            issues.append(
                ValidationIssue(
                    file=relative, field="claims", message="claims must be a list of mappings"
                )
            )
            continue
        claim_id = str(item.get("id", "")).strip()
        predicate = str(item.get("predicate", "")).strip()
        if not claim_id:
            issues.append(
                ValidationIssue(file=relative, field="claims", message="claim missing id")
            )
            continue
        if not predicate:
            issues.append(
                ValidationIssue(file=relative, field="claims", message="claim missing predicate")
            )
            continue
        status = str(item.get("status", "")).strip()
        object_value = item.get("object")
        if object_value is not None:
            object_value = str(object_value)
        value_value = item.get("value")
        value_type = item.get("value_type")
        if value_type is not None:
            value_type = str(value_type)
        confidence = item.get("confidence")
        origin = str(item.get("origin", CLAIM_ORIGIN_AUTHORED)) or CLAIM_ORIGIN_AUTHORED
        evidence: list[ClaimEvidence] = []
        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, list):
            for anchor in raw_evidence:
                if not isinstance(anchor, dict):
                    issues.append(
                        ValidationIssue(
                            file=relative,
                            field="claims",
                            message=f"claim {claim_id}: evidence must be a list of mappings",
                        )
                    )
                    continue
                section = anchor.get("section")
                lines = anchor.get("lines")
                line_start: int | None = None
                line_end: int | None = None
                if lines is not None:
                    if (
                        not isinstance(lines, list)
                        or len(lines) != 2
                        or any(not isinstance(n, int) or isinstance(n, bool) for n in lines)
                    ):
                        issues.append(
                            ValidationIssue(
                                file=relative,
                                field="claims",
                                message=(
                                    f"claim {claim_id}: evidence lines must be "
                                    f"[start, end] integers"
                                ),
                            )
                        )
                        lines = None
                    else:
                        line_start, line_end = lines
                evidence.append(
                    ClaimEvidence(
                        section=str(section) if section is not None else None,
                        line_start=line_start,
                        line_end=line_end,
                    )
                )
        valid_from = item.get("valid_from")
        valid_to = item.get("valid_to")
        claims.append(
            Claim(
                id=claim_id,
                predicate=predicate,
                status=status,
                object=object_value,
                value=value_value,
                value_type=value_type,
                confidence=float(confidence) if confidence is not None else None,
                origin=origin,
                valid_from=str(valid_from) if valid_from is not None else None,
                valid_to=str(valid_to) if valid_to is not None else None,
                evidence=evidence,
            )
        )
    return claims, issues


class NavigationIndexCollisionError(ValueError):
    """Raised when publication encounters an authored reserved index path."""


class ReservedArtifactClassification(msgspec.Struct, frozen=True):
    """Result of classifying a reserved-basename Markdown file's marker.

    ``valid`` is True only when a reserved file carries a well-formed, supported
    marker for the artifact kind its basename requires. ``issue`` carries an
    actionable message when the marker is missing or malformed.
    """

    valid: bool = False
    issue: str | None = None


def _reserved_artifact_basename(file: Path) -> str | None:
    """Return the canonical reserved basename ``file`` occupies, or ``None``.

    Reserved basenames are matched case-insensitively (``index.md``,
    ``INDEX.md``, ``Hot.MD``). Returns the canonical lowercase basename so the
    rest of the contract keys off one form.
    """
    lower = file.name.lower()
    return lower if lower in RESERVED_ARTIFACT_MARKERS else None


# ---------------------------------------------------------------------------
# Read-only content source (issue #120, ADR-0013).
#
# Loading, validation, and fingerprinting read canonical Knowledge Base content
# through this small seam so the SAME logic serves a local filesystem root and
# an in-memory materialization of an immutable S3 Published Version. The write
# path (reserved-artifact commit) stays filesystem-bound; only the read path is
# source-polymorphic. Filesystem behavior is byte-for-byte unchanged.
# ---------------------------------------------------------------------------


class _KbSource(Protocol):
    """Read-only source of canonical Knowledge Base content.

    Canonical content is exposed as relative POSIX paths so a local filesystem
    root and an in-memory materialization of a remote Published Version are
    interchangeable for read-only loading, validation, and fingerprinting. The
    ``root`` is a diagnostic label for the resolved location; read-only loading
    never touches the filesystem through it.
    """

    root: Path

    def markdown_files(self) -> list[str]:
        """Canonical Markdown files as sorted relative POSIX paths."""
        ...

    def read_bytes(self, rel: str) -> bytes:
        """Return the raw bytes of canonical file ``rel``."""
        ...

    def control_file_bytes(self) -> bytes | None:
        """Return the Control File bytes, or ``None`` when absent."""
        ...


class _FilesystemKbSource:
    """A :class:`_KbSource` over a local filesystem root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def markdown_files(self) -> list[str]:
        return [file.relative_to(self.root).as_posix() for file in _markdown_files(self.root)]

    def read_bytes(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def control_file_bytes(self) -> bytes | None:
        path = _control_file_path(self.root)
        return path.read_bytes() if path.exists() else None


class _InMemoryKbSource:
    """A :class:`_KbSource` over an in-memory materialization (no disk bytes).

    Used by the S3 capability (issue #120): an immutable Published Version's
    canonical content is read from object storage into memory, validated against
    the manifest content digests, and loaded here so the Core SDK never writes a
    managed Knowledge Base copy to local disk.
    """

    def __init__(self, files: dict[str, bytes], root: Path) -> None:
        self.root = root
        self._files = files

    def markdown_files(self) -> list[str]:
        return sorted(rel for rel in self._files if PurePosixPath(rel).suffix.lower() == ".md")

    def read_bytes(self, rel: str) -> bytes:
        return self._files[rel]

    def control_file_bytes(self) -> bytes | None:
        return self._files.get(CONTROL_FILE_BASENAME)


def _reserved_artifact_basename_of(relative: str) -> str | None:
    """String form of :func:`_reserved_artifact_basename` for a relative path."""
    lower = PurePosixPath(relative).name.lower()
    return lower if lower in RESERVED_ARTIFACT_MARKERS else None


def _markdown_files(root: Path) -> list[Path]:
    """Return Markdown files with case-insensitive ``.md`` extension matching."""
    return sorted(
        file for file in root.rglob("*") if file.is_file() and file.suffix.lower() == ".md"
    )


def _classify_reserved_artifact(
    data: dict[str, Any], basename: str
) -> ReservedArtifactClassification:
    """Classify a reserved-basename file's marker for the artifact kind ``basename``.

    A valid marker is a ``lumio`` mapping whose ``artifact`` matches the kind the
    basename reserves (e.g. ``index.md`` -> ``navigation-index``) and whose
    ``version`` is a genuine integer in that artifact's supported set. Returns a
    classification whose ``valid`` flag is True only for a fully-formed supported
    marker.
    """
    expected_artifact = RESERVED_ARTIFACT_MARKERS[basename]
    label = RESERVED_ARTIFACT_LABELS[expected_artifact]
    supported_versions = RESERVED_ARTIFACT_VERSIONS[expected_artifact]
    lumio_meta = data.get("lumio")
    if not isinstance(lumio_meta, dict):
        return ReservedArtifactClassification(
            issue=(
                f"reserved {label} path '{basename}' requires a valid 'lumio' "
                f"{expected_artifact} marker; see ADR-0007 and ADR-0008"
            ),
        )

    artifact = lumio_meta.get("artifact")
    if artifact != expected_artifact:
        return ReservedArtifactClassification(
            issue=(
                f"reserved {label} path '{basename}' has marker artifact "
                f"'{artifact}'; expected '{expected_artifact}'"
            ),
        )

    # Require a genuine integer: Python treats True == 1 and 1.0 == 1, so a
    # YAML boolean or float must not masquerade as a supported version.
    version = lumio_meta.get("version")
    is_int_version = isinstance(version, int) and not isinstance(version, bool)
    if not is_int_version or version not in supported_versions:
        return ReservedArtifactClassification(
            issue=(
                f"{label} marker version {version!r} on '{basename}' is not "
                f"supported; supported versions: {sorted(supported_versions)}"
            ),
        )

    return ReservedArtifactClassification(valid=True)


def _load_page(text: str, relative: str) -> tuple[CompiledPage, dict[str, Any]]:
    """Parse a single Markdown page into a Compiled Page and raw frontmatter.

    ``text`` is the page source and ``relative`` is its POSIX path relative to
    the Knowledge Base root. Source-agnostic so the same parser serves a
    filesystem root and an in-memory materialization (issue #120, ADR-0013).
    """
    data, body, body_start_line = _parse_frontmatter(text, Path(relative))
    claims, claim_issues = _as_claims(data.get("claims"), relative)
    review_after, _review_issues = _as_review_after(data.get("review_after"), relative)
    page = CompiledPage(
        path=relative,
        title=str(data.get("title", "")),
        id=str(data.get("id", "")),
        entity_types=_as_string_list(data.get("entity_types")),
        aliases=_as_string_list(data.get("aliases")),
        tags=_as_string_list(data.get("tags")),
        summary=str(data["summary"]) if "summary" in data else None,
        lifecycle=str(data.get("lifecycle")) if "lifecycle" in data else None,
        visibility=str(data.get("visibility")) if "visibility" in data else None,
        review_after=review_after,
        sources=_as_sources(data.get("sources")),
        claims=claims,
        synthetic=bool(data.get("synthetic", False)),
        body=body,
        body_start_line=body_start_line,
    )
    return page, data


def _claim_id_issue(claim_id: str) -> str | None:
    """Return a blocking-issue message for an invalid Claim ID, else None."""
    if not claim_id.startswith(CLAIM_ID_PREFIX):
        return (
            f"claim id {claim_id!r} must use the '{CLAIM_ID_PREFIX}' prefix "
            f"(e.g. {CLAIM_ID_PREFIX}slug)"
        )
    slug = claim_id[len(CLAIM_ID_PREFIX) :]
    if not IDENTIFIER_SLUG_RE.match(slug):
        return (
            f"claim id {claim_id!r} is not a valid slug: use lowercase "
            f"ASCII letters, digits, and hyphens; begin and end with a letter or digit"
        )
    return None


def _entity_id_issue(entity_id: str) -> str | None:
    """Return a blocking-issue message for an invalid Entity ID, else None."""
    if not entity_id.startswith(ENTITY_ID_PREFIX):
        return (
            f"entity id {entity_id!r} must use the '{ENTITY_ID_PREFIX}' prefix "
            f"(e.g. {ENTITY_ID_PREFIX}slug)"
        )
    slug = entity_id[len(ENTITY_ID_PREFIX) :]
    if not IDENTIFIER_SLUG_RE.match(slug):
        return (
            f"entity id {entity_id!r} is not a valid slug: use lowercase "
            f"ASCII letters, digits, and hyphens; begin and end with a letter or digit"
        )
    return None


def _entity_claim_page_issues(
    page: CompiledPage, data: dict[str, Any], relative: str
) -> list[ValidationIssue]:
    """Validate a page's Entity identity and Claim shape (ontology-free part).

    Cross-page ontology conformance (known types/predicates, domain/range,
    object resolution, ID uniqueness, evidence bounds) lives in
    :func:`_ontology_issues` because it needs the Control File and the whole
    page set.
    """
    issues: list[ValidationIssue] = []
    has_entity_contract = "id" in data or "entity_types" in data or "claims" in data

    if has_entity_contract:
        if not page.id:
            issues.append(
                ValidationIssue(file=relative, field="id", message="missing required field: id")
            )
        else:
            id_issue = _entity_id_issue(page.id)
            if id_issue:
                issues.append(ValidationIssue(file=relative, field="id", message=id_issue))
        if not page.entity_types:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="entity_types",
                    message="an Entity page must declare at least one entity type",
                )
            )
        if "entity_types" in data and not isinstance(data["entity_types"], list):
            issues.append(
                ValidationIssue(
                    file=relative, field="entity_types", message="entity_types must be a list"
                )
            )

    for claim in page.claims:
        id_issue = _claim_id_issue(claim.id)
        if id_issue:
            issues.append(ValidationIssue(file=relative, field="claims", message=id_issue))
        if claim.status not in PUBLISHED_CLAIM_STATUSES:
            if claim.status in PROPOSAL_ONLY_CLAIM_STATUSES:
                issues.append(
                    ValidationIssue(
                        file=relative,
                        field="claims",
                        message=(
                            f"claim {claim.id}: status '{claim.status}' is an Ingest "
                            f"Proposal state; an active Compiled Page may declare only "
                            f"{', '.join(sorted(PUBLISHED_CLAIM_STATUSES))} claims"
                        ),
                    )
                )
            else:
                issues.append(
                    ValidationIssue(
                        file=relative,
                        field="claims",
                        message=(
                            f"claim {claim.id}: invalid status {claim.status!r}; "
                            f"published statuses are: "
                            f"{', '.join(sorted(PUBLISHED_CLAIM_STATUSES))}"
                        ),
                    )
                )
        has_object = claim.object is not None
        has_value = claim.value is not None
        if claim.value is None and claim.value_type is not None:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=f"claim {claim.id}: value_type without a value",
                )
            )
        if has_object and has_value:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=(f"claim {claim.id}: exactly one of object or value is allowed"),
                )
            )
        elif not has_object and not has_value:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=(f"claim {claim.id}: exactly one of object or value is required"),
                )
            )
        elif has_value and claim.value is not None and claim.value_type is None:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=f"claim {claim.id}: value requires value_type",
                )
            )
        if claim.origin not in CLAIM_ORIGINS:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=f"claim {claim.id}: unknown origin {claim.origin!r}",
                )
            )
        for field_name, raw_time in (
            ("valid_from", claim.valid_from),
            ("valid_to", claim.valid_to),
        ):
            if raw_time is None:
                continue
            try:
                _date_cls.fromisoformat(str(raw_time))
            except ValueError:
                issues.append(
                    ValidationIssue(
                        file=relative,
                        field="claims",
                        message=(
                            f"claim {claim.id}: {field_name} {raw_time!r} is not an ISO 8601 date"
                        ),
                    )
                )
        if not claim.evidence:
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="claims",
                    message=(
                        f"claim {claim.id}: missing evidence; every Claim needs at "
                        f"least one anchor into the owning page's published content"
                    ),
                )
            )

    return issues


def _body_line_count(page: CompiledPage) -> int:
    """Number of lines in the page body (the anchorable content)."""
    if not page.body:
        return 0
    return page.body.count("\n") + (0 if page.body.endswith("\n") else 1)


def _body_sections(page: CompiledPage) -> set[str]:
    """Markdown ATX headings present in the page body."""
    return {
        match.group(1).strip()
        for match in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", page.body, re.MULTILINE)
    }


def _literal_value_kind(value: object) -> str | None:
    """Classify a raw literal value's YAML type against LITERAL_KINDS."""
    if isinstance(value, bool):
        return LITERAL_KIND_BOOLEAN
    if isinstance(value, (int, float)):
        return LITERAL_KIND_NUMBER
    if isinstance(value, str):
        return LITERAL_KIND_STRING
    return None


def _ontology_issues(pages: list[CompiledPage], ontology: Ontology | None) -> list[ValidationIssue]:
    """Validate Entity identity, Claims, and redirects against the ontology.

    Aggregates every blocking finding: duplicate/dangling Entity and Claim
    IDs, unknown Entity Types and Predicates, subject domain and object range
    violations, literal-kind mismatches, missing evidence anchors, evidence
    section/line bounds, redirect target resolution, and redirect cycles.
    ``ontology`` is None in Legacy Flat Mode (no Control File): only the
    identity/uniqueness checks that do not need the ontology run.
    """
    issues: list[ValidationIssue] = []
    entity_types = ontology.entity_types if ontology is not None else {}
    predicates = ontology.predicates if ontology is not None else {}

    # ADR-0021: in a Knowledge Base with a Control File (ontology present),
    # EVERY page declares exactly one stable Entity ID and at least one
    # controlled Entity Type. In Legacy Flat Mode (no Control File) the entity
    # contract is optional — there is no ontology to control it against.
    if ontology is not None:
        for page in pages:
            if not page.id:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="id",
                        message=(
                            "missing required field: id (every page in a "
                            "version-2 Knowledge Base declares one stable Entity ID)"
                        ),
                    )
                )
            if not page.entity_types:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="entity_types",
                        message=("an Entity page must declare at least one entity type"),
                    )
                )

    seen_entity_ids: dict[str, str] = {}
    seen_claim_ids: dict[str, str] = {}
    pages_by_entity: dict[str, CompiledPage] = {}
    # First pass: register every Entity ID before any Claim is validated, so
    # a Claim may reference an object declared later in page order.
    for page in pages:
        if not page.id:
            continue
        pages_by_entity.setdefault(page.id, page)
    for page in pages:
        if not page.id:
            continue
        if page.id in seen_entity_ids:
            issues.append(
                ValidationIssue(
                    file=page.path,
                    field="id",
                    message=(
                        f"duplicate entity id: {page.id} (also declared by "
                        f"{seen_entity_ids[page.id]})"
                    ),
                )
            )
        else:
            seen_entity_ids[page.id] = page.path
        for entity_type in page.entity_types:
            if entity_type not in entity_types:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="entity_types",
                        message=(
                            f"unknown entity type: {entity_type}"
                            + (
                                ""
                                if entity_types
                                else "; declare it under ontology.entity_types in lumio.yaml"
                            )
                        ),
                    )
                )
        line_count = _body_line_count(page)
        sections = _body_sections(page)
        for claim in page.claims:
            if claim.id in seen_claim_ids:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="claims",
                        message=(
                            f"duplicate claim id: {claim.id} (also declared by "
                            f"{seen_claim_ids[claim.id]})"
                        ),
                    )
                )
            else:
                seen_claim_ids[claim.id] = page.path
            definition = predicates.get(claim.predicate)
            if definition is None:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="claims",
                        message=(
                            f"claim {claim.id}: unknown predicate: {claim.predicate}"
                            + (
                                ""
                                if predicates
                                else "; declare it under ontology.predicates in lumio.yaml"
                            )
                        ),
                    )
                )
                # Domain/range checks need the definition, but object existence
                # is definition-independent and still validated (ADR-0021).
            if (
                definition is not None
                and definition.subject_types
                and not set(page.entity_types) & set(definition.subject_types)
            ):
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="claims",
                        message=(
                            f"claim {claim.id}: predicate {claim.predicate} requires a "
                            f"subject with one of types {sorted(definition.subject_types)} "
                            f"(page has {sorted(page.entity_types) or 'no types'}) — domain violation"
                        ),
                    )
                )
            if claim.object is not None:
                if definition is not None and definition.literal_kind is not None:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: predicate {claim.predicate} does not "
                                f"accept an entity object (literal_kind: "
                                f"{definition.literal_kind})"
                            ),
                        )
                    )
                target = pages_by_entity.get(claim.object)
                if target is None:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=f"claim {claim.id}: dangling object entity: {claim.object}",
                        )
                    )
                elif (
                    definition is not None
                    and definition.object_types
                    and not set(target.entity_types) & set(definition.object_types)
                ):
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: predicate {claim.predicate} requires an "
                                f"object with one of types {sorted(definition.object_types)} "
                                f"(entity {claim.object} has "
                                f"{sorted(target.entity_types) or 'no types'}) — range violation"
                            ),
                        )
                    )
            elif claim.value is not None or claim.value_type is not None:
                if definition is not None and definition.literal_kind is None:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: predicate {claim.predicate} does not "
                                f"accept a literal object"
                            ),
                        )
                    )
                elif definition is not None and claim.value_type != definition.literal_kind:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: literal kind mismatch: predicate "
                                f"{claim.predicate} accepts {definition.literal_kind}, "
                                f"claim declares {claim.value_type}"
                            ),
                        )
                    )
                value_kind = _literal_value_kind(claim.value)
                expected = definition.literal_kind if definition is not None else claim.value_type
                if value_kind is not None and value_kind != expected:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: literal value kind mismatch: value "
                                f"is {value_kind}, {claim.predicate} expects {expected}"
                            ),
                        )
                    )
            for anchor in claim.evidence:
                if anchor.section is None and anchor.line_start is None:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: evidence anchor must identify a "
                                f"section or a bounded line range"
                            ),
                        )
                    )
                if anchor.section is not None and anchor.section not in sections:
                    issues.append(
                        ValidationIssue(
                            file=page.path,
                            field="claims",
                            message=(
                                f"claim {claim.id}: unknown evidence section: {anchor.section!r}"
                            ),
                        )
                    )
                if anchor.line_start is not None or anchor.line_end is not None:
                    start = anchor.line_start or 1
                    end = anchor.line_end or start
                    if start < 1 or end < start or end > max(line_count, 1):
                        issues.append(
                            ValidationIssue(
                                file=page.path,
                                field="claims",
                                message=(
                                    f"claim {claim.id}: evidence lines [{start}, {end}] "
                                    f"are out of bounds (body has {line_count} lines)"
                                ),
                            )
                        )

    if ontology is not None:
        for redirect in ontology.redirects:
            id_issue = _entity_id_issue(redirect.from_id)
            if id_issue:
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME, field="ontology.redirects", message=id_issue
                    )
                )
            elif redirect.from_id in seen_entity_ids:
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME,
                        field="ontology.redirects",
                        message=(
                            f"redirect source {redirect.from_id} is a live entity; "
                            f"only a retired (merged) entity may redirect"
                        ),
                    )
                )
            target = redirect.to_id
            resolved: set[str] = set()
            cursor: str | None = target
            hop_redirects = {r.from_id: r.to_id for r in ontology.redirects}
            while cursor is not None and cursor in hop_redirects and cursor not in resolved:
                resolved.add(cursor)
                cursor = hop_redirects[cursor]
            if cursor in resolved:
                # The walk revisited a redirect id: the chain never terminates.
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME,
                        field="ontology.redirects",
                        message=(
                            f"redirect cycle: {redirect.from_id} -> {target} never "
                            f"reaches a live entity (merge-cycle)"
                        ),
                    )
                )
            elif cursor not in seen_entity_ids:
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME,
                        field="ontology.redirects",
                        message=(
                            f"redirect target does not resolve: {redirect.from_id} -> "
                            f"{target} (terminal id {cursor} is not a live entity)"
                        ),
                    )
                )

    return issues


def _as_ontology(value: Any) -> tuple[Ontology, list[ValidationIssue]]:
    """Build the :class:`Ontology` from raw Control File YAML.

    Structural only: known shapes, valid slug ids, known literal kinds, and
    internal reference resolution (a Predicate's ``subject_types`` /
    ``object_types`` / ``inverse`` must reference declared vocabulary).
    """
    if value is None:
        return Ontology(), []
    if not isinstance(value, dict):
        return Ontology(), [
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="ontology",
                message="ontology must be a mapping with entity_types and predicates",
            )
        ]
    issues: list[ValidationIssue] = []

    raw_entity_types = value.get("entity_types")
    entity_types: dict[str, EntityTypeDefinition] = {}
    if raw_entity_types is not None:
        if not isinstance(raw_entity_types, dict):
            issues.append(
                ValidationIssue(
                    file=CONTROL_FILE_BASENAME,
                    field="ontology.entity_types",
                    message="ontology.entity_types must be a mapping of type id to definition",
                )
            )
        else:
            for type_id, raw_def in raw_entity_types.items():
                name_issue = _category_name_issue(str(type_id))
                if name_issue is not None:
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="ontology.entity_types",
                            message=f"invalid entity type id {type_id!r}: use a lowercase slug",
                        )
                    )
                    continue
                description = None
                if isinstance(raw_def, dict) and raw_def.get("description") is not None:
                    description = str(raw_def["description"])
                entity_types[type_id] = EntityTypeDefinition(description=description)

    raw_predicates = value.get("predicates")
    predicates: dict[str, PredicateDefinition] = {}
    if raw_predicates is not None:
        if not isinstance(raw_predicates, dict):
            issues.append(
                ValidationIssue(
                    file=CONTROL_FILE_BASENAME,
                    field="ontology.predicates",
                    message="ontology.predicates must be a mapping of predicate id to definition",
                )
            )
        else:
            for predicate_id, raw_def in raw_predicates.items():
                if not isinstance(predicate_id, str) or not IDENTIFIER_SLUG_RE.match(predicate_id):
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="ontology.predicates",
                            message=(
                                f"invalid predicate id {predicate_id!r}: use a lowercase "
                                f"slug (letters, digits, hyphens)"
                            ),
                        )
                    )
                    continue
                if not isinstance(raw_def, dict):
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="ontology.predicates",
                            message=f"predicate {predicate_id!r} must be a mapping",
                        )
                    )
                    continue
                subject_types = _as_string_list(raw_def.get("subject_types"))
                object_types = _as_string_list(raw_def.get("object_types"))
                literal_kind = raw_def.get("literal_kind")
                if literal_kind is not None and literal_kind not in LITERAL_KINDS:
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="ontology.predicates",
                            message=(
                                f"predicate {predicate_id!r}: unknown literal_kind "
                                f"{literal_kind!r}; known kinds: {sorted(LITERAL_KINDS)}"
                            ),
                        )
                    )
                    literal_kind = None
                inverse = raw_def.get("inverse")
                synonyms = _as_string_list(raw_def.get("synonyms"))
                description = raw_def.get("description")
                predicates[predicate_id] = PredicateDefinition(
                    subject_types=subject_types,
                    object_types=object_types,
                    literal_kind=str(literal_kind) if literal_kind is not None else None,
                    inverse=str(inverse) if inverse is not None else None,
                    synonyms=synonyms,
                    description=str(description) if description is not None else None,
                )

    raw_redirects = value.get("redirects")
    redirects: list[EntityRedirect] = []
    if raw_redirects is not None:
        if not isinstance(raw_redirects, dict):
            issues.append(
                ValidationIssue(
                    file=CONTROL_FILE_BASENAME,
                    field="ontology.redirects",
                    message="ontology.redirects must be a mapping of retired id to surviving id",
                )
            )
        else:
            for from_id, to_id in raw_redirects.items():
                redirects.append(EntityRedirect(from_id=str(from_id), to_id=str(to_id)))

    # Internal reference resolution inside the ontology itself.
    for predicate_id, definition in predicates.items():
        for type_id in definition.subject_types + definition.object_types:
            if type_id not in entity_types:
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME,
                        field="ontology.predicates",
                        message=(
                            f"predicate {predicate_id!r} references undeclared "
                            f"entity type {type_id!r}"
                        ),
                    )
                )
        if definition.inverse is not None and definition.inverse not in predicates:
            issues.append(
                ValidationIssue(
                    file=CONTROL_FILE_BASENAME,
                    field="ontology.predicates",
                    message=(
                        f"predicate {predicate_id!r} declares inverse "
                        f"{definition.inverse!r}, which is not a declared predicate"
                    ),
                )
            )

    return (
        Ontology(entity_types=entity_types, predicates=predicates, redirects=redirects),
        issues,
    )


def is_due_for_review(review_after: str | None, *, today: _date_cls | None = None) -> bool:
    """Whether a page with ``review_after`` is due for review (ADR-0023).

    Due means ``today >= review_after``; an absent field never carries a
    freshness opinion. Expects the canonical ``YYYY-MM-DD`` string produced by
    :func:`_as_review_after`. Advisory only — never blocks validation.
    """
    if review_after is None:
        return False
    return (today if today is not None else _date_cls.today()) >= _date_cls.fromisoformat(
        review_after
    )


def due_review_pages(
    pages: Sequence[CompiledPage], *, today: _date_cls | None = None
) -> list[CompiledPage]:
    """Return the pages due for review, most overdue first (ADR-0023).

    Deterministic ranking: ascending ``review_after`` (the oldest review date
    is the most overdue), then by path for stable ties. Model-free and
    read-only; computed from the current date at call time.
    """
    due = [page for page in pages if is_due_for_review(page.review_after, today=today)]
    return sorted(due, key=lambda page: (page.review_after or "", page.path))


def _validate_page(
    page: CompiledPage, data: dict[str, Any], relative: str
) -> list[ValidationIssue]:
    """Validate a single page, returning all issues for that page."""
    page_issues: list[ValidationIssue] = []

    if not page.title:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="title",
                message="missing required field: title",
            )
        )

    if "lifecycle" not in data:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="lifecycle",
                message="missing required field: lifecycle",
            )
        )
    elif page.lifecycle not in VALID_LIFECYCLES:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="lifecycle",
                message=f"invalid lifecycle value: {page.lifecycle}",
            )
        )

    if "visibility" not in data:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="visibility",
                message="missing required field: visibility",
            )
        )
    elif page.visibility not in VALID_VISIBILITIES:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="visibility",
                message=f"invalid visibility value: {page.visibility}",
            )
        )

    # Freshness (ADR-0023): a malformed review_after date blocks; a due page
    # is advisory only — a warning that never fails validation.
    _review_after, review_after_issues = _as_review_after(data.get("review_after"), relative)
    page_issues.extend(review_after_issues)
    if not review_after_issues and is_due_for_review(page.review_after):
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="review_after",
                message=(
                    f"page is due for review (review_after {page.review_after} "
                    "reached); re-review it or extend the date"
                ),
                severity="warning",
            )
        )

    if "tags" not in data:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="tags",
                message="missing required field: tags",
            )
        )
    elif not isinstance(data["tags"], list):
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="tags",
                message="tags must be a list",
            )
        )
    elif not page.tags:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="tags",
                message="tags must not be empty",
            )
        )

    if "aliases" in data and not isinstance(data["aliases"], list):
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="aliases",
                message="aliases must be a list",
            )
        )

    sources_valid = True
    if "sources" in data:
        if not isinstance(data["sources"], list):
            page_issues.append(
                ValidationIssue(
                    file=relative,
                    field="sources",
                    message="sources must be a list",
                )
            )
            sources_valid = False
        elif any(not isinstance(item, dict) for item in data["sources"]):
            page_issues.append(
                ValidationIssue(
                    file=relative,
                    field="sources",
                    message="sources must be a list of mappings",
                )
            )
            sources_valid = False

    if sources_valid and not page.synthetic and not page.sources:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="sources",
                message="non-synthetic page must have at least one source",
            )
        )

    if "relationships" in data:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="relationships",
                message=(
                    "relationships frontmatter input was removed; declare "
                    "evidence-bearing claims instead (ADR-0021)"
                ),
            )
        )

    page_issues.extend(_entity_claim_page_issues(page, data, relative))

    if not page.summary:
        page_issues.append(
            ValidationIssue(
                file=relative,
                field="summary",
                message="missing recommended field: summary",
                severity="warning",
            )
        )

    return page_issues


def _load_pages_and_validate(
    source: _KbSource,
) -> tuple[list[CompiledPage], list[ValidationIssue]]:
    """Load every Markdown page from ``source`` and collect per-page issues.

    A validly-marked reserved artifact (case-insensitive ``index.md``,
    ``log.md``, or ``hot.md`` with the matching supported ``lumio`` marker) is
    a reserved derived artifact: it is excluded from the loaded Compiled Page
    collection. A reserved path with a missing or malformed marker is a
    blocking validation error rather than being skipped (issues #64 and #77).

    Source-agnostic (issue #120, ADR-0013): the same logic serves a filesystem
    root and an in-memory materialization of an immutable S3 Published Version.
    """
    pages: list[CompiledPage] = []
    issues: list[ValidationIssue] = []

    for relative in source.markdown_files():
        reserved_basename = _reserved_artifact_basename_of(relative)
        basename = PurePosixPath(relative).name

        try:
            page, data = _load_page(source.read_bytes(relative).decode("utf-8"), relative)
        except FrontmatterError as exc:
            # A reserved path that cannot even parse frontmatter is still a
            # blocking collision: report it as a reserved-artifact issue.
            if reserved_basename is not None:
                label = RESERVED_ARTIFACT_LABELS[RESERVED_ARTIFACT_MARKERS[reserved_basename]]
                issues.append(
                    ValidationIssue(
                        file=relative,
                        field="lumio",
                        message=(
                            f"reserved {label} path '{basename}' has invalid frontmatter: {exc}"
                        ),
                    )
                )
                continue
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="frontmatter",
                    message=str(exc),
                )
            )
            continue

        # Reserved derived-artifact classification (issues #64 and #77).
        if reserved_basename is not None:
            classification = _classify_reserved_artifact(data, reserved_basename)
            if classification.valid:
                # A valid derived artifact: excluded from pages, no issue.
                continue
            # Missing or malformed marker on a reserved path is blocking.
            issues.append(
                ValidationIssue(
                    file=relative,
                    field="lumio",
                    message=classification.issue
                    or (
                        f"reserved '{basename}' is not a valid "
                        f"{RESERVED_ARTIFACT_MARKERS[reserved_basename]} marker"
                    ),
                )
            )
            continue

        pages.append(page)
        issues.extend(_validate_page(page, data, relative))

    return pages, issues


def _cross_page_issues(
    pages: list[CompiledPage],
    index: _KnowledgeIndex,
) -> list[ValidationIssue]:
    """Return cross-page validation issues including QA issue kinds (issue #86).

    Surfaces the pre-existing structural checks (duplicate title, duplicate
    alias, unresolved relationship target) PLUS the Maintainer QA issue kinds
    defined in ADR-0011 and issue #86:

    * **orphan** — pages with no inbound topology. The finding discloses
      whether it uses canonical-graph scope (no reviewed inbound
      Relationships) or Discovery-Graph scope (no inbound topology at all,
      including Extracted References), so a page reachable only via body
      links is not confused with one having reviewed semantic inbound.
    * **broken-internal-link** — broken/ambiguous/escaping/duplicate/
      unsupported link outcomes from the shared Extracted Reference
      resolver, carried with source page, severity, and location context.
      Exactly-resolved links are derived state, never broken (ADR-0011).
    * **stale** — pages in the ``deprecated`` Lifecycle, surfaced as
      content-health signals for Maintainer review.
    * **contradiction** — ``contradicts`` typed Relationships, surfaced so
      active contradiction claims are visible at a glance.

    Missing-frontmatter issues are already produced per-page by
    ``_validate_page``; this function does not duplicate them.

    All new QA findings are non-blocking ``warning`` severity, consistent
    with ADR-0011's rule that derived reference state and content-health
    advisories never block validation. The checks are deterministic and
    model-free.

    ``index`` is the Discovery Graph index from issue #107, carrying the
    canonical and discovery adjacency plus the shared resolver's extraction
    diagnostics. The caller builds it once so every QA check consumes the
    same derived outcomes.
    """
    issues: list[ValidationIssue] = []

    titles_by_page: dict[str, list[CompiledPage]] = {}
    aliases_by_page: dict[str, list[CompiledPage]] = {}

    for page in pages:
        if page.title:
            titles_by_page.setdefault(page.title, []).append(page)
        for alias in page.aliases:
            if alias:
                aliases_by_page.setdefault(alias, []).append(page)

    for title, dup_pages in titles_by_page.items():
        if len(dup_pages) > 1:
            for page in dup_pages:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="title",
                        message=f"duplicate canonical title: {title}",
                    )
                )

    for alias, dup_pages in aliases_by_page.items():
        if len(dup_pages) > 1:
            for page in dup_pages:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="aliases",
                        message=f"duplicate alias: {alias}",
                    )
                )

    canonical_titles = set(titles_by_page.keys())
    if not canonical_titles and not pages:
        return issues
    del canonical_titles

    issues.extend(_orphan_issues(pages, index))
    issues.extend(_broken_internal_link_issues(index))
    issues.extend(_stale_issues(pages))
    issues.extend(_contradiction_issues(pages))

    return issues


def _orphan_issues(pages: list[CompiledPage], index: _KnowledgeIndex) -> list[ValidationIssue]:
    """Return orphan-page findings with canonical vs discovery scope disclosure.

    A page is an orphan when it has no inbound topology from OTHER pages.
    Two scopes are distinguished (ADR-0011, issue #86 AC2):

    * **Discovery scope** — the page has no inbound topology at all: no
      reviewed Relationship and no Extracted Reference targets it. This is
      the stronger orphan signal.
    * **Canonical scope** — the page has inbound Extracted References (body
      links) but no reviewed inbound Relationships. It has navigational
      context but no semantic inbound; it is an orphan of the reviewed
      canonical graph only.

    Self-targeting edges (a Relationship or body link from a page to itself)
    do not count as inbound topology and do not mask orphan status.
    """
    issues: list[ValidationIssue] = []
    for page in pages:
        title = page.title
        if not title:
            continue
        key = index.graph_key_by_title.get(title)
        if key is None:
            continue
        has_canonical_inbound = any(edge.endpoint != key for edge in index.incoming.get(key, []))
        has_discovery_inbound = any(
            edge.endpoint != key for edge in index.discovery_incoming.get(key, [])
        )
        if has_discovery_inbound:
            # Has inbound topology (at least via body links). If it also has
            # canonical inbound it is not an orphan at all; otherwise it is
            # an orphan of the canonical graph only.
            if not has_canonical_inbound:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="topology",
                        severity="warning",
                        message=(
                            "orphan page (canonical scope): no reviewed inbound "
                            "Relationships; has inbound Extracted References"
                        ),
                    )
                )
        else:
            # No inbound topology from any other page in either graph scope.
            issues.append(
                ValidationIssue(
                    file=page.path,
                    field="topology",
                    severity="warning",
                    message=("orphan page (discovery scope): no inbound topology at all"),
                )
            )
    return issues


def _broken_internal_link_issues(
    index: _KnowledgeIndex,
) -> list[ValidationIssue]:
    """Surface shared-resolver link diagnostics as broken-internal-link findings.

    Consumes the ``ExtractionDiagnostic`` outcomes produced by the shared
    Extracted Reference resolver (issue #107, ADR-0011) and surfaces each as
    a non-blocking ``warning`` with source page, target, kind, and line
    location so a Maintainer can act. Exactly-resolved links produce Extracted
    References, NOT diagnostics, and are therefore never reported here
    (issue #86 AC4). External, escaping, and reserved-artifact targets are
    expected, correct exclusions and are intentionally silent in the
    resolver; this function does not re-introduce them.
    """
    issues: list[ValidationIssue] = []
    for diag in index.extraction_diagnostics:
        issues.append(
            ValidationIssue(
                file=diag.source_path,
                field="internal-links",
                severity="warning",
                message=(
                    f"{diag.kind} internal link to '{diag.target}' "
                    f"(line {diag.line_start}): {diag.detail}"
                ),
            )
        )
    return issues


def _stale_issues(pages: list[CompiledPage]) -> list[ValidationIssue]:
    """Return non-blocking findings for pages in the ``deprecated`` Lifecycle.

    A deprecated page is valid content that has been deliberately marked as
    superseded or outdated. Surfacing it as ``stale`` gives Maintainers a
    content-health signal without blocking validation.
    """
    issues: list[ValidationIssue] = []
    for page in pages:
        if (page.lifecycle or "") == "deprecated":
            issues.append(
                ValidationIssue(
                    file=page.path,
                    field="lifecycle",
                    severity="warning",
                    message="stale page: deprecated lifecycle",
                )
            )
    return issues


def _contradiction_issues(pages: list[CompiledPage]) -> list[ValidationIssue]:
    """Return non-blocking findings for ``contradicts``-predicated Claims.

    A claim whose predicate is ``contradicts`` (when the KB's ontology
    declares it) is a valid, reviewed semantic claim. It is surfaced here so
    active contradiction claims are visible at a glance in the QA report,
    supporting review-by-exception (#111).
    """
    issues: list[ValidationIssue] = []
    for page in pages:
        for claim in page.claims:
            if claim.predicate != "contradicts" or claim.object is None:
                continue
            if claim.status != "accepted":
                continue
            issues.append(
                ValidationIssue(
                    file=page.path,
                    field="claims",
                    severity="warning",
                    message=f"contradiction claim: {claim.object}",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Knowledge Base Control File (issue #77).
#
# A categorized Knowledge Base carries a versioned root ``lumio.yaml`` declaring
# its Content Category catalog and Maintainer-pinned Hot Index titles. It is
# KB-local content control: validated by the Core SDK, portable with the KB,
# fingerprinted as canonical content, but neither a Compiled Page nor an OKF
# concept. A Knowledge Base with no Control File loads in Legacy Flat Mode with
# a non-blocking migration warning (ADR-0008).
# ---------------------------------------------------------------------------


class ControlFileError(Exception):
    """Raised when a Knowledge Base Control File cannot be parsed."""


def _control_file_path(root: Path) -> Path:
    return root / CONTROL_FILE_BASENAME


def _as_categories(value: Any) -> tuple[list[ContentCategory], bool]:
    """Build ``ContentCategory`` records from raw YAML, reporting structural validity.

    Returns the parsed categories and a flag that is False when the raw value
    was structurally invalid (the caller emits a dedicated validation issue).
    """
    if not isinstance(value, list):
        return [], False
    categories: list[ContentCategory] = []
    for item in value:
        if isinstance(item, str):
            # A bare-string category must be non-empty, exactly like the
            # ``name`` field of a mapping entry. An empty bare name is a
            # blocking validation error (issue #77).
            if not item.strip():
                return [], False
            categories.append(ContentCategory(name=item))
            continue
        if not isinstance(item, dict):
            return [], False
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return [], False
        description = item.get("description")
        categories.append(
            ContentCategory(
                name=name,
                description=str(description) if description is not None else None,
            )
        )
    return categories, True


def _category_name_issue(name: str) -> str | None:
    """Return a blocking-issue message for an invalid Content Category name.

    Validates the slug shape and the reserved-name guard from ADR-0009:
    lowercase ASCII letters/digits/hyphens, leading letter, bounded length,
    and no collision with reserved basenames/markers (``index``, ``hot``,
    ``log``, ``lumio``). Returns ``None`` when the name is acceptable.
    """
    if name in RESERVED_CATEGORY_NAMES:
        return (
            f"content category {name!r} collides with a reserved basename or "
            f"marker (index, hot, log, lumio); see ADR-0007 and ADR-0009"
        )
    if not CATEGORY_SLUG_RE.match(name):
        return (
            f"content category {name!r} is not a valid slug: use lowercase "
            f"ASCII letters, digits, and hyphens; begin with a letter; "
            f"1..{CATEGORY_MAX_LENGTH} characters (ADR-0009)"
        )
    return None


def _as_hot_index_pins(value: Any) -> tuple[list[HotIndexPin], bool]:
    """Build ``HotIndexPin`` records from raw YAML, reporting structural validity.

    Returns the parsed pins and a flag that is False when the raw value was
    structurally invalid (the caller emits a dedicated validation issue). A
    blank or whitespace-only pin title is rejected for both the bare-string and
    mapping forms, exactly like a Category ``name`` (issue #77).
    """
    if not isinstance(value, list):
        return [], False
    pins: list[HotIndexPin] = []
    for item in value:
        if isinstance(item, str):
            if not item.strip():
                return [], False
            pins.append(HotIndexPin(title=item))
            continue
        if not isinstance(item, dict):
            return [], False
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], False
        note = item.get("note")
        pins.append(HotIndexPin(title=title, note=str(note) if note is not None else None))
    return pins, True


def _load_and_validate_control_file(
    source: _KbSource, pages: list[CompiledPage] | None
) -> tuple[KnowledgeBaseControlFile | None, list[ValidationIssue]]:
    """Load and validate the root Control File, or signal Legacy Flat Mode.

    Returns the parsed Control File (or ``None`` when absent) plus validation
    issues. An absent Control File yields a single non-blocking migration
    warning — Legacy Flat Mode keeps existing root-level pages valid. When
    present, the file is validated as a KB-local content control: supported
    version, a Content Category catalog that is either absent/empty (applies
    the seeded default) or a well-formed list of slug-valid, reserved-name-
    safe, unique category names (ADR-0009), and Hot Index pins that resolve
    to existing Canonical Page Titles (analogous to Relationship target
    resolution). Declared categories are first-class for navigation,
    validation, and retrieval alongside seeded ones; each Compiled Page's
    path-derived category must resolve to a declared entry. Pass
    ``pages=None`` to skip cross-page pin and category resolution (the
    lightweight parse seam used by :func:`load_control_file`).

    Source-agnostic (issue #120, ADR-0013): reads canonical content through a
    :class:`_KbSource` so the same validation serves a filesystem root and an
    in-memory S3 materialization.
    """
    raw_bytes = source.control_file_bytes()
    if raw_bytes is None:
        return None, [
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="lumio",
                severity="warning",
                message=(
                    "Knowledge Base has no control file; loading in Legacy Flat Mode. "
                    "Establish lumio.yaml through a reviewed migration proposal to adopt "
                    "the categorized-KB contract (see ADR-0008)."
                ),
            )
        ]

    issues: list[ValidationIssue] = []
    try:
        raw = yaml.decode(raw_bytes)
    except Exception as exc:
        return None, [
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="lumio",
                message=f"control file is not valid YAML: {exc}",
            )
        ]

    if not isinstance(raw, dict):
        return None, [
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="lumio",
                message="control file must be a YAML mapping",
            )
        ]

    version = raw.get("version")
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version in SUPPORTED_CONTROL_FILE_VERSIONS
    ):
        parsed_version = version
    else:
        parsed_version = CONTROL_FILE_VERSION
        issues.append(
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="version",
                message=(
                    f"control file version {version!r} is not supported; "
                    f"supported versions: {sorted(SUPPORTED_CONTROL_FILE_VERSIONS)}"
                ),
            )
        )

    mode = raw.get("mode", KB_MODE_CATEGORIZED)
    if not isinstance(mode, str) or not mode.strip():
        issues.append(
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="mode",
                message="control file mode must be a non-empty string",
            )
        )
        mode = KB_MODE_CATEGORIZED
    elif mode == KB_MODE_LEGACY:
        # Legacy Flat Mode is represented by the *absence* of a Control File,
        # not by a present Control File that declares it. A present Control File
        # must declare ``mode: categorized``; ``legacy-flat`` here is
        # contradictory and a blocking error (ADR-0008, issue #77).
        issues.append(
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="mode",
                message=(
                    "control file mode 'legacy-flat' is not valid for a present "
                    "control file; Legacy Flat Mode has no control file (remove "
                    "lumio.yaml to load in Legacy Flat Mode)"
                ),
            )
        )
        mode = KB_MODE_CATEGORIZED
    elif mode not in SUPPORTED_CONTROL_FILE_MODES:
        issues.append(
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="mode",
                message=(
                    f"control file mode {mode!r} is not supported; "
                    f"a present control file must declare mode: categorized"
                ),
            )
        )
        mode = KB_MODE_CATEGORIZED

    # Content Category catalog (ADR-0009): the catalog is data, not a fixed
    # enum. An absent or empty declaration applies the seeded default; a
    # present declaration is validated for slug shape, reserved-name safety,
    # and uniqueness. Declared categories are first-class for navigation,
    # validation, and retrieval alongside seeded ones.
    raw_categories = raw.get("categories")
    categories: list[ContentCategory] = []
    catalog_structurally_valid = True
    if raw_categories is None or (isinstance(raw_categories, list) and not raw_categories):
        # Absent or empty declaration applies the seeded default (ADR-0009).
        categories = list(SEED_CATEGORY_CATALOG)
    else:
        parsed_categories, categories_ok = _as_categories(raw_categories)
        if not categories_ok:
            catalog_structurally_valid = False
            issues.append(
                ValidationIssue(
                    file=CONTROL_FILE_BASENAME,
                    field="categories",
                    message=(
                        "control file categories must be a list of mappings "
                        "with a non-empty 'name' (and optional 'description') "
                        "or bare name strings (ADR-0009)"
                    ),
                )
            )
            # Use the seeded default as the effective catalog so downstream
            # page-category checks remain meaningful despite the parse error.
            categories = list(SEED_CATEGORY_CATALOG)
        else:
            categories = parsed_categories
            seen: set[str] = set()
            for category in categories:
                name_issue = _category_name_issue(category.name)
                if name_issue is not None:
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="categories",
                            message=name_issue,
                        )
                    )
                elif category.name in seen:
                    issues.append(
                        ValidationIssue(
                            file=CONTROL_FILE_BASENAME,
                            field="categories",
                            message=f"duplicate content category: {category.name}",
                        )
                    )
                seen.add(category.name)

    # Page path-category validation (ADR-0009): a Compiled Page's category is
    # the first segment of its path. In a categorized Knowledge Base every
    # page with a directory component must live under a declared Content
    # Category; otherwise validation fails. Skipped on the structural parse
    # seam (pages is None) and when the catalog itself failed to parse.
    if catalog_structurally_valid and pages is not None:
        declared_category_names = {category.name for category in categories}
        for page in pages:
            parts = PurePosixPath(page.path).parts
            if len(parts) <= 1:
                # Root-level page: no category routing (Legacy Flat Mode
                # compatibility; migration routes every page to a category).
                continue
            page_category = parts[0]
            if page_category not in declared_category_names:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="category",
                        message=(
                            f"page category {page_category!r} is not in the "
                            f"Knowledge Base's declared catalog; declare it in "
                            f"lumio.yaml or move the page under a declared "
                            f"category (ADR-0009)"
                        ),
                    )
                )

    pins, pins_ok = _as_hot_index_pins(raw.get("hot_index", []))
    if not pins_ok:
        issues.append(
            ValidationIssue(
                file=CONTROL_FILE_BASENAME,
                field="hot_index",
                message=(
                    "control file hot_index must be a list of mappings with a "
                    "non-empty 'title' (and optional 'note')"
                ),
            )
        )
    else:
        # Hot Index pins are KB-local content controls: every pinned title must
        # resolve to an existing Canonical Page Title, just as Relationship
        # targets must. An unresolved pin cannot render in the Hot Index. The
        # parse seam (pages=None) skips cross-page resolution.
        if pages is None:
            canonical_titles: set[str] = set()
        else:
            canonical_titles = {page.title for page in pages if page.title}
        for pin in pins:
            if pages is None:
                continue
            if pin.title not in canonical_titles:
                issues.append(
                    ValidationIssue(
                        file=CONTROL_FILE_BASENAME,
                        field="hot_index",
                        message=(
                            f"unresolved Hot Index pin: {pin.title} is not a Canonical Page Title"
                        ),
                    )
                )

    ontology, ontology_issues = _as_ontology(raw.get("ontology"))
    issues.extend(ontology_issues)
    control = KnowledgeBaseControlFile(
        version=parsed_version,
        categories=categories,
        hot_index=pins,
        mode=str(mode),
        path=CONTROL_FILE_BASENAME,
        ontology=ontology,
    )
    return control, issues


def load_control_file(path: str | Path) -> KnowledgeBaseControlFile | None:
    """Load and return the root Knowledge Base Control File, or ``None`` if absent.

    Validates structurally and raises :class:`ControlFileError` on a malformed
    file (any error-severity control-file issue). Use :func:`load_knowledge_base`
    for the full report form (issues collected rather than raised). This is the
    parse seam for tools that need the control record without re-running
    whole-KB validation.
    """
    root = Path(path).resolve()
    control, issues = _load_and_validate_control_file(_FilesystemKbSource(root), None)
    if any(i.severity == "error" for i in issues):
        raise ControlFileError("; ".join(i.message for i in issues if i.severity == "error"))
    return control


def seeded_control_file() -> KnowledgeBaseControlFile:
    """Return a Control File carrying the seeded category catalog and no pins.

    A new categorized Knowledge Base adopts this catalog; Maintainers propose
    catalog and Hot Index pin changes through the normal review-and-publish
    workflow. The seeded categories are broad navigation routing only (issue #76).
    The version-2 seed carries an empty ontology: Entity Types and Predicates
    are KB-local content a Maintainer declares deliberately (ADR-0021).
    """
    return KnowledgeBaseControlFile(
        version=CONTROL_FILE_VERSION,
        categories=list(SEED_CATEGORY_CATALOG),
        hot_index=[],
        mode=KB_MODE_CATEGORIZED,
        path=CONTROL_FILE_BASENAME,
        ontology=Ontology(),
    )


def write_control_file(path: str | Path, control: KnowledgeBaseControlFile) -> Path:
    """Write a Control File deterministically as ``lumio.yaml`` at the KB root.

    The Control File is canonical KB content (it travels with the KB), so it is
    written with a stable key order and quoted scalars so unchanged content
    produces byte-identical output. Establishing or migrating the Control File
    is an explicit, reviewed Maintainer action — never an automatic upgrade
    (ADR-0008).
    """
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = _control_file_path(root)
    lines: list[str] = [
        f"version: {control.version}",
        f'mode: "{control.mode}"',
        "categories:",
    ]
    for category in control.categories:
        name = _control_yaml_scalar(category.name)
        if category.description is not None:
            lines.append(f"  - name: {name}")
            lines.append(f"    description: {_control_yaml_scalar(category.description)}")
        else:
            lines.append(f"  - {name}")
    if control.hot_index:
        lines.append("hot_index:")
        for pin in control.hot_index:
            title = _control_yaml_scalar(pin.title)
            if pin.note is not None:
                lines.append(f"  - title: {title}")
                lines.append(f"    note: {_control_yaml_scalar(pin.note)}")
            else:
                lines.append(f"  - {title}")
    _append_ontology_lines(lines, control)
    content = "\n".join(lines) + "\n"
    _atomic_write_text(target, content)
    return target


def _append_ontology_lines(lines: list[str], control: KnowledgeBaseControlFile) -> None:
    """Append the deterministic ontology section for a version-2 Control File.

    Keys are emitted in sorted order and scalars reuse the shared YAML scalar
    renderer so unchanged ontology content produces byte-identical output.
    An empty ontology still writes its three empty sections so the version-2
    contract is visible and diffable.
    """
    ontology = control.ontology
    if ontology is None:
        return
    lines.append("ontology:")
    lines.append("  entity_types:")
    for type_id in sorted(ontology.entity_types):
        definition = ontology.entity_types[type_id]
        if definition.description is not None:
            lines.append(f"    {type_id}:")
            lines.append(f"      description: {_control_yaml_scalar(definition.description)}")
        else:
            lines.append(f"    {type_id}: {{}}")
    lines.append("  predicates:")
    for predicate_id in sorted(ontology.predicates):
        definition = ontology.predicates[predicate_id]
        lines.append(f"    {predicate_id}:")
        if definition.subject_types:
            lines.append("      subject_types:")
            lines.extend(f"        - {t}" for t in definition.subject_types)
        if definition.object_types:
            lines.append("      object_types:")
            lines.extend(f"        - {t}" for t in definition.object_types)
        if definition.literal_kind is not None:
            lines.append(f"      literal_kind: {definition.literal_kind}")
        if definition.inverse is not None:
            lines.append(f"      inverse: {_control_yaml_scalar(definition.inverse)}")
        if definition.synonyms:
            lines.append("      synonyms:")
            lines.extend(f"        - {s}" for s in definition.synonyms)
        if definition.description is not None:
            lines.append(f"      description: {_control_yaml_scalar(definition.description)}")
    lines.append("  redirects:")
    for redirect in sorted(ontology.redirects, key=lambda r: (r.from_id, r.to_id)):
        lines.append(f"    {redirect.from_id}: {redirect.to_id}")


def _control_yaml_scalar(value: str) -> str:
    """Render a deterministic YAML scalar for the Control File.

    Simple identifiers are emitted bare for readability; anything containing a
    character YAML would reinterpret is double-quoted with backslash escapes.
    """
    if (
        value
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_\-./]*", value)
        and not value.startswith(("-", ".", "@", "%"))
    ):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_proposed_control_file(
    control: KnowledgeBaseControlFile,
    pages: Sequence[CompiledPage] | None = None,
) -> list[ValidationIssue]:
    """Validate a *proposed* Control File and return its validation issues.

    The proposed Control File is written to an ephemeral root and validated
    with the same structural and Hot Index pin-resolution rules the loader
    applies, so a Maintainer reviews the exact issues the Core SDK would raise
    on load. Pass ``pages`` to resolve Hot Index pins against the current
    Canonical Page Titles; pass ``None`` for the structural-only parse seam.
    Returns an empty list when the proposed Control File is valid (issue #77).

    The proposed Control File never touches the live Knowledge Base: it is
    written under a private temporary root and discarded when this call returns.
    """
    with tempfile.TemporaryDirectory(prefix="lumio-control-validate-") as tmp:
        root = Path(tmp)
        write_control_file(root, control)
        # Pass ``pages`` through unchanged: ``None`` selects the structural
        # parse seam (no pin resolution), while a page list resolves pins.
        page_list = list(pages) if pages is not None else None
        _, issues = _load_and_validate_control_file(_FilesystemKbSource(root), page_list)
        return list(issues)


def extend_control_file_categories(
    control: KnowledgeBaseControlFile,
    names: Iterable[str],
) -> KnowledgeBaseControlFile:
    """Return a *proposed* Control File adding ``names`` to the category catalog.

    A Maintainer-gated extension seam for external-vault import (ADR-0009,
    issue #87): each unmapped external category becomes a proposed declared
    category rather than an automatic one. Names already present are not
    duplicated; new categories are appended in first-seen order with no
    description (a reviewing Maintainer fills that in). The result is a
    *proposed* Control File: it is never written automatically and must pass
    :func:`validate_proposed_control_file` (and the Maintainer review) before
    publish. The seeded catalog stays the default; declaring extra categories
    is opt-in.
    """
    existing = [category.name for category in control.categories]
    seen = set(existing)
    added: list[ContentCategory] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        added.append(ContentCategory(name=name))
    if not added:
        return control
    return msgspec.structs.replace(control, categories=[*control.categories, *added])


def _atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically (temp file + ``os.replace``)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".lumio-artifact-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(Path(tmp_name), target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_and_validate(source: _KbSource) -> tuple[KnowledgeBase, ValidationReport]:
    """Load and fully validate canonical Knowledge Base content from ``source``.

    Source-agnostic (issue #120, ADR-0013): the resolved ``source.root`` is the
    Knowledge Base root carried on the returned :class:`KnowledgeBase` for
    diagnostics; read-only loading never touches the filesystem through it.
    """
    pages, issues = _load_pages_and_validate(source)
    issues.extend(_cross_page_issues(pages, _KnowledgeIndex(pages)))
    control, control_issues = _load_and_validate_control_file(source, pages)
    issues.extend(control_issues)
    # Entity/Claim ontology validation (ADR-0021, issue #168): runs with the
    # parsed ontology when a Control File is present, and in identity-only
    # mode (uniqueness + dangling objects) in Legacy Flat Mode.
    issues.extend(_ontology_issues(pages, control.ontology if control else None))
    return KnowledgeBase(root=source.root, pages=pages, control=control), ValidationReport(
        issues=issues
    )


def load_knowledge_base(path: str | Path) -> tuple[KnowledgeBase, ValidationReport]:
    """Load a Knowledge Base from a filesystem path into typed records.

    Returns the loaded Knowledge Base and a validation report that collects
    every issue found while loading, rather than failing on the first one.
    """
    root = Path(path).resolve()
    if not root.exists():
        raise KnowledgeBaseError(f"path does not exist: {root}")
    if not root.is_dir():
        raise KnowledgeBaseError(f"path is not a directory: {root}")

    return _load_and_validate(_FilesystemKbSource(root))


def validate(path: str | Path) -> ValidationReport:
    """Validate a Knowledge Base, collecting every issue in a single report."""
    root = Path(path).resolve()

    if not root.exists():
        return ValidationReport(
            issues=[ValidationIssue(file=str(root), field="path", message="path does not exist")]
        )

    if not root.is_dir():
        return ValidationReport(
            issues=[
                ValidationIssue(file=str(root), field="path", message="path is not a directory")
            ]
        )

    return _load_and_validate(_FilesystemKbSource(root))[1]


def fingerprint_sources(root: str | Path) -> SourceFingerprint:
    """Return a deterministic digest over canonical Knowledge Base content.

    Canonical content is the root Control File (``lumio.yaml``, when present —
    it is KB-local content that travels with the KB) plus every authored
    Markdown page. A validly-marked reserved derived artifact (Navigation
    Index, Activity Log, or Hot Index) is excluded so regenerating it never
    makes the canonical fingerprint appear stale. Authored Markdown on a
    reserved path without a valid marker is still fingerprinted — there is no
    escape hatch from fingerprinting without occupying the valid reserved
    artifact role (issues #64 and #77).
    """
    return _fingerprint_sources(_FilesystemKbSource(Path(root).resolve()))


def _fingerprint_sources(source: _KbSource) -> SourceFingerprint:
    """Source-agnostic fingerprint core (issue #120, ADR-0013).

    Computes the same deterministic digest over canonical content whether the
    source is a filesystem root or an in-memory materialization of an
    immutable S3 Published Version, so a reader can validate the Published
    Version fingerprint recorded in a remote manifest.
    """
    sources: list[SourceFileDigest] = []

    # The Control File is canonical KB content: it defines the categorized-KB
    # contract and travels with the KB, so changes to it are real changes.
    control_bytes = source.control_file_bytes()
    if control_bytes is not None:
        sources.append(
            SourceFileDigest(
                path=CONTROL_FILE_BASENAME,
                digest=hashlib.sha256(control_bytes).hexdigest(),
            )
        )

    for relative in source.markdown_files():
        raw = source.read_bytes(relative)
        reserved_basename = _reserved_artifact_basename_of(relative)
        if reserved_basename is not None:
            try:
                data, _body, _line = _parse_frontmatter(raw.decode("utf-8"), Path(relative))
            except FrontmatterError:
                # Malformed reserved file: fingerprint it normally (blocking
                # validation already reports the marker problem).
                data = None
            if data is not None and _classify_reserved_artifact(data, reserved_basename).valid:
                continue
        sources.append(
            SourceFileDigest(
                path=relative,
                digest=hashlib.sha256(raw).hexdigest(),
            )
        )
    combined = "\n".join(f"{source.path}:{source.digest}" for source in sources)
    overall = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return SourceFingerprint(digest=overall, sources=sources)


def canonical_content(source: _KbSource) -> dict[str, bytes]:
    """Return every canonical Knowledge Base file as ``{relative_path: bytes}``.

    Canonical content is the Control File (when present) plus every Markdown
    file under the source, including validly-marked reserved artifacts. This is
    the complete tree a publisher materializes under an immutable version prefix
    (ADR-0013) and the complete tree a reader must materialize to load, validate,
    and fingerprint a Published Version byte-for-byte. Reserved-artifact
    *exclusion* happens only at fingerprint time (:func:`_fingerprint_sources`),
    never at materialization time — a reader needs the full tree so the loader
    can classify reserved artifacts exactly as the filesystem loader does.
    """
    content: dict[str, bytes] = {}
    control_bytes = source.control_file_bytes()
    if control_bytes is not None:
        content[CONTROL_FILE_BASENAME] = control_bytes
    for relative in source.markdown_files():
        content[relative] = source.read_bytes(relative)
    return content


def is_fresh(previous: SourceFingerprint, current: SourceFingerprint) -> bool:
    """Return whether ``current`` matches ``previous`` (i.e., the index is fresh)."""
    return previous.digest == current.digest


# ---------------------------------------------------------------------------
# Navigation Index generation (issue #65).
#
# A Published Version materializes one reserved, marked ``index.md`` at the
# Knowledge Base root and in every directory containing Compiled Pages anywhere
# beneath it. The root index is an exhaustive catalog of every Compiled Page
# grouped by directory (Karpathy-style); each non-root index is a shallow guide
# to its immediate child directories and immediate Compiled Pages (OKF-style
# progressive disclosure). Generated indexes are deterministic (byte-identical
# for unchanged canonical content), carry the Navigation Index marker, omit
# exchange-only metadata, and are written atomically so a generation failure
# leaves the previously published tree usable. Loading, fingerprinting, and
# retrieval already exclude valid marked indexes (issue #64).
# ---------------------------------------------------------------------------


def _nav_index_frontmatter() -> str:
    """Return the deterministic Navigation Index frontmatter marker block."""
    return f"---\nlumio:\n  artifact: navigation-index\n  version: {NAV_INDEX_VERSION}\n---"


def _page_dir(page_path: str) -> str:
    """Return the posix parent directory of a page path; ``""`` for root pages."""
    parent = PurePosixPath(page_path).parent
    rendered = parent.as_posix()
    return "" if rendered == "." else rendered


def _ancestor_dirs(dir_path: str) -> list[str]:
    """Return ``dir_path`` and every ancestor up to the root (``""``)."""
    dirs: list[str] = []
    current = dir_path
    while True:
        dirs.append(current)
        if current == "":
            break
        parent = PurePosixPath(current).parent.as_posix()
        current = "" if parent == "." else parent
    return dirs


def _immediate_child_dirs(dir_path: str, index_dirs: set[str]) -> list[str]:
    """Return sorted direct children of ``dir_path`` present in ``index_dirs``."""
    children: list[str] = []
    for candidate in index_dirs:
        if candidate == dir_path:
            continue
        if dir_path == "":
            # Direct children of root have no path separator.
            if "/" not in candidate:
                children.append(candidate)
            continue
        prefix = dir_path + "/"
        if candidate.startswith(prefix):
            rest = candidate[len(prefix) :]
            if "/" not in rest:
                children.append(candidate)
    return sorted(children)


def _markdown_link_destination(path: str) -> str:
    """Percent-encode a relative Markdown destination while preserving separators."""
    return quote(path, safe="/")


def _format_page_entry(title: str, link: str, summary: str | None) -> str:
    """Render one page entry: title, portable relative link, and summary."""
    # Escape link-text brackets so a title containing ``[`` or ``]`` survives.
    safe_title = title.replace("[", "\\[").replace("]", "\\]")
    destination = _markdown_link_destination(link)
    if summary:
        return f"- [{safe_title}]({destination}) — {summary}"
    return f"- [{safe_title}]({destination})"


def _page_sort_key(page: CompiledPage) -> tuple[str, str]:
    """Deterministic total-order key for page entries.

    Primary key is the relative path (unique across a Published Version, so the
    order is always total); the Canonical Page Title is the explicit tie-breaker
    the Navigation Index spec requires.
    """
    return (page.path, page.title)


def _index_structure(
    pages: Sequence[CompiledPage],
) -> tuple[dict[str, list[CompiledPage]], set[str]]:
    """Group pages by directory and compute every directory that needs an index.

    Returns a mapping of directory posix path to its immediate Compiled Pages,
    plus the set of every directory (the root ``""`` and every ancestor of a
    page directory) that must receive a Navigation Index. The root is always
    present so an empty Knowledge Base still gets a root index.
    """
    pages_by_dir: dict[str, list[CompiledPage]] = {}
    index_dirs: set[str] = {""}
    for page in pages:
        directory = _page_dir(page.path)
        pages_by_dir.setdefault(directory, []).append(page)
        for ancestor in _ancestor_dirs(directory):
            index_dirs.add(ancestor)
    return pages_by_dir, index_dirs


def _root_index_body(pages_by_dir: dict[str, list[CompiledPage]]) -> str:
    """Return the exhaustive root catalog body without frontmatter.

    Opens with the ``# Navigation Index`` heading and lists every Compiled Page
    grouped by directory. Native generation and the OKF Exchange Profile share
    this body and swap only the frontmatter.
    """
    lines: list[str] = ["# Navigation Index"]

    # Root-level pages form the implicit top group (listed directly under the
    # heading); they sort first because "" precedes every named directory.
    root_pages = sorted(pages_by_dir.get("", []), key=_page_sort_key)
    if root_pages:
        lines.append("")
        for page in root_pages:
            # The root index lives at the root, so links are full paths.
            lines.append(_format_page_entry(page.title, page.path, page.summary))

    for dir_path in sorted(d for d in pages_by_dir if d):
        lines.append("")
        lines.append(f"## {dir_path}")
        lines.append("")
        for page in sorted(pages_by_dir[dir_path], key=_page_sort_key):
            lines.append(_format_page_entry(page.title, page.path, page.summary))

    return "\n".join(lines)


def _dir_index_body(
    dir_path: str,
    pages_by_dir: dict[str, list[CompiledPage]],
    index_dirs: set[str],
) -> str:
    """Return a shallow non-root index body without frontmatter.

    Lists immediate child directories, then immediate Compiled Pages. Native
    generation and the OKF Exchange Profile share this body.
    """
    lines: list[str] = [f"# {dir_path}"]

    children = _immediate_child_dirs(dir_path, index_dirs)
    if children:
        lines.append("")
        lines.append("## Directories")
        lines.append("")
        for child in children:
            name = child.rsplit("/", 1)[-1]
            lines.append(_format_page_entry(name, f"{name}/index.md", None))

    immediate_pages = sorted(pages_by_dir.get(dir_path, []), key=_page_sort_key)
    if immediate_pages:
        lines.append("")
        lines.append("## Pages")
        lines.append("")
        for page in immediate_pages:
            # Links from a directory index are portable relative basenames.
            link = PurePosixPath(page.path).name
            lines.append(_format_page_entry(page.title, link, page.summary))

    return "\n".join(lines)


def _render_root_index(pages_by_dir: dict[str, list[CompiledPage]]) -> str:
    """Render the exhaustive root catalog: marker frontmatter plus the body."""
    return _nav_index_frontmatter() + "\n\n" + _root_index_body(pages_by_dir) + "\n"


def _render_dir_index(
    dir_path: str,
    pages_by_dir: dict[str, list[CompiledPage]],
    index_dirs: set[str],
) -> str:
    """Render a shallow non-root index: marker frontmatter plus the body."""
    return (
        _nav_index_frontmatter()
        + "\n\n"
        + _dir_index_body(dir_path, pages_by_dir, index_dirs)
        + "\n"
    )


def generate_navigation_indexes(
    pages: Sequence[CompiledPage],
) -> dict[str, str]:
    """Return every Navigation Index for a set of Compiled Pages.

    Maps each generated index's relative path (``index.md`` at the root,
    ``<dir>/index.md`` elsewhere) to its deterministic Markdown content. The
    root index catalogs every page grouped by directory; each non-root index is
    a shallow guide to its immediate child directories and immediate pages.
    Empty sections are omitted and ordering is by relative path with the
    Canonical Page Title as an explicit tie-breaker (see ``_page_sort_key``),
    so unchanged pages always produce byte-identical indexes. The root index is
    always generated, even for a Knowledge Base with no Compiled Pages.

    Pure: performs no filesystem I/O.
    """
    pages_by_dir, index_dirs = _index_structure(pages)

    contents: dict[str, str] = {}
    for dir_path in sorted(index_dirs):
        relative = f"{dir_path}/index.md" if dir_path else NAV_INDEX_BASENAME
        if dir_path == "":
            contents[relative] = _render_root_index(pages_by_dir)
        else:
            contents[relative] = _render_dir_index(dir_path, pages_by_dir, index_dirs)
    return contents


def _stage_index(target: Path, content: str, *, prefix: str = ".lumio-nav-") -> Path:
    """Write ``content`` to a temp file in ``target``'s directory and return it.

    The temp file is a sibling of the eventual destination so the later
    ``os.replace`` is atomic on POSIX. The destination is not touched yet.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return Path(tmp_name)


def publish_navigation_indexes(path: str | Path) -> list[Path]:
    """Generate and atomically commit every Navigation Index under ``path``.

    Loads the Knowledge Base, computes the complete index set in memory, and
    commits it as one atomic transaction (see ``_commit_navigation_indexes``):
    every index is staged and every existing target backed up before any
    destination is swapped, so a failure rolls the tree fully back to its prior
    state. Used on the publish path after canonical pages are applied; it
    rewrites every generated index so a Published Version always carries the
    full current catalog. Returns the committed index paths in path order.
    """
    root = Path(path).resolve()
    kb, report = load_knowledge_base(root)
    collisions = [
        issue
        for issue in report.issues
        if issue.field == "lumio" and PurePosixPath(issue.file).name.lower() == NAV_INDEX_BASENAME
    ]
    if collisions:
        paths = ", ".join(sorted(issue.file for issue in collisions))
        raise NavigationIndexCollisionError(
            f"Cannot publish with authored or malformed Navigation Index paths: {paths}"
        )
    contents = generate_navigation_indexes(kb.pages)
    return _commit_navigation_indexes(root, contents, force_write=True)


def regenerate_navigation_indexes(path: str | Path) -> list[Path]:
    """Regenerate Navigation Indexes from canonical content after a sync.

    Used after a Git/shared/hybrid pull so a synced working copy always serves a
    complete, deterministic catalog. Loads the Knowledge Base and commits only
    the indexes whose bytes differ from disk (stale, hand-edited, or missing)
    plus pruning orphaned valid marked indexes, as one atomic transaction (see
    ``_commit_navigation_indexes``). Authored or malformed reserved files are
    left untouched — they are blocking validation errors, not silently fixed —
    and a fresh tree is not churned. A failure rolls the tree fully back to its
    prior state. Regeneration touches only reserved derived artifacts, so the
    canonical content fingerprint is unchanged (issues #64 and #66). Returns the
    committed index paths in path order.
    """
    root = Path(path).resolve()
    kb, _report = load_knowledge_base(root)
    contents = generate_navigation_indexes(kb.pages)
    return _commit_navigation_indexes(root, contents, force_write=False)


def _backup_index_path(target: Path, *, prefix: str = ".lumio-nav-") -> Path:
    """Return a unique backup path for ``target`` inside its own directory."""
    return target.parent / f"{prefix}backup-{uuid.uuid4().hex}"


def _reserved_logical_path(relative: str, basename: str) -> str:
    """Fold only the reserved basename while preserving parent-directory case."""
    path = PurePosixPath(relative)
    return (path.parent / basename).as_posix()


def _same_physical_file(left: Path, right: Path) -> bool:
    """Return whether two existing paths identify the same filesystem entry."""
    try:
        return left.samefile(right)
    except OSError:
        return False


def _commit_reserved_artifacts(
    root: Path,
    contents: dict[str, str],
    *,
    basename: str,
    force_write: bool = False,
    stage_prefix: str = ".lumio-nav-",
) -> list[Path]:
    """Atomically make the on-disk reserved artifact tree match ``contents``.

    Generalized form of the Navigation Index commit, parameterized by the
    reserved ``basename`` (``index.md``, ``hot.md``, ...). Writes the artifacts
    in ``contents`` and prunes orphaned valid marked artifacts of the same
    basename as a single transaction. Existing valid case-variant artifacts are
    normalized to the canonical lowercase basename so generated links remain
    portable. A reserved path holding authored or malformed content is always
    skipped so it remains a visible validation error. With ``force_write`` False
    (the sync path), fresh canonical artifacts are not churned; with
    ``force_write`` True (the publish path), every unblocked artifact in
    ``contents`` is rewritten.

    The commit is two-phase and atomic across the whole set. Every change is
    staged first — new content to temp files and every existing target moved to
    a unique backup — and only after every stage is on disk are the targets
    swapped into place with atomic ``os.replace`` calls. A failure at any point
    fully rolls back: staged temps are removed, every backed-up original is
    restored (overwriting any new content a partial commit already placed), and
    any newly created target a partial commit had already placed is removed.
    The tree is therefore left exactly as it was — never a mix of old and new
    artifacts and never a half-written set (ADR-0007 and ADR-0008). Returns the
    committed artifact paths in path order.
    """
    keep = set(contents)

    # Discover reserved artifacts by folding only their basename. Parent paths
    # remain case-sensitive: ``A/index.md`` and ``a/index.md`` belong to distinct
    # Knowledge Base directories. Classification is cached so authored or
    # malformed collisions can block both publish and regeneration unchanged.
    reserved_paths: dict[str, list[Path]] = {}
    reserved_validity: dict[Path, bool] = {}
    for file in _markdown_files(root):
        if file.name.lower() != basename:
            continue
        relative = file.relative_to(root).as_posix()
        logical_path = _reserved_logical_path(relative, basename)
        reserved_paths.setdefault(logical_path, []).append(file)
        try:
            data, _body, _line = _parse_frontmatter(file.read_text(encoding="utf-8"), file)
        except FrontmatterError:
            reserved_validity[file] = False
        else:
            reserved_validity[file] = _classify_reserved_artifact(data, basename).valid

    writes: list[tuple[str, Path | None]] = []
    prunes: list[Path] = []

    def queue_prune(file: Path) -> None:
        """Queue each distinct directory entry once, even when files are hard-linked."""
        if file not in prunes:
            prunes.append(file)

    for relative in sorted(contents):
        target = root / relative
        variants = reserved_paths.get(relative, [])

        # Any authored or malformed case-insensitive collision remains a
        # blocking validation error. Do not overwrite it on publish and do not
        # partially normalize other variants in the same directory.
        if any(not reserved_validity[file] for file in variants):
            continue

        # Select the physical artifact that currently occupies the canonical
        # target. ``samefile`` detects a differently-cased alias on a
        # case-insensitive filesystem; on a case-sensitive filesystem the first
        # valid variant becomes the source of the transactional normalization.
        original_target = next((file for file in variants if file == target), None)
        if original_target is None:
            original_target = next(
                (file for file in variants if _same_physical_file(file, target)),
                variants[0] if variants else None,
            )

        expected = contents[relative].encode("utf-8")
        needs_normalization = original_target is not None and original_target != target
        current = original_target.read_bytes() if original_target is not None else None
        if force_write or needs_normalization or current != expected:
            writes.append((relative, original_target))

        for file in variants:
            if file == original_target:
                continue
            queue_prune(file)

    # Valid artifacts outside the generated set are orphaned and pruned in the
    # same transaction. Authored or malformed reserved files remain for
    # validation to report.
    for logical_path, variants in sorted(reserved_paths.items()):
        if logical_path in keep:
            continue
        for file in variants:
            if reserved_validity[file]:
                queue_prune(file)

    # Prepare: stage new content and move every existing target (and every
    # prune target) to a backup. Commit: swap each temp into place. Both phases
    # use only atomic per-file operations; rollback undoes the lot.
    staged: list[tuple[Path, Path, Path | None]] = []  # temp, target, original
    backups: list[tuple[Path, Path]] = []  # backup, original
    try:
        for relative, original_target in writes:
            target = root / relative
            if original_target is not None:
                backup = _backup_index_path(original_target, prefix=stage_prefix)
                os.replace(original_target, backup)
                backups.append((backup, original_target))
            staged.append(
                (
                    _stage_index(target, contents[relative], prefix=stage_prefix),
                    target,
                    original_target,
                )
            )
        for target in prunes:
            backup = _backup_index_path(target, prefix=stage_prefix)
            os.replace(target, backup)
            backups.append((backup, target))

        written: list[Path] = []
        for temp_path, target, _original_target in staged:
            os.replace(temp_path, target)
            written.append(target)
    except Exception:
        # Roll back to the pre-commit tree. Remove any staged temp still on
        # disk, restore every backed-up original, then remove a newly committed
        # canonical target only when it is physically distinct from the restored
        # case-variant original.
        for temp_path, _target, _original_target in staged:
            try:
                temp_path.unlink()
            except OSError:
                pass
        for backup, original_target in backups:
            try:
                os.replace(backup, original_target)
            except OSError:
                pass
        for _temp_path, target, original_target in staged:
            if not target.exists():
                continue
            if original_target is not None and (
                target == original_target or _same_physical_file(target, original_target)
            ):
                continue
            try:
                target.unlink()
            except OSError:
                pass
        raise

    # Success: the old content and pruned files survive only in backups now.
    for backup, _target in backups:
        try:
            backup.unlink()
        except OSError:
            pass

    return written


def _commit_navigation_indexes(
    root: Path,
    contents: dict[str, str],
    *,
    force_write: bool = False,
) -> list[Path]:
    """Atomically commit the Navigation Index set (thin wrapper over the generalized commit)."""
    return _commit_reserved_artifacts(
        root, contents, basename=NAV_INDEX_BASENAME, force_write=force_write
    )


# ---------------------------------------------------------------------------
# Hot Index, Activity Log, and reserved-artifact publication (issue #77).
#
# A categorized Knowledge Base publishes a regenerated, Maintainer-pinned Hot
# Index (``hot.md``) and an append-only Activity Log (``log.md``) alongside the
# Navigation Index hierarchy. The Hot Index is regenerated from the Control
# File's pinned titles and current Compiled Pages on every publish/sync. The
# Activity Log is append-only: it records one grep-friendly entry per
# successful published Knowledge Base state transition and is never regenerated
# or pruned. See ADR-0008.
# ---------------------------------------------------------------------------


def _hot_index_frontmatter() -> str:
    """Return the deterministic Hot Index frontmatter marker block."""
    return f"---\nlumio:\n  artifact: {HOT_INDEX_ARTIFACT}\n  version: {HOT_INDEX_VERSION}\n---"


def _activity_log_frontmatter() -> str:
    """Return the deterministic Activity Log frontmatter marker block."""
    return (
        f"---\nlumio:\n  artifact: {ACTIVITY_LOG_ARTIFACT}\n  version: {ACTIVITY_LOG_VERSION}\n---"
    )


def generate_hot_index(
    control: KnowledgeBaseControlFile | None,
    pages: Sequence[CompiledPage],
) -> str | None:
    """Render the Maintainer-pinned Hot Index markdown, or ``None`` if no pins.

    The Hot Index lists exactly the Compiled Pages whose Canonical Page Titles
    the Control File pins, in pin order. Entries carry the title, a portable
    relative Markdown link from the root, and the page summary. A pin whose
    title resolves to no page is skipped here (validation reports it as an
    unresolved pin). Returns ``None`` when the Knowledge Base has no Control
    File or no pins, so publication does not create an empty Hot Index.

    Pure: performs no filesystem I/O.
    """
    if control is None or not control.hot_index:
        return None
    by_title: dict[str, CompiledPage] = {}
    for page in pages:
        if page.title:
            by_title.setdefault(page.title, page)
    lines: list[str] = [_hot_index_frontmatter(), "", "# Hot Index", ""]
    rendered = False
    for pin in control.hot_index:
        page = by_title.get(pin.title)
        if page is None:
            continue
        rendered = True
        summary = pin.note if pin.note is not None else page.summary
        lines.append(_format_page_entry(pin.title, page.path, summary))
    if not rendered:
        return None
    return "\n".join(lines) + "\n"


def _format_activity_log_line(entry: ActivityLogEntry) -> str:
    """Render one grep-friendly Activity Log line.

    The leading ISO-8601 UTC timestamp and operation token make entries
    greppable by date (``grep ^2026-``) and operation (``grep publish``); the
    free-text description follows a colon so it stays scannable.
    """
    description = entry.description.replace("\n", " ").strip()
    return f"{entry.timestamp} {entry.operation}: {description}"


def make_activity_log_entry(
    *,
    operation: str,
    description: str,
    timestamp: datetime | None = None,
) -> ActivityLogEntry:
    """Build a grep-friendly Activity Log entry with a normalized UTC timestamp."""
    moment = timestamp if timestamp is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return ActivityLogEntry(
        timestamp=moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        operation=operation,
        description=description,
    )


def append_activity_log_entry(
    path: str | Path,
    entry: ActivityLogEntry,
) -> Path:
    """Append exactly one Activity Log entry to the root ``log.md``.

    Creates ``log.md`` with the ``activity-log`` marker and a heading if absent,
    and appends a single grep-friendly line otherwise. The Activity Log is
    append-only: existing history is never rewritten, reordered, or pruned. The
    entry records only a successful published Knowledge Base state transition —
    Reader queries, failed or discarded proposals, unpublished uploads, and
    private audit events never reach this portable log (ADR-0008).

    The write is atomic for the new file case (temp + ``os.replace``); for an
    existing log it appends in place because the artifact is append-only and
    never partially regenerated.
    """
    root = Path(path).resolve()
    log_path = root / ACTIVITY_LOG_BASENAME
    line = _format_activity_log_line(entry) + "\n"
    if not log_path.exists():
        content = _activity_log_frontmatter() + "\n\n# Activity Log\n\n" + line
        _atomic_write_text(log_path, content)
        return log_path
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return log_path


def _reserved_artifact_collisions(report: ValidationReport) -> list[ValidationIssue]:
    """Return the blocking reserved-artifact collision issues in a report."""
    return [
        issue
        for issue in report.issues
        if issue.field == "lumio"
        and issue.severity == "error"
        and PurePosixPath(issue.file).name.lower() in RESERVED_ARTIFACT_MARKERS
    ]


def _describe_reserved_collision(issue: ValidationIssue) -> str:
    """Render one collision as ``<Label> (<path>)`` for an actionable error."""
    basename = issue.file.rsplit("/", 1)[-1].lower()
    artifact = RESERVED_ARTIFACT_MARKERS.get(basename)
    label = RESERVED_ARTIFACT_LABELS.get(artifact) if artifact is not None else None
    return f"{label or 'reserved artifact'} ({issue.file})"


def publish_hot_index(path: str | Path) -> list[Path]:
    """Regenerate and force-commit the Maintainer-pinned Hot Index under ``path``.

    Loads the Knowledge Base and its Control File, renders the Hot Index from
    the pinned titles, and commits it (force) as one atomic transaction via the
    generalized reserved-artifact commit. A Knowledge Base with no Control File
    or no pins prunes any valid marked Hot Index and creates none. Authored or
    malformed ``hot.md`` collisions are left for validation to report. Returns
    the committed paths in path order.
    """
    root = Path(path).resolve()
    kb, _report = load_knowledge_base(root)
    content = generate_hot_index(kb.control, kb.pages)
    contents: dict[str, str] = {HOT_INDEX_BASENAME: content} if content is not None else {}
    return _commit_reserved_artifacts(
        root, contents, basename=HOT_INDEX_BASENAME, force_write=True, stage_prefix=".lumio-hot-"
    )


def regenerate_hot_index(path: str | Path) -> list[Path]:
    """Regenerate the Hot Index after a sync (non-force), pruning when un-pinned."""
    root = Path(path).resolve()
    kb, _report = load_knowledge_base(root)
    content = generate_hot_index(kb.control, kb.pages)
    contents: dict[str, str] = {HOT_INDEX_BASENAME: content} if content is not None else {}
    return _commit_reserved_artifacts(
        root, contents, basename=HOT_INDEX_BASENAME, force_write=False, stage_prefix=".lumio-hot-"
    )


def publish_reserved_artifacts(path: str | Path) -> list[Path]:
    """Regenerate Navigation Indexes and the Hot Index for a publication.

    The publish-path orchestrator: loads the Knowledge Base, refuses to publish
    when any reserved artifact path carries authored or malformed content (a
    blocking collision), then force-commits the complete Navigation Index
    hierarchy and the Maintainer-pinned Hot Index. The Activity Log is not
    touched here — it is append-only and is written separately, only after the
    Knowledge Base state is successfully published (see
    :func:`append_activity_log_entry`). Returns the committed paths in path
    order (ADR-0008).
    """
    root = Path(path).resolve()
    kb, report = load_knowledge_base(root)
    collisions = _reserved_artifact_collisions(report)
    if collisions:
        described = ", ".join(sorted(_describe_reserved_collision(issue) for issue in collisions))
        raise NavigationIndexCollisionError(
            f"Cannot publish with authored or malformed reserved artifacts: {described}"
        )
    written: list[Path] = []
    written.extend(
        _commit_reserved_artifacts(
            root,
            generate_navigation_indexes(kb.pages),
            basename=NAV_INDEX_BASENAME,
            force_write=True,
        )
    )
    hot_content = generate_hot_index(kb.control, kb.pages)
    hot_contents: dict[str, str] = (
        {HOT_INDEX_BASENAME: hot_content} if hot_content is not None else {}
    )
    written.extend(
        _commit_reserved_artifacts(
            root,
            hot_contents,
            basename=HOT_INDEX_BASENAME,
            force_write=True,
            stage_prefix=".lumio-hot-",
        )
    )
    return written


def regenerate_reserved_artifacts(path: str | Path) -> list[Path]:
    """Regenerate Navigation Indexes and the Hot Index after a sync (non-force).

    The sync-path orchestrator: after a Git/shared/hybrid pull, regenerates the
    Navigation Index hierarchy and the Hot Index from canonical content without
    churning fresh artifacts, and prunes orphaned valid marked artifacts. The
    Activity Log is append-only history and is never regenerated or pruned here.
    Returns the committed paths in path order (ADR-0008).
    """
    root = Path(path).resolve()
    kb, _report = load_knowledge_base(root)
    written: list[Path] = []
    written.extend(
        _commit_reserved_artifacts(
            root,
            generate_navigation_indexes(kb.pages),
            basename=NAV_INDEX_BASENAME,
            force_write=False,
        )
    )
    hot_content = generate_hot_index(kb.control, kb.pages)
    hot_contents: dict[str, str] = (
        {HOT_INDEX_BASENAME: hot_content} if hot_content is not None else {}
    )
    written.extend(
        _commit_reserved_artifacts(
            root,
            hot_contents,
            basename=HOT_INDEX_BASENAME,
            force_write=False,
            stage_prefix=".lumio-hot-",
        )
    )
    return written
