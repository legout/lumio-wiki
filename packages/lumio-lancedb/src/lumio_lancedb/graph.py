"""Disposable LanceDB Entity and Claim graph projections (issue #171, ADR-0021).

Materializes the Knowledge Base's Entities and Claims as two LanceDB tables
beside the Evidence/page tables: ``entities`` (one row per stable Entity —
plus one row per retired redirect — with types, aliases, lifecycle, page
path, and normalized search text) and ``graph_edges`` (one row per Claim —
any lifecycle, entity object or typed literal, confidence/valid-time,
evidence anchors — plus one row per Extracted Reference, marked separately).

The projections are disposable and fingerprint-bound, never canonical:

* :func:`build_graph_tables` builds fresh-snapshot tables from a loaded
  Knowledge Base and records the source fingerprint beside them.
* :func:`load_graph_state` reads the accepted entity-to-entity Claim edges
  and Extracted References back into the SAME in-memory
  :class:`lumio_wiki.records.GraphState` the zero-index MessagePack cache
  produces, so traversal stays in memory — never one LanceDB query per hop.
* Missing, corrupt, stale, or fingerprint-mismatched tables never raise:
  ``load_graph_state`` returns ``None`` so the caller falls back to the
  MessagePack artifact or deterministic in-memory derivation (ADR-0021).

Semantic candidate retrieval (vectors over these tables) requires an
embedder and is deliberately not built here; scalar and lexical graph use
needs no embedder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec
from lumio_wiki.graph_state import GRAPH_ARTIFACT_VERSION
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    EXTRACTOR_VERSION,
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    GRAPH_EDGE_ORIGINS,
    GRAPH_EDGE_SCOPE_CANONICAL,
    GRAPH_EDGE_SCOPE_DISCOVERY,
    Entity,
    EntityResolutionCandidate,
    GraphEdge,
    GraphState,
    SourceFingerprint,
)

try:  # pragma: no cover - exercised indirectly via build/search tests
    import pyarrow as pa
    from lancedb.index import FTS, LabelList
except ImportError:  # pragma: no cover - optional dependency unavailable
    pa = None
    FTS = None
    LabelList = None

from lumio_lancedb.index import _load_fingerprint, _save_fingerprint
from lumio_lancedb.location import IndexLocation, as_location

ENTITY_TABLE_NAME = "entities"
GRAPH_EDGE_TABLE_NAME = "graph_edges"


def _entity_schema() -> pa.Schema:
    """Arrow schema for the ``entities`` table (issue #171 scope).

    ``redirect_to`` is empty for page-owning Entities and carries the
    surviving Entity ID for retired redirect rows (no page of their own).
    A vector column is deliberately absent: semantic entity search requires
    an embedder and has no measured use yet (ADR-0021 keeps LanceDB
    projections limited to measured-useful indexes).
    """
    return pa.schema(
        [
            pa.field("entity_id", pa.string()),
            pa.field("title", pa.string()),
            pa.field("entity_types", pa.list_(pa.string())),
            pa.field("aliases", pa.list_(pa.string())),
            pa.field("lifecycle", pa.string()),
            pa.field("redirect_to", pa.string()),
            pa.field("page_path", pa.string()),
            pa.field("search_text", pa.string()),
        ]
    )


def _graph_edge_schema() -> pa.Schema:
    """Arrow schema for the ``graph_edges`` table (issue #171 scope).

    One row per Claim (any published lifecycle; ``status`` keeps disputed and
    superseded Claims inspectable without entering traversal) and one row per
    Extracted Reference (``kind`` marks them separately; they never carry
    Claim identity). ``endpoint`` is the neighbor's graph key — empty for
    literal Claims and Claims whose object Entity has no loaded page, which
    therefore never enter traversal. Evidence anchors are carried as one JSON
    document per row.
    """
    return pa.schema(
        [
            pa.field("edge_id", pa.string()),
            pa.field("kind", pa.string()),
            pa.field("subject", pa.string()),
            pa.field("endpoint", pa.string()),
            pa.field("predicate", pa.string()),
            pa.field("status", pa.string()),
            pa.field("object_entity", pa.string()),
            pa.field("value", pa.string()),
            pa.field("value_type", pa.string()),
            pa.field("confidence", pa.float64()),
            pa.field("valid_from", pa.string()),
            pa.field("valid_to", pa.string()),
            pa.field("claim_origin", pa.string()),
            pa.field("evidence", pa.string()),
            pa.field("source_path", pa.string()),
            pa.field("line_start", pa.int64()),
            pa.field("line_end", pa.int64()),
            pa.field("extractor_version", pa.string()),
            pa.field("scope", pa.string()),
            pa.field("search_text", pa.string()),
        ]
    )


def _entity_search_text(page: Any) -> str:
    """Normalized lexical surface for one Entity: title, aliases, types."""
    values = [page.title, *page.aliases, *page.entity_types]
    return "\n".join(value for value in values if value)


def _evidence_json(claim: Any) -> str:
    return msgspec.json.encode(
        [
            {"section": e.section, "line_start": e.line_start, "line_end": e.line_end}
            for e in claim.evidence
        ]
    ).decode()


def _literal_text(value: str | float | int | bool | None) -> str:
    return "" if value is None else str(value)


def _entity_rows(kb: Any) -> list[dict]:
    """One row per page-owning Entity plus one row per retired redirect."""
    rows: list[dict] = []
    for page in kb.pages:
        if not page.id:
            # Legacy Flat Mode page: no Entity contract to project.
            continue
        rows.append(
            {
                "entity_id": page.id,
                "title": page.title,
                "entity_types": list(page.entity_types),
                "aliases": list(page.aliases),
                "lifecycle": page.lifecycle or "",
                "redirect_to": "",
                "page_path": page.path,
                "search_text": _entity_search_text(page),
            }
        )
    ontology = kb.control.ontology if kb.control is not None else None
    for redirect in getattr(ontology, "redirects", None) or ():
        rows.append(
            {
                "entity_id": redirect.from_id,
                "title": "",
                "entity_types": [],
                "aliases": [],
                "lifecycle": "",
                "redirect_to": redirect.to_id,
                "page_path": "",
                "search_text": redirect.from_id,
            }
        )
    rows.sort(key=lambda row: row["entity_id"])
    return rows


def _graph_edge_rows(kb: Any) -> list[dict]:
    """One row per Claim plus one row per Extracted Reference, deterministic.

    Claim rows exist for EVERY published Claim (accepted, disputed,
    superseded; entity object or typed literal) so the table keeps
    non-traversal Claims inspectable. ``endpoint`` is non-empty only when the
    claim is an accepted-resolution entity edge — exactly the edges the
    in-memory ``GraphState`` carries (mirroring ``_KnowledgeIndex``).
    """
    index = kb._knowledge_index()
    rows: list[dict] = []
    for page in kb.pages:
        if not page.title:
            continue
        subject = page.id or page.title
        for claim in page.claims:
            endpoint = ""
            if claim.object is not None:
                target = index.by_entity_id.get(claim.object)
                if target is not None and target.title:
                    endpoint = target.id or target.title
            in_traversal = claim.status == CLAIM_STATUS_ACCEPTED and endpoint != ""
            rows.append(
                {
                    "edge_id": claim.id,
                    "kind": GRAPH_EDGE_ORIGIN_CLAIM,
                    "subject": subject,
                    "endpoint": endpoint,
                    "predicate": claim.predicate,
                    "status": claim.status,
                    "object_entity": claim.object or "",
                    "value": _literal_text(claim.value),
                    "value_type": claim.value_type or "",
                    "confidence": claim.confidence,
                    "valid_from": claim.valid_from or "",
                    "valid_to": claim.valid_to or "",
                    "claim_origin": claim.origin,
                    "evidence": _evidence_json(claim),
                    "source_path": page.path,
                    "line_start": 0,
                    "line_end": 0,
                    "extractor_version": "",
                    "scope": GRAPH_EDGE_SCOPE_CANONICAL if in_traversal else "",
                    "search_text": "\n".join(
                        value
                        for value in (
                            claim.predicate,
                            claim.object or "",
                            _literal_text(claim.value),
                            subject,
                            endpoint,
                        )
                        if value
                    ),
                }
            )
    # Extracted Reference rows: non-canonical discovery topology, marked
    # separately and never promoted to Claims (ADR-0011, ADR-0021). The
    # discovery adjacency holds exactly the reference edges that resolved, so
    # it — not the raw extraction list — is the authoritative source.
    for subject_key, edges in sorted(index.discovery_adjacency.items()):
        seen: dict[tuple[str, int, int, str], int] = {}
        for edge in edges:
            if edge.origin != GRAPH_EDGE_ORIGIN_EXTRACTED:
                continue
            identity = (
                edge.source_path,
                edge.line_start,
                edge.line_end,
                edge.endpoint,
            )
            occurrence = seen.get(identity, 0)
            seen[identity] = occurrence + 1
            rows.append(
                {
                    "edge_id": f"ref:{edge.source_path}:{edge.line_start}:"
                    f"{edge.line_end}:{edge.endpoint}:{occurrence}",
                    "kind": GRAPH_EDGE_ORIGIN_EXTRACTED,
                    "subject": subject_key,
                    "endpoint": edge.endpoint,
                    "predicate": "",
                    "status": "",
                    "object_entity": "",
                    "value": "",
                    "value_type": "",
                    "confidence": None,
                    "valid_from": "",
                    "valid_to": "",
                    "claim_origin": "",
                    "evidence": "[]",
                    "source_path": edge.source_path,
                    "line_start": edge.line_start,
                    "line_end": edge.line_end,
                    "extractor_version": edge.extractor_version,
                    "scope": GRAPH_EDGE_SCOPE_DISCOVERY,
                    "search_text": f"{subject_key}\n{edge.endpoint}",
                }
            )
    rows.sort(key=lambda row: (row["kind"], row["subject"], row["edge_id"]))
    return rows


def build_graph_tables(
    kb: Any,
    location: str | Path | IndexLocation,
    fingerprint: SourceFingerprint,
) -> None:
    """Build fresh ``entities`` and ``graph_edges`` tables beside the Evidence
    tables and record the source fingerprint beside them.

    Scalar indexes serve exact ID/status/kind lookups; ``LabelList`` indexes
    accelerate containment lookups over the ``aliases``/``entity_types`` list
    columns; FTS over ``search_text`` serves lexical candidate retrieval.
    Never queries LanceDB during traversal: consumers load adjacency once via
    :func:`load_graph_state`.
    """
    index = as_location(location)
    index.prepare()
    db = index.connect()

    entity_rows = _entity_rows(kb)
    entities = db.create_table(
        ENTITY_TABLE_NAME,
        data=entity_rows,
        schema=_entity_schema(),
        mode="overwrite",
    )
    if entity_rows:
        entities.create_index("entity_id")
        entities.create_index("aliases", config=LabelList())
        entities.create_index("entity_types", config=LabelList())
        entities.create_index("search_text", config=FTS())

    edge_rows = _graph_edge_rows(kb)
    edges = db.create_table(
        GRAPH_EDGE_TABLE_NAME,
        data=edge_rows,
        schema=_graph_edge_schema(),
        mode="overwrite",
    )
    if edge_rows:
        edges.create_index("status")
        edges.create_index("kind")
        edges.create_index("search_text", config=FTS())

    _save_fingerprint(index, fingerprint)


def has_graph_tables(
    index_dir: str | Path | IndexLocation,
    expected_fingerprint: SourceFingerprint | None = None,
) -> bool:
    """Health probe: both graph tables exist, and optionally match a fingerprint.

    Extends the local/remote :class:`~lumio_lancedb.location.IndexLocation`
    health contract for the graph projections (issue #171): verifies table
    presence through a real connection (unlike the sidecar-only
    ``has_index``), and — when ``expected_fingerprint`` is given — that the
    recorded index fingerprint matches, so a stale projection is reported
    unhealthy. Used to gate requested-table completeness after a build or
    publication.
    """
    index = as_location(index_dir)
    if index is None or not index.has_index():
        return False
    if expected_fingerprint is not None:
        stored = _load_fingerprint(index)
        if stored is None or stored.digest != expected_fingerprint.digest:
            return False
    db = index.connect()
    names = set(db.list_tables().tables)
    return ENTITY_TABLE_NAME in names and GRAPH_EDGE_TABLE_NAME in names


def _edge_from_row(row: dict) -> GraphEdge | None:
    """Rebuild one traversal edge from a row, or ``None``.

    ``None`` means the row never enters traversal (disputed/superseded/
    literal/unresolved Claims). A row that violates its kind's contract is
    corrupt: the caller treats that as an unusable index (returns ``None``
    from :func:`load_graph_state`), mirroring ``_decode_edge`` semantics.
    """
    kind = row["kind"]
    if kind not in GRAPH_EDGE_ORIGINS:
        raise _CorruptGraphTable(f"unknown edge kind {kind!r}")
    scope = row["scope"]
    if kind == GRAPH_EDGE_ORIGIN_CLAIM:
        if not row["edge_id"] or not row["predicate"]:
            raise _CorruptGraphTable("claim row missing edge_id/predicate")
        if scope not in ("", GRAPH_EDGE_SCOPE_CANONICAL):
            raise _CorruptGraphTable(f"claim row has invalid scope {scope!r}")
        if row["status"] != CLAIM_STATUS_ACCEPTED or not row["endpoint"]:
            return None
        return GraphEdge(
            endpoint=row["endpoint"],
            predicate=row["predicate"],
            claim_id=row["edge_id"],
            origin=GRAPH_EDGE_ORIGIN_CLAIM,
            scope=GRAPH_EDGE_SCOPE_CANONICAL,
        )
    # Extracted Reference row: never carries Claim identity; carries source
    # provenance with a bounded 1-based line range (ADR-0021).
    if row["predicate"] or row["status"]:
        raise _CorruptGraphTable("extracted-reference row carries Claim identity")
    if scope != GRAPH_EDGE_SCOPE_DISCOVERY:
        raise _CorruptGraphTable(f"reference row has invalid scope {scope!r}")
    if not row["source_path"] or not row["extractor_version"]:
        raise _CorruptGraphTable("reference row missing source provenance")
    line_start, line_end = row["line_start"], row["line_end"]
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        raise _CorruptGraphTable("reference row line anchors are not integers")
    if line_start < 1 or line_end < line_start:
        raise _CorruptGraphTable("reference row line anchors out of range")
    return GraphEdge(
        endpoint=row["endpoint"],
        predicate="",
        claim_id="",
        origin=GRAPH_EDGE_ORIGIN_EXTRACTED,
        source_path=row["source_path"],
        line_start=line_start,
        line_end=line_end,
        extractor_version=row["extractor_version"],
        scope=GRAPH_EDGE_SCOPE_DISCOVERY,
    )


class _CorruptGraphTable(Exception):
    """Internal marker: a graph-edge row violates its kind's contract."""


def load_graph_state(
    location: str | Path | IndexLocation,
    expected_fingerprint: SourceFingerprint,
) -> GraphState | None:
    """Load accepted adjacency from the graph tables into an in-memory state.

    Returns a :class:`~lumio_wiki.records.GraphState` behaviorally identical
    to the zero-index MessagePack graph for the same fingerprint (same
    version, extractor version, outgoing/incoming adjacency, edge count), or
    ``None`` when the index is missing, unhealthy, stale
    (fingerprint mismatch), or corrupt — the caller then falls back to the
    MessagePack artifact or deterministic in-memory derivation. Traversal
    never queries LanceDB: the whole adjacency is read once, here.
    """
    index = as_location(location)
    if index is None:
        return None

    # Every read/decode failure below — an unavailable store, a malformed
    # fingerprint sidecar, an unreadable table, a schema mismatch, a row
    # violating its kind contract — means the projection cannot serve graph
    # state: return None so the caller falls back (missing/unhealthy/stale/
    # corrupt all disclose the same way).
    try:
        if not index.has_index():
            return None
        stored = _load_fingerprint(index)
        if stored is None or stored.digest != expected_fingerprint.digest:
            return None
        db = index.connect()
        names = set(db.list_tables().tables)
        if ENTITY_TABLE_NAME not in names or GRAPH_EDGE_TABLE_NAME not in names:
            return None
        table = db.open_table(GRAPH_EDGE_TABLE_NAME)
        rows = table.to_arrow().to_pylist()

        outgoing: dict[str, list[GraphEdge]] = {}
        incoming: dict[str, list[GraphEdge]] = {}
        for row in rows:
            edge = _edge_from_row(row)
            if edge is None:
                continue
            subject = row["subject"]
            if not subject:
                raise _CorruptGraphTable("edge row missing subject")
            outgoing.setdefault(subject, []).append(edge)
            incoming.setdefault(edge.endpoint, []).append(edge.reversed(subject))
    except Exception:
        return None
    # Same deterministic bucket ordering the zero-index graph guarantees, so
    # traversal behavior is identical (ADR-0021 parity requirement).
    for bucket in (*outgoing.values(), *incoming.values()):
        bucket.sort(key=lambda edge: edge.sort_key)
    return GraphState(
        version=GRAPH_ARTIFACT_VERSION,
        fingerprint_digest=expected_fingerprint.digest,
        extractor_version=EXTRACTOR_VERSION,
        outgoing=outgoing,
        incoming=incoming,
        edge_count=sum(len(bucket) for bucket in outgoing.values()),
    )


def search_entity_candidates(
    location: str | Path | IndexLocation,
    query: str,
    *,
    limit: int = 5,
    expected_fingerprint: SourceFingerprint | None = None,
) -> list[EntityResolutionCandidate] | None:
    """Lexically search the ``entities`` projection for resolution candidates.

    Returns scored, review-only :class:`EntityResolutionCandidate` records
    (issue #172): advisory context for a Maintainer, never a merge, write, or
    publish. ``None`` means candidate search is unavailable — the index or
    ``entities`` table is missing, stale (fingerprint mismatch), or errored —
    so callers disclose the unavailability truthfully instead of implying an
    empty result set. Needs no embedder: lexical FTS over ``search_text``.
    """
    index = as_location(location)
    if index is None:
        return None
    try:
        if not index.has_index():
            return None
        if expected_fingerprint is not None:
            stored = _load_fingerprint(index)
            if stored is None or stored.digest != expected_fingerprint.digest:
                return None
        db = index.connect()
        if ENTITY_TABLE_NAME not in db.list_tables().tables:
            return None
        table = db.open_table(ENTITY_TABLE_NAME)
        if not query.strip() or limit <= 0:
            return []
        rows = (
            table.search(query, query_type="fts")
            .select(["entity_id", "title", "page_path", "_score"])
            # Retired redirect rows (redirect_to set, no page of their own)
            # are not resolution candidates: only page-owning Entities are.
            .where("redirect_to = ''")
            .limit(limit)
            .to_list()
        )
        return [
            EntityResolutionCandidate(
                entity=Entity(
                    id=row["entity_id"],
                    title=row["title"],
                    path=row["page_path"],
                ),
                score=round(float(row["_score"]), 4),
                reason="LanceDB FTS entity candidate (review only)",
            )
            for row in rows
            if row["entity_id"]
        ]
    except Exception:
        return None
