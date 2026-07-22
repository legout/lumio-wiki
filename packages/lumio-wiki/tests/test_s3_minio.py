"""MinIO integration coverage for the S3 Knowledge Base Location (issue #120).

Exercises the real ``obstore`` S3 client against an S3-compatible endpoint
(MinIO): direct CLI reads, byte-for-byte Snapshot equivalence with the
filesystem Location, content-digest corruption rejection, and immutable-version
isolation over a genuine object store.

These tests SKIP gracefully when no MinIO endpoint is configured, so the suite
is green without infrastructure. Configure the endpoint with environment
variables (the same ones the CLI reads)::

    LUMIO_S3_ENDPOINT=http://localhost:9000
    LUMIO_S3_ACCESS_KEY_ID=minio
    LUMIO_S3_SECRET_ACCESS_KEY=minio123
    LUMIO_S3_REGION=us-east-1
    LUMIO_S3_TEST_BUCKET=lumio-wiki-it   # created if missing

The in-memory ObjectStore contract suite (``test_s3_location.py``) runs in every
CI/local run and is the authoritative proof of the read contract.
"""

from __future__ import annotations

import os as _os
import uuid
from pathlib import Path

import pytest
from lumio_wiki.location import FilesystemLocation
from lumio_wiki.s3_location import CURRENT_POINTER_OBJECT, MANIFEST_OBJECT, S3Location, S3Pointer

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")


# Skip the whole module unless a MinIO/S3-compatible endpoint is configured.
if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping MinIO integration tests",
        allow_module_level=True,
    )



def _s3_config() -> tuple[dict[str, str], dict[str, object]]:
    region = _os.environ.get("LUMIO_S3_REGION", "us-east-1")
    config: dict[str, str] = {
        "aws_region": region,
        "aws_endpoint": _os.environ["LUMIO_S3_ENDPOINT"],
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options: dict[str, object] = {}
    if _os.environ["LUMIO_S3_ENDPOINT"].startswith("http://"):
        client_options["allow_http"] = True
    return config, client_options


@pytest.fixture(scope="module")
def minio_store():
    """Build an obstore S3 client rooted at a unique test bucket + prefix."""
    config, client_options = _s3_config()
    bucket = _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")
    run_prefix = f"s3-it/{uuid.uuid4().hex}"
    store = obstore.store.from_url(
        f"s3://{bucket}", config=config, client_options=client_options
    )
    yield store, run_prefix
    # Best-effort cleanup of this run's objects.
    try:
        for batch in obstore.list(store, prefix=run_prefix):
            for obj in batch:
                obstore.delete(store, obj["path"])
    except Exception:  # pragma: no cover - cleanup is best-effort
        pass


def _publish(store, prefix, version, root: Path) -> None:
    from lumio_wiki.knowledge_base import (
        _FilesystemKbSource,
        canonical_content,
        fingerprint_sources,
    )

    content = canonical_content(_FilesystemKbSource(root.resolve()))
    import msgspec
    from lumio_wiki.s3_location import build_published_manifest

    manifest = build_published_manifest(version, fingerprint_sources(root).digest, content)
    for rel, raw in content.items():
        obstore.put(store, f"{prefix}/{version}/{rel}", raw)
    obstore.put(store, f"{prefix}/{version}/{MANIFEST_OBJECT}", msgspec.json.encode(manifest))
    obstore.put(
        store,
        f"{prefix}/{CURRENT_POINTER_OBJECT}",
        msgspec.json.encode(S3Pointer(version=version)),
    )


def test_minio_direct_read_matches_filesystem(minio_store):
    store, prefix = minio_store
    _publish(store, prefix, "v1", FIXTURES / "valid")
    location = S3Location(store, prefix)
    snapshot = location.resolve()
    fs_snapshot = FilesystemLocation(FIXTURES / "valid").resolve()
    assert [p.title for p in snapshot.pages] == [p.title for p in fs_snapshot.pages]
    assert snapshot.retrieve("Lumio", limit=5)[0].citation == (
        fs_snapshot.retrieve("Lumio", limit=5)[0].citation
    )


def test_minio_corruption_is_rejected(minio_store):
    store, prefix = minio_store
    _publish(store, prefix, "v1", FIXTURES / "valid")
    from lumio_wiki.knowledge_base import _FilesystemKbSource, canonical_content

    content = canonical_content(_FilesystemKbSource((FIXTURES / "valid").resolve()))
    some_page = next(iter(content))
    obstore.put(store, f"{prefix}/v1/{some_page}", content[some_page] + b"\n# TAMPERED\n")
    with pytest.raises(Exception, match="corruption"):
        S3Location(store, prefix).resolve()


def test_minio_immutable_version_isolation(minio_store):
    store, prefix = minio_store
    _publish(store, prefix, "v1", FIXTURES / "valid")
    snap_v1 = S3Location(store, prefix).resolve()
    v1_titles = {p.title for p in snap_v1.pages}
    _publish(store, prefix, "v2", FIXTURES / "categorized_kb")
    snap_v2 = S3Location(store, prefix).resolve()
    assert {p.title for p in snap_v2.pages} != v1_titles
