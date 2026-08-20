"""MinIO integration coverage for complete S3 publication (issue #163).

Proves the *conditional activation* contract against a real S3-compatible
endpoint: compare-and-swap activation against the originally observed pointer,
explicit CAS rollback, and report-only cleanup candidates. The remote LanceDB
publication journey is covered by the ``lumio-lancedb`` MinIO suite; every
state transition is covered deterministically by the in-memory suite
(``test_s3_publish.py``).

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring
the other MinIO suites.
"""

from __future__ import annotations

import os as _os
import uuid
from pathlib import Path

import msgspec
import pytest
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    MANIFEST_OBJECT,
    S3Location,
    S3Pointer,
)
from lumio_wiki.s3_publish import (
    S3PublicationConflict,
    list_cleanup_candidates,
    publish_s3_version,
    rollback_s3_version,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
VALID = FIXTURES / "valid"
CATEGORIZED = FIXTURES / "categorized_kb"

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")

# Skip the whole module unless a MinIO/S3-compatible endpoint is configured.
if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping S3 publish MinIO tests",
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


def _new_store():
    """A fresh store client (independent session, like a separate publisher)."""
    config, client_options = _s3_config()
    bucket = _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")
    return obstore.store.from_url(
        f"s3://{bucket}", config=config, client_options=client_options
    )


@pytest.fixture()
def kb_prefix():
    """A unique Knowledge Base prefix per test, cleaned up best-effort."""
    store = _new_store()
    prefix = f"s3-publish-it/{uuid.uuid4().hex}"
    yield store, prefix
    try:  # pragma: no cover - cleanup is best-effort
        for batch in obstore.list(store, prefix=prefix):
            for obj in batch:
                obstore.delete(store, obj["path"])
        obstore.delete(store, f"{prefix}/{CURRENT_POINTER_OBJECT}")
    except Exception:  # pragma: no cover
        pass


def _pointer(store, prefix: str) -> str:
    raw = obstore.get(store, f"{prefix}/{CURRENT_POINTER_OBJECT}")
    return msgspec.json.decode(bytes(raw.bytes()), type=S3Pointer).version


def _list(store, prefix: str) -> list[str]:
    paths = []
    for batch in obstore.list(store, prefix=prefix):
        for obj in batch:
            paths.append(obj["path"])
    return sorted(paths)


# ---------------------------------------------------------------------------
# Conditional activation against the originally observed pointer.
# ---------------------------------------------------------------------------


def test_minio_two_publishers_conditional_activation_fails_closed(kb_prefix):
    """A publisher that observed v1 fails closed when another publisher
    advanced the pointer in between — against a real S3-compatible backend
    with real conditional puts and ETags."""
    store, prefix = kb_prefix
    publish_s3_version(store, prefix, source_root=VALID, version="v1")
    assert _pointer(store, prefix) == "v1"

    # Publisher B resolves the pointer (observes v1) from its own session.
    publisher_b = _new_store()
    observed = _pointer(publisher_b, prefix)
    assert observed == "v1"

    # Publisher A wins the race and advances to v2.
    publish_s3_version(
        store, prefix, source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    assert _pointer(store, prefix) == "v2"

    # Publisher B's activation now fails closed: the pointer moved.
    with pytest.raises(S3PublicationConflict):
        publish_s3_version(
            publisher_b,
            prefix,
            source_root=VALID,
            version="v3",
            expected_pointer_version=observed,
        )
    assert _pointer(store, prefix) == "v2"
    # Readers stay on A's active version (categorized content).
    from lumio_wiki.knowledge_base import fingerprint_sources

    snapshot = S3Location(store, prefix).resolve()
    assert snapshot.fingerprint == fingerprint_sources(CATEGORIZED)


def test_minio_sequential_publications_and_rollback(kb_prefix):
    """Publish two versions then CAS-rollback to the first against MinIO."""
    store, prefix = kb_prefix
    publish_s3_version(store, prefix, source_root=VALID, version="v1")
    v1_objects = _list(store, f"{prefix}/v1/")
    publish_s3_version(
        store, prefix, source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    assert _pointer(store, prefix) == "v2"

    manifest = rollback_s3_version(store, prefix, version="v1", expected_pointer_version="v2")
    assert manifest.version == "v1"
    assert _pointer(store, prefix) == "v1"
    # No object under the target version was rewritten.
    assert _list(store, f"{prefix}/v1/") == v1_objects
    # A reader resolves the rolled-back content.
    snapshot = S3Location(store, prefix).resolve()
    assert snapshot.fingerprint.digest == manifest.fingerprint


def test_minio_stale_rollback_fails_closed(kb_prefix):
    store, prefix = kb_prefix
    publish_s3_version(store, prefix, source_root=VALID, version="v1")
    publish_s3_version(
        store, prefix, source_root=CATEGORIZED, version="v2", expected_pointer_version="v1"
    )
    with pytest.raises(S3PublicationConflict):
        rollback_s3_version(store, prefix, version="v1", expected_pointer_version="v0")
    assert _pointer(store, prefix) == "v2"


def test_minio_cleanup_candidates_are_reported_not_deleted(kb_prefix):
    store, prefix = kb_prefix
    publish_s3_version(store, prefix, source_root=VALID, version="v1")
    # Simulate an interrupted build: residue without a manifest.
    obstore.put(store, f"{prefix}/v2/overview.md", b"partial", mode="create")
    candidates = list_cleanup_candidates(store, prefix)
    assert [c.version for c in candidates] == ["v2"]
    assert candidates[0].object_count >= 1
    # Report-only: the residue is untouched.
    assert f"{prefix}/v2/overview.md" in _list(store, prefix)
    assert f"{prefix}/v1/{MANIFEST_OBJECT}" in _list(store, prefix)


def test_minio_reused_version_prefix_is_rejected(kb_prefix):
    """A retried build never mutates an existing immutable version."""
    store, prefix = kb_prefix
    publish_s3_version(store, prefix, source_root=VALID, version="v1")
    with pytest.raises(Exception, match="already exists"):
        publish_s3_version(store, prefix, source_root=CATEGORIZED, version="v1")
    assert _pointer(store, prefix) == "v1"
