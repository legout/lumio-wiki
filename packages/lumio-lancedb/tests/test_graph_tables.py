"""Entity and Claim graph projections in LanceDB (issue #171, ADR-0021).

Proves the disposable-projection contract:

* ``build_graph_tables`` materializes ``entities`` and ``graph_edges`` beside
  the Evidence tables, fingerprint-bound, with every Claim inspectable by
  lifecycle and every Extracted Reference marked separately.
* ``load_graph_state`` rebuilds the SAME in-memory ``GraphState`` the
  zero-index MessagePack graph produces (behavioral parity, ADR-0021), and
  returns ``None`` — never raises — for missing, stale, or corrupt tables so
  callers fall back truthfully.
* Graph build/load needs no embedder; only semantic retrieval modes do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_lancedb import (
    ENTITY_TABLE_NAME,
    GRAPH_EDGE_TABLE_NAME,
    LanceDBRetrievalAdapter,
    build_graph_tables,
    build_lancedb_index,
    has_graph_tables,
    load_graph_state,
)
from lumio_lancedb.index import build_lexical_index
from lumio_wiki.embeddings import EmbeddingError
from lumio_wiki.graph_state import build_graph_state
from lumio_wiki.knowledge_base import EXTRACTOR_VERSION, fingerprint_sources, load_knowledge_base
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    CompiledPage,
    SourceFingerprint,
)

ROOT = Path(__file__).parents[3]

_CONTROL_V2 = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    software-system:
      description: "A software system."
    library: {}
  predicates:
    uses:
      subject_types:
        - software-system
      object_types:
        - library
        - software-system
    described-as:
      subject_types:
        - software-system
        - library
      literal_kind: string
  redirects:
    entity:old-lumio: entity:lumio
"""


def _entity_page(
    title: str,
    entity_id: str,
    entity_types: list[str],
    *,
    claims_yaml: str = "",
    body: str = "## Overview\n\nOverview paragraph.\n",
    aliases: list[str] | None = None,
    lifecycle: str = "approved",
) -> str:
    types_yaml = "".join(f"  - {t}\n" for t in entity_types)
    aliases_yaml = "".join(f'  - "{a}"\n' for a in (aliases or []))
    claims = f"claims:\n{claims_yaml}" if claims_yaml else ""
    return (
        "---\n"
        f'id: "{entity_id}"\n'
        f'title: "{title}"\n'
        f"entity_types:\n{types_yaml}"
        + (f"aliases:\n{aliases_yaml}" if aliases else "")
        + 'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        f'lifecycle: "{lifecycle}"\n'
        'visibility: "public"\n'
        f'sources:\n  - id: "src-{entity_id}"\n    title: "{title} Source"\n'
        f"{claims}"
        "---\n\n"
        f"# {title}\n\n{body}"
    )


def _claim_yaml(
    claim_id: str,
    predicate: str,
    *,
    status: str = CLAIM_STATUS_ACCEPTED,
    object: str | None = None,
    value_yaml: str = "",
    evidence_yaml: str | None = None,
) -> str:
    object_yaml = f'    object: "{object}"\n' if object is not None else ""
    if evidence_yaml is None:
        evidence_yaml = '      - section: "Overview"\n'
    return (
        f"  - id: {claim_id}\n"
        f"    predicate: {predicate}\n"
        f"{object_yaml}{value_yaml}"
        f"    status: {status}\n"
        f"    evidence:\n{evidence_yaml}"
    )


def _write_kb(root: Path, files: dict[str, str]):
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]
    return kb


@pytest.fixture()
def categorized_kb(tmp_path):
    """A categorized KB exercising every projection input.

    * two typed Entities, one with aliases;
    * an accepted entity Claim (canonical edge);
    * a disputed entity Claim (inspectable, never traversed);
    * an accepted literal Claim (never a graph edge);
    * one body link -> an Extracted Reference (discovery edge);
    * one ontology redirect (retired Entity ID row).
    """
    body = "## Overview\n\nSee [LanceDB](lancedb.md) for storage.\n"
    return _write_kb(
        tmp_path,
        {
            "lumio.yaml": _CONTROL_V2,
            "concepts/lumio.md": _entity_page(
                "Lumio",
                "entity:lumio",
                ["software-system"],
                aliases=["Lumio Platform"],
                claims_yaml=(
                    _claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb")
                    + _claim_yaml(
                        "claim:lumio-disputed", "uses", status="disputed", object="entity:lancedb"
                    )
                    + _claim_yaml(
                        "claim:lumio-described",
                        "described-as",
                        value_yaml='    value: "portable"\n    value_type: string\n',
                    )
                ),
                body=body,
            ),
            "concepts/lancedb.md": _entity_page("LanceDB", "entity:lancedb", ["library"]),
        },
    )


def _rows(tmp_path, table_name):
    import lancedb

    db = lancedb.connect(tmp_path / "lance")
    return db.open_table(table_name).to_arrow().to_pylist()


# ---------------------------------------------------------------------------
# Build: fresh-snapshot tables beside the Evidence tables.
# ---------------------------------------------------------------------------


def test_build_creates_matching_entity_and_edge_tables(categorized_kb, tmp_path):
    location = tmp_path / "lance"
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, location, fingerprint)

    assert has_graph_tables(location)

    entities = {row["entity_id"]: row for row in _rows(tmp_path, ENTITY_TABLE_NAME)}
    assert set(entities) == {"entity:lumio", "entity:lancedb", "entity:old-lumio"}
    lumio = entities["entity:lumio"]
    assert lumio["title"] == "Lumio"
    assert lumio["entity_types"] == ["software-system"]
    assert lumio["aliases"] == ["Lumio Platform"]
    assert lumio["lifecycle"] == "approved"
    assert lumio["redirect_to"] == ""
    assert lumio["page_path"] == "concepts/lumio.md"
    # Normalized search text covers title, aliases, and types.
    assert "Lumio Platform" in lumio["search_text"]
    assert "software-system" in lumio["search_text"]
    # The retired redirect Entity has no page and resolves to the survivor.
    redirect = entities["entity:old-lumio"]
    assert redirect["redirect_to"] == "entity:lumio"
    assert redirect["page_path"] == ""

    edges = _rows(tmp_path, GRAPH_EDGE_TABLE_NAME)
    by_id = {row["edge_id"]: row for row in edges}
    # Every Claim is a row, regardless of lifecycle; the Extracted Reference
    # is marked separately and never carries Claim identity.
    assert set(by_id) == {
        "claim:lumio-uses-lancedb",
        "claim:lumio-disputed",
        "claim:lumio-described",
    } | {row_id for row_id in by_id if row_id.startswith("ref:")}
    assert sum(1 for row in edges if row["kind"] == GRAPH_EDGE_ORIGIN_EXTRACTED) == 1

    accepted = by_id["claim:lumio-uses-lancedb"]
    assert accepted["kind"] == GRAPH_EDGE_ORIGIN_CLAIM
    assert accepted["subject"] == "entity:lumio"
    assert accepted["endpoint"] == "entity:lancedb"
    assert accepted["status"] == CLAIM_STATUS_ACCEPTED
    assert accepted["object_entity"] == "entity:lancedb"
    assert '"section"' in accepted["evidence"]  # evidence anchors serialized

    disputed = by_id["claim:lumio-disputed"]
    assert disputed["status"] == "disputed"
    assert disputed["scope"] == ""  # never a traversal edge

    literal = by_id["claim:lumio-described"]
    assert literal["value"] == "portable"
    assert literal["value_type"] == "string"
    assert literal["endpoint"] == ""  # literal Claims are not graph edges

    ref_row = next(row for row in edges if row["kind"] == GRAPH_EDGE_ORIGIN_EXTRACTED)
    assert ref_row["subject"] == "entity:lumio"
    assert ref_row["endpoint"] == "entity:lancedb"
    assert ref_row["source_path"] == "concepts/lumio.md"
    assert ref_row["line_start"] >= 1
    assert ref_row["extractor_version"]
    assert ref_row["scope"] == "discovery"


def test_build_is_deterministic_and_fingerprint_bound(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    first = _rows(tmp_path, GRAPH_EDGE_TABLE_NAME)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    second = _rows(tmp_path, GRAPH_EDGE_TABLE_NAME)
    assert first == second

    from lumio_lancedb.index import _load_fingerprint
    from lumio_lancedb.location import as_location

    assert _load_fingerprint(as_location(tmp_path / "lance")) == fingerprint


def test_entity_fts_candidate_retrieval(categorized_kb, tmp_path):
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint_sources(tmp_path))
    import lancedb

    table = lancedb.connect(tmp_path / "lance").open_table(ENTITY_TABLE_NAME)
    hits = table.search("platform", query_type="fts").select(["entity_id"]).limit(5).to_list()
    assert [row["entity_id"] for row in hits] == ["entity:lumio"]


def test_scalar_status_and_kind_filters(categorized_kb, tmp_path):
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint_sources(tmp_path))
    import lancedb

    table = lancedb.connect(tmp_path / "lance").open_table(GRAPH_EDGE_TABLE_NAME)
    refs = (
        table.search()
        .where(f"kind = '{GRAPH_EDGE_ORIGIN_EXTRACTED}'")
        .select(["edge_id"])
        .limit(10)
        .to_list()
    )
    assert len(refs) == 1
    accepted = (
        table.search()
        .where(f"status = '{CLAIM_STATUS_ACCEPTED}'")
        .select(["edge_id"])
        .limit(10)
        .to_list()
    )
    assert {row["edge_id"] for row in accepted} == {
        "claim:lumio-uses-lancedb",
        "claim:lumio-described",
    }


# ---------------------------------------------------------------------------
# Load: behavioral parity with the zero-index MessagePack graph.
# ---------------------------------------------------------------------------


def test_load_graph_state_matches_zero_index_graph(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)

    loaded = load_graph_state(tmp_path / "lance", fingerprint)
    zero_index = build_graph_state(
        categorized_kb._knowledge_index(), fingerprint, EXTRACTOR_VERSION
    )
    assert loaded is not None
    assert loaded == zero_index

    # Non-trivial content: the accepted Claim edge and the Extracted
    # Reference both appear, in both directions.
    assert loaded.edge_count == 2
    assert {e.endpoint for e in loaded.outgoing["entity:lumio"]} == {
        "entity:lancedb",
    }
    claim_edges = [
        e for e in loaded.outgoing["entity:lumio"] if e.origin == GRAPH_EDGE_ORIGIN_CLAIM
    ]
    assert len(claim_edges) == 1
    assert claim_edges[0].claim_id == "claim:lumio-uses-lancedb"
    assert claim_edges[0].scope == "canonical"


def test_disputed_claims_never_enter_loaded_adjacency(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    loaded = load_graph_state(tmp_path / "lance", fingerprint)
    assert loaded is not None
    outgoing_ids = {e.claim_id for buckets in loaded.outgoing.values() for e in buckets}
    assert "claim:lumio-disputed" not in outgoing_ids
    assert "claim:lumio-described" not in outgoing_ids


def test_legacy_flat_kb_projects_reference_edges_only(tmp_path):
    kb = _write_kb(
        tmp_path,
        {
            "alpha.md": (
                "---\n"
                'title: "Alpha"\n'
                'tags:\n  - "test"\n'
                'summary: "Alpha."\n'
                'lifecycle: "approved"\n'
                'visibility: "public"\n'
                'sources:\n  - id: "alpha-src"\n    title: "Alpha Source"\n'
                "---\n\n"
                "# Alpha\n\nSee [Beta](beta.md).\n"
            ),
            "beta.md": (
                "---\n"
                'title: "Beta"\n'
                'tags:\n  - "test"\n'
                'summary: "Beta."\n'
                'lifecycle: "approved"\n'
                'visibility: "public"\n'
                'sources:\n  - id: "beta-src"\n    title: "Beta Source"\n'
                "---\n\n"
                "# Beta\n\nBack to [Alpha](alpha.md).\n"
            ),
        },
    )
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(kb, tmp_path / "lance", fingerprint)

    assert _rows(tmp_path, ENTITY_TABLE_NAME) == []  # no Entity contracts
    loaded = load_graph_state(tmp_path / "lance", fingerprint)
    zero_index = build_graph_state(kb._knowledge_index(), fingerprint, EXTRACTOR_VERSION)
    assert loaded == zero_index
    assert loaded.edge_count == 2  # two Extracted References, title-keyed
    assert all(
        e.origin == GRAPH_EDGE_ORIGIN_EXTRACTED
        for buckets in loaded.outgoing.values()
        for e in buckets
    )


# ---------------------------------------------------------------------------
# Fallback: missing, stale, or corrupt tables never raise.
# ---------------------------------------------------------------------------


def test_missing_graph_tables_return_none(categorized_kb, tmp_path):
    # An evidence-only index (no graph tables built) is not a graph source.
    build_lexical_index(list(categorized_kb.pages), tmp_path / "lance")
    assert load_graph_state(tmp_path / "lance", fingerprint_sources(tmp_path)) is None


def test_absent_index_returns_none(tmp_path):
    assert load_graph_state(tmp_path / "nowhere", SourceFingerprint(digest="0" * 64)) is None


def test_stale_fingerprint_returns_none(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    assert load_graph_state(tmp_path / "lance", SourceFingerprint(digest="f" * 64)) is None


def test_corrupt_edge_rows_return_none(categorized_kb, tmp_path):
    import lancedb

    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)

    # Tamper: a claim row without a predicate violates its kind contract.
    db = lancedb.connect(tmp_path / "lance")
    table = db.open_table(GRAPH_EDGE_TABLE_NAME)
    rows = table.to_arrow().to_pylist()
    for row in rows:
        if row["edge_id"] == "claim:lumio-uses-lancedb":
            row["predicate"] = ""
    db.create_table(GRAPH_EDGE_TABLE_NAME, data=rows, schema=table.schema, mode="overwrite")
    assert load_graph_state(tmp_path / "lance", fingerprint) is None


def test_corrupt_reference_provenance_returns_none(categorized_kb, tmp_path):
    import lancedb

    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)

    db = lancedb.connect(tmp_path / "lance")
    table = db.open_table(GRAPH_EDGE_TABLE_NAME)
    rows = table.to_arrow().to_pylist()
    for row in rows:
        if row["kind"] == GRAPH_EDGE_ORIGIN_EXTRACTED:
            row["line_start"] = 0  # invalid: references anchor at line >= 1
    db.create_table(GRAPH_EDGE_TABLE_NAME, data=rows, schema=table.schema, mode="overwrite")
    assert load_graph_state(tmp_path / "lance", fingerprint) is None


# ---------------------------------------------------------------------------
# Embedder boundaries and the fresh local build path.
# ---------------------------------------------------------------------------


def test_graph_build_and_load_need_no_embedder(categorized_kb, tmp_path):
    # Lexical/scalar graph use is model-free end to end (AC #171).
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    assert load_graph_state(tmp_path / "lance", fingerprint) is not None

    # Semantic retrieval still requires an embedder.
    adapter = LanceDBRetrievalAdapter()
    with pytest.raises(EmbeddingError):
        adapter.retrieve(
            list(categorized_kb.pages),
            "Lumio",
            mode="semantic",
            index_dir=tmp_path / "lance",
            embedder=None,
        )


def test_build_lancedb_index_builds_matching_graph_and_evidence_tables(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_lancedb_index(categorized_kb, tmp_path / "lance")

    import lancedb

    names = set(lancedb.connect(tmp_path / "lance").list_tables().tables)
    assert {"evidence", "pages", ENTITY_TABLE_NAME, GRAPH_EDGE_TABLE_NAME} <= names
    assert has_graph_tables(tmp_path / "lance")
    # Matching fingerprint: the graph projection serves the same KB snapshot
    # the Evidence tables were built from.
    assert load_graph_state(tmp_path / "lance", fingerprint) is not None


def test_build_over_stale_index_replaces_graph_tables(categorized_kb, tmp_path):
    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", SourceFingerprint(digest="0" * 64))
    assert load_graph_state(tmp_path / "lance", fingerprint) is None  # stale
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)
    assert load_graph_state(tmp_path / "lance", fingerprint) is not None


def test_projection_of_synthetic_and_unloaded_pages_is_truthful(categorized_kb, tmp_path):
    # A page list with no Knowledge Base (direct adapter use) never reaches
    # build_graph_tables; guard the seam by asserting it accepts the KB only.
    with pytest.raises(AttributeError):
        build_graph_tables(
            [CompiledPage(path="x.md", title="X")],
            tmp_path / "lance",
            SourceFingerprint(digest="0" * 64),
        )


def test_schema_mismatched_tables_return_none(categorized_kb, tmp_path):
    """A graph_edges table missing required columns is corrupt, not fatal."""
    import lancedb
    import pyarrow as pa

    fingerprint = fingerprint_sources(tmp_path)
    build_graph_tables(categorized_kb, tmp_path / "lance", fingerprint)

    db = lancedb.connect(tmp_path / "lance")
    db.create_table(
        GRAPH_EDGE_TABLE_NAME,
        data=[{"edge_id": "claim:x"}],
        schema=pa.schema([pa.field("edge_id", pa.string())]),
        mode="overwrite",
    )
    assert load_graph_state(tmp_path / "lance", fingerprint) is None
