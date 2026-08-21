"""Remote LanceDB index locations — deterministic contract coverage (issue #122).

Covers the typed ``IndexLocation`` seam, remote metadata sidecars over an
in-memory object store, fingerprint/embedding-model identity, and the truthful
zero-index fallback when a remote index is missing — all without a live S3
endpoint. The companion ``test_remote_lancedb_minio.py`` exercises a real
remote build/search against MinIO and skips when no endpoint is configured.

This module never tests LanceDB internals: it proves the *contract* (build,
search, fallback, identity validation) over a local filesystem and an obstore
``MemoryStore`` (ADR-0013).
"""

from __future__ import annotations

import hashlib

import msgspec
import pytest
from lumio_lancedb import (
    LanceDBRetrievalAdapter,
    LocalIndexLocation,
    RemoteIndexLocation,
    build_lexical_index,
    build_semantic_index,
    has_semantic_index,
    search_hybrid_index,
    search_lexical_index,
    search_pages,
    search_semantic_index,
)
from lumio_lancedb.index import _load_fingerprint, _save_fingerprint
from lumio_lancedb.location import as_location
from lumio_wiki.embeddings import EmbeddingError
from lumio_wiki.records import (
    CompiledPage,
    EmbeddingModelInfo,
    Relationship,
    SourceFingerprint,
)


def _obstore():
    """Return obstore, skipping just this test when the ``[s3]`` extra is absent.

    Local-only tests below never call this, so they run on the base wheel
    without obstore (ADR-0010).
    """
    return pytest.importorskip("obstore", reason="obstore required for remote sidecar tests")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


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
    """Deterministic, dependency-free embedder matching the ``Embedder`` protocol."""

    def __init__(self, name: str = "fake-model", dimension: int = 8) -> None:
        self._info = EmbeddingModelInfo(name=name, dimension=dimension)

    @property
    def model_info(self) -> EmbeddingModelInfo:
        return self._info

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256((text + self._info.name).encode()).digest()
            vec = [(b - 128) / 128.0 for b in digest[: self._info.dimension]]
            while len(vec) < self._info.dimension:
                vec.append(0.0)
            out.append(vec[: self._info.dimension])
        return out

def _fingerprint() -> SourceFingerprint:
    return SourceFingerprint(digest="0" * 64)


# ---------------------------------------------------------------------------
# as_location coercion.
# ---------------------------------------------------------------------------


def test_as_location_normalizes_path_str_and_passthrough():
    from pathlib import Path

    loc = as_location("/tmp/x")
    assert isinstance(loc, LocalIndexLocation)
    assert isinstance(as_location(Path("/tmp/x")), LocalIndexLocation)
    assert as_location(None) is None
    remote = RemoteIndexLocation("s3://b/p")
    assert as_location(remote) is remote


def test_as_location_rejects_unknown_types():
    with pytest.raises(TypeError):
        as_location(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Local typed-location build/search equivalence (Path == LocalIndexLocation).
# ---------------------------------------------------------------------------


def test_local_index_location_builds_and_searches(tmp_path):
    index_dir = tmp_path / "idx"
    build_lexical_index(_pages(), index_dir)
    assert isinstance(as_location(index_dir), LocalIndexLocation)

    # Searching through the typed location yields the same citation-ready results.
    via_path = search_lexical_index(index_dir, "LanceDB", limit=5)
    via_location = search_lexical_index(LocalIndexLocation(index_dir), "LanceDB", limit=5)
    assert via_path and via_location
    assert [r.evidence.id for r in via_path] == [r.evidence.id for r in via_location]
    assert via_location[0].trace.stages
    assert via_location[0].citation.page_title


def test_page_search_uses_bm25_scores_from_typed_location(tmp_path):
    from lumio_lancedb.index import _indexed_page_scores

    index_dir = tmp_path / "idx"
    build_lexical_index(_pages(), index_dir)
    location = LocalIndexLocation(index_dir)
    scores = _indexed_page_scores(location, "LanceDB")
    results = search_pages(_pages(), "LanceDB", limit=5, index_dir=location)

    assert scores
    assert results
    assert results[0].score == pytest.approx(scores[results[0].page.path], abs=1e-4)
    assert [result.page.path for result in results] == [
        path
        for path, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def test_local_semantic_and_hybrid_through_location(tmp_path):
    index_dir = tmp_path / "idx"
    embedder = _FakeEmbedder()
    build_lexical_index(_pages(), index_dir)
    build_semantic_index(_pages(), index_dir, embedder)
    assert has_semantic_index(index_dir) is True
    assert has_semantic_index(LocalIndexLocation(index_dir)) is True

    vec = embedder.embed(["LanceDB retrieval"])[0]
    sem = search_semantic_index(LocalIndexLocation(index_dir), vec, limit=5, score_threshold=0.0)
    hybrid = search_hybrid_index(
        LocalIndexLocation(index_dir), "LanceDB", vec, limit=5, score_threshold=0.0
    )
    assert isinstance(sem, list)
    assert isinstance(hybrid, list)


# ---------------------------------------------------------------------------
# Remote metadata sidecars over an in-memory object store.
# ---------------------------------------------------------------------------


def _remote(store=None, prefix="kb/v1/derived/lance"):
    store = store if store is not None else _obstore().store.MemoryStore()
    return RemoteIndexLocation(
        f"s3://bucket/{prefix}",
        storage_options={"region": "us-east-1"},
        store=store,
        sidecar_prefix=prefix,
    )


def test_remote_sidecar_round_trip_via_memory_store():
    ob = _obstore()
    store = ob.store.MemoryStore()
    loc = _remote(store)
    assert loc.is_remote is True
    assert loc.has_index() is False  # nothing published yet

    fp = _fingerprint()
    _save_fingerprint(loc, fp)
    assert loc.has_index() is True  # freshness sidecar present
    assert _load_fingerprint(loc) == fp

    # The sidecar landed at the documented object key beside the tables.
    result = ob.get(store, "kb/v1/derived/lance/fingerprint.json")
    assert msgspec.json.decode(bytes(result.bytes()), type=SourceFingerprint) == fp


def test_remote_embedding_model_identity_round_trip():
    loc = _remote()
    from lumio_lancedb.index import _load_model, _save_model

    info = EmbeddingModelInfo(name="remote-model", dimension=16)
    assert _load_model(loc) is None
    _save_model(loc, info)
    assert _load_model(loc) == info


def test_remote_describe_is_secret_free():
    loc = RemoteIndexLocation(
        "s3://bucket/kb/v1/derived/lance",
        storage_options={"aws_access_key_id": "SECRET"},
    )
    assert "SECRET" not in loc.describe
    assert loc.describe == "s3://bucket/kb/v1/derived/lance"


# ---------------------------------------------------------------------------
# Fingerprint / embedding-model identity rejection.
# ---------------------------------------------------------------------------


def test_local_semantic_model_mismatch_is_rejected(tmp_path):
    index_dir = tmp_path / "idx"
    build_lexical_index(_pages(), index_dir)
    build_semantic_index(_pages(), index_dir, _FakeEmbedder(name="model-a"))

    adapter = LanceDBRetrievalAdapter()
    with pytest.raises(EmbeddingError):
        adapter.retrieve(
            _pages(),
            "LanceDB",
            limit=5,
            index_dir=index_dir,
            mode="semantic",
            embedder=_FakeEmbedder(name="model-b"),
        )


def test_remote_fingerprint_sidecar_round_trips_canonical_value():
    """A remote sidecar carrying a different fingerprint is read verbatim.

    The publisher writes the canonical fingerprint; a reader compares it against
    the current source and rejects a mismatch. Here we prove the sidecar records
    the canonical fingerprint exactly so that comparison is trustworthy.
    """
    loc = _remote()
    canonical = SourceFingerprint(digest="a" * 64)
    _save_fingerprint(loc, canonical)
    loaded = _load_fingerprint(loc)
    assert loaded == canonical
    assert loaded != _fingerprint()  # a different fingerprint is detectably different


# ---------------------------------------------------------------------------
# Zero-index fallback when an index is missing (the key safety property).
# ---------------------------------------------------------------------------


def test_missing_remote_index_falls_back_to_zero_index():
    loc = _remote()  # no sidecars -> has_index() is False
    adapter = LanceDBRetrievalAdapter()

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=loc)

    # Zero-index retrieval over the same snapshot still answers.
    assert results, "missing index must fall back to zero-index retrieval, not empty"
    stage = results[0].trace.stages[0]
    assert stage.name == "index-fallback"
    assert "missing" in stage.detail
    assert "zero-index" in stage.detail
    # The evidence still carries a citation and snippet (citation-ready contract).
    assert results[0].citation.page_title
    assert results[0].snippet


def test_missing_local_index_falls_back_to_zero_index(tmp_path):
    loc = LocalIndexLocation(tmp_path / "never-built")
    adapter = LanceDBRetrievalAdapter()

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=loc)

    assert results
    assert results[0].trace.stages[0].name == "index-fallback"


def test_none_index_returns_empty_not_fallback():
    adapter = LanceDBRetrievalAdapter()
    assert adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=None) == []


def test_remote_fallback_respects_eligible_pages():
    loc = _remote()
    adapter = LanceDBRetrievalAdapter()

    pages = _pages()
    # Restrict to a single eligible page; the fallback must honor it.
    results = adapter.retrieve(
        pages,
        "LanceDB",
        limit=5,
        index_dir=loc,
        eligible_pages=[pages[0]],
    )
    assert results
    assert all(r.evidence.page_path == pages[0].path for r in results)


def test_remote_unhealthy_index_records_error_in_trace():
    """A remote index whose sidecars exist but LanceDB connect fails degrades.

    We force an unhealthy path: a fingerprint sidecar is present (so
    has_index() is True) but the LanceDB URI points at a guaranteed-refused
    endpoint, so the search raises and the adapter records the error and falls
    back to zero-index. A refused TCP port fails fast regardless of AWS creds,
    so this is deterministic in any environment.
    """
    ob = _obstore()
    store = ob.store.MemoryStore()
    loc = RemoteIndexLocation(
        "s3://lumio-unreachable-test/p/derived/lance",
        storage_options={
            "region": "us-east-1",
            "endpoint": "http://127.0.0.1:1",  # refused -> fast, deterministic
            "allow_http": "true",
        },
        store=store,
        sidecar_prefix="p/derived/lance",
    )
    _save_fingerprint(loc, _fingerprint())  # sidecar present -> has_index True
    adapter = LanceDBRetrievalAdapter()

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=loc)

    assert results
    stage = results[0].trace.stages[0]
    assert stage.name == "index-fallback"
    assert "unavailable" in stage.detail
