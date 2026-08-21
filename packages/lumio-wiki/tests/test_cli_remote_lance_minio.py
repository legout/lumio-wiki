"""MinIO integration coverage for CLI remote-LanceDB binding (issue #162).

Proves the standalone ``lumio-wiki search`` command consumes the active S3
Published Version's remote LanceDB index through public seams only: publish a
real version (with and without ``--retrieval lancedb``), then run the CLI
against ``s3://bucket/prefix`` with ``LUMIO_RETRIEVAL_BACKEND=lancedb``.

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring
the other MinIO suites.
"""

from __future__ import annotations

import os as _os
import uuid
from pathlib import Path

import pytest
from lumio_wiki import cli

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")
pytest.importorskip("lumio_lancedb", reason="lumio-lancedb required for the CLI binding journey")

# Skip the whole module unless a MinIO/S3-compatible endpoint is configured.
if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping CLI remote LanceDB MinIO tests",
        allow_module_level=True,
    )

from lumio_lancedb import remote_publication_builder  # noqa: E402
from lumio_wiki.s3_publish import publish_s3_version  # noqa: E402


def _s3_config():
    region = _os.environ.get("LUMIO_S3_REGION", "us-east-1")
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]
    config = {
        "aws_region": region,
        "aws_endpoint": endpoint,
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options: dict[str, object] = {}
    if endpoint.startswith("http://"):
        client_options["allow_http"] = True
    # LanceDB storage options from the same credentials/region/endpoint
    # configuration as the canonical reads (issue #162, ADR-0013).
    storage_options = {
        "region": region,
        "endpoint": endpoint,
        "access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    if endpoint.startswith("http://"):
        storage_options["allow_http"] = "true"
    return config, client_options, storage_options


def _bucket() -> str:
    return _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")


@pytest.fixture(scope="module")
def minio_env():
    config, client_options, storage_options = _s3_config()
    bucket = _bucket()
    store = obstore.store.from_url(f"s3://{bucket}", config=config, client_options=client_options)
    yield store, bucket, storage_options
    # Best-effort cleanup of this run's objects.
    try:  # pragma: no cover - cleanup is best-effort
        for run in _RUN_PREFIXES:
            for batch in obstore.list(store, prefix=run):
                for obj in batch:
                    obstore.delete(store, obj["path"])
    except Exception:  # pragma: no cover
        pass


_RUN_PREFIXES: list[str] = []


def _publish(store, bucket, storage_options, prefix: str, *, with_lance: bool):
    """Publish the valid fixture KB (with or without a remote LanceDB index)."""
    source = FIXTURES / "valid"
    index_builder = None
    if with_lance:
        index_builder = remote_publication_builder(
            store_uri=f"s3://{bucket}", storage_options=storage_options
        )
    return publish_s3_version(
        store,
        prefix,
        source_root=source,
        version="v1",
        index_builder=index_builder,
    )


def _run_search(monkeypatch, bucket: str, prefix: str, query: str) -> int:
    """Run the real CLI search over the S3 URI with the lancedb backend configured."""
    monkeypatch.setenv("LUMIO_RETRIEVAL_BACKEND", "lancedb")
    rc = cli.main(["search", f"s3://{bucket}/{prefix}", query])
    assert rc == 0
    return rc


def test_cli_search_uses_published_remote_lancedb(monkeypatch, capsys, minio_env):
    """Positive journey: BM25 lexical search over the published remote index."""
    store, bucket, storage_options = minio_env
    run_prefix = f"s3-it/{uuid.uuid4().hex}"
    _RUN_PREFIXES.append(run_prefix)
    manifest = _publish(store, bucket, storage_options, run_prefix, with_lance=True)

    rc = _run_search(monkeypatch, bucket, run_prefix, "architecture")
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Architecture" in out
    assert "architecture.md" in out
    # Healthy path: no fallback disclosure.
    assert "note:" not in out
    # The published version is the one that was resolved.
    assert manifest.version == "v1"


def test_cli_search_falls_back_when_no_index_was_published(monkeypatch, capsys, minio_env):
    """Missing remote index: truthful zero-index fallback with a disclosed note."""
    store, bucket, storage_options = minio_env
    run_prefix = f"s3-it/{uuid.uuid4().hex}"
    _RUN_PREFIXES.append(run_prefix)
    _publish(store, bucket, storage_options, run_prefix, with_lance=False)

    rc = _run_search(monkeypatch, bucket, run_prefix, "architecture")
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Architecture" in out  # zero-index results still served
    assert "note:" in out and "missing" in out and "zero-index" in out


def test_cli_search_falls_back_on_fingerprint_mismatch(monkeypatch, capsys, minio_env):
    """A stale (tampered) fingerprint sidecar is rejected and disclosed."""
    import msgspec
    from lumio_wiki.fingerprint_store import FINGERPRINT_FILE
    from lumio_wiki.records import SourceFingerprint

    store, bucket, storage_options = minio_env
    run_prefix = f"s3-it/{uuid.uuid4().hex}"
    _RUN_PREFIXES.append(run_prefix)
    _publish(store, bucket, storage_options, run_prefix, with_lance=True)
    # Fault injection: overwrite the freshness sidecar with a foreign
    # fingerprint (fault injection mirrors the S3 contract suite's pattern).
    sidecar_key = f"{run_prefix}/v1/derived/lance/{FINGERPRINT_FILE}"
    obstore.put(store, sidecar_key, msgspec.json.encode(SourceFingerprint(digest="e" * 64)))

    rc = _run_search(monkeypatch, bucket, run_prefix, "architecture")
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Architecture" in out
    assert "note:" in out and "stale" in out and "fingerprint mismatch" in out
