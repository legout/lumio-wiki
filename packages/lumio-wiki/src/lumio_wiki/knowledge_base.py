"""Knowledge Base loading, validation, indexing, and retrieval."""

import hashlib
import heapq
import os
import re
import tempfile
import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import msgspec
import msgspec.yaml as yaml

from lumio_wiki.embeddings import (
    DEFAULT_SEMANTIC_THRESHOLD,
    Embedder,
    RetrievalMode,
)
from lumio_wiki.fingerprint_store import load_stored_fingerprint
from lumio_wiki.page_search import search_pages as search_pages_over
from lumio_wiki.records import (
    EXTRACTOR_VERSION,
    ActivityLogEntry,
    CompiledPage,
    ContentCategory,
    ExtractedReference,
    HealthReport,
    HotIndexPin,
    KnowledgeBaseControlFile,
    PageSearchResult,
    RegistryEntry,
    Relationship,
    RetrievalResult,
    Source,
    SourceFileDigest,
    SourceFingerprint,
    ValidationIssue,
    ValidationReport,
)
from lumio_wiki.retrieval import RetrievalAdapter, default_retrieval_adapter

VALID_LIFECYCLES = {"draft", "review", "approved", "deprecated"}
VALID_VISIBILITIES = {"public", "internal", "restricted"}
PREFERRED_RELATIONSHIP_TYPES = frozenset(
    {
        "relates-to",
        "uses",
        "extends",
        "implements",
        "contradicts",
        "derived-from",
        "replaces",
    }
)

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
            f"unknown graph direction {direction!r}; expected one of "
            f"{sorted(GRAPH_DIRECTIONS)}"
        )


def _require_non_negative_limit(name: str, value: int) -> None:
    """Validate that a graph traversal limit is a non-negative integer."""
    if value < 0:
        raise ValueError(
            f"{name} must be a non-negative integer, got {value}"
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
CONTROL_FILE_VERSION = 1
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

    def _knowledge_index(self) -> _KnowledgeIndex:
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


    def related_from(self, title: str, relationship_type: str | None = None) -> list[Relationship]:
        """Return relationships whose source page has the given Canonical Title."""
        relationships: list[Relationship] = []
        for page in self.lookup_by_title(title):
            for rel in page.relationships:
                if relationship_type is None or rel.type == relationship_type:
                    relationships.append(rel)
        return relationships

    def graph_path(self, source_title: str, target_title: str) -> list[str] | None:
        """Return the shortest directed path from ``source_title`` to ``target_title``.

        Returns ``None`` if no path exists.
        """
        if source_title == target_title:
            return [source_title]

        adjacency = self._knowledge_index().adjacency
        if source_title not in adjacency:
            return None

        queue: deque[tuple[str, list[str]]] = deque([(source_title, [source_title])])
        visited = {source_title}

        while queue:
            current, path = queue.popleft()
            for next_title, _ in adjacency.get(current, []):
                if next_title == target_title:
                    return path + [next_title]
                if next_title not in visited:
                    visited.add(next_title)
                    queue.append((next_title, path + [next_title]))

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

        Expands graph neighbors breadth-first up to ``max_depth`` hops,
        following ``direction`` (``"outgoing"``, ``"incoming"``, or
        ``"both"``). ``scope`` selects the graph: ``"canonical"`` (default)
        traverses only reviewed typed Relationships; ``"discovery"`` traverses
        canonical Relationships PLUS Extracted References derived from
        exactly-resolved internal links in Compiled Page bodies (ADR-0011,
        issue #107). In discovery scope, equivalent edges (same
        source_title -> target_title) are deduplicated to a single endpoint.

        Traversal operates ONLY over ``candidate_titles`` — the
        already-authorized Canonical Page Titles the caller has visibility to.
        Endpoint resolution and expansion never return a page outside that set;
        when ``candidate_titles`` is ``None`` every loaded Compiled Page title
        is the authorized universe. ``title`` itself is never included in the
        result.

        ``max_edges`` bounds the number of edges inspected/expanded and
        ``max_results`` bounds the returned title count; both truncate
        deterministically (results are sorted by Canonical Page Title).
        ``max_depth``, ``max_edges``, and ``max_results`` must all be
        non-negative. When ``title`` is not in the authorized candidate set the
        result is empty.
        """
        _require_graph_scope(scope)
        _require_graph_direction(direction)
        _require_non_negative_limit("max_depth", max_depth)
        _require_non_negative_limit("max_edges", max_edges)
        _require_non_negative_limit("max_results", max_results)
        candidate = self._graph_candidate(candidate_titles)
        if title not in candidate:
            return []
        neighbors = self._graph_neighbor_fn(direction, scope)

        visited = {title}
        found: list[str] = []
        frontier: deque[tuple[str, int]] = deque([(title, 0)])
        edges_expanded = 0
        while frontier:
            current, depth = frontier.popleft()
            if depth >= max_depth:
                continue
            # Iterate raw Relationship edges in deterministic order and debit
            # the expanded-edge budget per ELIGIBLE edge inspected (after the
            # relationship-type and candidate-set filters), not per unique
            # endpoint. Parallel edges to the same endpoint each consume the
            # budget; non-candidate / wrong-type / self edges are filtered in
            # O(1) without expanding the graph.
            for endpoint, rtype in neighbors(current):
                if relationship_type is not None and rtype != relationship_type:
                    continue
                if endpoint == title or endpoint not in candidate:
                    continue
                edges_expanded += 1
                if edges_expanded > max_edges:
                    frontier.clear()
                    break
                if endpoint not in visited:
                    visited.add(endpoint)
                    found.append(endpoint)
                    frontier.append((endpoint, depth + 1))

        # Deterministic truncation: sort all discovered titles, then cap.
        # max_results is validated non-negative above, so the slice always binds.
        return sorted(found)[:max_results]

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
            return [source_title] if source_title in candidate else None
        if source_title not in candidate or target_title not in candidate:
            return None
        neighbors = self._graph_neighbor_fn(direction, scope)

        visited = {source_title}
        queue: deque[tuple[str, list[str]]] = deque(
            [(source_title, [source_title])]
        )
        edges_expanded = 0
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            # Debit the expanded-edge budget per ELIGIBLE edge inspected (after
            # the candidate-set filter), not per unique endpoint.
            # ``shortest_path`` does not filter by relationship type, so every
            # candidate edge counts.
            for endpoint, _rtype in neighbors(current):
                if endpoint == current or endpoint not in candidate:
                    continue
                edges_expanded += 1
                if edges_expanded > max_edges:
                    return None
                if endpoint == target_title:
                    return path + [endpoint]
                if endpoint not in visited:
                    visited.add(endpoint)
                    queue.append((endpoint, path + [endpoint]))
        return None

    def _graph_candidate(self, candidate_titles: Iterable[str] | None) -> frozenset[str]:
        """Return the authorized candidate title set, defaulting to all titles."""
        if candidate_titles is None:
            return frozenset(
                page.title for page in self.pages if page.title
            )
        return frozenset(candidate_titles)

    def _graph_neighbor_fn(self, direction: str, scope: str = GRAPH_SCOPE_CANONICAL):
        """Return a ``title -> iterable[(endpoint, relationship_type)]`` closure.

        Yields the RAW edges reachable from ``title`` per ``direction`` in
        deterministic ``(endpoint, relationship_type)`` order, WITHOUT
        materializing, de-duplicating, or sorting a per-node neighbor list.
        The caller debits its expanded-edge budget per eligible edge as it
        inspects them, applying the relationship-type, candidate-set, and
        self-loop filters itself (those filters do not expand the graph).

        ``scope`` selects the graph: ``canonical`` uses only reviewed typed
        Relationships; ``discovery`` uses canonical Relationships PLUS
        Extracted References (equivalent edges deduplicated to a single
        endpoint). Extracted edges carry an empty relationship type.

        Determinism comes from the adjacency being stored pre-sorted by
        ``(endpoint, relationship_type)`` in ``_KnowledgeIndex``; ``both``
        direction lazily merges the two pre-sorted streams via ``heapq.merge``.
        """
        index = self._knowledge_index()
        if scope == GRAPH_SCOPE_DISCOVERY:
            outgoing = index.discovery_adjacency
            incoming = index.discovery_incoming
        else:
            outgoing = index.adjacency
            incoming = index.incoming

        if direction == GRAPH_DIRECTION_OUTGOING:

            def neighbors(title: str):
                return outgoing.get(title, ())

        elif direction == GRAPH_DIRECTION_INCOMING:

            def neighbors(title: str):
                return incoming.get(title, ())

        else:  # GRAPH_DIRECTION_BOTH

            def neighbors(title: str):
                return heapq.merge(
                    outgoing.get(title, ()), incoming.get(title, ())
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

    def extraction_diagnostics(self) -> list[ExtractionDiagnostic]:
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

    def build_index(
        self,
        index_dir: str | Path | None = None,
        *,
        embedder: Embedder | None = None,
        retrieval: RetrievalAdapter | None = None,
    ) -> KnowledgeBase:
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
    ) -> list[RetrievalResult]:
        """Retrieve citation-ready Evidence for ``query``.

        Delegates to the bound retrieval adapter, defaulting to the
        always-available zero-index lexical adapter. The default lexical path
        works over the loaded Compiled Pages and needs no ``index_dir``; the
        LanceDB BM25 / semantic / hybrid paths require the LanceDB retrieval
        adapter to be bound via :meth:`build_index` (``retrieval=...``) and are
        wired in a later task.

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
        """
        chosen_dir = Path(index_dir) if index_dir is not None else self.index_dir
        effective_query = query
        if hints:
            extra = " ".join(hint for hint in hints if hint)
            if extra:
                effective_query = f"{query} {extra}".strip()
        resolved = chosen_dir.resolve() if chosen_dir is not None else None
        return self._retrieval_adapter().retrieve(
            self.pages,
            effective_query,
            limit=limit,
            index_dir=resolved,
            mode=mode,
            embedder=embedder,
            score_threshold=score_threshold,
        )


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
                relationship_count=len(page.relationships),
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
        missing_summaries = sorted(
            entry.path for entry in self.registry() if not entry.summary
        )

        validation = validate(self.root)

        broken_relationships: set[str] = set()
        unknown_relationship_types: set[str] = set()
        invalid_fields: set[str] = set()
        for issue in validation.issues:
            message = issue.message
            if message.startswith("unresolved relationship target: "):
                broken_relationships.add(issue.file)
            elif message.startswith("unknown relationship type '"):
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


def _scan_body_links(
    body: str, body_start_line: int
) -> list[tuple[str, int, int]]:
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
                    target_page, reason = _resolve_wikilink_target(
                        dest, by_title, by_alias
                    )

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
                        detail=(
                            f"{reason} extracted-link target: {dest}"
                        ),
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
    """In-memory exact-lookup and graph indexes derived from a list of pages."""

    def __init__(self, pages: list[CompiledPage]) -> None:
        self.by_title: dict[str, list[CompiledPage]] = {}
        self.by_alias: dict[str, list[CompiledPage]] = {}
        self.by_tag: dict[str, list[CompiledPage]] = {}
        self.by_source: dict[str, list[CompiledPage]] = {}
        self.by_lifecycle: dict[str, list[CompiledPage]] = {}
        self.adjacency: dict[str, list[tuple[str, str]]] = {}
        # Reverse adjacency: target title -> [(source title, relationship type)].
        # Built alongside the outgoing adjacency so incoming traversal and
        # ``both`` direction are derived from the same Relationship set without
        # a second pass (issue #106).
        self.incoming: dict[str, list[tuple[str, str]]] = {}

        for page in pages:
            self.by_title.setdefault(page.title, []).append(page)
            for alias in page.aliases:
                self.by_alias.setdefault(alias, []).append(page)
            for tag in page.tags:
                self.by_tag.setdefault(tag, []).append(page)
            for source in page.sources:
                self.by_source.setdefault(source.id, []).append(page)
            self.by_lifecycle.setdefault(page.lifecycle or "", []).append(page)
            self.adjacency.setdefault(page.title, []).extend(
                (rel.target, rel.type) for rel in page.relationships
            )
            for rel in page.relationships:
                self.incoming.setdefault(rel.target, []).append(
                    (page.title, rel.type)
                )
        # Canonical adjacency is stored pre-sorted by ``(endpoint, type)`` so
        # graph traversal iterates Relationship edges deterministically without
        # per-node materialization/sorting (ADR-0011, issue #106).
        for edges in self.adjacency.values():
            edges.sort()
        for edges in self.incoming.values():
            edges.sort()

        # Discovery Graph adjacency (issue #107, ADR-0011): canonical
        # Relationships PLUS Extracted References, with equivalent edges
        # (same source_title -> target_title) deduplicated deterministically.
        # An extracted edge to a target already reachable via a canonical
        # Relationship is not added again, so discovery traversal surfaces
        # each endpoint once. Extracted edges carry an empty relationship
        # type so the existing per-edge budget loop treats them uniformly.
        extracted, diags = _extract_references(pages)
        self.extracted_references = extracted
        self.extraction_diagnostics = diags
        self.discovery_adjacency: dict[str, list[tuple[str, str]]] = {}
        self.discovery_incoming: dict[str, list[tuple[str, str]]] = {}
        for source_title, edges in self.adjacency.items():
            seen: set[str] = set()
            merged: list[tuple[str, str]] = []
            for endpoint, rtype in edges:
                if endpoint in seen:
                    continue
                seen.add(endpoint)
                merged.append((endpoint, rtype))
            self.discovery_adjacency[source_title] = merged
        for ref in extracted:
            bucket = self.discovery_adjacency.setdefault(ref.source_title, [])
            if any(endpoint == ref.target_title for endpoint, _ in bucket):
                continue
            bucket.append((ref.target_title, ""))
        for source_title, edges in self.incoming.items():
            seen_in: set[str] = set()
            merged_in: list[tuple[str, str]] = []
            for endpoint, rtype in edges:
                if endpoint in seen_in:
                    continue
                seen_in.add(endpoint)
                merged_in.append((endpoint, rtype))
            self.discovery_incoming[source_title] = merged_in
        for ref in extracted:
            bucket = self.discovery_incoming.setdefault(ref.target_title, [])
            if any(endpoint == ref.source_title for endpoint, _ in bucket):
                continue
            bucket.append((ref.source_title, ""))
        for edges in self.discovery_adjacency.values():
            edges.sort()
        for edges in self.discovery_incoming.values():
            edges.sort()


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


def _as_relationships(value: Any) -> list[Relationship]:
    if not isinstance(value, list):
        return []
    relationships: list[Relationship] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        relationships.append(
            Relationship(
                target=str(item.get("target", "")),
                type=str(item.get("type", "")),
            )
        )
    return relationships


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


def _markdown_files(root: Path) -> list[Path]:
    """Return Markdown files with case-insensitive ``.md`` extension matching."""
    return sorted(
        file
        for file in root.rglob("*")
        if file.is_file() and file.suffix.lower() == ".md"
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


def _load_page(file: Path, root: Path) -> tuple[CompiledPage, dict[str, Any]]:
    """Parse a single Markdown page into a record and raw frontmatter."""
    text = file.read_text(encoding="utf-8")
    data, body, body_start_line = _parse_frontmatter(text, file)
    page = CompiledPage(
        path=file.relative_to(root).as_posix(),
        title=str(data.get("title", "")),
        aliases=_as_string_list(data.get("aliases")),
        tags=_as_string_list(data.get("tags")),
        summary=str(data["summary"]) if "summary" in data else None,
        lifecycle=str(data.get("lifecycle")) if "lifecycle" in data else None,
        visibility=str(data.get("visibility")) if "visibility" in data else None,
        sources=_as_sources(data.get("sources")),
        relationships=_as_relationships(data.get("relationships")),
        synthetic=bool(data.get("synthetic", False)),
        body=body,
        body_start_line=body_start_line,
    )
    return page, data


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
        if not isinstance(data["relationships"], list):
            page_issues.append(
                ValidationIssue(
                    file=relative,
                    field="relationships",
                    message="relationships must be a list",
                )
            )
        elif any(not isinstance(item, dict) for item in data["relationships"]):
            page_issues.append(
                ValidationIssue(
                    file=relative,
                    field="relationships",
                    message="relationships must be a list of mappings",
                )
            )
        else:
            for rel in page.relationships:
                if rel.type and rel.type not in PREFERRED_RELATIONSHIP_TYPES:
                    page_issues.append(
                        ValidationIssue(
                            file=relative,
                            field="relationships",
                            severity="warning",
                            message=(
                                f"unknown relationship type '{rel.type}'; "
                                f"preferred types are: "
                                f"{', '.join(sorted(PREFERRED_RELATIONSHIP_TYPES))}"
                            ),
                        )
                    )

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
    root: Path,
) -> tuple[list[CompiledPage], list[ValidationIssue]]:
    """Load every Markdown page under ``root`` and collect per-page issues.

    A validly-marked reserved artifact (case-insensitive ``index.md``,
    ``log.md``, or ``hot.md`` with the matching supported ``lumio`` marker) is
    a reserved derived artifact: it is excluded from the loaded Compiled Page
    collection. A reserved path with a missing or malformed marker is a
    blocking validation error rather than being skipped (issues #64 and #77).
    """
    pages: list[CompiledPage] = []
    issues: list[ValidationIssue] = []

    for file in _markdown_files(root):
        relative = file.relative_to(root).as_posix()
        reserved_basename = _reserved_artifact_basename(file)

        try:
            page, data = _load_page(file, root)
        except FrontmatterError as exc:
            # A reserved path that cannot even parse frontmatter is still a
            # blocking collision: report it as a reserved-artifact issue.
            if reserved_basename is not None:
                label = RESERVED_ARTIFACT_LABELS[
                    RESERVED_ARTIFACT_MARKERS[reserved_basename]
                ]
                issues.append(
                    ValidationIssue(
                        file=relative,
                        field="lumio",
                        message=(
                            f"reserved {label} path '{file.name}' has invalid "
                            f"frontmatter: {exc}"
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
                    message=classification.issue or (
                        f"reserved '{file.name}' is not a valid "
                        f"{RESERVED_ARTIFACT_MARKERS[reserved_basename]} marker"
                    ),
                )
            )
            continue

        pages.append(page)
        issues.extend(_validate_page(page, data, relative))

    return pages, issues


def _cross_page_issues(pages: list[CompiledPage]) -> list[ValidationIssue]:
    """Return duplicate title, duplicate alias, and unresolved relationship issues."""
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
    for page in pages:
        for rel in page.relationships:
            if rel.target and rel.target not in canonical_titles:
                issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="relationships",
                        message=f"unresolved relationship target: {rel.target}",
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
        pins.append(
            HotIndexPin(title=title, note=str(note) if note is not None else None)
        )
    return pins, True


def _load_and_validate_control_file(
    root: Path, pages: list[CompiledPage] | None
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
    """
    path = _control_file_path(root)
    if not path.exists():
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
        raw = yaml.decode(path.read_bytes())
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
    if raw_categories is None or (
        isinstance(raw_categories, list) and not raw_categories
    ):
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
                            f"unresolved Hot Index pin: {pin.title} is not a "
                            f"Canonical Page Title"
                        ),
                    )
                )

    control = KnowledgeBaseControlFile(
        version=parsed_version,
        categories=categories,
        hot_index=pins,
        mode=str(mode),
        path=CONTROL_FILE_BASENAME,
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
    control, issues = _load_and_validate_control_file(root, None)
    if any(i.severity == "error" for i in issues):
        raise ControlFileError(
            "; ".join(i.message for i in issues if i.severity == "error")
        )
    return control


def seeded_control_file() -> KnowledgeBaseControlFile:
    """Return a Control File carrying the seeded category catalog and no pins.

    A new categorized Knowledge Base adopts this catalog; Maintainers propose
    catalog and Hot Index pin changes through the normal review-and-publish
    workflow. The seeded categories are broad navigation routing only (issue #76).
    """
    return KnowledgeBaseControlFile(
        version=CONTROL_FILE_VERSION,
        categories=list(SEED_CATEGORY_CATALOG),
        hot_index=[],
        mode=KB_MODE_CATEGORIZED,
        path=CONTROL_FILE_BASENAME,
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
    content = "\n".join(lines) + "\n"
    _atomic_write_text(target, content)
    return target


def _control_yaml_scalar(value: str) -> str:
    """Render a deterministic YAML scalar for the Control File.

    Simple identifiers are emitted bare for readability; anything containing a
    character YAML would reinterpret is double-quoted with backslash escapes.
    """
    if value and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_\-./]*", value) and not value.startswith(
        ("-", ".", "@", "%")
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
        _, issues = _load_and_validate_control_file(root, page_list)
        return list(issues)


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


def _load_and_validate(path: Path) -> tuple[KnowledgeBase, ValidationReport]:
    """Load and fully validate a Knowledge Base from an existing directory."""
    root = path.resolve()
    pages, issues = _load_pages_and_validate(root)
    issues.extend(_cross_page_issues(pages))
    control, control_issues = _load_and_validate_control_file(root, pages)
    issues.extend(control_issues)
    return KnowledgeBase(root=root, pages=pages, control=control), ValidationReport(issues=issues)


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

    return _load_and_validate(root)


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

    return _load_and_validate(root)[1]


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
    root = Path(root).resolve()
    sources: list[SourceFileDigest] = []

    # The Control File is canonical KB content: it defines the categorized-KB
    # contract and travels with the KB, so changes to it are real changes.
    control_path = _control_file_path(root)
    if control_path.exists():
        sources.append(
            SourceFileDigest(
                path=CONTROL_FILE_BASENAME,
                digest=hashlib.sha256(control_path.read_bytes()).hexdigest(),
            )
        )

    for file in _markdown_files(root):
        reserved_basename = _reserved_artifact_basename(file)
        if reserved_basename is not None:
            try:
                data, _body, _line = _parse_frontmatter(
                    file.read_text(encoding="utf-8"), file
                )
            except FrontmatterError:
                # Malformed reserved file: fingerprint it normally (blocking
                # validation already reports the marker problem).
                data = None
            if data is not None and _classify_reserved_artifact(
                data, reserved_basename
            ).valid:
                continue
        sources.append(
            SourceFileDigest(
                path=file.relative_to(root).as_posix(),
                digest=hashlib.sha256(file.read_bytes()).hexdigest(),
            )
        )
    combined = "\n".join(f"{source.path}:{source.digest}" for source in sources)
    overall = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    return SourceFingerprint(digest=overall, sources=sources)


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
    return (
        "---\n"
        "lumio:\n"
        "  artifact: navigation-index\n"
        f"  version: {NAV_INDEX_VERSION}\n"
        "---"
    )


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
            rest = candidate[len(prefix):]
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
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=prefix, suffix=".tmp"
    )
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
        if issue.field == "lumio"
        and PurePosixPath(issue.file).name.lower() == NAV_INDEX_BASENAME
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
            data, _body, _line = _parse_frontmatter(
                file.read_text(encoding="utf-8"), file
            )
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
                target == original_target
                or _same_physical_file(target, original_target)
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
    return (
        "---\n"
        "lumio:\n"
        f"  artifact: {HOT_INDEX_ARTIFACT}\n"
        f"  version: {HOT_INDEX_VERSION}\n"
        "---"
    )


def _activity_log_frontmatter() -> str:
    """Return the deterministic Activity Log frontmatter marker block."""
    return (
        "---\n"
        "lumio:\n"
        f"  artifact: {ACTIVITY_LOG_ARTIFACT}\n"
        f"  version: {ACTIVITY_LOG_VERSION}\n"
        "---"
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
        content = (
            _activity_log_frontmatter()
            + "\n\n# Activity Log\n\n"
            + line
        )
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
    contents: dict[str, str] = (
        {HOT_INDEX_BASENAME: content} if content is not None else {}
    )
    return _commit_reserved_artifacts(
        root, contents, basename=HOT_INDEX_BASENAME, force_write=True, stage_prefix=".lumio-hot-"
    )


def regenerate_hot_index(path: str | Path) -> list[Path]:
    """Regenerate the Hot Index after a sync (non-force), pruning when un-pinned."""
    root = Path(path).resolve()
    kb, _report = load_knowledge_base(root)
    content = generate_hot_index(kb.control, kb.pages)
    contents: dict[str, str] = (
        {HOT_INDEX_BASENAME: content} if content is not None else {}
    )
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
        described = ", ".join(
            sorted(_describe_reserved_collision(issue) for issue in collisions)
        )
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
