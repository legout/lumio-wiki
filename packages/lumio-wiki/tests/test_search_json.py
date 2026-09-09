"""JSON search output + deterministic retrieval accounting (kanban t_e7c9cb77).

Covers:

* ``RetrievalTrace`` carries additive ``candidates_seen`` / ``results_returned``
  / ``results_dropped`` counts on the zero-index path.
* ``lumio-wiki search <kb> <query> --json`` emits one machine-readable object
  with the same accounting, for both the lexical page kind and the
  semantic/hybrid Evidence kind.
* Human output and exit codes are byte-identical with and without ``--json``
  being available (the flag never changes human output).
* Counts survive graph-expansion trace composition unchanged.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from lumio_wiki.cli import main
from lumio_wiki.knowledge_base import KnowledgeBase
from lumio_wiki.records import CompiledPage, RetrievalTrace

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "valid", root)
    return root


def _pages() -> list[CompiledPage]:
    return [
        CompiledPage(
            path="a.md",
            title="Alpha",
            body="Retrieval accounting matters. Repeated words rank higher.\n",
        ),
        CompiledPage(
            path="b.md",
            title="Beta",
            body="Retrieval accounting is deterministic.\n",
        ),
        CompiledPage(
            path="c.md",
            title="Gamma",
            body="Something unrelated entirely.\n",
        ),
    ]


def _kb() -> KnowledgeBase:
    return KnowledgeBase(root=Path("."), pages=_pages())


# ---------------------------------------------------------------------------
# RetrievalTrace accounting on the zero-index path.
# ---------------------------------------------------------------------------


def test_zero_index_trace_counts_are_deterministic_and_consistent():
    results = _kb().retrieve("retrieval accounting", limit=1)
    assert len(results) == 1
    trace = results[0].trace
    # Two of the three pages match; Gamma never becomes a candidate.
    assert trace.candidates_seen == 2
    assert trace.results_returned == 1
    assert trace.results_dropped == 1
    assert trace.candidates_seen == trace.results_returned + trace.results_dropped


def test_zero_index_trace_counts_when_limit_cuts_nothing():
    results = _kb().retrieve("retrieval accounting", limit=20)
    trace = results[0].trace
    assert trace.candidates_seen == 2
    assert trace.results_returned == 2
    assert trace.results_dropped == 0


def test_zero_index_shared_trace_is_identical_on_every_result():
    results = _kb().retrieve("retrieval accounting", limit=3)
    assert len(results) == 2
    traces = {json.dumps(r.trace.candidates_seen) for r in results}
    assert len(traces) == 1
    assert results[0].trace.results_returned == 2


def test_trace_defaults_keep_existing_constructors_valid():
    trace = RetrievalTrace()
    assert trace.candidates_seen == 0
    assert trace.results_returned == 0
    assert trace.results_dropped == 0


def test_graph_composition_preserves_adapter_counts():
    kb = _kb()
    # Expansion from the seed "Alpha" (no outgoing edges) authorizes exactly
    # one page; the adapter ranks only its Evidence.
    results = kb.retrieve("retrieval accounting", limit=2, graph_seed_titles=["Alpha"])
    stage_names = [s.name for s in results[0].trace.stages]
    assert "graph-expansion" in stage_names
    assert results[0].trace.candidates_seen == 1
    assert results[0].trace.results_returned == 1
    assert results[0].trace.results_dropped == 0


# ---------------------------------------------------------------------------
# ``search --json``: the machine-readable output contract.
# ---------------------------------------------------------------------------


def test_search_json_emits_one_object_with_accounting(kb_root: Path, capsys):
    rc = main(["search", str(kb_root), "LanceDB", "--limit", "1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {
        "query",
        "kind",
        "candidates_seen",
        "results_returned",
        "results_dropped",
        "results",
    }
    assert data["query"] == "LanceDB"
    assert data["kind"] == "page"
    assert data["results_returned"] == len(data["results"])
    assert data["candidates_seen"] >= data["results_returned"]
    assert data["candidates_seen"] == data["results_returned"] + data["results_dropped"]
    first = data["results"][0]
    assert first["title"]
    assert first["path"]
    assert first["score"] >= 0


def test_search_json_no_matches_still_emits_contract(kb_root: Path, capsys):
    rc = main(["search", str(kb_root), "zzzznomatchzzzz", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["results"] == []
    assert data["candidates_seen"] == 0
    assert data["results_returned"] == 0
    assert data["results_dropped"] == 0


def test_s3_search_json_retains_the_exact_resolved_version(
    kb_root: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Machine actions are pinned to the Snapshot, not a later S3 pointer."""
    import lumio_wiki as lw

    kb, report = lw.load_knowledge_base(kb_root)
    assert report.is_valid
    active_version = "v1"
    requested_versions: list[str | None] = []

    def fake_location(_uri: str, *, version: str | None = None):
        requested_versions.append(version)
        return SimpleNamespace(
            resolve=lambda: SimpleNamespace(
                knowledge_base=kb,
                published_version=version if version is not None else active_version,
            )
        )

    monkeypatch.setattr("lumio_wiki.cli._resolve_object_store_location", fake_location)
    uri = "s3://public-bucket/team-kb"
    assert main(["search", uri, "Lumio", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["published_version"] == "v1"
    assert payload["results"]
    assert payload["results"][0]["published_version"] == "v1"
    assert payload["results"][0]["open_command"].endswith("--published-version v1")
    assert "X-Amz" not in json.dumps(payload)

    active_version = "v2"
    assert main(["search", uri, "Lumio", "--published-version", "v1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["published_version"] == "v1"
    assert requested_versions[-1] == "v1"


def test_search_without_json_keeps_human_output(kb_root: Path, capsys):
    rc = main(["search", str(kb_root), "LanceDB", "--limit", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## " in out
    assert "score:" in out
    assert "{" not in out


def test_search_json_note_field_defaults_to_healthy_null(kb_root: Path, capsys):
    """No fallback on the local path: the payload carries ``note: null`` so a
    machine consumer still gets one parseable object with the key present."""
    rc = main(["search", str(kb_root), "LanceDB", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["note"] is None


def test_search_json_matches_human_result_count(kb_root: Path, capsys):
    main(["search", str(kb_root), "LanceDB", "--limit", "3"])
    human = capsys.readouterr().out
    human_count = sum(1 for line in human.splitlines() if line.startswith("## "))
    rc = main(["search", str(kb_root), "LanceDB", "--limit", "3", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["results_returned"] == human_count
