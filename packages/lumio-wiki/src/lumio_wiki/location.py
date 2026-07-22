"""Public Knowledge Base Location + immutable Snapshot seam (issue #119, ADR-0013).

A **Knowledge Base Location** resolves an immutable **Published Version** (a
**Snapshot**) from a local filesystem or — in a future capability — object
storage. The filesystem implementation here is the verified reference: it wraps
the *existing* filesystem loading, validation, fingerprinting, zero-index
retrieval, and Discovery Graph traversal, so every observable result matches the
path-based Core SDK behavior exactly. Native S3 Locations are the parent epic
(#118) and are intentionally out of scope for this module.

This seam preserves every existing filesystem workflow:

* The path-based entrypoint :func:`lumio_wiki.knowledge_base.load_knowledge_base`
  is retained unchanged and remains the shared loading core.
* New clients open, validate, search, retrieve, traverse the Discovery Graph,
  and cite a local Published Version through the Location/Snapshot seam without
  changing observable results.

See ADR-0013 (S3-native Knowledge Base Locations), ADR-0011 (Discovery Graph),
ADR-0010 (Core SDK / zero-index), and the ubiquitous language in CONTEXT.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

import msgspec

from lumio_wiki.embeddings import DEFAULT_SEMANTIC_THRESHOLD, Embedder, RetrievalMode
from lumio_wiki.knowledge_base import (
    DEFAULT_GRAPH_MAX_DEPTH,
    DEFAULT_GRAPH_MAX_EDGES,
    DEFAULT_GRAPH_MAX_RESULTS,
    GRAPH_DIRECTION_OUTGOING,
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    ExtractionDiagnostic,
    KnowledgeBase,
    KnowledgeBaseError,
    fingerprint_sources,
    load_knowledge_base,
)
from lumio_wiki.records import (
    CompiledPage,
    ExtractedReference,
    KnowledgeBaseControlFile,
    PageSearchResult,
    RegistryEntry,
    Relationship,
    RetrievalResult,
    SourceFingerprint,
    ValidationReport,
)

# Stable identifier for the filesystem Location implementation. Future
# implementations (S3, etc.) declare their own ``kind`` so traces and
# diagnostics can name the resolved source without coupling to a class name.
FILESYSTEM_LOCATION_KIND = "filesystem"

__all__ = [
    "FILESYSTEM_LOCATION_KIND",
    "FilesystemLocation",
    "KnowledgeBaseLocation",
    "KnowledgeBaseSnapshot",
    "open_filesystem_knowledge_base",
    "open_knowledge_base",
]


@runtime_checkable
class KnowledgeBaseLocation(Protocol):
    """Resolve a Knowledge Base's Published Versions into immutable Snapshots.

    A Location is deployment or CLI configuration: it knows how to reach a
    Knowledge Base (a local directory today; an object-storage prefix in the S3
    capability) and how to resolve the current immutable Published Version into
    a :class:`KnowledgeBaseSnapshot`. Per ADR-0013 and the ubiquitous language,
    a Location's credentials are deployment configuration — they are never
    portable Knowledge Base content and never travel with the Published Version.

    The contract is verified through public behavior only: a Location has a
    stable :attr:`kind`, a secret-free :meth:`describe`, and a :meth:`resolve`
    that returns an immutable Snapshot. ``runtime_checkable`` lets clients
    assert conformance structurally without inheriting a base class.
    """

    @property
    def kind(self) -> str:
        """A short, stable identifier for the location implementation."""
        ...

    def describe(self) -> str:
        """Return a human-readable, secret-free description for diagnostics/traces.

        Implementations MUST NOT include credentials, tokens, or secrets.
        """
        ...

    def resolve(self) -> KnowledgeBaseSnapshot:
        """Resolve the current Published Version into an immutable Snapshot.

        Raises :class:`lumio_wiki.knowledge_base.KnowledgeBaseError` when the
        location cannot be resolved (e.g. a missing or non-directory path).
        """
        ...


class KnowledgeBaseSnapshot(msgspec.Struct, frozen=True):
    """An immutable Published Version view of a resolved Knowledge Base.

    A Snapshot is what a reader, the CLI, or a local agent holds after a
    Location resolves a Published Version. It is the read-only surface over the
    loaded Compiled Pages: validation, fingerprinting, search, zero-index
    retrieval, Discovery Graph traversal, Citations, and Retrieval Traces all
    delegate to the underlying :class:`KnowledgeBase` and match the filesystem
    behavior byte-for-byte.

    The Published Version fingerprint (:attr:`fingerprint`) is captured at
    resolve time and is part of the immutable contract — it identifies the exact
    version a reader synced to. A Snapshot never exposes write, build, or
    publish operations: those remain on :class:`KnowledgeBase` and the
    path-based publishing entrypoints.
    """

    knowledge_base: KnowledgeBase
    validation_report: ValidationReport
    fingerprint: SourceFingerprint
    location: KnowledgeBaseLocation

    # ------------------------------------------------------------------
    # Immutable Published Version identity and provenance.
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The resolved Knowledge Base root for this Published Version."""
        return self.knowledge_base.root

    @property
    def pages(self) -> tuple[CompiledPage, ...]:
        """The Compiled Pages of this Published Version (immutable view).

        Returns a fresh immutable tuple so a caller cannot mutate the Snapshot's
        page collection (append/clear/sort). The CompiledPage records themselves
        are frozen, so the returned view is fully immutable.
        """
        return tuple(self.knowledge_base.pages)

    @property
    def control(self) -> KnowledgeBaseControlFile | None:
        """The Knowledge Base Control File, if this is a categorized Knowledge Base."""
        return self.knowledge_base.control

    @property
    def index_dir(self) -> Path | None:
        """The derived index directory bound to the underlying Knowledge Base, if any.

        A freshly resolved filesystem Snapshot has no bound index directory, so
        retrieval defaults to the always-available zero-index path.
        """
        return self.knowledge_base.index_dir

    # ------------------------------------------------------------------
    # Lookups and authorization selection.
    # ------------------------------------------------------------------

    def lookup_by_title(self, title: str) -> list[CompiledPage]:
        return self.knowledge_base.lookup_by_title(title)

    def lookup_by_alias(self, alias: str) -> list[CompiledPage]:
        return self.knowledge_base.lookup_by_alias(alias)

    def lookup_by_tag(self, tag: str) -> list[CompiledPage]:
        return self.knowledge_base.lookup_by_tag(tag)

    def lookup_by_source(self, source_id: str) -> list[CompiledPage]:
        return self.knowledge_base.lookup_by_source(source_id)

    def lookup_by_lifecycle(self, lifecycle: str) -> list[CompiledPage]:
        return self.knowledge_base.lookup_by_lifecycle(lifecycle)

    def public_pages(self) -> list[CompiledPage]:
        """Return the public Compiled Pages (the public export authorization scope)."""
        return self.knowledge_base.public_pages()

    def registry(self) -> list[RegistryEntry]:
        """Return a compact registry entry for every Compiled Page."""
        return self.knowledge_base.registry()

    # ------------------------------------------------------------------
    # Search, retrieval, Citations, and Retrieval Traces.
    # ------------------------------------------------------------------

    def search_pages(
        self,
        query: str,
        limit: int = 20,
        pages: Sequence[CompiledPage] | None = None,
    ) -> list[PageSearchResult]:
        """Search page metadata and body text with deterministic lexical ranking."""
        return self.knowledge_base.search_pages(query, limit=limit, pages=pages)

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
        """Retrieve citation-ready Evidence for ``query`` over this Published Version.

        Defaults to the always-available zero-index lexical adapter; observable
        results, Citations, and Retrieval Traces match the path-based loader.
        """
        return self.knowledge_base.retrieve(
            query,
            limit=limit,
            index_dir=index_dir,
            hints=hints,
            mode=mode,
            embedder=embedder,
            score_threshold=score_threshold,
            graph_seed_titles=graph_seed_titles,
            graph_scope=graph_scope,
            graph_direction=graph_direction,
            graph_max_depth=graph_max_depth,
        )

    # ------------------------------------------------------------------
    # Discovery Graph traversal (ADR-0011).
    # ------------------------------------------------------------------

    def related_from(
        self, title: str, relationship_type: str | None = None
    ) -> list[Relationship]:
        return self.knowledge_base.related_from(title, relationship_type)

    def graph_path(self, source_title: str, target_title: str) -> list[str] | None:
        return self.knowledge_base.graph_path(source_title, target_title)

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
        return self.knowledge_base.related_pages(
            title,
            direction=direction,
            scope=scope,
            candidate_titles=candidate_titles,
            relationship_type=relationship_type,
            max_depth=max_depth,
            max_edges=max_edges,
            max_results=max_results,
        )

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
        return self.knowledge_base.shortest_path(
            source_title,
            target_title,
            direction=direction,
            scope=scope,
            candidate_titles=candidate_titles,
            max_depth=max_depth,
            max_edges=max_edges,
        )

    def extracted_references(self, title: str) -> list[ExtractedReference]:
        return self.knowledge_base.extracted_references(title)

    def extraction_diagnostics(self) -> list[ExtractionDiagnostic]:
        return self.knowledge_base.extraction_diagnostics()


class FilesystemLocation:
    """The verified reference Location: a local filesystem Knowledge Base.

    Resolves the current content of a local directory into an immutable
    Snapshot by loading, validating, and fingerprinting it through the existing
    path-based Core SDK loader. This is the reference implementation every
    future Location (S3, etc.) must match in observable behavior.

    The path is not validated until :meth:`resolve` is called, so a
    ``FilesystemLocation`` can be constructed ahead of time as deployment
    configuration and resolved lazily.
    """

    kind = FILESYSTEM_LOCATION_KIND

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The local filesystem path this Location resolves."""
        return self._path

    def describe(self) -> str:
        return f"filesystem:{self._path}"

    def resolve(self) -> KnowledgeBaseSnapshot:
        root = self._path.resolve()
        if not root.exists():
            raise KnowledgeBaseError(f"path does not exist: {root}")
        if not root.is_dir():
            raise KnowledgeBaseError(f"path is not a directory: {root}")
        kb, report = load_knowledge_base(root)
        return KnowledgeBaseSnapshot(
            knowledge_base=kb,
            validation_report=report,
            fingerprint=fingerprint_sources(root),
            location=self,
        )


def open_knowledge_base(location: KnowledgeBaseLocation) -> KnowledgeBaseSnapshot:
    """Open a Knowledge Base through the Location seam.

    The canonical Location-based entrypoint: resolves ``location``'s current
    Published Version into an immutable Snapshot. Equivalent to
    ``location.resolve()``; provided as a discoverable public function.
    """
    return location.resolve()


def open_filesystem_knowledge_base(path: str | Path) -> KnowledgeBaseSnapshot:
    """Open a local filesystem Knowledge Base as an immutable Snapshot.

    Convenience shorthand for ``FilesystemLocation(path).resolve()``. Clients
    migrating from the path-based :func:`load_knowledge_base` entrypoint can
    adopt this to receive the richer Snapshot (validation report, Published
    Version fingerprint, and provenance) while keeping identical observable
    results.
    """
    return FilesystemLocation(path).resolve()
