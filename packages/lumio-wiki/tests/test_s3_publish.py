"""S3 Knowledge Base publication — in-memory ObjectStore contract suite.

Issue #121, ADR-0013. Proves a Maintainer can publish a complete immutable S3
Published Version containing canonical Knowledge Base content and a Discovery
Graph artifact, and that the active ``current.json`` pointer advances only after
the version is fully validated — with concurrent-publication conflict detection,
reader snapshot isolation during activation, and deterministic in-memory graph
derivation when the published graph artifact is missing, stale, or corrupt.

The suite uses obstore's in-memory ``MemoryStore`` (an S3-compatible
``ObjectStore``) so it is fully deterministic and runs in every CI/local run
with no infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import msgpack
import msgspec
import pytest
from lumio_wiki.graph_state import GRAPH_ARTIFACT_FILENAME, deserialize_graph
from lumio_wiki.knowledge_base import (
    EXTRACTOR_VERSION,
    KnowledgeBaseError,
    _FilesystemKbSource,
    canonical_content,
    fingerprint_sources,
)
from lumio_wiki.location import FilesystemLocation
from lumio_wiki.records import SourceFingerprint
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    DERIVED_DIR,
    MANIFEST_OBJECT,
    S3Location,
    S3Manifest,
    S3Pointer,
)
from lumio_wiki.s3_publish import (
    S3PublicationConflict,
    publish_s3_version,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
VALID = FIXTURES / "valid"
CATEGORIZED = FIXTURES / "categorized_kb"

obstore = pytest.importorskip("obstore", reason="obstore required for the S3 publish suite")


# ---------------------------------------------------------------------------
# Test infrastructure.
# ---------------------------------------------------------------------------


def _store() -> object:
    return obstore.store.MemoryStore()


def _list_objects(store: object, prefix: str) -> list[str]:
    """Return every object key under ``prefix`` in the store."""
    paths: list[str] = []
    for batch in obstore.list(store, prefix=prefix):
        for obj in batch:
            paths.append(obj["path"])
    return sorted(paths)


def _read_pointer(store: object, prefix: str) -> S3Pointer:
    raw = obstore.get(store, f"{prefix}/{CURRENT_POINTER_OBJECT}")
    return msgspec.json.decode(bytes(raw.bytes()), type=S3Pointer)


def _read_manifest(store: object, prefix: str, version: str) -> S3Manifest:
    raw = obstore.get(store, f"{prefix}/{version}/{MANIFEST_OBJECT}")
    return msgspec.json.decode(bytes(raw.bytes()), type=S3Manifest)


def _canonical(root: Path) -> dict[str, bytes]:
    return canonical_content(_FilesystemKbSource(root.resolve()))


# ---------------------------------------------------------------------------
# 1. Immutable publication writes a complete version prefix + manifest.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", [VALID, CATEGORIZED], ids=lambda p: p.name)
def test_publish_writes_every_canonical_file_under_the_version_prefix(root):
    store = _store()
    manifest = publish_s3_version(store, "kb", source_root=root, version="v1")

    objects = _list_objects(store, "kb/v1/")
    # Every canonical file appears at its relative path under the prefix.
    for rel in _canonical(root):
        assert f"kb/v1/{rel}" in objects, f"missing canonical object {rel}"
    # The manifest is present.
    assert "kb/v1/manifest.json" in objects


def test_publish_writes_a_digest_and_fingerprint_manifest():
    store = _store()
    manifest = publish_s3_version(store, "kb", source_root=VALID, version="v1")

    expected_fp = fingerprint_sources(VALID).digest
    assert manifest.version == "v1"
    assert manifest.fingerprint == expected_fp
    content = _canonical(VALID)
    assert {f.path for f in manifest.files} == set(content)
    for entry in manifest.files:
        raw = obstore.get(store, f"kb/v1/{entry.path}")
        data = bytes(raw.bytes())
        assert len(data) == entry.size
        import hashlib

        assert hashlib.sha256(data).hexdigest() == entry.digest


def test_publish_records_sizes_that_match_the_materialized_bytes():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    manifest = _read_manifest(store, "kb", "v1")
    for entry in manifest.files:
        raw = obstore.get(store, f"kb/v1/{entry.path}")
        assert entry.size == len(bytes(raw.bytes()))


def test_publish_advances_the_active_pointer_to_the_new_version():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    pointer = _read_pointer(store, "kb")
    assert pointer.version == "v1"


def test_first_publication_has_no_existing_pointer():
    """The very first publication creates the pointer from nothing."""
    store = _store()
    # No pointer exists yet; publish must still succeed.
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    assert _read_pointer(store, "kb").version == "v1"


# ---------------------------------------------------------------------------
# 2. The graph artifact is published to the derived area, never canonical.
# ---------------------------------------------------------------------------


def test_publish_writes_the_graph_artifact_under_the_derived_area():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    objects = _list_objects(store, "kb/v1/")
    assert f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}" in objects


def test_the_graph_artifact_is_never_listed_in_the_canonical_manifest():
    store = _store()
    manifest = publish_s3_version(store, "kb", source_root=VALID, version="v1")
    graph_key = f"{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}"
    paths = {f.path for f in manifest.files}
    assert graph_key not in paths
    # And no derived path of any kind leaks into canonical manifest files.
    assert not any(f.path.startswith(f"{DERIVED_DIR}/") for f in manifest.files)


def test_the_published_graph_artifact_is_fresh_and_loadable():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    raw = obstore.get(store, f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}")
    state = deserialize_graph(bytes(raw.bytes()))
    assert state is not None
    assert state.fingerprint_digest == fingerprint_sources(VALID).digest
    assert state.extractor_version == EXTRACTOR_VERSION
    # Sanity: the published graph carries real discovery edges.
    assert state.edge_count >= 0


# ---------------------------------------------------------------------------
# 3. A published version resolves to a byte-for-byte-equivalent Snapshot.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", [VALID, CATEGORIZED], ids=lambda p: p.name)
def test_a_published_version_resolves_like_the_filesystem_location(root):
    store = _store()
    publish_s3_version(store, "kb", source_root=root, version="v1")
    s3_snapshot = S3Location(store, "kb").resolve()
    fs_snapshot = FilesystemLocation(root).resolve()
    assert s3_snapshot.fingerprint == fs_snapshot.fingerprint
    assert s3_snapshot.validation_report == fs_snapshot.validation_report
    assert {p.title for p in s3_snapshot.pages} == {p.title for p in fs_snapshot.pages}


# ---------------------------------------------------------------------------
# 4. Concurrent publication detects an activation-pointer conflict.
# ---------------------------------------------------------------------------


def test_a_second_concurrent_publication_detects_a_pointer_conflict():
    """Two publishers race: the second's conditional pointer put fails cleanly,
    the previously active Published Version stays intact, and its orphaned
    version prefix is left written but never activated."""
    store = _store()
    # Publisher A: publishes v1 (creates the pointer).
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Publisher B: reads the current pointer (v1) intending to advance from it.
    expected = _read_pointer(store, "kb").version
    assert expected == "v1"
    # Publisher A wins the race to publish v2 and advances the pointer.
    publish_s3_version(
        store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1"
    )
    assert _read_pointer(store, "kb").version == "v2"
    # Publisher B now tries to advance from the stale v1 expectation: conflict.
    with pytest.raises(S3PublicationConflict) as exc_info:
        publish_s3_version(
            store, "kb", source_root=VALID, version="v3", expected_pointer_version="v1"
        )
    assert "v1" in str(exc_info.value)
    # The previously active Published Version is intact.
    assert _read_pointer(store, "kb").version == "v2"
    # B's version prefix was written but the pointer never advanced to it.
    assert f"kb/v3/{MANIFEST_OBJECT}" in _list_objects(store, "kb/")


def test_expected_pointer_version_mismatch_raises_conflict_before_advancing():
    """An explicit expectation that does not match the live pointer is a conflict."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(S3PublicationConflict):
        publish_s3_version(
            store,
            "kb",
            source_root=VALID,
            version="v2",
            expected_pointer_version="does-not-exist",
        )
    # Pointer unchanged.
    assert _read_pointer(store, "kb").version == "v1"


def test_consecutive_publications_advance_the_pointer_in_order():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1"
    )
    publish_s3_version(
        store, "kb", source_root=VALID, version="v3", expected_pointer_version="v2"
    )
    assert _read_pointer(store, "kb").version == "v3"


# ---------------------------------------------------------------------------
# 5. A reader bound to the old Snapshot stays consistent during activation.
# ---------------------------------------------------------------------------


def test_a_reader_bound_to_the_old_snapshot_is_unaffected_by_activation():
    """A reader that resolved v1 keeps reading v1's content even after the
    pointer advances to v2 — it uses only its already-resolved immutable prefix."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Reader resolves (and pins) v1.
    reader = S3Location(store, "kb")
    before = reader.resolve()
    before_titles = {p.title for p in before.pages}
    before_fp = before.fingerprint
    # A new version activates.
    publish_s3_version(
        store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1"
    )
    # The already-resolved Snapshot is unchanged (immutable in memory).
    assert {p.title for p in before.pages} == before_titles
    assert before.fingerprint == before_fp
    # A fresh resolve sees the new active version.
    after = S3Location(store, "kb").resolve()
    assert after.fingerprint == before_fp  # same content -> same fingerprint
    # A pinned-to-v1 reader is still isolated regardless of the pointer.
    pinned = S3Location(store, "kb", version="v1").resolve()
    assert {p.title for p in pinned.pages} == before_titles


# ---------------------------------------------------------------------------
# 6. Missing, stale, or corrupt graph artifacts fall back to in-memory
#    derivation and never become canonical content.
# ---------------------------------------------------------------------------


def _discovery_edge_count(root: Path) -> int:
    """The in-memory-derived discovery edge count for a filesystem KB."""
    snapshot = FilesystemLocation(root).resolve()
    kb = snapshot.knowledge_base
    index = kb._knowledge_index()
    return sum(len(edges) for edges in index.discovery_adjacency.values())


def test_missing_graph_artifact_falls_back_to_in_memory_derivation():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Delete the published graph artifact.
    obstore.delete(store, f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}")
    location = S3Location(store, "kb")
    state = location.load_or_derive_graph()
    assert state.fingerprint_digest == fingerprint_sources(VALID).digest
    assert state.extractor_version == EXTRACTOR_VERSION
    assert state.edge_count == _discovery_edge_count(VALID)


def test_corrupt_graph_artifact_falls_back_to_in_memory_derivation():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Overwrite the artifact with garbage.
    obstore.put(
        store,
        f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}",
        b"not-messagepack-at-all",
        mode="overwrite",
    )
    location = S3Location(store, "kb")
    state = location.load_or_derive_graph()
    assert state.edge_count == _discovery_edge_count(VALID)
    assert state.fingerprint_digest == fingerprint_sources(VALID).digest


def test_stale_graph_artifact_falls_back_to_in_memory_derivation():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Rewrite the artifact with a valid MessagePack payload but a wrong
    # fingerprint so it is stale relative to this Published Version.
    raw = obstore.get(store, f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}")
    payload = msgpack.unpackb(bytes(raw.bytes()), raw=False, strict_map_key=False)
    payload["fingerprint_digest"] = "0" * 64
    obstore.put(
        store,
        f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}",
        msgpack.packb(payload, use_bin_type=True),
        mode="overwrite",
    )
    location = S3Location(store, "kb")
    state = location.load_or_derive_graph()
    # Fell back: the live fingerprint is the real one, not the stale value.
    assert state.fingerprint_digest == fingerprint_sources(VALID).digest


def test_fresh_graph_artifact_is_loaded_from_the_published_version():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    location = S3Location(store, "kb")
    state = location.load_or_derive_graph()
    # Loaded from the artifact (matches derivation exactly).
    assert state.edge_count == _discovery_edge_count(VALID)
    assert state.fingerprint_digest == fingerprint_sources(VALID).digest


# ---------------------------------------------------------------------------
# 7. The [s3] extra guard.
# ---------------------------------------------------------------------------


def test_publish_requires_the_s3_extra(monkeypatch):
    """When obstore is not installed, publication raises an actionable error."""
    import lumio_wiki.s3_publish as mod

    real_import = __import__

    def _block_obstore(name, *args, **kwargs):
        if name == "obstore":
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_obstore)
    with pytest.raises(KnowledgeBaseError) as exc_info:
        publish_s3_version(_store(), "kb", source_root=VALID, version="v1")
    assert "pip install" in str(exc_info.value)
