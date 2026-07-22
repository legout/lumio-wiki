"""MinIO integration coverage for remote LanceDB indexes (issue #122, ADR-0013).

Proves the enhanced-retrieval *contract* against a real S3-compatible endpoint:
build lexical + semantic indexes under an immutable Published Version's
``derived/lance/`` prefix, search them, validate the remote fingerprint /
embedding-model sidecars, and confirm the zero-index fallback when the index is
missing. This never tests LanceDB internals — only the citation-ready
Retrieval Result contract over a remote index.

Skips the whole module unless ``LUMIO_S3_ENDPOINT`` is configured, mirroring the
``lumio-wiki`` MinIO suite.
"""

from __future__ import annotations

import os as _os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]

obstore = pytest.importorskip("obstore", reason="obstore required for MinIO integration")

# Skip the whole module unless a MinIO/S3-compatible endpoint is configured.
if not _os.environ.get("LUMIO_S3_ENDPOINT"):  # pragma: no cover
    pytest.skip(
        "LUMIO_S3_ENDPOINT not set; skipping remote LanceDB MinIO tests",
        allow_module_level=True,
    )

from lumio_lancedb import (  # noqa: E402
    LanceDBRetrievalAdapter,
    RemoteIndexLocation,
    has_semantic_index,
)
from lumio_wiki.embeddings import EmbeddingError  # noqa: E402
from lumio_wiki.records import (  # noqa: E402
    CompiledPage,
    EmbeddingModelInfo,
    Relationship,
    SourceFingerprint,
)


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
    """Deterministic embedder matching the ``Embedder`` protocol."""

    def __init__(self, name="minio-fake", dimension=8):
        self._info = EmbeddingModelInfo(name=name, dimension=dimension)

    @property
    def model_info(self):
        return self._info

    def embed(self, texts):
        import hashlib

        out = []
        for text in texts:
            digest = hashlib.sha256((text + self._info.name).encode()).digest()
            vec = [(b - 128) / 128.0 for b in digest[: self._info.dimension]]
            while len(vec) < self._info.dimension:
                vec.append(0.0)
            out.append(vec[: self._info.dimension])
        return out


def _config_and_options():
    region = _os.environ.get("LUMIO_S3_REGION", "us-east-1")
    endpoint = _os.environ["LUMIO_S3_ENDPOINT"]
    config = {
        "aws_region": region,
        "aws_endpoint": endpoint,
        "aws_access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "aws_secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    client_options = {}
    if endpoint.startswith("http://"):
        client_options["allow_http"] = True
    # LanceDB connects to its own S3 URI with its own storage_options (ADR-0013).
    storage_options = {
        "region": region,
        "endpoint": endpoint,
        "access_key_id": _os.environ["LUMIO_S3_ACCESS_KEY_ID"],
        "secret_access_key": _os.environ["LUMIO_S3_SECRET_ACCESS_KEY"],
    }
    if endpoint.startswith("http://"):
        storage_options["allow_http"] = "true"
    return config, client_options, storage_options


@pytest.fixture(scope="module")
def remote_location():
    """Build a RemoteIndexLocation rooted at a unique test prefix in MinIO."""
    config, client_options, storage_options = _config_and_options()
    bucket = _os.environ.get("LUMIO_S3_TEST_BUCKET", "lumio-wiki-it")
    run_prefix = f"s3-it/{uuid.uuid4().hex}"
    store = obstore.store.from_url(
        f"s3://{bucket}", config=config, client_options=client_options
    )
    lance_prefix = f"{run_prefix}/derived/lance"
    loc = RemoteIndexLocation(
        f"s3://{bucket}/{lance_prefix}",
        storage_options=storage_options,
        store=store,
        sidecar_prefix=lance_prefix,
    )
    yield loc, store, lance_prefix
    # Best-effort cleanup of this run's objects.
    try:  # pragma: no cover - cleanup is best-effort
        for batch in obstore.list(store, prefix=run_prefix):
            for obj in batch:
                obstore.delete(store, obj["path"])
    except Exception:  # pragma: no cover
        pass


def test_minio_remote_lexical_build_and_search(remote_location):
    loc, store, prefix = remote_location
    pages = _pages()
    fp = SourceFingerprint(digest="minio-canonical-fingerprint")
    adapter = LanceDBRetrievalAdapter()

    adapter.build_index(pages, loc, fingerprint=fp)

    results = adapter.retrieve(pages, "LanceDB", limit=5, index_dir=loc)
    assert results, "remote lexical search must hit the fixture query"
    first = results[0]
    assert first.evidence.source_type == "compiled_markdown"
    assert first.citation.page_title
    assert first.snippet
    assert first.trace.stages


def test_minio_remote_fingerprint_sidecar_is_canonical(remote_location):
    """The remote index records the canonical fingerprint beside the tables."""
    loc, store, prefix = remote_location
    from lumio_lancedb.index import _load_fingerprint

    loaded = _load_fingerprint(loc)
    assert loaded is not None
    assert loaded.digest == "minio-canonical-fingerprint"

    # The sidecar object exists at the documented key in the object store.
    result = obstore.get(store, f"{prefix}/fingerprint.json")
    import msgspec

    assert (
        msgspec.json.decode(bytes(result.bytes()), type=SourceFingerprint).digest
        == "minio-canonical-fingerprint"
    )


def test_minio_remote_semantic_build_search_and_identity(remote_location):
    loc, store, prefix = remote_location
    pages = _pages()
    embedder = _FakeEmbedder()

    # Build the semantic vector index beside the lexical one.
    adapter = LanceDBRetrievalAdapter()
    adapter.build_index(pages, loc, embedder=embedder)
    assert has_semantic_index(loc) is True

    # The embedding-model identity is recorded remotely.
    from lumio_lancedb.index import _load_model

    assert _load_model(loc) == embedder.model_info

    semantic = adapter.retrieve(
        pages, "LanceDB", limit=5, index_dir=loc, mode="semantic",
        embedder=embedder, score_threshold=0.0,
    )
    hybrid = adapter.retrieve(
        pages, "LanceDB", limit=5, index_dir=loc, mode="hybrid",
        embedder=embedder, score_threshold=0.0,
    )
    assert isinstance(semantic, list)
    assert isinstance(hybrid, list)


def test_minio_remote_model_mismatch_is_rejected(remote_location):
    loc, store, prefix = remote_location
    pages = _pages()
    adapter = LanceDBRetrievalAdapter()
    adapter.build_index(pages, loc, embedder=_FakeEmbedder(name="model-a"))

    with pytest.raises(EmbeddingError):
        adapter.retrieve(
            pages, "LanceDB", limit=5, index_dir=loc, mode="semantic",
            embedder=_FakeEmbedder(name="model-b"),
        )


def test_minio_missing_remote_index_falls_back_to_zero_index(remote_location):
    """A prefix with no published index selects zero-index retrieval."""
    loc, store, prefix = remote_location
    # A distinct prefix that was never built.
    missing = RemoteIndexLocation(
        loc.uri + "-missing",
        storage_options=loc.storage_options,
        store=store,
        sidecar_prefix=loc.sidecar_prefix + "-missing",
    )
    adapter = LanceDBRetrievalAdapter()
    results = adapter.retrieve(_pages(), "LanceDB", limit=5, index_dir=missing)
    assert results, "missing remote index must fall back to zero-index retrieval"
    assert results[0].trace.stages[0].name == "index-fallback"
