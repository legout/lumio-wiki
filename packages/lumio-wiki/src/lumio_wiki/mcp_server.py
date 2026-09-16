"""Read-only MCP server (Plan 06, PRD-0007, ADR-0028).

``lumio-wiki mcp <location>`` serves ONE Knowledge Base Location — a local
path or an ``s3://`` object-store URI — over MCP stdio through the official
``mcp`` SDK's high-level server API. Every tool call resolves a fresh
immutable Snapshot through :func:`lumio_wiki.location.open_knowledge_base`;
tools are pure reads and never write Knowledge Base content.

The v1 tool surface is the contract table in ``docs/plans/06-mcp-server.md``:
``search``, ``page``, ``related``, ``paths``, ``hot``, ``index``, ``health``,
and ``status``. Retrieval tools project Snapshot/CLI behavior verbatim
(``search_pages``, ``lookup_by_title``/``lookup_by_alias``, ``related_pages``,
``shortest_path``, ``generate_hot_index``, ``generate_navigation_indexes``);
diagnostics derive only from in-memory Snapshot data — never
``KnowledgeBase.health_report()`` (its filesystem revalidation is invalid for
S3 Snapshots) and never a derived-index directory or graph materialization.

The SDK lives behind the optional ``[mcp]`` extra and is imported lazily here,
so importing ``lumio-wiki`` never pulls it in; a missing installation produces
the exact install command instead of a bare ``ModuleNotFoundError``.

SDK API name note: ``mcp`` 2.x renamed the high-level ``FastMCP`` class to
``MCPServer`` (``mcp.server.mcpserver``) with the decorator API unchanged.
Plan 06's ``mcp.server.fastmcp`` module path was stale at approval time; the
lane decision (2026-09-15, M1) floors the dependency at the current stable
2.x and uses ``MCPServer``. Tool names, arguments, results, stdio transport,
and ``[mcp]`` packaging are unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from lumio_wiki.knowledge_base import (
    DEFAULT_GRAPH_MAX_DEPTH,
    DEFAULT_GRAPH_MAX_EDGES,
    DEFAULT_GRAPH_MAX_RESULTS,
    NAV_INDEX_BASENAME,
    KnowledgeBaseError,
    generate_hot_index,
    generate_navigation_indexes,
)
from lumio_wiki.location import (
    KnowledgeBaseLocation,
    KnowledgeBaseSnapshot,
    open_knowledge_base,
)
from lumio_wiki.s3_location import S3Location

__all__ = [
    "build_server",
    "health_rows",
    "hot_rows",
    "index_rows",
    "page_rows",
    "path_rows",
    "related_rows",
    "run_server",
    "search_rows",
    "status_rows",
]

GraphScope = Literal["canonical", "discovery"]
GraphDirection = Literal["outgoing", "incoming", "both"]


def _import_server() -> Any:
    """Import and return the SDK server class, or raise the missing-extra error.

    Mirrors the ``[s3]`` extra's guard in ``s3_location._require_obstore``:
    the caller gets the exact install command, never a raw import traceback.
    """
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:
        raise KnowledgeBaseError(
            "the MCP server requires the optional '[mcp]' extra; "
            "install it with:\n  pip install 'lumio-wiki[mcp]'"
        ) from exc
    return MCPServer


# ---------------------------------------------------------------------------
# The v1 tool projections. Each takes one Snapshot and returns the exact
# JSON shape from the contract table; the registered tools resolve a fresh
# Snapshot per call and delegate here.
# ---------------------------------------------------------------------------


def search_rows(snapshot: KnowledgeBaseSnapshot, query: str, limit: int = 20) -> dict[str, Any]:
    """Answer the v1 ``search`` contract from one Snapshot.

    Returns ``{"results": [{"title", "path", "passage"}]}`` — the citation
    data the guardrails require. ``passage`` is the deterministic matched
    excerpt from ``Snapshot.search_pages``; ``limit`` is forwarded unchanged.
    """
    results = snapshot.search_pages(query, limit=limit)
    return {
        "results": [
            {"title": result.page.title, "path": result.page.path, "passage": result.snippet}
            for result in results
        ]
    }


def page_rows(snapshot: KnowledgeBaseSnapshot, title: str) -> dict[str, Any]:
    """Answer the v1 ``page`` contract: canonical lookup, then alias fallback.

    Mirrors the CLI's plain ``page`` output for one page (the first match of
    ``lookup_by_title`` / ``lookup_by_alias``); an unknown title is the honest
    ``{"found": false}``, never an error.
    """
    pages = snapshot.lookup_by_title(title)
    if not pages:
        pages = snapshot.lookup_by_alias(title)
    if not pages:
        return {"found": False}
    page = pages[0]
    return {
        "found": True,
        "title": page.title,
        "path": page.path,
        "summary": page.summary,
        "markdown": page.body,
    }


def related_rows(
    snapshot: KnowledgeBaseSnapshot,
    title: str,
    *,
    relationship_type: str | None = None,
    depth: int = 1,
    scope: str = "canonical",
    direction: str = "outgoing",
    max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
    max_results: int = DEFAULT_GRAPH_MAX_RESULTS,
) -> dict[str, Any]:
    """Answer the v1 ``related`` contract: sorted title rows plus the echo.

    Forwards the CLI's ``related`` arguments to ``Snapshot.related_pages``
    unchanged; the CLI's Entity-ID suffixes are display only and are not data.
    """
    titles = snapshot.related_pages(
        title,
        direction=direction,
        scope=scope,
        relationship_type=relationship_type,
        max_depth=depth,
        max_edges=max_edges,
        max_results=max_results,
    )
    return {"results": sorted(titles), "scope": scope, "direction": direction}


def path_rows(
    snapshot: KnowledgeBaseSnapshot,
    source: str,
    target: str,
    *,
    scope: str = "canonical",
    direction: str = "outgoing",
    max_depth: int = DEFAULT_GRAPH_MAX_DEPTH,
    max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
) -> dict[str, Any]:
    """Answer the v1 ``paths`` contract from ``Snapshot.shortest_path``.

    Both the found and not-found shapes echo the requested scope/direction;
    a missing path is ``{"found": false, ...}``, never an error.
    """
    path = snapshot.shortest_path(
        source,
        target,
        direction=direction,
        scope=scope,
        max_depth=max_depth,
        max_edges=max_edges,
    )
    if path is None:
        return {"found": False, "scope": scope, "direction": direction}
    return {"found": True, "path": list(path), "scope": scope, "direction": direction}


def hot_rows(snapshot: KnowledgeBaseSnapshot) -> dict[str, Any]:
    """Answer the v1 ``hot`` contract: the in-memory Hot Index.

    ``generate_hot_index`` returns ``None`` when no pins are configured; that
    is the honest ``{"markdown": null}``, never an error.
    """
    knowledge_base = snapshot.knowledge_base
    return {"markdown": generate_hot_index(knowledge_base.control, knowledge_base.pages)}


def index_rows(snapshot: KnowledgeBaseSnapshot) -> dict[str, Any]:
    """Answer the v1 ``index`` contract: the ROOT Navigation Index only.

    The root ``index.md`` entry of ``generate_navigation_indexes`` — the
    ``lumio-wiki index`` default view — not the per-directory set.
    """
    indexes = generate_navigation_indexes(snapshot.pages)
    return {"markdown": indexes[NAV_INDEX_BASENAME]}


def _graph_disclosure(snapshot: KnowledgeBaseSnapshot) -> tuple[dict[str, Any], bool, str | None]:
    """Return ``(graph, derived_available, derived_kind)`` without writing.

    Filesystem Locations probe the derived index directory only after it is
    known to exist, then use the read-only ``graph_health``; a materialized,
    fresh artifact is ``source: "derived"``, everything else honestly reports
    ``source: "zero-index"`` with the actual state. S3 Locations use the
    read-only ``graph_state_with_source`` seam. Nothing here creates a
    derived-index directory or materializes a graph artifact.
    """
    location = snapshot.location
    if isinstance(location, S3Location):  # narrowing: the S3-only read seam
        state, source_label, note = location.graph_state_with_source(snapshot)
        if source_label == "published artifact":
            graph = {
                "materialized": True,
                "fresh": True,
                "edges": state.edge_count,
                "source": "derived",
                "disclosure": "published graph artifact (manifest digest-verified)",
            }
        else:
            graph = {
                "materialized": False,
                "fresh": None,
                "edges": state.edge_count,
                "source": "zero-index",
                "disclosure": note or "graph derived in memory",
            }
        descriptor = snapshot.remote_derived_index
        return graph, descriptor is not None, "remote-lancedb" if descriptor else None

    from lumio_wiki.cli import default_index_dir

    derived_dir = default_index_dir(snapshot.root)
    if derived_dir.is_dir():
        report = snapshot.knowledge_base.graph_health(derived_dir)
        if report.materialized and report.graph_fresh:
            graph = {
                "materialized": True,
                "fresh": True,
                "edges": report.edge_count,
                "source": "derived",
                "disclosure": "graph artifact is materialized and fresh",
            }
        else:
            state = "stale" if report.materialized else "missing"
            graph = {
                "materialized": report.materialized,
                "fresh": report.graph_fresh,
                "edges": report.edge_count,
                "source": "zero-index",
                "disclosure": f"graph artifact is {state}; serving from the in-memory graph",
            }
        return graph, True, "filesystem"
    return (
        {
            "materialized": False,
            "fresh": None,
            "edges": None,
            "source": "zero-index",
            "disclosure": "no derived index directory",
        },
        False,
        None,
    )


def health_rows(snapshot: KnowledgeBaseSnapshot) -> dict[str, Any]:
    """Answer the v1 ``health`` contract from in-memory Snapshot data only.

    Mirrors ``KnowledgeBase.health_report()``'s derivation of the
    broken/unknown/invalid sets from validation issues, but reads the
    Snapshot's own ``validation_report`` (never a filesystem revalidation,
    which is invalid for S3 Snapshots) and the Snapshot's pages. Pure read.
    """
    knowledge_base = snapshot.knowledge_base
    report = snapshot.validation_report

    broken_relationships: set[str] = set()
    unknown_relationship_types: set[str] = set()
    invalid_fields: set[str] = set()
    for issue in report.issues:
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
    for page in knowledge_base.pages:
        for alias in page.aliases:
            if alias:
                alias_locations.setdefault(alias, []).append(page.path)
    duplicate_aliases = sorted(alias for alias, paths in alias_locations.items() if len(paths) > 1)

    graph, derived_available, derived_kind = _graph_disclosure(snapshot)
    return {
        "valid": report.is_valid,
        "pages": len(knowledge_base.pages),
        "issues": len(report.issues),
        "broken_relationships": sorted(broken_relationships),
        "unknown_relationship_types": sorted(unknown_relationship_types),
        "invalid_fields": sorted(invalid_fields),
        "duplicate_aliases": duplicate_aliases,
        "missing_summaries": sorted(
            entry.path for entry in knowledge_base.registry() if not entry.summary
        ),
        "graph": graph,
        "derived_index": {"available": derived_available, "kind": derived_kind},
    }


def _installed_extras() -> list[str]:
    """Installed optional extras, detected exactly like ``lumio-wiki doctor``."""
    from lumio_wiki.cli import _detect_module

    extras = []
    if all(_detect_module(module) for module in ("liteparse", "markitdown", "anydoc")):
        extras.append("documents")
    if _detect_module("openai"):
        extras.append("llm")
    if _detect_module("obstore"):
        extras.append("s3")
    if _detect_module("lancedb"):
        extras.append("lancedb")
    return extras


def status_rows(snapshot: KnowledgeBaseSnapshot) -> dict[str, Any]:
    """Answer the v1 ``status`` contract from one Snapshot.

    ``retrieval`` is ``"zero-index"`` unconditionally: the server binds no
    remote retrieval adapter; a remote derived index is disclosed separately.
    """
    return {
        "location": {"kind": snapshot.location.kind},
        "fingerprint": snapshot.fingerprint.digest,
        "pages": len(snapshot.pages),
        "valid": snapshot.validation_report.is_valid,
        "retrieval": "zero-index",
        "remote_derived_index": {"available": snapshot.remote_derived_index is not None},
        "extras": _installed_extras(),
    }


# ---------------------------------------------------------------------------
# Server assembly.
# ---------------------------------------------------------------------------


def build_server(location: KnowledgeBaseLocation) -> Any:
    """Build the stdio MCP server bound to ONE Knowledge Base Location."""
    server_factory = _import_server()
    server = server_factory("lumio-wiki")

    @server.tool()
    def search(query: str, limit: int = 20) -> dict[str, Any]:
        """Search the Knowledge Base; returns title/path/passage rows."""
        return search_rows(open_knowledge_base(location), query, limit=limit)

    @server.tool()
    def page(title: str) -> dict[str, Any]:
        """Read one Compiled Page by Canonical Page Title (or alias)."""
        return page_rows(open_knowledge_base(location), title)

    @server.tool()
    def related(
        title: str,
        relationship_type: str | None = None,
        depth: int = 1,
        scope: GraphScope = "canonical",
        direction: GraphDirection = "outgoing",
        max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
        max_results: int = DEFAULT_GRAPH_MAX_RESULTS,
    ) -> dict[str, Any]:
        """List pages related to a title; sorted rows with scope/direction."""
        return related_rows(
            open_knowledge_base(location),
            title,
            relationship_type=relationship_type,
            depth=depth,
            scope=scope,
            direction=direction,
            max_edges=max_edges,
            max_results=max_results,
        )

    @server.tool()
    def paths(
        source: str,
        target: str,
        scope: GraphScope = "canonical",
        direction: GraphDirection = "outgoing",
        max_depth: int = DEFAULT_GRAPH_MAX_DEPTH,
        max_edges: int = DEFAULT_GRAPH_MAX_EDGES,
    ) -> dict[str, Any]:
        """Find the shortest directed path between two titles."""
        return path_rows(
            open_knowledge_base(location),
            source,
            target,
            scope=scope,
            direction=direction,
            max_depth=max_depth,
            max_edges=max_edges,
        )

    @server.tool()
    def hot() -> dict[str, Any]:
        """Read the Maintainer-pinned Hot Index (null when no pins exist)."""
        return hot_rows(open_knowledge_base(location))

    @server.tool()
    def index() -> dict[str, Any]:
        """Read the root Navigation Index (the full page catalog)."""
        return index_rows(open_knowledge_base(location))

    @server.tool()
    def health() -> dict[str, Any]:
        """Report validation, page, and graph state. Read-only; writes nothing."""
        return health_rows(open_knowledge_base(location))

    @server.tool()
    def status() -> dict[str, Any]:
        """Report the bound Location, fingerprint, and retrieval state."""
        return status_rows(open_knowledge_base(location))

    return server


def _reject_invalid_snapshot(snapshot: KnowledgeBaseSnapshot) -> None:
    """Refuse to serve a Snapshot whose validation report has errors (AC1)."""
    if not snapshot.validation_report.is_valid:
        raise KnowledgeBaseError(
            "the Knowledge Base is invalid; run 'lumio-wiki validate' and fix "
            "the reported errors before serving it"
        )


def run_server(argument: str) -> int:
    """Bind ONE Knowledge Base Location, prove it resolves, serve MCP over stdio.

    Filesystem arguments construct a :class:`FilesystemLocation`; ``s3://``
    arguments reuse the CLI's existing resolver (``_resolve_s3_snapshot``,
    which applies the env config and the redacting URI parser) and resolve
    once up front — so invalid paths, unloadable or invalid Knowledge Bases,
    and missing credentials fail with bounded, credential-free errors before
    the server starts, and the supplied URI is never echoed.
    """
    from lumio_wiki.cli import _is_object_store_uri, _resolve_s3_snapshot
    from lumio_wiki.location import FilesystemLocation

    if _is_object_store_uri(argument):
        # One construction + resolution; failures carry the redacted location.
        snapshot = _resolve_s3_snapshot(argument)
        location = snapshot.location
    else:
        location = FilesystemLocation(argument)
        snapshot = open_knowledge_base(location)
    _reject_invalid_snapshot(snapshot)
    server = build_server(location)
    server.run()  # stdio is the SDK default transport
    return 0
