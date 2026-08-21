"""Dependency-neutral remote derived-index descriptor on the Snapshot (#162).

The resolved S3 Snapshot — not each CLI caller — knows where its Published
Version's remote LanceDB index lives. This suite proves the Snapshot exposes a
dependency-neutral ``RemoteDerivedIndex`` descriptor (connect URI, sidecar
prefix, version, Published Version fingerprint, and the already-authenticated
object store) so the CLI can hand it to ``lumio-lancedb`` without
reconstructing object keys (ADR-0019, issue #162).

Uses obstore's in-memory ``MemoryStore``: deterministic, no infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest
from lumio_wiki.location import FilesystemLocation, RemoteDerivedIndex
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    MANIFEST_OBJECT,
    S3Location,
    S3Pointer,
    build_published_manifest,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

obstore = pytest.importorskip("obstore", reason="obstore required for the descriptor suite")


def _publish_version(store, prefix: str, version: str, root: Path) -> None:
    """Materialize the fixture KB under an immutable version prefix."""
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
        store, f"{prefix}/{CURRENT_POINTER_OBJECT}", msgspec.json.encode(S3Pointer(version=version))
    )


def _published_store():
    store = obstore.store.MemoryStore()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    return store


def test_filesystem_snapshot_has_no_remote_derived_index():
    snapshot = FilesystemLocation(FIXTURES / "valid").resolve()
    assert snapshot.remote_derived_index is None


def test_s3_snapshot_exposes_remote_derived_index_descriptor():
    store = _published_store()
    location = S3Location(store, "kb", store_uri="s3://bucket")
    snapshot = location.resolve()

    descriptor = snapshot.remote_derived_index
    assert isinstance(descriptor, RemoteDerivedIndex)
    assert descriptor.version == "v1"
    assert descriptor.uri == "s3://bucket/kb/v1/derived/lance"
    assert descriptor.sidecar_prefix == "kb/v1/derived/lance"
    assert descriptor.fingerprint.digest == snapshot.fingerprint.digest
    # The same authenticated object store the canonical reads used, so a
    # consumer never rebuilds one (issue #162: reuse credentials/region/endpoint).
    assert descriptor.store is store
    # Secret-free: the descriptor carries a URI, never credentials.
    assert "aws" not in descriptor.uri.lower()
    assert "@" not in descriptor.uri


def test_descriptor_absent_when_store_uri_unknown():
    store = _published_store()
    location = S3Location(store, "kb")  # direct construction, no container URI
    assert location.resolve().remote_derived_index is None


def test_from_url_records_store_uri_and_pinned_version_descriptor():
    """``from_url`` snapshots carry the descriptor for the pinned version."""
    store = _published_store()
    from lumio_wiki.s3_location import S3Location as _Loc

    # Build through the public from_url seam with an injected store is not
    # possible (from_url constructs its own), so pin and verify via kwargs the
    # same way from_url does: store_uri + explicit version.
    location = _Loc(store, "kb", version="v1", store_uri="s3://bucket")
    descriptor = location.resolve().remote_derived_index
    assert descriptor is not None
    assert descriptor.version == "v1"
    assert descriptor.uri.endswith("/kb/v1/derived/lance")


def test_descriptor_is_a_plain_dependency_neutral_record():
    """The record type is defined by lumio-wiki and imports no backend."""
    import lumio_wiki.location as location_module

    record = RemoteDerivedIndex(
        uri="s3://bucket/kb/v1/derived/lance",
        sidecar_prefix="kb/v1/derived/lance",
        version="v1",
        fingerprint=snapshot_fingerprint_stub(),
        store=None,
    )
    assert record.uri
    source = Path(location_module.__file__).read_text(encoding="utf-8")
    assert "lumio_lancedb" not in source
    assert "lancedb" not in source.lower()


def snapshot_fingerprint_stub():
    from lumio_wiki.records import SourceFingerprint

    return SourceFingerprint(digest="a" * 64)
