"""Graph exchange export + stub import tests (issue #193, ADR-0024, PRD-0005).

Four test layers, matching the PRD-0005 testing decisions:

* Gold-file tests for both export formats over ``eval/fixture_kb`` (node/edge
  counts, typed-vs-untyped edge marking, determinism).
* Round-trip test: ``export_graph`` -> ``import_graph`` -> one staged Ingest
  Proposal that validates cleanly.
* Import tests: foreign wiki-export lineage ``graph.json``, malformed input,
  broken-link diagnostics.
* Security test: ``restricted``/``internal`` page titles, summaries, and edges
  are absent from every export artifact (ADR-0007 export boundary).
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from lumio_wiki import (
    export_graph,
    import_graph,
    load_knowledge_base,
    seeded_control_file,
    write_control_file,
)
from lumio_wiki.cli import main
from lumio_wiki.graph_exchange import (
    EDGE_KIND_REFERENCE,
    EDGE_KIND_RELATIONSHIP,
    GraphExchangeExport,
    GraphImportError,
)
from lumio_wiki.okf import ExportVisibilityScope, select_export_pages
from lumio_wiki.records import CompiledPage

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
EVAL_KB = ROOT / "eval" / "fixture_kb"
GUARDRAILS_KB = FIXTURES / "guardrails"
CATEGORIZED_KB = FIXTURES / "categorized_kb"

GRAPHML_NS = "{http://graphml.graphdrawing.org/xmlns}"


# ---------------------------------------------------------------------------
# Export: gold-file tests over eval/fixture_kb
# ---------------------------------------------------------------------------


def _export_eval_fixture() -> tuple[GraphExchangeExport, dict]:
    kb, _report = load_knowledge_base(EVAL_KB)
    pages = select_export_pages(kb.pages, ExportVisibilityScope.PUBLIC)
    export = export_graph(pages)
    return export, json.loads(export.graph_json)


def test_export_graph_node_link_shape_over_eval_fixture():
    """graph.json is NetworkX node_link over the authorized public page set."""
    export, payload = _export_eval_fixture()

    assert payload["directed"] is True
    assert payload["multigraph"] is False
    assert payload["graph"]["format"] == "lumio-graph-exchange"
    nodes = payload["nodes"]
    assert len(nodes) == 24
    # Node fields: identity, Canonical Page Title, tags, summary. Root-level
    # fixture pages carry no Content Category, so the field is absent.
    node = next(n for n in nodes if n["title"] == "Access Control")
    assert node["id"] == "entity:access-control"
    assert node["label"] == "Access Control"
    assert "fixture" in node["tags"]
    assert node["summary"]
    assert "category" not in node
    # No node ever carries body content or Sources.
    assert "body" not in node
    assert "sources" not in node


def test_export_graph_marks_typed_vs_untyped_edges():
    """Edges carry a kind: relationship (typed Claims) vs reference (extracted)."""
    export, payload = _export_eval_fixture()

    links = payload["links"]
    kinds = {link["kind"] for link in links}
    assert kinds <= {EDGE_KIND_RELATIONSHIP, EDGE_KIND_REFERENCE}
    assert EDGE_KIND_REFERENCE in kinds
    relationships = [link for link in links if link["kind"] == EDGE_KIND_RELATIONSHIP]
    references = [link for link in links if link["kind"] == EDGE_KIND_REFERENCE]
    # The fixture KB has accepted entity-to-entity Claims (typed) plus body
    # links (untyped); both classes must be present and distinguishable.
    assert relationships, "fixture KB must exercise typed relationship edges"
    assert references, "fixture KB must exercise extracted reference edges"
    for link in relationships:
        assert link["predicate"]
    for link in references:
        assert "predicate" not in link
    # Every edge endpoint is an exported node id.
    node_ids = {node["id"] for node in payload["nodes"]}
    assert all(link["source"] in node_ids for link in links)
    assert all(link["target"] in node_ids for link in links)
    assert export.relationship_count == len(relationships)
    assert export.reference_count == len(references)


def test_export_graph_is_deterministic():
    """Identical authorized pages yield byte-identical artifacts."""
    export_a, _ = _export_eval_fixture()
    export_b, _ = _export_eval_fixture()
    assert export_a.graph_json == export_b.graph_json
    assert export_a.graphml == export_b.graphml


def test_export_graph_matches_committed_gold_files():
    """Gold-file contract: the categorized fixture's export is byte-stable.

    The committed artifacts under ``tests/fixtures/graph_exchange/`` pin the
    serialized shape; any intentional format change updates them deliberately
    (``graph.json`` node fields are additive-only once shipped, PRD-0005).
    """
    kb, _report = load_knowledge_base(CATEGORIZED_KB)
    export = export_graph(select_export_pages(kb.pages, ExportVisibilityScope.ALL))

    gold = FIXTURES / "graph_exchange"
    assert export.graph_json == (gold / "graph.json").read_text(encoding="utf-8")
    assert export.graphml == (gold / "graph.graphml").read_text(encoding="utf-8")


def test_import_graph_stub_matches_committed_gold_markdown(tmp_path: Path):
    """Gold-file contract: the foreign stub page renders byte-stable."""
    graph_path = tmp_path / "foreign.json"
    graph_path.write_text(json.dumps(WIKI_EXPORT_GRAPH), encoding="utf-8")
    dest_kb, _report = load_knowledge_base(_fresh_categorized_kb(tmp_path / "dest"))
    proposal = import_graph(graph_path, dest_kb)
    assert not proposal.blocked

    gold = (FIXTURES / "graph_exchange" / "transformers_stub.md").read_text(encoding="utf-8")
    stub = next(p for p in proposal.proposed_pages if p.title == "Transformer Architecture")
    assert stub.markdown == gold


def test_export_graph_graphml_matches_json():
    """graph.graphml carries the same node/edge set for Gephi/yEd/Cytoscape."""
    export, payload = _export_eval_fixture()

    root = ET.fromstring(export.graphml)
    assert root.tag == f"{GRAPHML_NS}graphml"
    graph = root.find(f"{GRAPHML_NS}graph")
    assert graph is not None
    assert graph.attrib["edgedefault"] == "directed"
    xml_nodes = graph.findall(f"{GRAPHML_NS}node")
    xml_edges = graph.findall(f"{GRAPHML_NS}edge")
    assert len(xml_nodes) == len(payload["nodes"])
    assert len(xml_edges) == len(payload["links"])
    # Edge kind marking survives the GraphML projection.
    kinds = set()
    for edge in xml_edges:
        for data in edge.findall(f"{GRAPHML_NS}data"):
            if data.attrib["key"] == "kind":
                kinds.add(data.text)
    assert kinds <= {EDGE_KIND_RELATIONSHIP, EDGE_KIND_REFERENCE}
    assert kinds == {link["kind"] for link in payload["links"]}


# ---------------------------------------------------------------------------
# Export: visibility security (ADR-0007 export boundary)
# ---------------------------------------------------------------------------


def _page(
    title: str,
    *,
    visibility: str,
    entity_id: str | None = None,
    body: str = "",
) -> CompiledPage:
    return CompiledPage(
        path=f"{title.lower().replace(' ', '_')}.md",
        title=title,
        id=entity_id or "",
        tags=["t"],
        summary=f"Summary of {title}.",
        lifecycle="approved",
        visibility=visibility,
        synthetic=True,
        body=body,
    )


def test_export_graph_public_scope_never_leaks_restricted_or_internal():
    """Restricted/internal titles, summaries, and edges are absent from BOTH artifacts."""
    public = _page(
        "Public Doc",
        visibility="public",
        entity_id="entity:public-doc",
        body="# Public Doc\n\nSee the [secret](secret.md) internals.\n",
    )
    restricted = _page("Secret Doc", visibility="restricted", entity_id="entity:secret-doc")
    internal = _page("Internal Doc", visibility="internal", entity_id="entity:internal-doc")

    authorized = select_export_pages([public, restricted, internal], ExportVisibilityScope.PUBLIC)
    export = export_graph(authorized)

    for artifact in (export.graph_json, export.graphml):
        assert "Secret Doc" not in artifact
        assert "Restricted" not in artifact or "Secret Doc" not in artifact
        assert "Internal Doc" not in artifact
        assert "Summary of Secret Doc" not in artifact
        assert "entity:secret-doc" not in artifact
        assert "entity:internal-doc" not in artifact
    payload = json.loads(export.graph_json)
    assert [node["title"] for node in payload["nodes"]] == ["Public Doc"]
    # The public page's body link to the restricted page produced no edge
    # (extraction ran over the authorized set only).
    assert payload["links"] == []
    assert payload["graph"]["node_count"] == 1


def test_export_graph_all_scope_includes_every_visibility_class():
    """The explicitly privileged ALL scope exports internal + restricted too."""
    pages = [
        _page("Public Doc", visibility="public", entity_id="entity:public-doc"),
        _page("Internal Doc", visibility="internal", entity_id="entity:internal-doc"),
        _page("Secret Doc", visibility="restricted", entity_id="entity:secret-doc"),
    ]
    authorized = select_export_pages(pages, ExportVisibilityScope.ALL)
    export = export_graph(authorized)

    payload = json.loads(export.graph_json)
    assert {node["title"] for node in payload["nodes"]} == {
        "Public Doc",
        "Internal Doc",
        "Secret Doc",
    }


# ---------------------------------------------------------------------------
# Round trip: export_graph -> import_graph -> one clean proposal
# ---------------------------------------------------------------------------


def _fresh_categorized_kb(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_control_file(root, seeded_control_file())
    return root


def test_round_trip_export_import_stages_one_clean_proposal(tmp_path: Path):
    """export-graph -> import-graph yields one reviewable stub proposal that validates."""
    kb, _report = load_knowledge_base(CATEGORIZED_KB)
    export = export_graph(select_export_pages(kb.pages, ExportVisibilityScope.ALL))

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(export.graph_json, encoding="utf-8")

    dest_root = _fresh_categorized_kb(tmp_path / "dest")
    dest_kb, _report = load_knowledge_base(dest_root)
    proposal = import_graph(graph_path, dest_kb)

    assert not proposal.blocked
    assert proposal.validation_report.is_valid
    assert {page.title for page in proposal.proposed_pages} == {
        "Lumio Overview",
        "Acme Corp",
    }
    # Stub pages are frontmatter skeletons plus link structure, no bodies.
    overview = next(p for p in proposal.proposed_pages if p.title == "Lumio Overview")
    assert overview.relative_path == "concepts/overview.md"
    assert overview.category == "concepts"
    assert "synthetic: true" in overview.markdown
    # Lumio lineage keeps its Entity identity: the stub re-declares the
    # original Entity ID and Entity Types for review.
    assert 'id: "entity:lumio-overview"' in overview.markdown
    assert '- "concept"' in overview.markdown
    # The typed `uses` edge arrives as link structure marked with its predicate.
    assert "uses" in overview.markdown
    assert "[Acme Corp](../entities/acme.md" in overview.markdown
    acme = next(p for p in proposal.proposed_pages if p.title == "Acme Corp")
    assert acme.relative_path == "entities/acme.md"
    assert acme.category == "entities"


def test_round_trip_public_scope_stubs_carry_no_restricted_content(tmp_path: Path):
    """A public-scope export re-imports only the public node."""
    kb, _report = load_knowledge_base(CATEGORIZED_KB)
    export = export_graph(select_export_pages(kb.pages, ExportVisibilityScope.PUBLIC))

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(export.graph_json, encoding="utf-8")
    dest_kb, _report = load_knowledge_base(_fresh_categorized_kb(tmp_path / "dest"))
    proposal = import_graph(graph_path, dest_kb)

    assert [page.title for page in proposal.proposed_pages] == ["Lumio Overview"]
    assert not proposal.blocked


# ---------------------------------------------------------------------------
# Import: foreign wiki-export lineage, malformed input, broken links
# ---------------------------------------------------------------------------


WIKI_EXPORT_GRAPH = {
    "directed": False,
    "multigraph": False,
    "graph": {"exported_at": "2026-01-01T00:00:00", "vault": "/tmp/vault"},
    "nodes": [
        {
            "id": "concepts/transformers",
            "label": "Transformer Architecture",
            "category": "concepts",
            "tags": ["ml", "architecture"],
            "summary": "Attention-based architecture.",
            "community": 0,
        },
        {
            "id": "entities/vaswani",
            "label": "Ashish Vaswani",
            "category": "entities",
            "tags": ["person"],
            "summary": "Lead author.",
            "community": 0,
        },
    ],
    "links": [
        {
            "source": "concepts/transformers",
            "target": "entities/vaswani",
            "relation": "wikilink",
            "confidence": "EXTRACTED",
        },
        {
            "source": "concepts/transformers",
            "target": "entities/missing",
            "relation": "extends",
            "confidence": "EXTRACTED",
            "typed": True,
        },
    ],
}


def test_import_graph_accepts_wiki_export_lineage(tmp_path: Path):
    """wiki-export graph.json stubs by label, maps category by directory convention."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(WIKI_EXPORT_GRAPH), encoding="utf-8")
    kb, _report = load_knowledge_base(_fresh_categorized_kb(tmp_path / "dest"))

    proposal = import_graph(graph_path, kb)

    assert not proposal.blocked
    by_title = {page.title: page for page in proposal.proposed_pages}
    assert set(by_title) == {"Transformer Architecture", "Ashish Vaswani"}
    transformers = by_title["Transformer Architecture"]
    assert transformers.relative_path == "concepts/transformers.md"
    assert transformers.category == "concepts"
    assert "architecture" in transformers.markdown
    # The plain wikilink edge arrives as an unmarked link; the typed edge to a
    # node that does not exist is disclosed as a diagnostic and dropped.
    assert "[Ashish Vaswani](../entities/vaswani.md)" in transformers.markdown
    diagnostics = proposal.okf_diagnostics
    assert any(d.kind == "broken-link" for d in diagnostics)


def test_import_graph_rejects_malformed_input(tmp_path: Path):
    kb_root = _fresh_categorized_kb(tmp_path / "dest")
    kb, _report = load_knowledge_base(kb_root)

    not_json = tmp_path / "not_json.json"
    not_json.write_text("{nope", encoding="utf-8")
    with pytest.raises(GraphImportError):
        import_graph(not_json, kb)

    missing_nodes = tmp_path / "missing_nodes.json"
    missing_nodes.write_text(json.dumps({"links": []}), encoding="utf-8")
    with pytest.raises(GraphImportError):
        import_graph(missing_nodes, kb)

    bad_nodes = tmp_path / "bad_nodes.json"
    bad_nodes.write_text(json.dumps({"nodes": ["nope"], "links": []}), encoding="utf-8")
    with pytest.raises(GraphImportError):
        import_graph(bad_nodes, kb)

    no_id = tmp_path / "no_id.json"
    no_id.write_text(json.dumps({"nodes": [{"label": "No Id"}], "links": []}), encoding="utf-8")
    with pytest.raises(GraphImportError):
        import_graph(no_id, kb)

    missing_file = tmp_path / "absent.json"
    with pytest.raises(GraphImportError):
        import_graph(missing_file, kb)


def test_import_graph_rejects_duplicate_and_colliding_nodes(tmp_path: Path):
    """Input-shape validation: duplicate ids and colliding paths never stage."""
    kb, _report = load_knowledge_base(_fresh_categorized_kb(tmp_path / "dest"))

    duplicate_id = tmp_path / "duplicate_id.json"
    duplicate_id.write_text(
        json.dumps({"nodes": [{"id": "a"}, {"id": "a"}], "links": []}),
        encoding="utf-8",
    )
    with pytest.raises(GraphImportError, match="duplicate node id"):
        import_graph(duplicate_id, kb)

    colliding_paths = tmp_path / "colliding.json"
    colliding_paths.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "x", "path": "concepts/overview.md"},
                    {"id": "y", "path": "concepts/overview.md"},
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GraphImportError, match="same stub path"):
        import_graph(colliding_paths, kb)


def test_import_graph_diagnoses_malformed_link_endpoints(tmp_path: Path):
    """Non-string link endpoints are diagnosed, never an uncaught crash."""
    graph_path = tmp_path / "weird.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [{"id": "concepts/ok", "label": "Ok"}],
                "links": [{"source": [], "target": "concepts/ok"}],
            }
        ),
        encoding="utf-8",
    )
    kb, _report = load_knowledge_base(_fresh_categorized_kb(tmp_path / "dest"))

    proposal = import_graph(graph_path, kb)

    assert not proposal.blocked
    assert any(
        d.kind == "broken-link" and "malformed link entry" in d.message
        for d in proposal.okf_diagnostics
    )


def test_import_graph_into_legacy_flat_mode(tmp_path: Path):
    """A destination without a Control File accepts root-level stubs unchanged."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(WIKI_EXPORT_GRAPH), encoding="utf-8")
    dest_root = tmp_path / "flat"
    dest_root.mkdir()
    kb, _report = load_knowledge_base(dest_root)

    proposal = import_graph(graph_path, kb)

    assert not proposal.blocked
    # Legacy Flat Mode: no category routing; stubs keep the source's
    # directory convention (foreign node ids are vault-relative paths).
    paths = {page.relative_path for page in proposal.proposed_pages}
    assert paths == {"concepts/transformers.md", "entities/vaswani.md"}


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_export_graph_writes_both_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root = tmp_path / "kb"
    shutil.copytree(EVAL_KB, root)
    out_dir = tmp_path / "out"

    rc = main(["export-graph", str(root), "--out-dir", str(out_dir)])

    assert rc == 0
    graph_json = out_dir / "graph.json"
    graphml = out_dir / "graph.graphml"
    assert graph_json.is_file() and graphml.is_file()
    payload = json.loads(graph_json.read_text(encoding="utf-8"))
    assert len(payload["nodes"]) == 24
    out = capsys.readouterr().out
    assert "graph.json" in out and "graph.graphml" in out


def test_cli_import_graph_stages_reviewable_proposal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    kb, _report = load_knowledge_base(CATEGORIZED_KB)
    export = export_graph(select_export_pages(kb.pages, ExportVisibilityScope.ALL))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(export.graph_json, encoding="utf-8")
    dest_root = _fresh_categorized_kb(tmp_path / "dest")

    rc = main(["import-graph", str(dest_root), str(graph_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out
    assert "proposal inspect" in out
