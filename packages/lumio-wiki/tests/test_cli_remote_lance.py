"""CLI binding of standalone ``lumio-wiki`` reads to a remote LanceDB index (#162).

Proves the standalone CLI uses the active S3 Published Version's remote LanceDB
index when ``LUMIO_RETRIEVAL_BACKEND=lancedb``: the pointer is resolved exactly
once, ``lumio-lancedb`` is bound dynamically from the Snapshot's
dependency-neutral descriptor (never by reconstructing S3 keys), lexical search
serves BM25, semantic/hybrid use the configured embedder with the exact remote
index model identity, and every degradable failure falls back to truthful
zero-index retrieval over the same Snapshot with a disclosed note
(issue #162, ADR-0013/0019).

Stub-based orchestration tests run everywhere (no LanceDB required); the
real-adapter section proves BM25/binding behavior with LanceDB installed; a
MinIO module covers the live endpoint journey.
"""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest
from lumio_wiki import cli
from lumio_wiki.location import FilesystemLocation, KnowledgeBaseSnapshot, RemoteDerivedIndex
from lumio_wiki.records import (
    CompiledPage,
    EmbeddingModelInfo,
    Relationship,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"


class _StubS3Location:
    """A stand-in S3 Location resolving to a prepared Snapshot."""

    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def resolve(self):
        return self._snapshot


class _StubRemoteLocation:
    """Stand-in for lumio_lancedb's IndexLocation with scripted behavior."""

    def __init__(
        self,
        *,
        has_index=True,
        describe="s3://bucket/kb/v1/derived/lance",
        fingerprint=None,
        error=None,
    ):
        self._has_index = has_index
        self._describe = describe
        self._fingerprint = fingerprint
        self._error = error
        self.calls: list[tuple] = []

    @property
    def describe(self):
        return self._describe

    def has_index(self):
        if self._error is not None:
            raise self._error
        return self._has_index

    def read_sidecar(self, name):
        import msgspec
        from lumio_wiki.fingerprint_store import FINGERPRINT_FILE

        if name == FINGERPRINT_FILE and self._fingerprint is not None:
            return msgspec.json.encode(self._fingerprint)
        return None


class _StubModule:
    """Stand-in for the dynamically imported ``lumio_lancedb`` module."""

    def __init__(self, *, results=None, error=None):
        self._results = results if results is not None else []
        self._error = error
        self.search_calls: list[dict] = []

    def search_pages(self, pages, query, limit=20, index_dir=None):
        self.search_calls.append(
            {"pages": pages, "query": query, "limit": limit, "index_dir": index_dir}
        )
        if self._error is not None:
            raise self._error
        return self._results


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Isolate env-derived config: clean cwd, no exported LUMIO retrieval keys."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "LUMIO_RETRIEVAL_BACKEND",
        "LUMIO_RETRIEVAL_MODE",
        "LUMIO_KB_PATH",
        "LUMIO_S3_REGION",
        "LUMIO_S3_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


def _fixture_snapshot() -> KnowledgeBaseSnapshot:
    return FilesystemLocation(FIXTURES / "valid").resolve()


def _snapshot_with_descriptor(snapshot, *, uri="s3://bucket/kb/v1/derived/lance"):
    """Return the Snapshot with a remote derived-index descriptor attached."""
    return msgspec.structs.replace(
        snapshot,
        remote_derived_index=RemoteDerivedIndex(
            uri=uri,
            sidecar_prefix="kb/v1/derived/lance",
            version="v1",
            fingerprint=snapshot.fingerprint,
            store=None,
        ),
    )


def _route_s3(monkeypatch, snapshot) -> None:
    def _fake_resolve(uri):
        assert cli._is_object_store_uri(uri), "S3 path must reach the object-store branch"
        return _StubS3Location(snapshot)

    monkeypatch.setattr(cli, "_resolve_object_store_location", _fake_resolve)


def _pages() -> list[CompiledPage]:
    return [
        CompiledPage(
            path="overview.md",
            title="Overview",
            body="Lumio uses LanceDB for derived evidence retrieval.\n",
            relationships=[Relationship(target="tech.md", type="depends-on")],
        ),
        CompiledPage(
            path="tech.md",
            title="Technology",
            body="Retrieval is backed by LanceDB full-text search.\n",
        ),
    ]


class _FakeEmbedder:
    """Deterministic Embedder: identical unit vectors for identical text."""

    def __init__(self, name="fake-model", dimension=8):
        self._info = EmbeddingModelInfo(name=name, dimension=dimension)

    @property
    def model_info(self):
        return self._info

    def embed(self, texts):
        import hashlib

        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            vec = [(b - 128) / 128.0 for b in digest[: self._info.dimension]]
            out.append(vec)
        return out


# ---------------------------------------------------------------------------
# Backend configuration resolution.
# ---------------------------------------------------------------------------


def test_retrieval_backend_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "weaviate")
    with pytest.raises(cli.CliError, match="LUMIO_RETRIEVAL_BACKEND"):
        cli._retrieval_backend()


# ---------------------------------------------------------------------------
# Actionable configuration errors (never silently degraded).
# ---------------------------------------------------------------------------


def test_lancedb_backend_without_adapter_is_actionable(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr("lumio_wiki.retrieval_eval.lancedb_available", lambda: False)
    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 2
    assert "lumio-lancedb" in capsys.readouterr().err


def test_semantic_mode_over_s3_requires_lancedb_backend(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "semantic"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LUMIO_RETRIEVAL_BACKEND=lancedb" in err


def test_index_dir_conflicts_with_s3_location(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr("lumio_wiki.retrieval_eval.lancedb_available", lambda: True)
    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--index-dir", "/tmp/lance"])
    assert rc == 2
    assert "--index-dir" in capsys.readouterr().err


def test_lexical_zero_index_backend_never_binds(monkeypatch, capsys):
    """Default backend over S3 stays on zero-index: no adapter import, no binding."""
    _route_s3(monkeypatch, _fixture_snapshot())

    def _explode(snapshot):
        raise AssertionError("zero-index backend must not bind lumio-lancedb")

    monkeypatch.setattr(cli, "_bind_remote_lancedb", _explode)
    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    assert "## " in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Lexical BM25 over the remote index, with truthful zero-index fallback.
# ---------------------------------------------------------------------------


def _bind_stub(monkeypatch, module, location):
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda snapshot: (module, location))


def test_lexical_healthy_remote_index_uses_bm25_page_search(monkeypatch, capsys):
    snapshot = _snapshot_with_descriptor(_fixture_snapshot())
    _route_s3(monkeypatch, snapshot)
    module = _StubModule()
    _bind_stub(monkeypatch, module, _StubRemoteLocation())

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    assert len(module.search_calls) == 1
    call = module.search_calls[0]
    assert call["query"] == "architecture"
    assert call["index_dir"] is not None
    # The loaded Compiled Pages of the SAME resolved Snapshot feed the ranking.
    assert [p.title for p in call["pages"]] == [p.title for p in snapshot.pages]
    # No fallback disclosure on the healthy path.
    assert "note:" not in capsys.readouterr().out


def test_lexical_missing_remote_index_falls_back_with_note(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    module = _StubModule()
    _bind_stub(monkeypatch, module, _StubRemoteLocation(has_index=False))

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert module.search_calls == []  # the index was never queried
    assert "## " in out  # zero-index results still served
    assert "note:" in out and "missing" in out and "zero-index" in out


def test_lexical_fingerprint_mismatch_falls_back_with_note(monkeypatch, capsys):
    from lumio_wiki.records import SourceFingerprint

    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    stale = SourceFingerprint(digest="f" * 64)
    module = _StubModule()
    _bind_stub(monkeypatch, module, _StubRemoteLocation(fingerprint=stale))

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert module.search_calls == []  # the stale index was never queried
    assert "note:" in out and "stale" in out and "fingerprint mismatch" in out


def test_lexical_connection_failure_falls_back_with_note(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    module = _StubModule(error=ConnectionError("endpoint unreachable"))
    _bind_stub(monkeypatch, module, _StubRemoteLocation())

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## " in out
    assert "note:" in out and "unavailable" in out and "endpoint unreachable" in out


def test_lexical_denied_remote_index_falls_back_with_note(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    module = _StubModule()
    _bind_stub(
        monkeypatch,
        module,
        _StubRemoteLocation(error=PermissionError("access denied")),
    )

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Architecture" in out
    assert "note:" in out and "access denied" in out and "zero-index" in out


def test_lexical_fallback_note_includes_index_location(monkeypatch, capsys):
    _route_s3(monkeypatch, _snapshot_with_descriptor(_fixture_snapshot()))
    # The note must name where the index was expected so a stale pointer is
    # diagnosable (truthful disclosure, issue #162).
    module = _StubModule()
    location = _StubRemoteLocation(has_index=False, describe="s3://bucket/kb/v9/derived/lance")
    _bind_stub(monkeypatch, module, location)
    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    assert "s3://bucket/kb/v9/derived/lance" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Mode selection.
# ---------------------------------------------------------------------------


def test_mode_defaults_from_env(monkeypatch, capsys):
    snapshot = _snapshot_with_descriptor(_fixture_snapshot())
    _route_s3(monkeypatch, snapshot)
    module = _StubModule()
    location = _StubRemoteLocation()
    _bind_stub(monkeypatch, module, location)
    monkeypatch.setenv("LUMIO_RETRIEVAL_MODE", "lexical")

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    assert len(module.search_calls) == 1  # lexical mode selected from .env/env


# ---------------------------------------------------------------------------
# Real lumio-lancedb: descriptor binding, BM25, semantic identity.
# ---------------------------------------------------------------------------

lumio_lancedb = pytest.importorskip(
    "lumio_lancedb", reason="lumio-lancedb required for real binding tests"
)


def test_bind_remote_lancedb_builds_remote_index_location(monkeypatch):
    snapshot = _snapshot_with_descriptor(_fixture_snapshot())
    monkeypatch.setenv("LUMIO_S3_REGION", "us-east-1")
    monkeypatch.setenv("LUMIO_S3_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("LUMIO_S3_ACCESS_KEY_ID", "minio")
    monkeypatch.setenv("LUMIO_S3_SECRET_ACCESS_KEY", "minio123")

    module, location = cli._bind_remote_lancedb(snapshot)

    assert module is lumio_lancedb
    assert isinstance(location, lumio_lancedb.RemoteIndexLocation)
    assert location.uri == "s3://bucket/kb/v1/derived/lance"
    assert location.sidecar_prefix == "kb/v1/derived/lance"
    # Same credentials/region/endpoint as canonical S3 reads, as LanceDB's own
    # storage options (issue #162: one configuration source, no secrets in
    # portable content).
    assert location.storage_options["region"] == "us-east-1"
    assert location.storage_options["endpoint"] == "http://localhost:9000"
    assert location.storage_options["allow_http"] == "true"
    assert location.storage_options["access_key_id"] == "minio"
    assert location.storage_options["secret_access_key"] == "minio123"


def test_bind_remote_lancedb_requires_descriptor(monkeypatch, capsys):
    snapshot = _fixture_snapshot()  # filesystem Snapshot: no descriptor
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    with pytest.raises(cli.CliError):
        cli._bind_remote_lancedb(msgspec.structs.replace(snapshot, remote_derived_index=None))


def test_lexical_search_serves_real_bm25_over_the_bound_location(monkeypatch, capsys, tmp_path):
    """End to end: fixture pages, real index, BM25 page search through the CLI."""
    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "lance")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, fingerprint=snapshot.fingerprint
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## " in out
    assert "architecture.md" in out
    assert "note:" not in out


def test_lexical_incomplete_remote_index_falls_back_with_note(monkeypatch, capsys, tmp_path):
    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "incomplete")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, fingerprint=snapshot.fingerprint
    )
    location.connect().drop_table(lumio_lancedb.PAGE_TABLE_NAME)
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))

    rc = cli.main(["search", "s3://bucket/kb", "architecture"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Architecture" in out
    assert "note:" in out and "incomplete" in out and "zero-index" in out


def test_semantic_remote_search_uses_bound_adapter(monkeypatch, capsys, tmp_path):
    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "lance")
    embedder = _FakeEmbedder(name="sem-model")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, embedder=embedder, fingerprint=snapshot.fingerprint
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))
    monkeypatch.setattr(
        cli, "_resolve_embedder", lambda model=None: _FakeEmbedder(name="sem-model")
    )

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "semantic"])
    assert rc == 0
    out = capsys.readouterr().out
    # Evidence output path (semantic), served from the bound remote index.
    assert "path:" in out
    assert "note:" not in out


def test_semantic_remote_model_mismatch_is_actionable(monkeypatch, capsys, tmp_path):
    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "lance")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages),
        location,
        embedder=_FakeEmbedder(name="model-a"),
        fingerprint=snapshot.fingerprint,
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))
    monkeypatch.setattr(cli, "_resolve_embedder", lambda model=None: _FakeEmbedder(name="model-b"))

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "semantic"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "embedding model" in err


def test_semantic_remote_missing_index_falls_back_with_note(monkeypatch, capsys, tmp_path):
    from lumio_lancedb import LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "absent")  # never built
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))
    monkeypatch.setattr(cli, "_resolve_embedder", lambda model=None: _FakeEmbedder())

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "semantic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "note:" in out and "zero-index" in out


def test_semantic_empty_fallback_still_discloses_missing_index(monkeypatch, capsys, tmp_path):
    from lumio_lancedb import LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "absent")
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))
    monkeypatch.setattr(cli, "_resolve_embedder", lambda model=None: _FakeEmbedder())

    rc = cli.main(
        ["search", "s3://bucket/kb", "no-such-evidence", "--mode", "semantic"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "note:" in out and "missing" in out and "zero-index" in out
    assert "No Evidence matched" in out


def test_no_managed_local_index_for_s3_semantic(monkeypatch, capsys, tmp_path):
    """An S3 URI never builds a local index: no .lumio under cwd, no Path coercion."""
    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "remote-lance")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, fingerprint=snapshot.fingerprint
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "lexical"])
    assert rc == 0
    assert not (tmp_path / ".lumio").exists()


# ---------------------------------------------------------------------------
# ``search --json`` over the S3 + LanceDB paths (kanban t_e7c9cb77).
# ---------------------------------------------------------------------------


def test_s3_lexical_json_emits_one_object_with_accounting(monkeypatch, capsys, tmp_path):
    import json as jsonlib

    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "lance")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, fingerprint=snapshot.fingerprint
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--json"])
    assert rc == 0
    data = jsonlib.loads(capsys.readouterr().out)
    assert data["kind"] == "page"
    assert data["results_returned"] == len(data["results"])
    assert data["candidates_seen"] >= data["results_returned"]
    assert data["candidates_seen"] == data["results_returned"] + data["results_dropped"]


def test_s3_lexical_json_fallback_accounting_stays_zero_index_truthful(
    monkeypatch, capsys, tmp_path
):
    """With --json the fallback is disclosed INSIDE the payload as a top-level
    ``note`` (ADR-0019: the degrading path says so; the accounting counts
    cannot distinguish a zero-index from a BM25 ranking), and stdout stays one
    parseable JSON object."""
    import json as jsonlib

    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "incomplete")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, fingerprint=snapshot.fingerprint
    )
    location.connect().drop_table(lumio_lancedb.PAGE_TABLE_NAME)
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--json"])
    assert rc == 0
    data = jsonlib.loads(capsys.readouterr().out)
    assert data["kind"] == "page"
    assert data["results"]
    assert data["candidates_seen"] == data["results_returned"] + data["results_dropped"]
    note = data["note"]
    assert note is not None
    assert "zero-index" in note


def test_semantic_remote_json_emits_evidence_kind(monkeypatch, capsys, tmp_path):
    import json as jsonlib

    from lumio_lancedb import LanceDBRetrievalAdapter, LocalIndexLocation

    snapshot = _fixture_snapshot()
    _route_s3(monkeypatch, _snapshot_with_descriptor(snapshot))
    location = LocalIndexLocation(tmp_path / "lance")
    embedder = _FakeEmbedder(name="sem-model")
    LanceDBRetrievalAdapter().build_index(
        list(snapshot.pages), location, embedder=embedder, fingerprint=snapshot.fingerprint
    )
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    monkeypatch.setattr(cli, "_bind_remote_lancedb", lambda s: (lumio_lancedb, location))
    monkeypatch.setattr(
        cli, "_resolve_embedder", lambda model=None: _FakeEmbedder(name="sem-model")
    )

    rc = cli.main(["search", "s3://bucket/kb", "architecture", "--mode", "semantic", "--json"])
    assert rc == 0
    data = jsonlib.loads(capsys.readouterr().out)
    assert data["kind"] == "evidence"
    assert data["results"]
    assert data["candidates_seen"] == data["results_returned"] + data["results_dropped"]
