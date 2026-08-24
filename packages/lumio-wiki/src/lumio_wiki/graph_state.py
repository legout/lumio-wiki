"""Materialized Discovery Graph serialization (issue #108, ADR-0011; #170, ADR-0021).

The Discovery Graph adjacency (canonical accepted Claims plus Extracted
References, in both outgoing and incoming directions) is materialized as a
versioned MessagePack artifact in the configured derived index directory. The
artifact carries the Knowledge Base fingerprint and extractor version so a
later load accepts it ONLY when both match the loaded Knowledge Base behavior.
A missing, stale, corrupt, partial, or incompatible artifact is ignored and
the graph is rebuilt deterministically in memory — these functions NEVER raise
on a bad artifact.

Since issue #170 (ADR-0021) the adjacency is keyed by stable Entity IDs, not
Canonical Page Titles: a title or path change does not alter Entity identity
or traversal topology. Each edge records its origin (accepted entity Claim or
Extracted Reference), Claim ID, Predicate, direction view, and — for Extracted
References — the source path/line and extractor version.

Markdown remains the source of truth (ADR-0011). The artifact lives outside
the Knowledge Base source tree and is never a Compiled Page, Reserved
Artifact, fingerprint input, or OKF export entry. It works in an isolated
``lumio-wiki`` installation without LanceDB, PyArrow, or an operational
database.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

import msgpack

from lumio_wiki.records import (
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    GRAPH_EDGE_ORIGINS,
    GRAPH_EDGE_SCOPE_CANONICAL,
    GRAPH_EDGE_SCOPE_DISCOVERY,
    GRAPH_EDGE_SCOPES,
    GraphEdge,
    GraphState,
    SourceFingerprint,
)

# The MessagePack artifact format version. Bumped to 2 with the Entity-ID
# rebuild (issue #170): keys are Entity IDs and edges carry
# (endpoint, predicate, claim_id, origin, source_path, line_start, line_end,
# extractor_version) metadata, which a version-1 reader could misinterpret.
# Bumped only when the serialized layout changes in a way an older reader
# could misinterpret. Independent of the link-extractor version
# (``EXTRACTOR_VERSION``), which tracks when the derived edge SET changes.
GRAPH_ARTIFACT_VERSION = 2

# The artifact filename inside the configured derived index directory. Sits
# beside optional LanceDB tables under the same logical index dir.
GRAPH_ARTIFACT_FILENAME = "discovery-graph.msgpack"

# Edge fields, in the exact order serialized into the MessagePack layout.
_EDGE_FIELDS = (
    "endpoint",
    "predicate",
    "claim_id",
    "origin",
    "source_path",
    "line_start",
    "line_end",
    "extractor_version",
    "scope",
)


class _GraphIndex(Protocol):
    """The adjacency shape ``serialize_graph`` reads from a ``_KnowledgeIndex``.

    Kept as a Protocol so this module does not import the private
    ``_KnowledgeIndex`` (which would create a circular import with
    ``knowledge_base``). Any object exposing the two discovery adjacency maps
    works. Keys are Entity IDs (Canonical Page Titles only for Legacy Flat
    Mode pages without an Entity ID); edges are ``GraphEdge`` records.
    """

    discovery_adjacency: dict[str, list[GraphEdge]]
    discovery_incoming: dict[str, list[GraphEdge]]


def build_graph_state(
    index: _GraphIndex,
    fingerprint: SourceFingerprint,
    extractor_version: str,
) -> GraphState:
    """Build an in-memory ``GraphState`` from a discovery index.

    The adjacency is copied so later mutation of ``index`` cannot affect the
    returned state. ``edge_count`` counts outgoing discovery edges (incoming
    is the reverse view over the same edge set).
    """
    outgoing = {key: list(edges) for key, edges in index.discovery_adjacency.items()}
    incoming = {key: list(edges) for key, edges in index.discovery_incoming.items()}
    edge_count = sum(len(edges) for edges in outgoing.values())
    return GraphState(
        version=GRAPH_ARTIFACT_VERSION,
        fingerprint_digest=fingerprint.digest,
        extractor_version=extractor_version,
        outgoing=outgoing,
        incoming=incoming,
        edge_count=edge_count,
    )


def _encode_edge(edge: GraphEdge) -> list[object]:
    return [getattr(edge, field) for field in _EDGE_FIELDS]


def serialize_graph(
    index: _GraphIndex,
    fingerprint: SourceFingerprint,
    extractor_version: str,
) -> bytes:
    """Serialize the discovery adjacency to deterministic MessagePack bytes.

    Keys are emitted in sorted order and edges preserve the pre-sorted order
    stored by ``_KnowledgeIndex``, so serializing the same index twice (or
    rebuilding from unchanged Markdown) yields byte-identical output.
    """
    state = build_graph_state(index, fingerprint, extractor_version)
    payload = {
        "version": state.version,
        "fingerprint_digest": state.fingerprint_digest,
        "extractor_version": state.extractor_version,
        "outgoing": {
            key: [_encode_edge(edge) for edge in state.outgoing[key]]
            for key in sorted(state.outgoing)
        },
        "incoming": {
            key: [_encode_edge(edge) for edge in state.incoming[key]]
            for key in sorted(state.incoming)
        },
        "edge_count": state.edge_count,
    }
    return msgpack.packb(payload, use_bin_type=True)


def _decode_edge(values: dict[str, object]) -> GraphEdge | None:
    """Validate one decoded edge's origin-variant semantics, or ``None``.

    An edge must declare a known ``origin`` and honor its variant contract
    (ADR-0021): an accepted-Claim edge carries a Claim ID and Predicate and
    never masquerades as an Extracted Reference; an Extracted Reference edge
    never carries Claim identity (it is not promoted) and carries source
    provenance (non-empty path, 1-based bounded line range, extractor
    version). Anything else is a corrupt/incompatible artifact.
    """
    origin = values["origin"]
    if origin not in GRAPH_EDGE_ORIGINS:
        return None
    scope = values["scope"]
    if scope not in GRAPH_EDGE_SCOPES:
        return None
    if origin == GRAPH_EDGE_ORIGIN_CLAIM:
        if not values["claim_id"] or not values["predicate"]:
            return None
        if scope != GRAPH_EDGE_SCOPE_CANONICAL:
            return None
    elif origin == GRAPH_EDGE_ORIGIN_EXTRACTED:
        if values["claim_id"] or values["predicate"]:
            return None
        if not values["source_path"] or not values["extractor_version"]:
            return None
        if scope != GRAPH_EDGE_SCOPE_DISCOVERY:
            return None
        line_start, line_end = values["line_start"], values["line_end"]
        if not isinstance(line_start, int) or not isinstance(line_end, int):
            return None
        if line_start < 1 or line_end < line_start:
            return None
    return GraphEdge(**values)  # type: ignore[arg-type]


def _decode_adjacency(
    raw: object,
) -> dict[str, list[GraphEdge]] | None:
    """Validate and decode a MessagePack adjacency map, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    result: dict[str, list[GraphEdge]] = {}
    for key, edges in raw.items():
        if not isinstance(key, str):
            return None
        if not isinstance(edges, list):
            return None
        bucket: list[GraphEdge] = []
        for edge in edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != len(_EDGE_FIELDS):
                return None
            values: dict[str, object] = {}
            for field, value in zip(_EDGE_FIELDS, edge, strict=True):
                if field in ("line_start", "line_end"):
                    if not isinstance(value, int) or isinstance(value, bool):
                        return None
                elif not isinstance(value, str):
                    return None
                values[field] = value
            decoded = _decode_edge(values)
            if decoded is None:
                return None
            bucket.append(decoded)
        result[key] = bucket
    return result


def _incoming_is_reverse_of_outgoing(
    outgoing: dict[str, list[GraphEdge]],
    incoming: dict[str, list[GraphEdge]],
) -> bool:
    """Return whether ``incoming`` is exactly the reverse view of ``outgoing``.

    For every edge under ``outgoing[src]`` there must be a mirrored edge under
    ``incoming[edge.endpoint]`` whose endpoint is ``src`` and whose origin
    metadata (Claim ID, Predicate, origin, provenance) is identical, and vice
    versa, with no stray or missing entries. Buckets are compared as sorted
    multisets so storage order does not matter.
    """
    expected: list[tuple[str, GraphEdge]] = []
    for src, edges in outgoing.items():
        for edge in edges:
            expected.append((edge.endpoint, edge.reversed(src)))
    expected_norm = sorted(expected, key=lambda pair: (pair[0], pair[1].sort_key))
    actual: list[tuple[str, GraphEdge]] = [
        (key, edge) for key, edges in incoming.items() for edge in edges
    ]
    actual_norm = sorted(actual, key=lambda pair: (pair[0], pair[1].sort_key))
    return expected_norm == actual_norm


def deserialize_graph(data: bytes) -> GraphState | None:
    """Decode MessagePack bytes into a ``GraphState``, or ``None`` if unusable.

    Returns ``None`` for any decode error, schema mismatch, missing key, or
    version mismatch (corrupt / partial / incompatible). Staleness (fingerprint
    or extractor-version drift versus the live Knowledge Base) is checked
    separately by :func:`load_graph_artifact`, which has the expected values.
    Never raises.
    """
    try:
        payload = msgpack.unpackb(data, raw=False, strict_map_key=False)
    except (msgpack.exceptions.UnpackException, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        version = payload["version"]
        fingerprint_digest = payload["fingerprint_digest"]
        extractor_version = payload["extractor_version"]
        outgoing_raw = payload["outgoing"]
        incoming_raw = payload["incoming"]
        edge_count = payload["edge_count"]
    except (KeyError, TypeError):
        return None
    if version != GRAPH_ARTIFACT_VERSION:
        return None
    if not isinstance(fingerprint_digest, str) or not isinstance(extractor_version, str):
        return None
    if not isinstance(edge_count, int) or isinstance(edge_count, bool):
        return None
    outgoing = _decode_adjacency(outgoing_raw)
    incoming = _decode_adjacency(incoming_raw)
    if outgoing is None or incoming is None:
        return None
    # Semantic consistency: a partially-written or tampered artifact can decode
    # cleanly yet carry an edge_count that disagrees with the adjacency, or an
    # incoming map that is not the reverse of outgoing. Reject both so a corrupt
    # artifact is never promoted to live graph state (ADR-0011, AC3).
    if edge_count != sum(len(edges) for edges in outgoing.values()):
        return None
    if not _incoming_is_reverse_of_outgoing(outgoing, incoming):
        return None
    return GraphState(
        version=version,
        fingerprint_digest=fingerprint_digest,
        extractor_version=extractor_version,
        outgoing=outgoing,
        incoming=incoming,
        edge_count=edge_count,
    )


def write_graph_artifact(
    index_dir: str | Path,
    *,
    index: _GraphIndex,
    fingerprint: SourceFingerprint,
    extractor_version: str,
) -> Path:
    """Atomically materialize the Discovery Graph artifact and return its path.

    Writes to a temp file in the same directory and ``os.replace``s it onto the
    final path so an interrupted build can never replace a healthy artifact
    with partial state. The temp file is cleaned up on failure.
    """
    index_dir_path = Path(index_dir)
    index_dir_path.mkdir(parents=True, exist_ok=True)
    target = index_dir_path / GRAPH_ARTIFACT_FILENAME
    data = serialize_graph(index, fingerprint, extractor_version)
    fd, tmp_name = tempfile.mkstemp(dir=str(index_dir_path), prefix=".lumio-graph-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(Path(tmp_name), target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def load_graph_artifact(
    index_dir: str | Path,
    *,
    fingerprint: SourceFingerprint,
    extractor_version: str,
) -> GraphState | None:
    """Load the artifact when fresh, or ``None`` if missing/stale/corrupt.

    Accepts the artifact only when its graph version decodes, its recorded
    fingerprint matches ``fingerprint.digest``, and its extractor version
    matches ``extractor_version``. Any other condition (absent file, decode
    error, schema drift, version mismatch, fingerprint drift) returns ``None``
    so the caller falls back to deterministic in-memory derivation. Never
    raises.
    """
    path = Path(index_dir) / GRAPH_ARTIFACT_FILENAME
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    state = deserialize_graph(data)
    if state is None:
        return None
    if state.fingerprint_digest != fingerprint.digest:
        return None
    if state.extractor_version != extractor_version:
        return None
    return state


