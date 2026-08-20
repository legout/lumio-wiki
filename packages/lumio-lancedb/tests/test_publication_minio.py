"""MinIO integration: complete S3 publication with a remote LanceDB index.

Issue #163. Proves the full publication journey against a real S3-compatible
endpoint: ``publish_s3_version`` with ``lumio_lancedb.remote_publication_builder``
builds the lexical index under ``{version}/derived/lance/``, health-checks it,
the publisher records completion metadata, activation advances the pointer —
and a Reader retrieves through the published remote index. Rollback
CAS-activates the prior version without touching its objects.

Skips unless ``LUMIO_S3_ENDPOINT`` is configured.
"""

from __future__ import annotations

import os as _os
import uuid
from pathlib import Path

import msgspec
import pytest

ROOT = Path(__file__).parents[3]

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")
pytest.importorskip("lumio_lancedb", reason="lumio-lancedb required for publication builder")

if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping remote LanceDB publication MinIO tests",
        allow_module_level=True,
    )

from lumio_lancedb import LanceDBRetrievalAdapter, RemoteIndexLocation, remote_publication_builder  # noqa: E402
from lumio_wiki.knowledge_base import fingerprint_sources  # noqa: E402
from lumio_wiki.s3_location import CURRENT_POINTER_OBJECT, S3Location, S3Pointer  # noqa: E402
from lumio_wiki.s3_publish import (  # noqa: E402
    LANCE_COMPLETION_OBJECT,
    LANCE_DERIVED_DIR,
    list_cleanup_candidates,
    publish_s3_version,
    rollback_s3_version,
)

FIXTURES = ROOT / "tests" / "fixtures"
VALID = FIXTURES / "valid"


def _config():
    region = _os.environ.get("LUMIO_S3_REGION", "us-east-1")
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]
    config = {
        "aws_region": region,
        "aws_endpoint": endpoint,
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options: dict[str, object] = {}
    storage_options = {
        "region": region,
        "endpoint": endpoint,
        "access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    if endpoint.startswith("http://"):
        client_options["allow_http"] = True
        storage_options["allow_http"] = "true"
    return config, client_options, storage_options


@pytest.fixture()
def kb():
    """A unique Knowledge Base prefix with a store + LanceDB builder factory."""
    config, client_options, storage_options = _config()
    bucket = _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")
    prefix = f"pub-it/{uuid.uuid4().hex}"
    store = obstore.store.from_url(
        f"s3://{bucket}", config=config, client_options=client_options
    )

    def builder():
        return remote_publication_builder(
            store_uri=f"s3://{bucket}", storage_options=storage_options
        )

    yield store, prefix, builder, storage_options
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


def test_minio_publish_with_remote_lancedb_builds_healthchecks_and_activates(kb):
    """The complete journey: publish v1 WITH a requested remote LanceDB index,
    verify completion metadata and the index, then retrieve through it."""
    store, prefix, builder, storage_options = kb

    manifest = publish_s3_version(
        store, prefix, source_root=VALID, version="v1", index_builder=builder()
    )
    assert _pointer(store, prefix) == "v1"

    # The completion metadata sits under the version's derived/lance/ area.
    raw = obstore.get(store, f"{prefix}/v1/{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}")
    completion_bytes = bytes(raw.bytes())
    import json

    completion = json.loads(completion_bytes)
    assert completion["fingerprint"] == fingerprint_sources(VALID).digest
    assert completion["model"] is None  # lexical-only publication path
    assert completion["tables"]["evidence"] > 0
    assert completion["tables"]["pages"] > 0
    # The completion metadata is digest-protected in the manifest.
    entry = next(
        f for f in manifest.derived_files
        if f.path == f"{LANCE_DERIVED_DIR}/{LANCE_COMPLETION_OBJECT}"
    )
    import hashlib

    assert entry.digest == hashlib.sha256(completion_bytes).hexdigest()

    # A Reader retrieves Evidence through the published remote index.
    snapshot = S3Location(store, prefix).resolve()
    lance_loc = RemoteIndexLocation(
        f"s3://{_os.environ.get('LUMIO_S3_TEST_BUCKET', 'lumio-wiki-it')}/{prefix}/v1/{LANCE_DERIVED_DIR}",
        storage_options=storage_options,
        store=store,
        sidecar_prefix=f"{prefix}/v1/{LANCE_DERIVED_DIR}",
    )
    adapter = LanceDBRetrievalAdapter(
        index_location=lance_loc, expected_fingerprint=snapshot.fingerprint
    )
    results = adapter.retrieve(
        snapshot.pages, "Lumio", limit=5, mode="lexical"
    )
    assert results, "remote index retrieval must answer over the published snapshot"
    assert results[0].trace.stages[0].name != "index-fallback"


def test_minio_requested_lancedb_failure_blocks_activation(kb):
    """A failing index build (or health check) leaves the pointer untouched
    and the version reported as a cleanup candidate — against MinIO."""
    store, prefix, builder, _ = kb
    # Publish a healthy v1 first (zero-index), then fail the v2 lance build.
    publish_s3_version(store, prefix, source_root=VALID, version="v1")

    def _exploding_builder(**_kwargs):
        raise RuntimeError("simulated lance health failure")

    with pytest.raises(RuntimeError, match="simulated lance health failure"):
        publish_s3_version(
            store,
            prefix,
            source_root=VALID,
            version="v2",
            expected_pointer_version="v1",
            index_builder=_exploding_builder,
        )
    assert _pointer(store, prefix) == "v1"
    candidates = list_cleanup_candidates(store, prefix)
    assert [c.version for c in candidates] == ["v2"]


def test_minio_rollback_to_a_lancedb_version_keeps_the_index_bound(kb):
    """Rollback re-activates a version whose lance index still serves."""
    store, prefix, builder, storage_options = kb
    publish_s3_version(
        store, prefix, source_root=VALID, version="v1", index_builder=builder()
    )
    # v2 without lance stays valid (back-compat), then roll back to v1.
    publish_s3_version(
        store, prefix, source_root=VALID, version="v2", expected_pointer_version="v1"
    )
    assert _pointer(store, prefix) == "v2"
    rollback_s3_version(store, prefix, version="v1", expected_pointer_version="v2")
    assert _pointer(store, prefix) == "v1"

    # The rolled-back version's remote index still serves retrieval.
    snapshot = S3Location(store, prefix).resolve()
    lance_loc = RemoteIndexLocation(
        f"s3://{_os.environ.get('LUMIO_S3_TEST_BUCKET', 'lumio-wiki-it')}/{prefix}/v1/{LANCE_DERIVED_DIR}",
        storage_options=storage_options,
        store=store,
        sidecar_prefix=f"{prefix}/v1/{LANCE_DERIVED_DIR}",
    )
    adapter = LanceDBRetrievalAdapter(
        index_location=lance_loc, expected_fingerprint=snapshot.fingerprint
    )
    results = adapter.retrieve(snapshot.pages, "Lumio", limit=5, mode="lexical")
    assert results
    assert results[0].trace.stages[0].name != "index-fallback"
