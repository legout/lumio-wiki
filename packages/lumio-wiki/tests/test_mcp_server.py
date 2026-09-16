"""Read-only MCP server — M1 contract suite (Plan 06, PRD-0007, ADR-0028).

Covers MV1: one server binds ONE Knowledge Base Location and its ``search``
tool answers from a fresh Snapshot; every startup failure class (invalid path,
unloadable Knowledge Base, unsafe S3 URI, missing S3 credentials, missing
``[mcp]`` extra) fails before the server starts with a bounded,
credential-free message that never echoes a supplied URI.

Tests call the tool functions in-process (no stdio subprocess) and drive
``_cmd_mcp`` startup failures through ``cli.main``. The S3 binding uses
obstore's in-memory ``MemoryStore`` (same pattern as ``test_s3_location.py``),
so the suite is deterministic and infrastructure-free.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import msgspec
import pytest
from lumio_wiki import cli
from lumio_wiki.location import FilesystemLocation

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
VALID = FIXTURES / "valid"
CATEGORIZED = FIXTURES / "categorized_kb"

obstore = pytest.importorskip("obstore", reason="obstore required for the S3 MCP binding suite")

from lumio_wiki.s3_location import (  # noqa: E402
    CURRENT_POINTER_OBJECT,
    MANIFEST_OBJECT,
    S3Location,
    S3Pointer,
    build_published_manifest,
)

# ---------------------------------------------------------------------------
# Test infrastructure.
# ---------------------------------------------------------------------------


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove Lumio/AWS configuration so tests never see host credentials."""
    for var in list(os.environ):
        if var.startswith(("LUMIO_", "AWS_")):
            monkeypatch.delenv(var, raising=False)


def _publish_version(store: Any, prefix: str, version: str, root: Path) -> None:
    """Materialize a filesystem Knowledge Base under an immutable version prefix."""
    from lumio_wiki.knowledge_base import (
        _FilesystemKbSource,
        canonical_content,
        fingerprint_sources,
    )

    content = canonical_content(_FilesystemKbSource(root.resolve()))
    manifest = build_published_manifest(version, fingerprint_sources(root).digest, content)
    for rel, raw in content.items():
        obstore.put(store, f"{prefix}/{version}/{rel}", raw)
    obstore.put(store, f"{prefix}/{version}/{MANIFEST_OBJECT}", msgspec.json.encode(manifest))
    obstore.put(
        store,
        f"{prefix}/{CURRENT_POINTER_OBJECT}",
        msgspec.json.encode(S3Pointer(version=version)),
    )


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """Call a registered tool in-process and decode its JSON result."""
    pytest.importorskip("mcp")
    import anyio

    async def call():
        return await server.call_tool(name, arguments)

    result = anyio.run(call)
    assert not result.is_error
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Missing ``[mcp]`` extra: one exact install command, non-zero exit.
# ---------------------------------------------------------------------------


def test_missing_mcp_extra_names_exact_install_command(tmp_path, monkeypatch, capsys):
    """A base install without [mcp] gets the exact command and no traceback."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    # Simulate the absent distribution (works whether or not mcp is installed).
    monkeypatch.setitem(sys.modules, "mcp", None)

    rc = cli.main(["mcp", str(VALID)])

    assert rc != 0
    err = capsys.readouterr().err
    assert "pip install 'lumio-wiki[mcp]'" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# The ``valid`` fixture binds; ``search`` answers the v1 contract.
# ---------------------------------------------------------------------------


def test_v1_tool_surface_matches_the_contract():
    """The v1 surface ships exactly the eight contract tools."""
    pytest.importorskip("mcp")
    import anyio
    from lumio_wiki.mcp_server import build_server

    server = build_server(FilesystemLocation(VALID))

    async def list_tools():
        return await server.list_tools()

    tools = anyio.run(list_tools)
    assert [tool.name for tool in tools] == [
        "search",
        "page",
        "related",
        "paths",
        "hot",
        "index",
        "health",
        "status",
    ]
    search_schema = next(tool for tool in tools if tool.name == "search").input_schema
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["limit"]["default"] == 20


def test_fixture_kb_binds_and_search_returns_contract_rows():
    """The bound fixture answers ``search`` with title/path/passage rows."""
    pytest.importorskip("mcp")
    from lumio_wiki.mcp_server import build_server

    server = build_server(FilesystemLocation(VALID))
    payload = _call_tool(server, "search", {"query": "LanceDB"})

    rows = payload["results"]
    assert rows, "expected at least one hit for 'LanceDB'"
    assert set(rows[0]) == {"title", "path", "passage"}
    assert rows[0]["title"] == "Architecture"
    assert rows[0]["path"] == "architecture.md"
    assert any("LanceDB" in row["passage"] for row in rows)


def test_search_rows_empty_and_limit_forwarding():
    """The honest empty case and the CLI's limit default forwarded unchanged."""
    from lumio_wiki.location import open_knowledge_base
    from lumio_wiki.mcp_server import search_rows

    snapshot = open_knowledge_base(FilesystemLocation(VALID))
    assert search_rows(snapshot, "zzznomatch") == {"results": []}
    assert len(search_rows(snapshot, "LanceDB", limit=1)["results"]) == 1


# ---------------------------------------------------------------------------
# S3 binding over the in-memory store; fresh Snapshot per tool call.
# ---------------------------------------------------------------------------


def test_s3_binding_answers_search_and_serves_a_fresh_snapshot():
    """S3Location(store, prefix) binds; repointing the pointer changes answers."""
    pytest.importorskip("mcp")
    from lumio_wiki.mcp_server import build_server

    store = obstore.store.MemoryStore()
    # Each publish sets the pointer, so publish v2 first and v1 last: the
    # server binds to v1, and the explicit flip below must be a real change.
    _publish_version(store, "kb", "v2", CATEGORIZED)
    _publish_version(store, "kb", "v1", VALID)
    server = build_server(S3Location(store, "kb"))

    payload = _call_tool(server, "search", {"query": "LanceDB"})
    assert payload["results"][0]["title"] == "Architecture"

    # Republish the pointer to v2: the same long-running server must answer
    # from a fresh Snapshot (the categorized fixture has no LanceDB match).
    obstore.put(
        store,
        f"kb/{CURRENT_POINTER_OBJECT}",
        msgspec.json.encode(S3Pointer(version="v2")),
    )
    assert _call_tool(server, "search", {"query": "LanceDB"}) == {"results": []}


def test_s3_startup_uri_path_binds_and_serves(tmp_path, monkeypatch):
    """The real ``mcp s3://...`` startup path end-to-end (MV1 coverage).

    Drives ``cli.main`` → ``_cmd_mcp`` → ``run_server`` →
    ``_resolve_s3_snapshot`` → the REAL ``_resolve_object_store_location``
    (env config via ``_s3_config_from_env`` + the redacting
    ``S3Location.from_url``) and stubs only the S3 store construction seam
    obstore uses (``obstore.store.from_url``, ``s3_location.py``) plus the
    serve loop, so no stdio session is needed. Pointer, manifest, digest
    validation, validity refusal, and the binding all run for real.

    The stub records the factory call: if production code bypassed the real
    resolver, the factory would not receive the ``LUMIO_S3_*``-derived
    config and the config assertion below fails.
    """
    pytest.importorskip("mcp")
    from lumio_wiki import mcp_server
    from lumio_wiki.location import open_knowledge_base

    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("LUMIO_S3_REGION", "us-mcp-test-1")
    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", VALID)

    # Stub ONLY the store construction seam: the real S3Store factory would
    # open network state; the in-memory store is the established pattern.
    factory_calls: list[dict[str, Any]] = []

    def _fake_store_factory(url, config=None, client_options=None, **kwargs):
        factory_calls.append({"url": url, "config": config})
        return store

    monkeypatch.setattr(obstore.store, "from_url", _fake_store_factory)

    served: dict[str, Any] = {}

    class _StubServer:
        def __init__(self, location):
            served["location"] = location

        def run(self):
            served["ran"] = True

    # Stub the server runner so the test never opens a stdio session.
    monkeypatch.setattr(mcp_server, "build_server", lambda location: _StubServer(location))

    rc = cli.main(["mcp", "s3://bucket/kb"])

    assert rc == 0
    assert served.get("ran") is True
    # The REAL resolver ran: the factory saw the container URI (path
    # stripped) and the LUMIO_S3_REGION-derived obstore config.
    assert factory_calls == [{"url": "s3://bucket", "config": {"aws_region": "us-mcp-test-1"}}]
    location = served["location"]
    assert location.kind == "s3"
    # The bound Location serves the expected data from a fresh Snapshot.
    snapshot = open_knowledge_base(location)
    assert snapshot.search_pages("LanceDB")[0].page.title == "Architecture"


# ---------------------------------------------------------------------------
# Startup failure classes: bounded, credential-free, before the server starts.
# ---------------------------------------------------------------------------


def test_invalid_path_fails_bounded_before_server_start(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    rc = cli.main(["mcp", str(tmp_path / "missing-kb")])

    assert rc != 0
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "Traceback" not in err


def test_invalid_kb_path_fails_bounded_before_server_start(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    not_a_dir = tmp_path / "not-a-kb"
    not_a_dir.write_text("just a file", encoding="utf-8")

    rc = cli.main(["mcp", str(not_a_dir)])

    assert rc != 0
    err = capsys.readouterr().err
    assert "not a directory" in err
    assert "Traceback" not in err


def test_invalid_kb_directory_fails_before_server_start(tmp_path, monkeypatch, capsys):
    """A KB whose validation report has errors is refused, never served (AC1)."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    shutil.copytree(VALID, tmp_path / "kb")
    (tmp_path / "kb" / "lumio.yaml").write_text("version: nope\n", encoding="utf-8")

    rc = cli.main(["mcp", str(tmp_path / "kb")])

    assert rc != 0
    err = capsys.readouterr().err
    assert "invalid" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("uri", "secret"),
    [
        ("s3://user:secret@bucket/kb", "user:secret"),
        ("s3://bucket/kb?token=secret", "token=secret"),
        ("s3://bucket/kb#peek", "#peek"),
    ],
)
def test_unsafe_uris_fail_bounded_without_echoing(uri, secret, tmp_path, monkeypatch, capsys):
    """Userinfo/query/fragment are refused; the secret never appears in output."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)

    rc = cli.main(["mcp", uri])

    assert rc == 2
    captured = capsys.readouterr()
    assert "must not include credentials" in captured.err
    assert secret not in captured.err + captured.out
    # The location may be echoed only in redacted form: scheme, host, path.
    assert "s3://bucket/kb" in captured.err
    assert "Traceback" not in captured.err


def test_missing_s3_credentials_fail_bounded(tmp_path, monkeypatch, capsys):
    """No credentials and a refused endpoint: bounded, redacted-label failure."""
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("LUMIO_S3_ENDPOINT", "http://127.0.0.1:9")

    rc = cli.main(["mcp", "s3://bucket/kb"])

    assert rc != 0
    captured = capsys.readouterr()
    assert "could not resolve S3 Knowledge Base at s3://bucket/kb" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# MV2 — retrieval-tool parity (page/related/paths/hot/index).
# ---------------------------------------------------------------------------

#: Every v1 tool with the arguments the no-write proof invokes it with.
ALL_TOOL_CALLS = [
    ("search", {"query": "LanceDB"}),
    ("page", {"title": "Lumio Overview"}),
    ("related", {"title": "Lumio Overview"}),
    ("paths", {"source": "Lumio Overview", "target": "Acme Corp"}),
    ("hot", {}),
    ("index", {}),
    ("health", {}),
    ("status", {}),
]


def _tree_digest(root: Path) -> str:
    """Stable digest over every tree entry: relative path + content.

    Directory entries are included (as ``<dir>``) so creating even an EMPTY
    derived-index directory changes the digest — PRD-0007 AC3 forbids
    derived-index directory creation, not just file writes.
    """
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        if path.is_dir():
            digest.update(b"<dir>")
        else:
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _store_state(store: Any) -> dict[str, bytes]:
    """Snapshot every object key + bytes of an in-memory store."""
    state: dict[str, bytes] = {}
    for batch in obstore.list(store):
        for meta in batch:
            state[meta["path"]] = bytes(obstore.get(store, meta["path"]).bytes())
    return state


@pytest.mark.parametrize("fixture", [VALID, CATEGORIZED], ids=["valid", "categorized"])
def test_page_tool_matches_cli_lookup(fixture):
    """``page`` mirrors the CLI's canonical-then-alias lookup and body output."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import page_rows

    snapshot = open_knowledge_base(FilesystemLocation(fixture))
    expected = snapshot.lookup_by_title("Lumio Overview")[0]
    assert page_rows(snapshot, "Lumio Overview") == {
        "found": True,
        "title": expected.title,
        "path": expected.path,
        "summary": expected.summary,
        "markdown": expected.body,
    }


def test_page_tool_alias_fallback_and_honest_not_found():
    """Alias resolution falls back like the CLI; unknown titles are not errors."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import page_rows

    snapshot = open_knowledge_base(FilesystemLocation(VALID))
    # ``Lumio`` is a registered alias of ``Lumio Overview`` on the valid fixture.
    assert page_rows(snapshot, "Lumio") == page_rows(snapshot, "Lumio Overview")
    assert page_rows(snapshot, "zzz-no-such-page") == {"found": False}


@pytest.mark.parametrize("fixture", [VALID, CATEGORIZED], ids=["valid", "categorized"])
def test_related_tool_matches_snapshot_semantics(fixture):
    """``related`` returns sorted titles and echoes the requested scope/direction."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import related_rows

    snapshot = open_knowledge_base(FilesystemLocation(fixture))
    payload = related_rows(snapshot, "Lumio Overview")
    assert payload == {
        "results": sorted(
            snapshot.related_pages("Lumio Overview", scope="canonical", direction="outgoing")
        ),
        "scope": "canonical",
        "direction": "outgoing",
    }


def test_related_tool_typed_relationships_and_discovery_scope():
    """The categorized fixture exercises predicate filters and discovery scope."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import related_rows

    snapshot = open_knowledge_base(FilesystemLocation(CATEGORIZED))
    canonical = related_rows(snapshot, "Lumio Overview", relationship_type="uses")
    assert canonical == {"results": ["Acme Corp"], "scope": "canonical", "direction": "outgoing"}
    # The honest empty case keeps the echo fields.
    assert related_rows(snapshot, "Lumio Overview", relationship_type="no-such-predicate") == {
        "results": [],
        "scope": "canonical",
        "direction": "outgoing",
    }
    discovery = related_rows(
        snapshot, "Lumio Overview", scope="discovery", relationship_type="uses"
    )
    assert discovery["scope"] == "discovery"
    assert set(canonical["results"]) <= set(discovery["results"])


@pytest.mark.parametrize("fixture", [VALID, CATEGORIZED], ids=["valid", "categorized"])
def test_paths_tool_matches_shortest_path(fixture):
    """``paths`` mirrors ``shortest_path`` and echoes scope/direction either way."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import path_rows

    snapshot = open_knowledge_base(FilesystemLocation(fixture))
    direct = snapshot.shortest_path(
        "Lumio Overview", "Acme Corp", scope="canonical", direction="outgoing"
    )
    found = path_rows(snapshot, "Lumio Overview", "Acme Corp")
    if direct is None:
        assert found == {"found": False, "scope": "canonical", "direction": "outgoing"}
    else:
        assert found == {
            "found": True,
            "path": direct,
            "scope": "canonical",
            "direction": "outgoing",
        }


def test_paths_tool_honest_negative_echoes_arguments():
    """A missing path reports ``found: false`` with the requested scope/direction."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import path_rows

    snapshot = open_knowledge_base(FilesystemLocation(CATEGORIZED))
    # The categorized graph is directional: the reverse canonical hop does
    # not exist (discovery + "both" can legitimately find one).
    assert snapshot.shortest_path("Acme Corp", "Lumio Overview") is None
    assert path_rows(snapshot, "Acme Corp", "Lumio Overview") == {
        "found": False,
        "scope": "canonical",
        "direction": "outgoing",
    }
    # A nonexistent endpoint is the robust honest negative, either way.
    assert path_rows(
        snapshot, "Acme Corp", "zzz-no-such-page", scope="discovery", direction="both"
    ) == {"found": False, "scope": "discovery", "direction": "both"}


@pytest.mark.parametrize("fixture", [VALID, CATEGORIZED], ids=["valid", "categorized"])
def test_hot_tool_matches_generate_hot_index(fixture):
    """``hot`` is the in-memory Hot Index; no pins yield an honest null."""
    from lumio_wiki.knowledge_base import generate_hot_index
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import hot_rows

    snapshot = open_knowledge_base(FilesystemLocation(fixture))
    expected = generate_hot_index(snapshot.knowledge_base.control, snapshot.pages)
    payload = hot_rows(snapshot)
    assert payload == {"markdown": expected}
    assert (payload["markdown"] is None) == (fixture == VALID)


def test_index_tool_returns_the_root_navigation_index_only():
    """``index`` is the root ``index.md`` entry of the generated mapping."""
    from lumio_wiki.knowledge_base import NAV_INDEX_BASENAME, generate_navigation_indexes
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import index_rows

    for fixture in (VALID, CATEGORIZED):
        snapshot = open_knowledge_base(FilesystemLocation(fixture))
        expected = generate_navigation_indexes(snapshot.pages)[NAV_INDEX_BASENAME]
        payload = index_rows(snapshot)
        assert payload == {"markdown": expected}
        assert payload["markdown"]  # the contract guarantees non-empty


def test_s3_snapshot_serves_identical_tool_results():
    """The retrieval tools are Location-uniform: S3 answers equal filesystem."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import (
        hot_rows,
        index_rows,
        page_rows,
        path_rows,
        related_rows,
    )

    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", CATEGORIZED)
    fs_snapshot = open_knowledge_base(FilesystemLocation(CATEGORIZED))
    s3_snapshot = open_knowledge_base(S3Location(store, "kb"))

    assert page_rows(s3_snapshot, "Lumio Overview") == page_rows(fs_snapshot, "Lumio Overview")
    assert related_rows(s3_snapshot, "Lumio Overview") == related_rows(
        fs_snapshot, "Lumio Overview"
    )
    assert related_rows(s3_snapshot, "Lumio Overview", relationship_type="uses") == related_rows(
        fs_snapshot, "Lumio Overview", relationship_type="uses"
    )
    assert path_rows(s3_snapshot, "Lumio Overview", "Acme Corp") == path_rows(
        fs_snapshot, "Lumio Overview", "Acme Corp"
    )
    assert hot_rows(s3_snapshot) == hot_rows(fs_snapshot)
    assert index_rows(s3_snapshot) == index_rows(fs_snapshot)


def test_s3_registered_page_tool_answers_via_fastmcp():
    """The registered ``page`` tool answers over the S3 binding in-process."""
    pytest.importorskip("mcp")
    from lumio_wiki.mcp_server import build_server

    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", CATEGORIZED)
    server = build_server(S3Location(store, "kb"))

    payload = _call_tool(server, "page", {"title": "Acme Corp"})
    assert payload["found"] is True
    assert payload["title"] == "Acme Corp"
    assert payload["path"] == "entities/acme.md"
    assert payload["markdown"]


# ---------------------------------------------------------------------------
# MV3 — diagnostics (health/status) + the no-write proof.
# ---------------------------------------------------------------------------


def _validation_projection(snapshot: Any) -> dict[str, Any]:
    """The Location-independent validation fields of the ``health`` tool."""
    from lumio_wiki.mcp_server import health_rows

    payload = health_rows(snapshot)
    return {
        key: payload[key]
        for key in (
            "valid",
            "pages",
            "issues",
            "broken_relationships",
            "unknown_relationship_types",
            "invalid_fields",
            "duplicate_aliases",
            "missing_summaries",
        )
    }


@pytest.mark.parametrize("fixture", [VALID, CATEGORIZED], ids=["valid", "categorized"])
def test_health_matches_the_in_memory_derivation(fixture):
    """Health fields derive only from Snapshot data, mirroring ``health_report``."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import health_rows

    snapshot = open_knowledge_base(FilesystemLocation(fixture))
    report = snapshot.validation_report
    payload = health_rows(snapshot)

    assert payload["valid"] == report.is_valid
    assert payload["pages"] == len(snapshot.pages)
    assert payload["issues"] == len(report.issues)
    assert payload["missing_summaries"] == sorted(
        entry.path for entry in snapshot.knowledge_base.registry() if not entry.summary
    )
    # The fixtures are healthy: no broken/unknown/invalid/duplicate rows.
    assert payload["broken_relationships"] == []
    assert payload["unknown_relationship_types"] == []
    assert payload["invalid_fields"] == []
    assert payload["duplicate_aliases"] == []


def test_health_graph_disclosure_on_a_plain_fixture_is_zero_index():
    """No derived directory: nothing is read, probed, or created."""
    import tempfile

    from lumio_wiki.cli import default_index_dir
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import health_rows

    with tempfile.TemporaryDirectory() as td:
        kb_copy = Path(td) / "kb"
        shutil.copytree(VALID, kb_copy)
        snapshot = open_knowledge_base(FilesystemLocation(kb_copy))
        before = _tree_digest(kb_copy)

        payload = health_rows(snapshot)

        assert payload["graph"] == {
            "materialized": False,
            "fresh": None,
            "edges": None,
            "source": "zero-index",
            "disclosure": "no derived index directory",
        }
        assert payload["derived_index"] == {"available": False, "kind": None}
        assert _tree_digest(kb_copy) == before
        assert not default_index_dir(kb_copy).exists()


def test_health_graph_disclosure_with_a_materialized_fresh_artifact():
    """A pre-materialized fresh artifact reports ``source: "derived"``."""
    import tempfile

    from lumio_wiki.cli import default_index_dir
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import health_rows

    with tempfile.TemporaryDirectory() as td:
        kb_copy = Path(td) / "kb"
        shutil.copytree(VALID, kb_copy)
        kb, _ = load_knowledge_base(kb_copy)
        kb.materialize_graph(default_index_dir(kb.root))
        snapshot = open_knowledge_base(FilesystemLocation(kb_copy))
        expected_edges = kb.graph_health(default_index_dir(kb.root)).edge_count

        payload = health_rows(snapshot)

        assert payload["graph"]["materialized"] is True
        assert payload["graph"]["fresh"] is True
        assert payload["graph"]["edges"] == expected_edges
        assert payload["graph"]["source"] == "derived"
        assert payload["derived_index"] == {"available": True, "kind": "filesystem"}


def test_status_matches_the_contract_projection():
    """``status`` reports the bound Location, fingerprint, and zero-index state."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base
    from lumio_wiki.mcp_server import status_rows

    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", CATEGORIZED)
    fs_snapshot = open_knowledge_base(FilesystemLocation(CATEGORIZED))
    s3_snapshot = open_knowledge_base(S3Location(store, "kb"))

    fs_payload = status_rows(fs_snapshot)
    # ``extras`` mirrors ``lumio-wiki doctor``'s fixed detection order.
    from lumio_wiki.cli import _detect_module

    doctor_order: list[str] = []
    if all(_detect_module(module) for module in ("liteparse", "markitdown", "anydoc")):
        doctor_order.append("documents")
    if _detect_module("openai"):
        doctor_order.append("llm")
    if _detect_module("obstore"):
        doctor_order.append("s3")
    if _detect_module("lancedb"):
        doctor_order.append("lancedb")
    assert fs_payload == {
        "location": {"kind": "filesystem"},
        "fingerprint": fs_snapshot.fingerprint.digest,
        "pages": len(fs_snapshot.pages),
        "valid": fs_snapshot.validation_report.is_valid,
        "retrieval": "zero-index",
        "remote_derived_index": {"available": False},
        "extras": doctor_order,
    }

    s3_payload = status_rows(s3_snapshot)
    assert s3_payload["location"] == {"kind": "s3"}
    # Location-independent fields are identical across Location kinds.
    fs_rest = {k: v for k, v in fs_payload.items() if k != "location"}
    s3_rest = {k: v for k, v in s3_payload.items() if k != "location"}
    assert s3_rest == fs_rest


def test_health_validation_fields_are_location_independent():
    """Health validation content is equal across Location kinds."""
    from lumio_wiki.location import FilesystemLocation, open_knowledge_base

    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", CATEGORIZED)
    fs_snapshot = open_knowledge_base(FilesystemLocation(CATEGORIZED))
    s3_snapshot = open_knowledge_base(S3Location(store, "kb"))
    assert _validation_projection(fs_snapshot) == _validation_projection(s3_snapshot)


@pytest.mark.parametrize("pre_materialized", [False, True], ids=["plain", "derived-graph"])
def test_full_tool_surface_never_writes_a_filesystem_fixture(pre_materialized):
    """MV3: constructing the server and calling every tool writes nothing.

    Run once on a plain fixture and once on a copy with a pre-materialized
    derived graph, proving the ``graph_health`` read path never writes either.
    """
    pytest.importorskip("mcp")
    import tempfile

    from lumio_wiki.cli import default_index_dir
    from lumio_wiki.knowledge_base import load_knowledge_base
    from lumio_wiki.mcp_server import build_server

    with tempfile.TemporaryDirectory() as td:
        kb_copy = Path(td) / "kb"
        shutil.copytree(VALID, kb_copy)
        if pre_materialized:
            kb, _ = load_knowledge_base(kb_copy)
            kb.materialize_graph(default_index_dir(kb.root))
        before = _tree_digest(kb_copy)
        server = build_server(FilesystemLocation(kb_copy))

        for name, arguments in ALL_TOOL_CALLS:
            _call_tool(server, name, arguments)

        assert _tree_digest(kb_copy) == before


def test_full_tool_surface_never_writes_the_s3_store():
    """MV3: the in-memory store's keys and bytes are unchanged after every tool."""
    pytest.importorskip("mcp")
    from lumio_wiki.mcp_server import build_server

    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", CATEGORIZED)
    before = _store_state(store)
    server = build_server(S3Location(store, "kb"))

    for name, arguments in ALL_TOOL_CALLS:
        _call_tool(server, name, arguments)

    assert _store_state(store) == before


def test_s3_health_reports_a_published_derived_graph(tmp_path):
    """MV3: the S3 "published artifact" branch reports real derived state.

    Publishes the categorized fixture with the REAL ``publish_s3_version``
    (which writes the graph artifact next to the canonical content, see
    ``_write_canonical_and_graph``), then asserts ``health`` reports
    ``source: "derived"`` with the artifact's edge count — and that the full
    tool surface leaves the store's keys and bytes unchanged.
    """
    pytest.importorskip("mcp")
    from lumio_wiki.mcp_server import build_server, health_rows
    from lumio_wiki.s3_publish import publish_s3_version

    kb_copy = tmp_path / "kb"
    shutil.copytree(CATEGORIZED, kb_copy)
    store = obstore.store.MemoryStore()
    publish_s3_version(store, "kb", source_root=kb_copy, version="v1")

    location = S3Location(store, "kb")
    snapshot = location.resolve()
    # The read-only seam itself labels the digest-verified published artifact.
    state, label, note = location.graph_state_with_source(snapshot)
    assert label == "published artifact"
    assert note is None
    assert state.edge_count > 0  # the categorized fixture has real graph edges

    payload = health_rows(snapshot)
    assert payload["graph"] == {
        "materialized": True,
        "fresh": True,
        "edges": state.edge_count,
        "source": "derived",
        "disclosure": "published graph artifact (manifest digest-verified)",
    }
    # The published graph artifact is not a remote LanceDB index.
    assert payload["derived_index"] == {"available": False, "kind": None}

    # The full tool surface leaves the published store byte-identical.
    before = _store_state(store)
    server = build_server(location)
    for name, arguments in ALL_TOOL_CALLS:
        _call_tool(server, name, arguments)
    assert _store_state(store) == before
