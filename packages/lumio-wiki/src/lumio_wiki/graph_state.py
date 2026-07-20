"""Materialized Discovery Graph serialization (issue #108, ADR-0011).

The Discovery Graph adjacency (canonical Relationships plus Extracted
References, in both outgoing and incoming directions) is materialized as a
versioned MessagePack artifact in the configured derived index directory. The
artifact carries the Knowledge Base fingerprint and extractor version so a
later load accepts it ONLY when both match the loaded Knowledge Base behavior.
A missing, stale, corrupt, partial, or incompatible artifact is ignored and
the graph is rebuilt deterministically in memory — these functions NEVER raise
on a bad artifact.

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

from lumio_wiki.records import GraphState, SourceFingerprint

# The MessagePack artifact format version. Bumped only when the serialized
# layout changes in a way an older reader could misinterpret. Independent of
# the link-extractor version (``EXTRACTOR_VERSION``), which tracks when the
# derived edge SET changes.
GRAPH_ARTIFACT_VERSION = 1

# The artifact filename inside the configured derived index directory. Sits
# beside optional LanceDB tables under the same logical index dir.
GRAPH_ARTIFACT_FILENAME = "discovery-graph.msgpack"


class _GraphIndex(Protocol):
    """The adjacency shape ``serialize_graph`` reads from a ``_KnowledgeIndex``.

    Kept as a Protocol so this module does not import the private
    ``_KnowledgeIndex`` (which would create a circular import with
    ``knowledge_base``). Any object exposing the two discovery adjacency maps
    works.
    """

    discovery_adjacency: dict[str, list[tuple[str, str]]]
    discovery_incoming: dict[str, list[tuple[str, str]]]


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
    outgoing = {
        title: [tuple(edge) for edge in edges]
        for title, edges in index.discovery_adjacency.items()
    }
    incoming = {
        title: [tuple(edge) for edge in edges]
        for title, edges in index.discovery_incoming.items()
    }
    edge_count = sum(len(edges) for edges in outgoing.values())
    return GraphState(
        version=GRAPH_ARTIFACT_VERSION,
        fingerprint_digest=fingerprint.digest,
        extractor_version=extractor_version,
        outgoing=outgoing,
        incoming=incoming,
        edge_count=edge_count,
    )


def serialize_graph(
    index: _GraphIndex,
    fingerprint: SourceFingerprint,
    extractor_version: str,
) -> bytes:
    """Serialize the discovery adjacency to deterministic MessagePack bytes.

    Keys are emitted in sorted order and edges preserve the pre-sorted
    ``(endpoint, type)`` order stored by ``_KnowledgeIndex``, so serializing
    the same index twice (or rebuilding from unchanged Markdown) yields
    byte-identical output.
    """
    state = build_graph_state(index, fingerprint, extractor_version)
    payload = {
        "version": state.version,
        "fingerprint_digest": state.fingerprint_digest,
        "extractor_version": state.extractor_version,
        "outgoing": {
            title: [[endpoint, etype] for endpoint, etype in state.outgoing[title]]
            for title in sorted(state.outgoing)
        },
        "incoming": {
            title: [[endpoint, etype] for endpoint, etype in state.incoming[title]]
            for title in sorted(state.incoming)
        },
        "edge_count": state.edge_count,
    }
    return msgpack.packb(payload, use_bin_type=True)


def _decode_adjacency(
    raw: object,
) -> dict[str, list[tuple[str, str]]] | None:
    """Validate and decode a MessagePack adjacency map, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    result: dict[str, list[tuple[str, str]]] = {}
    for key, edges in raw.items():
        if not isinstance(key, str):
            return None
        if not isinstance(edges, list):
            return None
        bucket: list[tuple[str, str]] = []
        for edge in edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                return None
            endpoint, etype = edge
            if not isinstance(endpoint, str) or not isinstance(etype, str):
                return None
            bucket.append((endpoint, etype))
        result[key] = bucket
    return result


def _incoming_is_reverse_of_outgoing(
    outgoing: dict[str, list[tuple[str, str]]],
    incoming: dict[str, list[tuple[str, str]]],
) -> bool:
    """Return whether ``incoming`` is exactly the reverse view of ``outgoing``.

    For every edge ``(endpoint, type)`` under ``outgoing[src]`` there must be a
    matching ``(src, type)`` under ``incoming[endpoint]``, and vice versa, with
    no stray or missing entries. Buckets are compared as sorted multisets so
    storage order does not matter.
    """
    expected: dict[str, list[tuple[str, str]]] = {}
    for src, edges in outgoing.items():
        for endpoint, etype in edges:
            expected.setdefault(endpoint, []).append((src, etype))
    expected_norm = {
        key: sorted(value) for key, value in expected.items()
    }
    actual_norm = {key: sorted(value) for key, value in incoming.items()}
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
    fd, tmp_name = tempfile.mkstemp(
        dir=str(index_dir_path), prefix=".lumio-graph-", suffix=".tmp"
    )
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
