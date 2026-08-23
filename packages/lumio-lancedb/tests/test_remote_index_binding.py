"""Instance-bound remote index locations on LanceDBRetrievalAdapter (issue #124).

A LanceDB-enhanced S3 deployment reads the active Snapshot's *published*
remote LanceDB index rather than building a local temp index. The adapter is
constructed with the remote ``IndexLocation`` and the Snapshot's expected
fingerprint bound at construction time, so the Knowledge Base's default
``retrieve()`` path (which passes ``index_dir=None`` for an S3 Snapshot) still
reaches the remote index, and a stale/missing/unhealthy index degrades to
truthful zero-index retrieval over the same snapshot (ADR-0013).
"""

from __future__ import annotations

import pytest

obstore = pytest.importorskip("obstore", reason="obstore required for remote binding tests")

from lumio_lancedb import (  # noqa: E402
    LanceDBRetrievalAdapter,
    LocalIndexLocation,
    RemoteIndexLocation,
)
from lumio_lancedb.index import _save_fingerprint  # noqa: E402
from lumio_wiki.records import (  # noqa: E402
    CompiledPage,
    SourceFingerprint,
)


def _pages() -> list[CompiledPage]:
    return [
        CompiledPage(
            path="overview.md",
            title="LanceDB Retrieval",
            body="Lumio uses LanceDB for BM25 full-text evidence search.\n",
        ),
        CompiledPage(
            path="tech.md",
            title="Technology",
            body="Retrieval is backed by LanceDB.\n",
        ),
    ]


def _fingerprint(digest: str = "a" * 64) -> SourceFingerprint:
    return SourceFingerprint(digest=digest)


def _remote(store=None, prefix="kb/v1/derived/lance"):
    store = store if store is not None else obstore.store.MemoryStore()
    return RemoteIndexLocation(
        f"s3://bucket/{prefix}",
        storage_options=None,
        store=store,
        sidecar_prefix=prefix,
    )


# ---------------------------------------------------------------------------
# A bound location is used when no explicit index_dir is passed.
# ---------------------------------------------------------------------------


def test_bound_missing_location_falls_back_to_zero_index():
    """An adapter bound to a missing remote index degrades to zero-index."""
    adapter = LanceDBRetrievalAdapter(index_location=_remote())

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=None)

    assert results, "bound missing index must fall back to zero-index, not empty"
    stage = results[0].trace.stages[0]
    assert stage.name == "index-fallback"
    assert "missing" in stage.detail
    assert results[0].citation.page_title


def test_explicit_index_dir_overrides_bound_location(tmp_path):
    """A caller-supplied index_dir wins over the construction-bound location."""
    pages = _pages()
    # Build a real local index so the explicit path returns enhanced results.
    built = tmp_path / "idx"
    adapter = LanceDBRetrievalAdapter(index_location=_remote())  # bound: empty/missing
    adapter.build_index(pages, built, fingerprint=_fingerprint())

    results = adapter.retrieve(pages, "LanceDB", limit=5, index_dir=LocalIndexLocation(built))

    # Enhanced (not fallback): the local built index served the query.
    assert results
    assert all(stage.name != "index-fallback" for r in results for stage in r.trace.stages)


def test_adapter_with_no_index_anywhere_returns_empty():
    """No bound location and no index_dir -> no results (enhancement not set up)."""
    adapter = LanceDBRetrievalAdapter()
    assert adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=None) == []


# ---------------------------------------------------------------------------
# A bound expected_fingerprint gates a present-but-stale remote index.
# ---------------------------------------------------------------------------


def test_bound_expected_fingerprint_mismatch_falls_back():
    """An instance-bound expected fingerprint detects a stale remote index.

    The remote sidecar advertises a different digest than the Snapshot the app
    is serving, so the adapter refuses to serve it and degrades to zero-index
    with the mismatch recorded in the trace (ADR-0013, #122 AC3).
    """
    loc = _remote()
    # Sidecar present (has_index True) but built from a DIFFERENT source.
    _save_fingerprint(loc, _fingerprint(digest="b" * 64))
    adapter = LanceDBRetrievalAdapter(
        index_location=loc,
        expected_fingerprint=_fingerprint(digest="a" * 64),
    )

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=None)

    assert results
    stage = results[0].trace.stages[0]
    assert stage.name == "index-fallback"
    assert "fingerprint mismatch" in stage.detail


def test_bound_expected_fingerprint_match_proceeds_then_degrades_unhealthy():
    """A matching bound fingerprint passes the gate; an unreachable table degrades.

    The fingerprint matches, so the gate opens; the LanceDB URI then points at a
    refused endpoint, so the search raises and the adapter records an
    *unavailable* (not *missing*) fallback.
    """
    store = obstore.store.MemoryStore()
    loc = RemoteIndexLocation(
        "s3://lumio-unreachable-test/p/derived/lance",
        storage_options={
            "region": "us-east-1",
            "endpoint": "http://127.0.0.1:1",
            "allow_http": "true",
        },
        store=store,
        sidecar_prefix="p/derived/lance",
    )
    fp = _fingerprint(digest="c" * 64)
    _save_fingerprint(loc, fp)
    adapter = LanceDBRetrievalAdapter(index_location=loc, expected_fingerprint=fp)

    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=None)

    assert results
    stage = results[0].trace.stages[0]
    assert stage.name == "index-fallback"
    assert "unavailable" in stage.detail


def test_bound_location_and_fingerprint_are_readable():
    """The construction-bound location/fingerprint are inspectable for diagnostics."""
    loc = _remote()
    fp = _fingerprint()
    adapter = LanceDBRetrievalAdapter(index_location=loc, expected_fingerprint=fp)
    assert adapter.index_location is loc
    assert adapter.expected_fingerprint == fp
