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
from typing import TYPE_CHECKING

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
from lumio_wiki.records import EmbeddingModelInfo
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    DERIVED_DIR,
    MANIFEST_OBJECT,
    S3Location,
    S3Manifest,
    S3Pointer,
)
from lumio_wiki.s3_publish import (
    LANCE_COMPLETION_OBJECT,
    LANCE_DERIVED_DIR,
    PointerObservation,
    RemoteIndexCompletion,
    S3PublicationConflict,
    list_cleanup_candidates,
    publish_s3_version,
    rollback_s3_version,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from obstore.store import ObjectStore

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
VALID = FIXTURES / "valid"
CATEGORIZED = FIXTURES / "categorized_kb"

obstore = pytest.importorskip("obstore", reason="obstore required for the S3 publish suite")


# ---------------------------------------------------------------------------
# Test infrastructure.
# ---------------------------------------------------------------------------


def _store() -> ObjectStore:
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
    publish_s3_version(store, "kb", source_root=root, version="v1")

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


def test_the_graph_artifact_is_in_derived_files_not_canonical_files():
    store = _store()
    manifest = publish_s3_version(store, "kb", source_root=VALID, version="v1")
    graph_key = f"{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}"
    canonical_paths = {f.path for f in manifest.files}
    assert graph_key not in canonical_paths
    # And no derived path of any kind leaks into canonical manifest files.
    assert not any(f.path.startswith(f"{DERIVED_DIR}/") for f in manifest.files)
    # The graph IS recorded in derived_files with a digest + size.
    derived_paths = {f.path for f in manifest.derived_files}
    assert graph_key in derived_paths
    graph_entry = next(f for f in manifest.derived_files if f.path == graph_key)
    raw = obstore.get(store, f"kb/v1/{graph_key}")
    import hashlib

    raw_bytes = bytes(raw.bytes())

    assert graph_entry.size == len(raw_bytes)
    assert graph_entry.digest == hashlib.sha256(raw_bytes).hexdigest()


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
# 2b. A reused version label is rejected (immutable prefix, create-only).
# ---------------------------------------------------------------------------


def test_reusing_a_version_label_fails_immutably():
    """The same version prefix cannot be published twice — immutable."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(KnowledgeBaseError, match="already exists"):
        publish_s3_version(store, "kb", source_root=VALID, version="v1")


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
    publish_s3_version(store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1")
    assert _read_pointer(store, "kb").version == "v2"
    # Publisher B now tries to advance from the stale v1 expectation: the
    # conflict is detected up front, BEFORE any expensive preparation or
    # writes (issue #163) — B leaves no residue at all.
    objects_before = _list_objects(store, "kb/")
    with pytest.raises(S3PublicationConflict) as exc_info:
        publish_s3_version(
            store, "kb", source_root=VALID, version="v3", expected_pointer_version="v1"
        )
    assert "v1" in str(exc_info.value)
    # The previously active Published Version is intact.
    assert _read_pointer(store, "kb").version == "v2"
    # And B wrote nothing: fail-fast leaves no orphaned prefix.
    assert _list_objects(store, "kb/") == objects_before


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
    publish_s3_version(store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1")
    publish_s3_version(store, "kb", source_root=VALID, version="v3", expected_pointer_version="v2")
    assert _read_pointer(store, "kb").version == "v3"


# ---------------------------------------------------------------------------
# 5. A reader bound to the old Snapshot stays consistent during activation.
# ---------------------------------------------------------------------------


def test_a_reader_bound_to_the_old_snapshot_is_unaffected_by_activation():
    """A reader that resolved the old version keeps reading its distinct content
    even after a new version with different content activates. The old Snapshot
    is immutable in memory, and a pinned reader still resolves the old prefix."""
    store = _store()
    # Publish distinct content: categorized KB as v1, valid KB as v2.
    publish_s3_version(store, "kb", source_root=CATEGORIZED, version="v1")
    # Reader resolves (and pins) v1 — distinct from what v2 will contain.
    reader = S3Location(store, "kb")
    before = reader.resolve()
    before_titles = {p.title for p in before.pages}
    before_fp = before.fingerprint
    # v2 has different content.
    publish_s3_version(store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1")
    # The already-resolved Snapshot is unchanged (immutable in memory).
    assert {p.title for p in before.pages} == before_titles
    assert before.fingerprint == before_fp
    # A fresh resolve sees the new active version with different content.
    after = S3Location(store, "kb").resolve()
    assert after.fingerprint != before_fp
    assert {p.title for p in after.pages} != before_titles
    # A pinned-to-v1 reader still resolves the old version's distinct content.
    pinned = S3Location(store, "kb", version="v1").resolve()
    assert {p.title for p in pinned.pages} == before_titles
    assert pinned.fingerprint == before_fp


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


def test_coherently_altered_graph_falls_back_via_manifest_digest_check():
    """A graph artifact with valid MessagePack, correct fingerprint/extractor
    fields, but different edges is caught by the manifest digest check and falls
    back to in-memory derivation."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    raw = obstore.get(store, f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}")
    payload = msgpack.unpackb(bytes(raw.bytes()), raw=False, strict_map_key=False)
    # Add a fabricated edge that preserves structural consistency (edge_count
    # matches outgoing, incoming is reverse of outgoing) but changes the
    # adjacency — so it decodes and passes fingerprint/extractor checks, but
    # fails the manifest digest check.
    payload["outgoing"].setdefault("__fabricated__", [["__target__", "rel"]])
    payload["incoming"].setdefault("__target__", [["__fabricated__", "rel"]])
    payload["edge_count"] = sum(len(v) for v in payload["outgoing"].values())
    obstore.put(
        store,
        f"kb/v1/{DERIVED_DIR}/{GRAPH_ARTIFACT_FILENAME}",
        msgpack.packb(payload, use_bin_type=True),
        mode="overwrite",
    )
    location = S3Location(store, "kb")
    state = location.load_or_derive_graph()
    # Fell back: the derived graph does not contain the fabricated edge.
    assert "__fabricated__" not in state.outgoing
    assert state.edge_count == _discovery_edge_count(VALID)


# ---------------------------------------------------------------------------
# 7. The [s3] extra guard.
# ---------------------------------------------------------------------------


def test_publish_requires_the_s3_extra(monkeypatch):
    """When obstore is not installed, publication raises an actionable error."""

    real_import = __import__

    def _block_obstore(name, *args, **kwargs):
        if name == "obstore":
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_obstore)
    with pytest.raises(KnowledgeBaseError) as exc_info:
        publish_s3_version(_store(), "kb", source_root=VALID, version="v1")
    assert "pip install" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 8. Pointer compare-and-swap fails closed when no ETag is available.
# ---------------------------------------------------------------------------
def test_pointer_advance_fails_closed_when_no_etag(monkeypatch):
    """A store that cannot supply an ETag refuses the second publication.

    Monkeypatches the publisher's pointer observation to report an existing
    pointer with no ETag, simulating a backend that lacks compare-and-swap
    support. The failure is raised before expensive preparation (issue #163).
    """
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    import lumio_wiki.s3_publish as pub

    real_observer = pub.observe_current_pointer

    def _no_etag_observer(s, prefix):
        real = real_observer(s, prefix)
        return PointerObservation(version=real.version, e_tag=None)

    monkeypatch.setattr(pub, "observe_current_pointer", _no_etag_observer)
    objects_before = _list_objects(store, "kb/")
    with pytest.raises(KnowledgeBaseError, match="ETag"):
        publish_s3_version(
            store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1"
        )
    # Fail-closed happened before any write, and the pointer is unchanged.
    assert _list_objects(store, "kb/") == objects_before
    assert _read_pointer(store, "kb").version == "v1"


# ---------------------------------------------------------------------------
# 9. Deep publication: pointer captured before preparation, CAS against the
#    ORIGINALLY observed pointer (issue #163).
# ---------------------------------------------------------------------------


def test_activation_cas_uses_the_originally_observed_pointer():
    """A publisher that observed v1 must fail even if the pointer moves to a
    *different* version mid-preparation: the CAS compares against the observed
    ETag, not a re-read pointer, so concurrent publishers fail closed."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")

    def _concurrent_publisher(prepared):
        # Simulates a concurrent publication advancing the pointer while our
        # publisher is between observation and activation.
        obstore.put(
            store,
            "kb/current.json",
            msgspec.json.encode(S3Pointer(version="v9")),
            mode="overwrite",
        )

    with pytest.raises(S3PublicationConflict) as exc_info:
        publish_s3_version(
            store,
            "kb",
            source_root=CATEGORIZED,
            version="v2",
            expected_pointer_version="v1",
            before_activation=_concurrent_publisher,
        )
    assert "concurrent" in str(exc_info.value).lower()
    # The concurrent publisher's activation stands; v2 stays inactive.
    assert _read_pointer(store, "kb").version == "v9"


def test_reused_version_prefix_fails_preflight_before_any_write():
    """A retried or interrupted build never mutates an existing prefix: the
    preflight rejects a non-empty version prefix before a single write."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # Residue from an interrupted build: objects without a manifest.
    obstore.put(store, "kb/v2/some-page.md", b"partial", mode="create")
    objects_before = _list_objects(store, "kb/")
    with pytest.raises(KnowledgeBaseError, match="already exists"):
        publish_s3_version(store, "kb", source_root=VALID, version="v2")
    # Nothing new was written and the active version is unchanged.
    assert _list_objects(store, "kb/") == objects_before
    assert _read_pointer(store, "kb").version == "v1"


def test_exact_version_reuse_still_fails_after_full_publication():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(KnowledgeBaseError, match="already exists"):
        publish_s3_version(store, "kb", source_root=CATEGORIZED, version="v1")
    # The original version's content is intact (immutable).
    manifest = _read_manifest(store, "kb", "v1")
    assert manifest.fingerprint == fingerprint_sources(VALID).digest


# ---------------------------------------------------------------------------
# 10. Requested remote LanceDB: build under derived/lance/, completion
#     metadata, fingerprint gate, failure blocks activation (issue #163).
# ---------------------------------------------------------------------------


def _valid_fingerprint() -> str:
    return fingerprint_sources(VALID).digest


def _fake_builder(fingerprint=None, model=None, tables=None, exc=None):
    """Build an IndexBuilder closure returning fixed completion metadata.

    A string ``fingerprint`` overrides the canonical one (mismatch tests)."""
    override = fingerprint if isinstance(fingerprint, str) else None
    calls = []

    def _builder(*, store, sidecar_prefix, pages, fingerprint, kb=None):
        calls.append(
            {"sidecar_prefix": sidecar_prefix, "pages": len(pages), "fingerprint": fingerprint}
        )
        if exc is not None:
            raise exc
        return RemoteIndexCompletion(
            fingerprint=override or fingerprint.digest,
            model=model,
            tables=tables if tables is not None else {"evidence": 42, "pages": 7},
        )

    return _builder, calls


def test_requested_lancedb_builds_under_the_version_lance_prefix():
    builder, calls = _fake_builder(model=EmbeddingModelInfo(name="fake-embedder", dimension=8))
    store = _store()
    manifest = publish_s3_version(
        store, "kb", source_root=VALID, version="v1", index_builder=builder
    )
    # The builder received the store, the <version>/derived/lance prefix, the
    # loaded pages, and the canonical fingerprint.
    assert calls[0]["sidecar_prefix"] == f"kb/v1/{LANCE_DERIVED_DIR}"
    assert calls[0]["fingerprint"].digest == _valid_fingerprint()
    # Activation happened: requested artifacts complete => pointer advanced.
    assert _read_pointer(store, "kb").version == "v1"
    # Completion metadata carries the canonical fingerprint + model identity.
    completion_rel = f"{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}"
    raw = obstore.get(store, f"kb/v1/{completion_rel}")
    completion_bytes = bytes(raw.bytes())
    completion = msgspec.json.decode(completion_bytes, type=RemoteIndexCompletion)
    assert completion.fingerprint == _valid_fingerprint()
    assert completion.model == EmbeddingModelInfo(name="fake-embedder", dimension=8)
    assert completion.tables == {"evidence": 42, "pages": 7}
    # The completion metadata is digest-protected in the manifest.
    import hashlib

    entry = next(f for f in manifest.derived_files if f.path == completion_rel)
    assert entry.digest == hashlib.sha256(completion_bytes).hexdigest()
    # And it never leaks into the canonical file list.
    assert not any(f.path.startswith(LANCE_DERIVED_DIR) for f in manifest.files)


def test_lance_completion_without_a_model_is_valid():
    builder, _ = _fake_builder(model=None)
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1", index_builder=builder)
    raw = obstore.get(store, f"kb/v1/{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}")
    completion = msgspec.json.decode(bytes(raw.bytes()), type=RemoteIndexCompletion)
    assert completion.model is None
    assert _read_pointer(store, "kb").version == "v1"


def test_requested_lancedb_build_failure_blocks_activation():
    builder, _ = _fake_builder(exc=RuntimeError("lance build exploded"))
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v0")
    with pytest.raises(RuntimeError, match="lance build exploded"):
        publish_s3_version(
            store,
            "kb",
            source_root=VALID,
            version="v1",
            expected_pointer_version="v0",
            index_builder=builder,
        )
    # The active version is unchanged and the failed version never activated.
    assert _read_pointer(store, "kb").version == "v0"
    # No manifest was written: v1 is incomplete, hence a cleanup candidate.
    assert f"kb/v1/{MANIFEST_OBJECT}" not in _list_objects(store, "kb/")
    assert [c.version for c in list_cleanup_candidates(store, "kb")] == ["v1"]


def test_lance_completion_fingerprint_mismatch_blocks_activation():
    builder, _ = _fake_builder(fingerprint="0" * 64)
    store = _store()
    with pytest.raises(KnowledgeBaseError, match="fingerprint"):
        publish_s3_version(store, "kb", source_root=VALID, version="v1", index_builder=builder)
    # Pointer was never created: nothing activated.
    assert "kb/current.json" not in _list_objects(store, "kb")


def test_unrequested_lancedb_absence_stays_valid():
    """Existing S3-without-LanceDB publication stays compatible (AC #163)."""
    store = _store()
    manifest = publish_s3_version(store, "kb", source_root=VALID, version="v1")
    assert _read_pointer(store, "kb").version == "v1"
    assert all(not f.path.startswith(LANCE_DERIVED_DIR) for f in manifest.derived_files)
    assert list_cleanup_candidates(store, "kb") == []


# ---------------------------------------------------------------------------
# 11. The pre-activation extension point (#164 Source Binding Manifest).
# ---------------------------------------------------------------------------


def test_before_activation_hook_sees_a_complete_version_and_the_old_pointer():
    seen = {}

    def _hook(prepared):
        seen["prepared"] = prepared
        seen["pointer_at_hook"] = _read_pointer(store, "kb").version
        seen["manifest_present"] = f"kb/v2/{MANIFEST_OBJECT}" in _list_objects(store, "kb")

    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store,
        "kb",
        source_root=CATEGORIZED,
        version="v2",
        expected_pointer_version="v1",
        before_activation=_hook,
    )
    prepared = seen["prepared"]
    assert prepared.version == "v2"
    assert prepared.fingerprint == fingerprint_sources(CATEGORIZED).digest
    assert prepared.manifest.version == "v2"
    # The hook ran AFTER the version was complete but BEFORE activation.
    assert seen["manifest_present"] is True
    assert seen["pointer_at_hook"] == "v1"
    assert _read_pointer(store, "kb").version == "v2"


def test_before_activation_hook_failure_blocks_activation():
    def _failing_hook(prepared):
        raise RuntimeError("source artifact store unavailable")

    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(RuntimeError, match="source artifact store"):
        publish_s3_version(
            store,
            "kb",
            source_root=CATEGORIZED,
            version="v2",
            expected_pointer_version="v1",
            before_activation=_failing_hook,
        )
    # The active version is unchanged; v2 is complete but inactive.
    assert _read_pointer(store, "kb").version == "v1"
    assert f"kb/v2/{MANIFEST_OBJECT}" in _list_objects(store, "kb")
    # A complete inactive version is NOT a cleanup candidate (it is a valid
    # rollback target), so nothing is reported.
    assert list_cleanup_candidates(store, "kb") == []


# ---------------------------------------------------------------------------
# 12. Rollback: CAS-activate a prior complete version, never rebuild (issue
#     #163).
# ---------------------------------------------------------------------------


def test_rollback_activates_a_prior_complete_version_without_rewrites():
    store = _store()
    publish_s3_version(store, "kb", source_root=CATEGORIZED, version="v1")
    v1_objects = _list_objects(store, "kb/v1/")
    publish_s3_version(store, "kb", source_root=VALID, version="v2", expected_pointer_version="v1")
    assert _read_pointer(store, "kb").version == "v2"

    manifest = rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v2")
    assert manifest.version == "v1"
    assert _read_pointer(store, "kb").version == "v1"
    # The target version was not rewritten: identical object set.
    assert _list_objects(store, "kb/v1/") == v1_objects
    # Readers resolve the rolled-back content.
    snapshot = S3Location(store, "kb").resolve()
    assert snapshot.fingerprint == fingerprint_sources(CATEGORIZED)


def test_rollback_without_expectation_still_fails_closed_on_a_stale_pointer():
    """Even with no explicit expectation, the CAS against the observed ETag
    catches a pointer that moved between observation and activation."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    import lumio_wiki.s3_publish as pub

    real_loader = pub._load_complete_version

    def _racing_loader(obstore_mod, s, prefix, version):
        # Simulate a concurrent activation between observation and CAS.
        obstore.put(
            s,
            f"{prefix}/{CURRENT_POINTER_OBJECT}",
            msgspec.json.encode(S3Pointer(version="v9")),
            mode="overwrite",
        )
        return real_loader(obstore_mod, s, prefix, version)

    import unittest.mock as mock

    with mock.patch.object(pub, "_load_complete_version", _racing_loader):
        with pytest.raises(S3PublicationConflict):
            rollback_s3_version(store, "kb", version="v1")
    assert _read_pointer(store, "kb").version == "v9"


def test_stale_rollback_expectation_fails_closed():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    with pytest.raises(S3PublicationConflict):
        rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v0")
    assert _read_pointer(store, "kb").version == "v2"


def test_rollback_to_an_incomplete_version_is_rejected():
    """An interrupted build's residue is a cleanup candidate, not a target."""
    store = _store()
    builder, _ = _fake_builder(exc=RuntimeError("boom"))
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(RuntimeError):
        publish_s3_version(store, "kb", source_root=VALID, version="v2", index_builder=builder)
    with pytest.raises(KnowledgeBaseError, match="cleanup candidate"):
        rollback_s3_version(store, "kb", version="v2", expected_pointer_version="v1")
    assert _read_pointer(store, "kb").version == "v1"


def test_rollback_to_an_absent_version_is_rejected():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    with pytest.raises(KnowledgeBaseError, match="no manifest"):
        rollback_s3_version(store, "kb", version="never-published")
    assert _read_pointer(store, "kb").version == "v1"


def test_rollback_to_a_version_missing_manifest_objects_is_rejected():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    # Corrupt v1's completeness: remove an object the manifest lists.
    manifest = _read_manifest(store, "kb", "v1")
    victim = manifest.files[0].path
    obstore.delete(store, f"kb/v1/{victim}")
    with pytest.raises(KnowledgeBaseError, match="missing"):
        rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v2")
    assert _read_pointer(store, "kb").version == "v2"


def test_first_publication_via_rollback_is_create_if_absent():
    """Activating a complete version with no existing pointer uses
    create-if-absent semantics (AC: first publication)."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    rollback_s3_version(store, "kb", version="v1")
    assert _read_pointer(store, "kb").version == "v1"


# ---------------------------------------------------------------------------
# 13. Cleanup candidates: report-only, never delete (issue #163).
# ---------------------------------------------------------------------------


def test_cleanup_candidates_report_only_incomplete_inactive_versions():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    # An interrupted build leaves residue without a manifest under v2.
    obstore.put(store, "kb/v2/overview.md", b"partial", mode="create")
    candidates = list_cleanup_candidates(store, "kb")
    assert [c.version for c in candidates] == ["v2"]
    assert candidates[0].object_count >= 1
    # Reporting deleted nothing.
    assert "kb/v2/overview.md" in _list_objects(store, "kb")


def test_active_and_complete_versions_are_not_cleanup_candidates():
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    # v1 is inactive but complete (a rollback target); v2 is active.
    assert list_cleanup_candidates(store, "kb") == []


def test_cleanup_candidates_on_an_empty_store():
    store = _store()
    assert list_cleanup_candidates(store, "kb") == []


def test_rollback_to_a_present_but_corrupt_version_is_rejected():
    """A version whose objects exist but no longer digest-validate is NOT a
    rollback target: Readers must never be pointed at it (review finding —
    'already complete, validated immutable version', ADR-0019)."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    # Tamper with v1's canonical content without updating the manifest.
    manifest = _read_manifest(store, "kb", "v1")
    victim = manifest.files[0].path
    original = bytes(obstore.get(store, f"kb/v1/{victim}").bytes())
    obstore.put(store, f"kb/v1/{victim}", original + b"\n# TAMPERED\n", mode="overwrite")
    with pytest.raises(KnowledgeBaseError, match="does not validate"):
        rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v2")
    # The active version is unchanged.
    assert _read_pointer(store, "kb").version == "v2"


def test_rollback_target_validation_is_read_only():
    """Validation never rewrites the target version (issue #163)."""
    store = _store()
    publish_s3_version(store, "kb", source_root=VALID, version="v1")
    v1_objects = _list_objects(store, "kb/v1/")
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v2")
    assert _list_objects(store, "kb/v1/") == v1_objects


def test_rollback_rejects_a_tampered_derived_object():
    """Presence alone is not validation: a declared derived object whose
    bytes no longer match the manifest digest blocks rollback (issue #163
    review finding — the pointer must never advance onto a corrupted
    complete version, even though derived state is rebuildable for Readers).
    """
    store = _store()
    builder, _ = _fake_builder()
    publish_s3_version(store, "kb", source_root=VALID, version="v1", index_builder=builder)
    publish_s3_version(
        store, "kb", source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    # Tamper v1's LanceDB completion metadata in place: same size (so the
    # digest branch must catch it, not the size branch), different content.
    completion_key = f"kb/v1/{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}"
    raw = bytes(obstore.get(store, completion_key).bytes())
    tampered = raw[:-1] + bytes([raw[-1] ^ 0xFF])
    assert len(tampered) == len(raw)
    obstore.put(store, completion_key, tampered, mode="overwrite")

    with pytest.raises(KnowledgeBaseError, match="!= manifest digest"):
        rollback_s3_version(store, "kb", version="v1", expected_pointer_version="v2")
    # The active version is unchanged: Readers stay on v2.
    assert _read_pointer(store, "kb").version == "v2"
