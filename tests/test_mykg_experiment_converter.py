"""Converter contract tests for the myKG experiment harness (issue #146).

The converter is the experiment-only, dependency-free script at
``experiments/mykg/convert_mykg.py``. These tests pin its mapping contract:

* myKG nodes -> candidate Compiled Pages (lifecycle ``review``, visibility
  ``internal``, ontology class -> free-form ``type``, edges NEVER written as
  frontmatter relationships);
* myKG edges -> a PRIVATE relationship-candidate sidecar;
* raw source paths / confidence / method stay out of the page (privacy).

One rendered page is also round-tripped through Lumio's own ``parse_frontmatter``
to prove the emitted tree is loadable by the external-import proposal path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from lumio_wiki import as_sources, parse_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
CONVERTER_PATH = REPO_ROOT / "experiments" / "mykg" / "convert_mykg.py"
SAMPLE_SESSION = REPO_ROOT / "experiments" / "mykg" / "sample"


@pytest.fixture(scope="module")
def converter():
    """Load the standalone converter module directly from its file path."""
    spec = importlib.util.spec_from_file_location("lumio_mykg_converter", CONVERTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lumio_mykg_converter"] = module
    spec.loader.exec_module(module)
    return module


def _write_session(tmp_path: Path, nodes: list, edges: list, schema: dict | None = None) -> Path:
    """Lay out a minimal myKG session (output/ + intermediate/) under tmp_path."""
    (tmp_path / "output").mkdir(parents=True)
    (tmp_path / "intermediate").mkdir(parents=True)
    (tmp_path / "output" / "nodes.jsonl").write_text(
        "".join(json.dumps(node) + "\n" for node in nodes), encoding="utf-8"
    )
    (tmp_path / "output" / "edges.jsonl").write_text(
        "".join(json.dumps(edge) + "\n" for edge in edges), encoding="utf-8"
    )
    if schema is not None:
        (tmp_path / "intermediate" / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return tmp_path


def test_sample_session_maps_nodes_to_candidate_pages(converter, tmp_path):
    out = tmp_path / "tree"
    result = converter.convert_session(str(SAMPLE_SESSION), converter.Settings())
    converter.write_tree(result, out, converter.Settings())

    pages = {page.source_node_id: page for page in result.pages}
    assert set(pages) == {"n_acme", "n_bob"}  # n_noname has no name -> skipped
    assert [skip["node_id"] for skip in result.skipped] == ["n_noname"]

    acme = pages["n_acme"]
    assert acme.title == "Acme"
    assert acme.type == "Organization"          # ontology class -> free-form type
    assert acme.lifecycle == "review"           # never 'approved'
    assert acme.visibility == "internal"        # conservative default
    assert acme.tags == ["organization"]        # type-derived tag (required, non-empty)
    assert len(acme.sources) == 1               # provenance present (>=1 Source required)
    assert acme.sources[0].id.startswith("mykg-")

    acme_md = (out / "entities" / f"{acme.slug}.md").read_text(encoding="utf-8")
    # Privacy: no node/attribute confidence, no method, no raw source path leaks into the page.
    for forbidden in ("0.93", "0.9", "0.8", "method", "confidence", "corpus/"):
        assert forbidden not in acme_md, f"page leaked private data: {forbidden!r}"
    # Edges are candidates only: NEVER a frontmatter relationships block.
    assert "relationships:" not in acme_md

    # Round-trip through Lumio's own frontmatter parser -> the tree is importable.
    data, _body, _line = parse_frontmatter(acme_md, Path("acme.md"))
    assert data["title"] == "Acme"
    assert data["lifecycle"] == "review"
    assert data["visibility"] == "internal"
    assert data["type"] == "Organization"
    assert "relationships" not in data
    sources = as_sources(data.get("sources"))
    assert len(sources) == 1
    assert sources[0].id.startswith("mykg-")


def test_edges_become_private_candidates_sidecar(converter, tmp_path):
    out = tmp_path / "tree"
    result = converter.convert_session(str(SAMPLE_SESSION), converter.Settings())
    converter.write_tree(result, out, converter.Settings())

    sidecar = (out / "relationship-candidates.jsonl").read_text(encoding="utf-8")
    candidates = [json.loads(line) for line in sidecar.splitlines() if line.strip()]
    assert len(candidates) == 2

    resolvable = next(c for c in candidates if c["edge_id"] == "e1")
    assert resolvable["from_candidate_title"] == "Bob Lee"
    assert resolvable["to_candidate_title"] == "Acme"
    assert resolvable["relation_type"] == "works_for"
    assert resolvable["confidence"] == pytest.approx(0.82)
    assert resolvable["method"] == "extracted"
    assert resolvable["disposition"] == "pending"   # maintainer fills this in
    assert resolvable["resolvable"]

    dangling = next(c for c in candidates if c["edge_id"] == "e2")
    assert dangling["to_candidate_title"] is None    # n_noname was skipped
    assert not dangling["resolvable"]
    assert any("dangling" in warning or "skipped/missing" in warning for warning in result.warnings)


def test_private_provenance_recorded_only_in_report(converter, tmp_path):
    out = tmp_path / "tree"
    result = converter.convert_session(str(SAMPLE_SESSION), converter.Settings())
    converter.write_tree(result, out, converter.Settings())

    report = json.loads((out / "conversion-report.json").read_text(encoding="utf-8"))
    raw_sources = {f for files in report["private_provenance"].values() for f in files}
    assert "corpus/acme-overview.md" in raw_sources          # PRIVATE: report only
    assert report["summary"]["pages"] == 2
    assert report["summary"]["candidates"] == 2

    # ...and the raw path must NOT appear in any published page.
    for page_md in (out / "entities").glob("*.md"):
        assert "corpus/" not in page_md.read_text(encoding="utf-8")


def test_title_and_alias_collisions_are_disambiguated(converter, tmp_path):
    nodes = [
        {
            "id": "a",
            "type": "Organization",
            "attributes": {"name": {"value": "Acme"}},
            "source_files": ["x.md"],
        },
        {
            "id": "b",
            "type": "Organization",
            "attributes": {"name": {"value": "Acme"}},
            "source_files": ["y.md"],
        },
        {
            "id": "c",
            "type": "Person",
            "attributes": {"name": {"value": "Someone"}},
            "aliases": ["Acme"],
            "source_files": ["z.md"],
        },
    ]
    session = _write_session(tmp_path, nodes, [])
    result = converter.convert_session(str(session), converter.Settings())

    titles = [page.title for page in result.pages]
    assert len({t.lower() for t in titles}) == 3            # all titles unique
    assert titles[0] == "Acme"
    assert titles[1].lower().startswith("acme") and titles[1] != "Acme"   # disambiguated

    # node c's alias "Acme" collided with a taken title -> dropped, not duplicated.
    page_c = next(page for page in result.pages if page.source_node_id == "c")
    assert page_c.aliases == []
    assert result.collisions
    assert any("dropped alias" in warning for warning in result.warnings)


def test_approved_lifecycle_is_rejected(converter, tmp_path):
    nodes = [
        {
            "id": "a",
            "type": "X",
            "attributes": {"name": {"value": "A"}},
            "source_files": ["x.md"],
        }
    ]
    session = _write_session(tmp_path, nodes, [])
    with pytest.raises(ValueError, match="never 'approved'"):
        converter.convert_session(str(session), converter.Settings(default_lifecycle="approved"))


def test_node_without_source_files_is_skipped(converter, tmp_path):
    # A myKG node without source_files has no provenance. It must NOT become a
    # Synthetic Page (those are derived from other compiled knowledge); it is
    # skipped, and edges into it become dangling candidates.
    nodes = [{"id": "a", "type": "Concept", "attributes": {"name": {"value": "Idea"}}}]
    session = _write_session(tmp_path, nodes, [])
    result = converter.convert_session(str(session), converter.Settings())

    assert result.pages == []
    assert [skip["node_id"] for skip in result.skipped] == ["a"]
    assert "provenance" in result.skipped[0]["reason"]


def test_cli_converts_sample_to_tree(converter, tmp_path):
    out = tmp_path / "cli-tree"
    rc = converter.main(["--session", str(SAMPLE_SESSION), "--out", str(out)])
    assert rc == 0
    assert (out / "entities").is_dir()
    assert (out / "relationship-candidates.jsonl").is_file()
    assert (out / "conversion-report.json").is_file()
    assert len(list((out / "entities").glob("*.md"))) == 2
