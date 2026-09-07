"""Tests for the materialized Discovery Graph artifact (issue #108, ADR-0011).

The Discovery Graph adjacency (canonical Relationships plus Extracted
References, in both outgoing and incoming directions) is materialized as a
versioned MessagePack artifact in the configured derived index directory. The
artifact carries the Knowledge Base fingerprint and extractor version; a
missing, stale, corrupt, partial, or incompatible artifact is ignored and the
graph is rebuilt deterministically in memory. Replacement is atomic, results
are byte-identical on rebuild, and the artifact is never a Compiled Page,
Reserved Artifact, fingerprint input, or OKF export entry.

All tests are zero-index: no LanceDB, PyArrow, or operational database.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import msgpack
import pytest
from lumio_wiki.graph_state import (
    GRAPH_ARTIFACT_FILENAME,
    GRAPH_ARTIFACT_VERSION,
    deserialize_graph,
    serialize_graph,
)
from lumio_wiki.knowledge_base import (
    EXTRACTOR_VERSION,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
    fingerprint_sources,
    load_knowledge_base,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    GRAPH_EDGE_ORIGIN_CLAIM,
    GRAPH_EDGE_ORIGIN_EXTRACTED,
    Claim,
    CompiledPage,
    GraphEdge,
    GraphHealthReport,
    Relationship,
    Source,
)

# ---------------------------------------------------------------------------
# Page + Knowledge Base builders.
# ---------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _claims_from(source_title: str, relationships: list[Relationship] | None) -> list[Claim]:
    """Convert title-level test edges to accepted entity-to-entity Claims."""
    return [
        Claim(
            id=f"claim:{_slug(source_title)}-{_slug(rel.target)}-{n}",
            predicate=rel.type,
            object=f"entity:{_slug(rel.target)}",
            status=CLAIM_STATUS_ACCEPTED,
        )
        for n, rel in enumerate(relationships or [])
    ]


def _page(
    title: str,
    body: str = "",
    *,
    path: str | None = None,
    aliases: list[str] | None = None,
    relationships: list[Relationship] | None = None,
    visibility: str = "public",
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower()}.md",
        title=title,
        id=f"entity:{_slug(title)}",
        entity_types=["concept"],
        aliases=aliases or [],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility=visibility,
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        claims=_claims_from(title, relationships),
        body=body,
    )


def _kb(root: Path, pages: list[CompiledPage]) -> KnowledgeBase:
    return KnowledgeBase(root=root, pages=pages)


def _linked_kb(root: Path) -> KnowledgeBase:
    """A small Discovery Graph with both canonical and extracted edges.

    Alpha --uses--> Beta            (canonical Relationship, outgoing)
    Alpha -link-> Gamma             (extracted reference, outgoing)
    Gamma  -link-> Beta             (extracted reference, outgoing)
    """
    alpha = _page(
        "Alpha",
        body="See [g](gamma.md).\n",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    beta = _page("Beta")
    gamma = _page("Gamma", body="Back to [b](beta.md).\n")
    return _kb(root, [alpha, beta, gamma])


# ---------------------------------------------------------------------------
# 1. The index directory can materialize a versioned MessagePack Discovery
#    Graph containing incoming and outgoing adjacency.
# ---------------------------------------------------------------------------


def test_artifact_contains_incoming_and_outgoing_adjacency(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"

    kb.materialize_graph(index_dir)
    state = deserialize_graph((index_dir / GRAPH_ARTIFACT_FILENAME).read_bytes())

    assert state is not None
    # Outgoing over Entity IDs (issue #170): entity:alpha -> entity:beta
    # (accepted Claim uses), entity:alpha -> entity:gamma (extracted),
    # entity:gamma -> entity:beta (extracted).
    [beta_edge, gamma_edge] = state.outgoing["entity:alpha"]
    assert beta_edge == GraphEdge(
        endpoint="entity:beta",
        predicate="uses",
        claim_id="claim:alpha-beta-0",
        origin=GRAPH_EDGE_ORIGIN_CLAIM,
    )
    assert gamma_edge.origin == GRAPH_EDGE_ORIGIN_EXTRACTED
    assert gamma_edge.endpoint == "entity:gamma"
    assert gamma_edge.predicate == "" and gamma_edge.claim_id == ""
    # Extracted provenance: source path, 1-based line range, extractor version.
    assert gamma_edge.source_path == "alpha.md"
    assert gamma_edge.line_start >= 1 and gamma_edge.line_end >= gamma_edge.line_start
    assert gamma_edge.extractor_version == EXTRACTOR_VERSION
    [gamma_out] = state.outgoing["entity:gamma"]
    assert gamma_out.endpoint == "entity:beta"
    assert gamma_out.origin == GRAPH_EDGE_ORIGIN_EXTRACTED
    # Incoming mirrors: entity:beta is reached from alpha (uses) and gamma
    # (extracted); entity:gamma is reached from alpha (extracted).
    assert [e.endpoint for e in state.incoming["entity:beta"]] == [
        "entity:alpha",
        "entity:gamma",
    ]
    assert state.incoming["entity:beta"][0].predicate == "uses"
    assert [e.endpoint for e in state.incoming["entity:gamma"]] == ["entity:alpha"]


# ---------------------------------------------------------------------------
# 2. The artifact records the fingerprint and extractor version and is accepted
#    only when both match the loaded Knowledge Base behavior.
# ---------------------------------------------------------------------------


def test_load_rejects_stale_fingerprint(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)

    # Mutate canonical source so the fingerprint changes.
    (root / "delta.md").write_text("# Delta\n", encoding="utf-8")
    stale_kb = _linked_kb(root)  # same pages, but root fingerprint changed

    state = stale_kb.load_or_derive_graph(index_dir)

    # Stale artifact ignored: the returned state is a fresh in-memory derivation
    # carrying the NEW fingerprint, not the persisted one.
    assert state.fingerprint_digest == fingerprint_sources(root).digest
    assert (
        state.fingerprint_digest
        != deserialize_graph((index_dir / GRAPH_ARTIFACT_FILENAME).read_bytes()).fingerprint_digest
    )


def test_load_rejects_wrong_extractor_version(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)

    # Rewrite the artifact with a mismatched extractor version.
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    payload = msgpack.unpackb(artifact.read_bytes(), raw=False, strict_map_key=False)
    payload["extractor_version"] = "999"
    artifact.write_bytes(msgpack.packb(payload, use_bin_type=True))

    state = kb.load_or_derive_graph(index_dir)
    # Mismatched extractor version -> ignored, fresh derivation takes over.
    assert state.extractor_version == EXTRACTOR_VERSION


def test_deserialize_rejects_wrong_graph_version(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index = kb._knowledge_index()
    fingerprint = fingerprint_sources(root)

    # Hand-craft a payload claiming a future incompatible version.
    payload = {
        "version": GRAPH_ARTIFACT_VERSION + 1,
        "fingerprint_digest": fingerprint.digest,
        "extractor_version": EXTRACTOR_VERSION,
        "outgoing": {},
        "incoming": {},
        "edge_count": 0,
    }
    data = msgpack.packb(payload, use_bin_type=True)

    assert deserialize_graph(data) is None
    assert serialize_graph(index, fingerprint, EXTRACTOR_VERSION) is not None


# ---------------------------------------------------------------------------
# 3. A missing artifact falls back to deterministic in-memory derivation;
#    stale, corrupt, partial, or incompatible artifacts are ignored and rebuilt.
# ---------------------------------------------------------------------------


def test_missing_artifact_falls_back_to_in_memory_derivation(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"  # does not exist yet

    state = kb.load_or_derive_graph(index_dir)

    index = kb._knowledge_index()
    assert state.outgoing == index.discovery_adjacency
    assert state.incoming == index.discovery_incoming
    assert state.edge_count == sum(len(e) for e in index.discovery_adjacency.values())


def test_corrupt_artifact_ignored_and_rebuilt(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(b"\x00\x01\x02 not msgpack \xff\xfe")

    # Direct deserialize returns None for corrupt bytes.
    assert deserialize_graph(b"garbage") is None

    # load_or_derive falls back to in-memory derivation.
    state = kb.load_or_derive_graph(index_dir)
    index = kb._knowledge_index()
    assert state.outgoing == index.discovery_adjacency


def test_partial_artifact_missing_keys_ignored(tmp_path):
    # A payload missing required keys is "partial" and must be ignored.
    partial = msgpack.packb({"version": GRAPH_ARTIFACT_VERSION}, use_bin_type=True)
    assert deserialize_graph(partial) is None

    missing_adjacency = msgpack.packb(
        {
            "version": GRAPH_ARTIFACT_VERSION,
            "fingerprint_digest": "abc",
            "extractor_version": EXTRACTOR_VERSION,
        },
        use_bin_type=True,
    )
    assert deserialize_graph(missing_adjacency) is None


def test_partial_artifact_malformed_adjacency_ignored():
    bad_edges = msgpack.packb(
        {
            "version": GRAPH_ARTIFACT_VERSION,
            "fingerprint_digest": "abc",
            "extractor_version": EXTRACTOR_VERSION,
            "outgoing": {"Alpha": [["Beta"]]},  # edge with wrong arity
            "incoming": {},
            "edge_count": 1,
        },
        use_bin_type=True,
    )
    assert deserialize_graph(bad_edges) is None


def test_inconsistent_edge_count_rejected():
    # Decodes cleanly but edge_count disagrees with the adjacency -> corrupt.
    bad = msgpack.packb(
        {
            "version": GRAPH_ARTIFACT_VERSION,
            "fingerprint_digest": "abc",
            "extractor_version": EXTRACTOR_VERSION,
            "outgoing": {"Alpha": [["Beta", ""]]},
            "incoming": {"Beta": [["Alpha", ""]]},
            "edge_count": 999,  # wrong: adjacency has 1 edge
        },
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def test_inconsistent_incoming_not_reverse_of_outgoing_rejected():
    # incoming does not mirror outgoing -> partially written / corrupt.
    bad = msgpack.packb(
        {
            "version": GRAPH_ARTIFACT_VERSION,
            "fingerprint_digest": "abc",
            "extractor_version": EXTRACTOR_VERSION,
            "outgoing": {"Alpha": [["Beta", ""]]},
            "incoming": {},  # missing the reverse edge
            "edge_count": 1,
        },
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def _v2_edge(
    endpoint: str = "entity:beta",
    predicate: str = "uses",
    claim_id: str = "claim:1",
    origin: str = "claim",
    source_path: str = "",
    line_start: int = 0,
    line_end: int = 0,
    extractor_version: str = "",
    scope: str = "canonical",
) -> list[object]:
    return [
        endpoint,
        predicate,
        claim_id,
        origin,
        source_path,
        line_start,
        line_end,
        extractor_version,
        scope,
    ]


def _v2_payload(outgoing_edge: list[object]) -> dict[str, object]:
    return {
        "version": GRAPH_ARTIFACT_VERSION,
        "fingerprint_digest": "abc",
        "extractor_version": EXTRACTOR_VERSION,
        "outgoing": {"entity:alpha": [outgoing_edge]},
        "incoming": {
            "entity:beta": [
                _v2_edge(
                    endpoint="entity:alpha",
                    predicate=str(outgoing_edge[1]),
                    claim_id=str(outgoing_edge[2]),
                    origin=str(outgoing_edge[3]),
                    source_path=str(outgoing_edge[4]),
                    line_start=outgoing_edge[5],  # type: ignore[arg-type]
                    line_end=outgoing_edge[6],  # type: ignore[arg-type]
                    extractor_version=str(outgoing_edge[7]),
                    scope=str(outgoing_edge[8]),
                )
            ]
        },
        "edge_count": 1,
    }


def test_unknown_edge_origin_rejected():
    # A corrupt artifact whose edges decode type-wise but carry an origin
    # outside GRAPH_EDGE_ORIGINS must not become live graph state (#170).
    bad = msgpack.packb(_v2_payload(_v2_edge(origin="banana")), use_bin_type=True)
    assert deserialize_graph(bad) is None


def test_unknown_edge_scope_rejected():
    bad = msgpack.packb(_v2_payload(_v2_edge(scope="banana")), use_bin_type=True)
    assert deserialize_graph(bad) is None


def test_claim_edge_with_discovery_scope_rejected():
    # An accepted-Claim edge belongs to the canonical scope; a mismatched
    # scope/origin pair is corrupt data.
    bad = msgpack.packb(_v2_payload(_v2_edge(scope="discovery")), use_bin_type=True)
    assert deserialize_graph(bad) is None


def test_extracted_edge_with_canonical_scope_rejected():
    # An Extracted Reference exists only in discovery scope.
    bad = msgpack.packb(
        _v2_payload(
            _v2_edge(
                predicate="",
                claim_id="",
                origin="extracted-reference",
                source_path="alpha.md",
                line_start=1,
                line_end=1,
                extractor_version=EXTRACTOR_VERSION,
                scope="canonical",
            )
        ),
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def test_claim_edge_without_claim_identity_rejected():
    bad = msgpack.packb(_v2_payload(_v2_edge(claim_id="", predicate="")), use_bin_type=True)
    assert deserialize_graph(bad) is None


def test_extracted_edge_with_claim_identity_rejected():
    # An Extracted Reference never carries a Claim ID (never promoted).
    bad = msgpack.packb(
        _v2_payload(
            _v2_edge(
                predicate="uses",
                claim_id="claim:1",
                origin="extracted-reference",
                source_path="alpha.md",
                line_start=1,
                line_end=1,
                extractor_version=EXTRACTOR_VERSION,
                scope="discovery",
            )
        ),
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def test_extracted_edge_without_provenance_rejected():
    # An Extracted Reference must carry source path, extractor version, and a
    # 1-based bounded line range (here: empty path and inverted range).
    bad = msgpack.packb(
        _v2_payload(
            _v2_edge(
                predicate="",
                claim_id="",
                origin="extracted-reference",
                source_path="",
                line_start=2,
                line_end=1,
                extractor_version=EXTRACTOR_VERSION,
                scope="discovery",
            )
        ),
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def test_stray_incoming_entry_rejected():
    # outgoing is empty but incoming has a stray entry -> inconsistent.
    bad = msgpack.packb(
        {
            "version": GRAPH_ARTIFACT_VERSION,
            "fingerprint_digest": "abc",
            "extractor_version": EXTRACTOR_VERSION,
            "outgoing": {},
            "incoming": {"Beta": [["Alpha", ""]]},
            "edge_count": 0,
        },
        use_bin_type=True,
    )
    assert deserialize_graph(bad) is None


def test_non_dict_payload_ignored():
    assert deserialize_graph(msgpack.packb([1, 2, 3], use_bin_type=True)) is None
    assert deserialize_graph(msgpack.packb("a string", use_bin_type=True)) is None


def test_incompatible_version_artifact_ignored_on_load(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(
        msgpack.packb(
            {
                "version": GRAPH_ARTIFACT_VERSION + 7,
                "fingerprint_digest": fingerprint_sources(root).digest,
                "extractor_version": EXTRACTOR_VERSION,
                "outgoing": {},
                "incoming": {},
                "edge_count": 0,
            },
            use_bin_type=True,
        )
    )

    state = kb.load_or_derive_graph(index_dir)
    # Incompatible persisted version ignored -> fresh derivation with real edges.
    assert state.edge_count == 3


# ---------------------------------------------------------------------------
# 4. Graph replacement is atomic: an interrupted build cannot replace a healthy
#    artifact with partial state.
# ---------------------------------------------------------------------------


def test_materialize_leaves_no_temp_files(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"

    kb.materialize_graph(index_dir)

    leftovers = [p.name for p in index_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    # Only the canonical artifact (plus nothing else) is present.
    assert sorted(p.name for p in index_dir.iterdir()) == [GRAPH_ARTIFACT_FILENAME]


def test_rematerialize_leaves_single_valid_artifact(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"

    first = kb.materialize_graph(index_dir)
    first_bytes = first.read_bytes()
    # Second materialization replaces, does not duplicate.
    second = kb.materialize_graph(index_dir)

    assert first == second
    artifacts = [p for p in index_dir.iterdir() if p.suffix == ".msgpack"]
    assert len(artifacts) == 1
    # And the artifact is still valid after replacement.
    assert deserialize_graph(second.read_bytes()) is not None
    assert second.read_bytes() == first_bytes


def test_atomic_write_does_not_clobber_on_simulated_failure(tmp_path, monkeypatch):
    # If the write fails mid-flight, a previously-healthy artifact must survive.
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    healthy_bytes = artifact.read_bytes()

    real_replace = __import__("os").replace

    def failing_replace(src, dst, *args, **kwargs):
        if str(dst).endswith(GRAPH_ARTIFACT_FILENAME):
            raise OSError("simulated interrupt")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("lumio_wiki.graph_state.os.replace", failing_replace)
    with pytest.raises(OSError):
        kb.materialize_graph(index_dir)

    # Healthy artifact untouched; no partial replacement.
    assert artifact.read_bytes() == healthy_bytes
    # Temp file cleaned up.
    leftovers = [p.name for p in index_dir.iterdir() if ".tmp" in p.name or "lumio-graph" in p.name]
    assert leftovers == [], f"temp leftovers: {leftovers}"


# ---------------------------------------------------------------------------
# 5. Deleting the artifact and rebuilding from unchanged Markdown reproduces
#    the same public graph results and deterministic ordering.
# ---------------------------------------------------------------------------


def test_delete_and_rebuild_reproduces_byte_identical_artifact(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"

    kb.materialize_graph(index_dir)
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    original_bytes = artifact.read_bytes()

    # Delete and rebuild from unchanged Markdown.
    artifact.unlink()
    assert not artifact.exists()
    kb.materialize_graph(index_dir)

    assert artifact.read_bytes() == original_bytes


def test_rebuilt_graph_reproduces_public_traversal_results(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"

    baseline_related = kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY)
    baseline_path = kb.shortest_path("Alpha", "Beta", scope=GRAPH_SCOPE_DISCOVERY)

    kb.materialize_graph(index_dir)
    # Materialization must not perturb public traversal behavior.
    assert kb.related_pages("Alpha", scope=GRAPH_SCOPE_DISCOVERY) == baseline_related
    assert kb.shortest_path("Alpha", "Beta", scope=GRAPH_SCOPE_DISCOVERY) == baseline_path

    # Load from artifact and rebuild a Knowledge Base whose in-memory graph is
    # identical to the persisted adjacency (adjacency equality => identical
    # deterministic public results).
    state = kb.load_or_derive_graph(index_dir)
    assert state.outgoing == kb._knowledge_index().discovery_adjacency


# ---------------------------------------------------------------------------
# 6. The artifact lives outside the Knowledge Base tree and is excluded from
#    Compiled Page loading, publication, OKF export, Reserved Artifacts, and
#    canonical fingerprint contents.
# ---------------------------------------------------------------------------


def _linked_disk_kb(root: Path) -> KnowledgeBase:
    """Write the linked graph to disk and load+validate a real Knowledge Base.

    Since ADR-0021 the canonical Alpha --uses--> Beta edge is an accepted,
    evidence-bearing Claim validated against a version-2 Control File
    ontology; the body links remain Extracted References.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "lumio.yaml").write_text(
        "version: 2\n"
        'mode: "categorized"\n'
        "categories:\n"
        "  - name: concepts\n"
        "ontology:\n"
        "  entity_types:\n"
        "    concept: {}\n"
        "  predicates:\n"
        "    uses:\n"
        "      subject_types:\n"
        "        - concept\n"
        "      object_types:\n"
        "        - concept\n",
        encoding="utf-8",
    )
    (root / "alpha.md").write_text(
        "---\n"
        'id: "entity:alpha"\n'
        'title: "Alpha"\n'
        "entity_types:\n"
        "  - concept\n"
        'tags: ["test"]\n'
        'summary: "Alpha summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src-alpha"\n'
        "claims:\n"
        "  - id: claim:alpha-beta\n"
        "    predicate: uses\n"
        '    object: "entity:beta"\n'
        "    status: accepted\n"
        "    evidence:\n"
        '      - section: "Alpha"\n'
        "---\n"
        "# Alpha\n\nSee [g](gamma.md).\n",
        encoding="utf-8",
    )
    (root / "beta.md").write_text(
        "---\n"
        'id: "entity:beta"\n'
        'title: "Beta"\n'
        "entity_types:\n"
        "  - concept\n"
        'tags: ["test"]\n'
        'summary: "Beta summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src-beta"\n'
        "---\n"
        "# Beta\n",
        encoding="utf-8",
    )
    (root / "gamma.md").write_text(
        "---\n"
        'id: "entity:gamma"\n'
        'title: "Gamma"\n'
        "entity_types:\n"
        "  - concept\n"
        'tags: ["test"]\n'
        'summary: "Gamma summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src-gamma"\n'
        "---\n"
        "# Gamma\n\nBack to [b](beta.md).\n",
        encoding="utf-8",
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [issue.message for issue in report.issues]
    return kb


def test_msgpack_artifact_not_loaded_as_compiled_page(tmp_path):
    root = tmp_path / "kb"
    kb = _linked_disk_kb(root)
    baseline_count = len(kb.pages)
    # Materialize into a derived dir INSIDE the KB root to prove exclusion.
    index_dir = root / "derived"
    kb.materialize_graph(index_dir)
    assert (index_dir / GRAPH_ARTIFACT_FILENAME).is_file()

    reloaded, report = load_knowledge_base(root)
    assert report.is_valid
    assert len(reloaded.pages) == baseline_count
    assert all(not page.path.endswith(".msgpack") for page in reloaded.pages)


def test_msgpack_artifact_not_in_canonical_fingerprint(tmp_path):
    root = tmp_path / "kb"
    kb = _linked_disk_kb(root)
    index_dir = root / "derived"
    before = fingerprint_sources(root).digest

    kb.materialize_graph(index_dir)

    after = fingerprint_sources(root).digest
    assert before == after


def test_msgpack_artifact_not_a_reserved_artifact_collision(tmp_path):
    root = tmp_path / "kb"
    kb = _linked_disk_kb(root)
    index_dir = root / "derived"
    kb.materialize_graph(index_dir)

    _, report = load_knowledge_base(root)
    # No validation issue references the .msgpack artifact.
    assert not any(".msgpack" in issue.file for issue in report.issues)
    assert report.is_valid


def test_msgpack_artifact_excluded_from_okf_export(tmp_path):
    from lumio_wiki.knowledge_base import export_bundle

    root = tmp_path / "kb"
    kb = _linked_disk_kb(root)
    index_dir = root / "derived"
    kb.materialize_graph(index_dir)

    bundle = export_bundle(kb)
    assert GRAPH_ARTIFACT_FILENAME not in bundle
    assert ".msgpack" not in bundle


def test_msgpack_artifact_not_published_or_disturbed(tmp_path):
    from lumio_wiki.knowledge_base import regenerate_reserved_artifacts

    root = tmp_path / "kb"
    kb = _linked_disk_kb(root)
    index_dir = root / "derived"
    kb.materialize_graph(index_dir)
    artifact = index_dir / GRAPH_ARTIFACT_FILENAME
    artifact_bytes = artifact.read_bytes()

    written = regenerate_reserved_artifacts(root)

    # Publication regenerates index.md/hot.md, never the .msgpack artifact.
    assert artifact.read_bytes() == artifact_bytes
    assert not any(p.suffix == ".msgpack" for p in written)
    # And the canonical fingerprint is still stable post-publish.
    assert fingerprint_sources(root).digest == fingerprint_sources(root).digest


# ---------------------------------------------------------------------------
# 7. Public index health reports graph freshness plus observable startup time,
#    edge count, materialized size, and traversal latency without exposing
#    MessagePack layout.
# ---------------------------------------------------------------------------


def test_graph_health_when_fresh_and_materialized(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)

    report = kb.graph_health(index_dir)

    assert isinstance(report, GraphHealthReport)
    assert report.graph_fresh is True
    assert report.materialized is True
    assert report.edge_count == 3
    assert report.materialized_size_bytes is not None
    assert report.materialized_size_bytes > 0
    assert report.startup_ms >= 0
    assert report.traversal_latency_ms is not None
    assert report.traversal_latency_ms >= 0
    assert report.fingerprint_digest == fingerprint_sources(root).digest


def test_graph_health_when_no_artifact(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"  # absent

    report = kb.graph_health(index_dir)

    assert report.graph_fresh is False
    assert report.materialized is False
    assert report.materialized_size_bytes is None
    # Edge count is still derivable in memory.
    assert report.edge_count == 3
    assert report.fingerprint_digest == fingerprint_sources(root).digest


def test_graph_health_when_stale(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)

    # Change canonical content -> artifact fingerprint no longer matches.
    (root / "delta.md").write_text("# Delta\n", encoding="utf-8")

    report = kb.graph_health(index_dir)
    assert report.graph_fresh is False
    assert report.materialized is True  # file is still there
    assert report.materialized_size_bytes is not None


def test_graph_health_when_corrupt(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(b"\x00corrupt\xff")

    report = kb.graph_health(index_dir)
    assert report.graph_fresh is False
    assert report.materialized is True
    # Falls back to derivation for the edge count.
    assert report.edge_count == 3


def test_graph_health_does_not_expose_msgpack_layout(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _linked_kb(root)
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)

    report = kb.graph_health(index_dir)

    # Only aggregate signals are exposed: no raw adjacency, no msgpack bytes,
    # no internal key names.
    exposed = set(report.__struct_fields__)
    assert exposed == {
        "graph_fresh",
        "materialized",
        "edge_count",
        "materialized_size_bytes",
        "startup_ms",
        "traversal_latency_ms",
        "fingerprint_digest",
    }


# ---------------------------------------------------------------------------
# 8. The feature works in an isolated lumio-wiki installation without LanceDB,
#    PyArrow, or an operational database.
# ---------------------------------------------------------------------------


def test_graph_materialization_does_not_require_lancedb_or_pyarrow(tmp_path):
    # A fresh subprocess importing ONLY lumio_wiki must build/load/health the
    # graph without pulling in lancedb or pyarrow.
    root = tmp_path / "kb"
    root.mkdir()
    index_dir = tmp_path / "idx"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from lumio_wiki import KnowledgeBase\n"
        "from lumio_wiki.records import CompiledPage, Source\n"
        f"root = Path({str(root)!r})\n"
        f"index_dir = Path({str(index_dir)!r})\n"
        "kb = KnowledgeBase(root=root, pages=[\n"
        "    CompiledPage(path='a.md', title='A', body='[b](b.md)\\n',\n"
        "                  sources=[Source(id='a')], tags=['t'], summary='a'),\n"
        "    CompiledPage(path='b.md', title='B',\n"
        "                  sources=[Source(id='b')], tags=['t'], summary='b'),\n"
        "])\n"
        "kb.materialize_graph(index_dir)\n"
        "kb.load_or_derive_graph(index_dir)\n"
        "kb.graph_health(index_dir)\n"
        "assert 'pyarrow' not in sys.modules, sys.modules.get('pyarrow')\n"
        "assert 'lancedb' not in sys.modules\n"
        "assert 'lumio_lancedb' not in sys.modules\n"
        "print('ISOLATION_OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ISOLATION_OK" in result.stdout


# ---------------------------------------------------------------------------
# 9. Determinism: stable timing-independent identity, and adjacency round-trip
#    preserves edge types in both directions.
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_canonical_edge_types(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    kb = _kb(
        root,
        [
            _page(
                "Alpha",
                relationships=[
                    Relationship(target="Beta", type="uses"),
                    Relationship(target="Gamma", type="implements"),
                ],
            ),
            _page("Beta"),
            _page("Gamma"),
        ],
    )
    index_dir = tmp_path / "idx"
    kb.materialize_graph(index_dir)
    state = deserialize_graph((index_dir / GRAPH_ARTIFACT_FILENAME).read_bytes())

    assert state is not None
    # Round-trip preserves Entity IDs, Claim IDs, Predicates, direction, and
    # edge origin (issue #170 AC6).
    assert state.outgoing["entity:alpha"] == [
        GraphEdge(
            endpoint="entity:beta",
            predicate="uses",
            claim_id="claim:alpha-beta-0",
            origin=GRAPH_EDGE_ORIGIN_CLAIM,
        ),
        GraphEdge(
            endpoint="entity:gamma",
            predicate="implements",
            claim_id="claim:alpha-gamma-1",
            origin=GRAPH_EDGE_ORIGIN_CLAIM,
        ),
    ]
    # Incoming carries the same typed edges reversed.
    assert state.incoming["entity:beta"] == [
        GraphEdge(
            endpoint="entity:alpha",
            predicate="uses",
            claim_id="claim:alpha-beta-0",
            origin=GRAPH_EDGE_ORIGIN_CLAIM,
        )
    ]
    assert state.incoming["entity:gamma"][0].predicate == "implements"
    assert state.incoming["entity:gamma"][0].endpoint == "entity:alpha"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
