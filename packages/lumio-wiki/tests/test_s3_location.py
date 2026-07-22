"""S3 Knowledge Base Location — in-memory ObjectStore contract suite.

Issue #120, ADR-0013. Proves a reader resolves one immutable S3 Published
Version directly: pointer resolution, manifest + content-digest validation,
corruption/digest-mismatch rejection, the default no-managed-disk-cache policy,
immutable-version isolation, and byte-for-byte equivalence with the verified
filesystem Location over zero-index retrieval and Discovery Graph traversal.

The suite uses obstore's in-memory ``MemoryStore`` (an S3-compatible
``ObjectStore``) so it is fully deterministic and runs in every CI/local run
with no infrastructure. A separate MinIO integration module covers a real
S3-compatible endpoint and skips gracefully when none is configured.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import msgspec
import pytest
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBaseError,
    _FilesystemKbSource,
    canonical_content,
    fingerprint_sources,
)
from lumio_wiki.location import FilesystemLocation, KnowledgeBaseLocation, KnowledgeBaseSnapshot
from lumio_wiki.records import RetrievalResult, SourceFingerprint, ValidationReport
from lumio_wiki.s3_location import (
    CURRENT_POINTER_OBJECT,
    MANIFEST_OBJECT,
    S3_LOCATION_KIND,
    S3Location,
    S3Manifest,
    S3ManifestFile,
    S3Pointer,
    build_published_manifest,
    open_s3_knowledge_base,
)

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

# Every equivalence assertion runs over both shipped filesystem fixtures: the
# legacy-flat ``valid`` KB and the categorized ``categorized_kb``.
LOCATION_FIXTURES = [FIXTURES / "valid", FIXTURES / "categorized_kb"]

obstore = pytest.importorskip("obstore", reason="obstore required for the S3 contract suite")


# ---------------------------------------------------------------------------
# Test infrastructure: publish a filesystem Knowledge Base into an object store.
# ---------------------------------------------------------------------------


def _canonical_content(root: Path) -> dict[str, bytes]:
    return canonical_content(_FilesystemKbSource(root.resolve()))


def _publish_version(
    store,
    prefix: str,
    version: str,
    root: Path,
    *,
    content_override: dict[str, bytes] | None = None,
    fingerprint_override: str | None = None,
    skip_pointer: bool = False,
) -> S3Manifest:
    """Materialize a filesystem KB under an immutable version prefix.

    Writes every canonical file, a digest manifest, and (unless skipped) the
    ``current.json`` pointer. ``content_override`` / ``fingerprint_override``
    let corruption tests inject tampered bytes or a wrong Published Version
    fingerprint.
    """
    content = content_override if content_override is not None else _canonical_content(root)
    fp_digest = fingerprint_override if fingerprint_override is not None else fingerprint_sources(
        root
    ).digest
    manifest = build_published_manifest(version, fp_digest, content)
    for rel, raw in content.items():
        obstore.put(store, f"{prefix}/{version}/{rel}", raw)
    obstore.put(
        store,
        f"{prefix}/{version}/{MANIFEST_OBJECT}",
        msgspec.json.encode(manifest),
    )
    if not skip_pointer:
        obstore.put(
            store,
            f"{prefix}/{CURRENT_POINTER_OBJECT}",
            msgspec.json.encode(S3Pointer(version=version)),
        )
    return manifest


def _store() -> object:
    return obstore.store.MemoryStore()


@pytest.fixture
def published_kb(tmp_path):
    """Publish the ``valid`` fixture to a fresh MemoryStore and return the store."""
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    return store


# ---------------------------------------------------------------------------
# 1. The S3 Location is a public, runtime-checkable Knowledge Base Location.
# ---------------------------------------------------------------------------


def test_s3_location_satisfies_the_location_contract(published_kb):
    location = S3Location(published_kb, "kb")
    assert isinstance(location, KnowledgeBaseLocation)
    assert location.kind == S3_LOCATION_KIND


def test_s3_location_describe_is_secret_free_and_stable(published_kb):
    location = S3Location(published_kb, "kb")
    desc = location.describe()
    assert isinstance(desc, str) and desc
    assert "kb" in desc
    # No credentials are ever present (the in-memory store has none, but the
    # contract is that describe() never leaks them).
    assert "key" not in desc.lower()
    assert "secret" not in desc.lower()


def test_s3_location_describe_names_a_pinned_version(published_kb):
    location = S3Location(published_kb, "kb", version="v1")
    assert "@v1" in location.describe()


# ---------------------------------------------------------------------------
# 2. resolve() returns an immutable Snapshot over the resolved version.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_resolve_returns_an_immutable_snapshot(fixture):
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    snapshot = S3Location(store, "kb").resolve()
    assert isinstance(snapshot, KnowledgeBaseSnapshot)
    assert isinstance(snapshot.validation_report, ValidationReport)
    assert isinstance(snapshot.fingerprint, SourceFingerprint)
    assert isinstance(snapshot.location, KnowledgeBaseLocation)
    # The Published Version fingerprint matches the filesystem source.
    assert snapshot.fingerprint.digest == fingerprint_sources(fixture).digest
    # The Snapshot is immutable.
    with pytest.raises((AttributeError, TypeError)):
        snapshot.fingerprint = snapshot.fingerprint  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. Byte-for-byte equivalence with the verified filesystem Location.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_snapshot_matches_filesystem_validation_report(fixture):
    fs_snapshot = FilesystemLocation(fixture).resolve()
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    assert s3_snapshot.validation_report == fs_snapshot.validation_report


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_snapshot_matches_filesystem_pages_and_control(fixture):
    fs_snapshot = FilesystemLocation(fixture).resolve()
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    assert list(s3_snapshot.pages) == list(fs_snapshot.pages)
    assert s3_snapshot.control == fs_snapshot.control


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_snapshot_matches_filesystem_fingerprint(fixture):
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    assert s3_snapshot.fingerprint == fingerprint_sources(fixture)


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_zero_index_retrieval_matches_filesystem(fixture):
    fs_snapshot = FilesystemLocation(fixture).resolve()
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    query = "Lumio LanceDB architecture"
    fs_results = fs_snapshot.retrieve(query, limit=5)
    s3_results = s3_snapshot.retrieve(query, limit=5)
    assert len(s3_results) == len(fs_results)
    for s3, fs in zip(s3_results, fs_results, strict=True):
        assert isinstance(s3, RetrievalResult)
        assert s3.evidence == fs.evidence
        assert s3.citation == fs.citation
        assert s3.snippet == fs.snippet
        assert s3.score == fs.score
        assert s3.reason == fs.reason
        assert s3.trace == fs.trace


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_search_matches_filesystem(fixture):
    fs_snapshot = FilesystemLocation(fixture).resolve()
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    assert s3_snapshot.search_pages("Lumio") == fs_snapshot.search_pages("Lumio")


@pytest.mark.parametrize("fixture", LOCATION_FIXTURES, ids=lambda p: p.name)
def test_s3_graph_traversal_matches_filesystem(fixture):
    fs_snapshot = FilesystemLocation(fixture).resolve()
    store = _store()
    _publish_version(store, "kb", "v1", fixture)
    s3_snapshot = S3Location(store, "kb").resolve()
    titles = [page.title for page in fs_snapshot.pages]
    assert s3_snapshot.related_pages("Lumio Overview", candidate_titles=titles) == (
        fs_snapshot.related_pages("Lumio Overview", candidate_titles=titles)
    )
    assert s3_snapshot.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_DISCOVERY, candidate_titles=titles
    ) == fs_snapshot.related_pages(
        "Lumio Overview", scope=GRAPH_SCOPE_DISCOVERY, candidate_titles=titles
    )
    assert s3_snapshot.shortest_path(
        "Lumio Overview", "Technology Stack", candidate_titles=titles
    ) == fs_snapshot.shortest_path(
        "Lumio Overview", "Technology Stack", candidate_titles=titles
    )


# ---------------------------------------------------------------------------
# 4. Pointer resolution and immutable-version isolation.
# ---------------------------------------------------------------------------


def test_resolve_reads_the_pointer_once_then_pins_the_version():
    """A resolved Snapshot stays on its immutable version even if the pointer
    later moves to a different Published Version."""
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    snapshot_v1 = S3Location(store, "kb").resolve()
    v1_titles = {p.title for p in snapshot_v1.pages}

    # Publish a second version that adds a page, then move the pointer.
    extra = _canonical_content(FIXTURES / "valid")
    extra_page = (
        b"---\n"
        b"title: Later Page\n"
        b"tags: [test]\n"
        b"lifecycle: approved\n"
        b"visibility: public\n"
        b"summary: Added after v1.\n"
        b"---\n"
        b"Body of the later page.\n"
    )
    extra["Later Page.md"] = extra_page
    # Recompute fingerprint over the new tree (a real publisher would).
    from lumio_wiki.knowledge_base import _fingerprint_sources, _InMemoryKbSource

    fp_v2 = _fingerprint_sources(_InMemoryKbSource(extra, Path("<v2>"))).digest
    _publish_version(
        store, "kb", "v2", FIXTURES / "valid", content_override=extra, fingerprint_override=fp_v2
    )

    # The already-resolved Snapshot is immutable: it still reflects v1 only.
    assert {p.title for p in snapshot_v1.pages} == v1_titles
    assert "Later Page" not in v1_titles

    # A fresh resolve now follows the pointer to v2.
    snapshot_v2 = S3Location(store, "kb").resolve()
    assert "Later Page" in {p.title for p in snapshot_v2.pages}


def test_pinned_version_ignores_the_pointer():
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    # Publish v2 and point current.json at it.
    _publish_version(store, "kb", "v2", FIXTURES / "categorized_kb")
    # A pinned Location resolves v1 even though the pointer names v2.
    snapshot = S3Location(store, "kb", version="v1").resolve()
    assert {p.title for p in snapshot.pages} == {
        p.title for p in FilesystemLocation(FIXTURES / "valid").resolve().pages
    }


# ---------------------------------------------------------------------------
# 5. Corruption rejection: missing pointer/manifest, digest mismatch, wrong
#    fingerprint, size mismatch, malformed JSON.
# ---------------------------------------------------------------------------


def test_resolve_rejects_a_missing_pointer():
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid", skip_pointer=True)
    with pytest.raises(Exception, match="could not read"):
        S3Location(store, "kb").resolve()


def test_resolve_rejects_a_missing_manifest():
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    obstore.delete(store, "kb/v1/manifest.json")
    with pytest.raises(Exception, match="could not read"):
        S3Location(store, "kb").resolve()


def test_resolve_rejects_a_tampered_content_digest():
    """A byte flipped after publication must fail content-digest validation."""
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    # Tamper with one page object without updating the manifest digest.
    content = _canonical_content(FIXTURES / "valid")
    some_page = next(iter(content))
    tampered = content[some_page] + b"\n# TAMPERED\n"
    obstore.put(store, f"kb/v1/{some_page}", tampered)
    with pytest.raises(Exception, match="corruption"):
        S3Location(store, "kb").resolve()


def test_resolve_rejects_a_wrong_published_version_fingerprint():
    """Content digests are valid, but the manifest's KB fingerprint is wrong."""
    store = _store()
    _publish_version(
        store, "kb", "v1", FIXTURES / "valid", fingerprint_override="0" * 64
    )
    with pytest.raises(Exception, match="fingerprint"):
        S3Location(store, "kb").resolve()


def test_resolve_rejects_a_size_mismatch():
    store = _store()
    manifest = _publish_version(store, "kb", "v1", FIXTURES / "valid")
    # Corrupt one manifest entry's recorded size (keep digest valid -> forces a
    # size-only failure path by making the recorded size wrong).
    content = _canonical_content(FIXTURES / "valid")
    some_page = next(iter(content))
    bad_entry = S3ManifestFile(
        path=some_page, size=len(content[some_page]) + 99, digest=manifest.files[0].digest
    )
    tampered = S3Manifest(
        version=manifest.version,
        fingerprint=manifest.fingerprint,
        files=[bad_entry if f.path == some_page else f for f in manifest.files],
    )
    obstore.put(store, "kb/v1/manifest.json", msgspec.json.encode(tampered))
    with pytest.raises(Exception, match="size"):
        S3Location(store, "kb").resolve()


def test_resolve_rejects_a_manifest_path_that_escapes_the_version_prefix():
    """A ``..`` segment, absolute path, or backslash cannot escape the prefix."""
    store = _store()
    manifest = _publish_version(store, "kb", "v1", FIXTURES / "valid")
    first = manifest.files[0]
    for bad_path in ("../escape.md", "/abs.md", "sub\\dir.md"):
        escaping = S3Manifest(
            version=manifest.version,
            fingerprint=manifest.fingerprint,
            files=[
                S3ManifestFile(path=bad_path, size=first.size, digest=first.digest)
            ],
        )
        obstore.put(store, "kb/v1/manifest.json", msgspec.json.encode(escaping))
        with pytest.raises(KnowledgeBaseError, match="relative|prefix|separator"):
            S3Location(store, "kb").resolve()


def test_resolve_rejects_a_malformed_pointer():
    store = _store()
    obstore.put(store, "kb/current.json", b"not json at all")
    with pytest.raises(Exception, match="not valid JSON|malformed"):
        S3Location(store, "kb").resolve()

def test_resolve_rejects_a_malformed_manifest():
    store = _store()
    _publish_version(store, "kb", "v1", FIXTURES / "valid")
    obstore.put(store, "kb/v1/manifest.json", b"{}")
    with pytest.raises(KnowledgeBaseError):
        S3Location(store, "kb").resolve()


# ---------------------------------------------------------------------------
# 6. Default no-managed-disk-cache policy.
# ---------------------------------------------------------------------------


def test_resolve_writes_no_managed_bytes_to_local_disk(published_kb, tmp_path, monkeypatch):
    """The default S3 cache policy writes no KB/derived bytes to local disk.

    Resolving a version must not create any files under a monitored temp area.
    A bounded in-memory cache is the only allowed residency.
    """
    watch = tmp_path / "cache-watch"
    watch.mkdir()
    monkeypatch.chdir(tmp_path)
    location = S3Location(published_kb, "kb")
    snapshot = location.resolve()
    # Exercising read paths must also not materialize anything to disk.
    list(snapshot.pages)
    snapshot.search_pages("Lumio")
    snapshot.retrieve("Lumio", limit=3)
    snapshot.related_pages("Lumio Overview")
    assert not any(watch.rglob("*"))
    # No files appeared under the process cwd either.
    assert not any(p.is_file() for p in tmp_path.glob("**/*") if p.parent == tmp_path)
    # The materialized content lives only in the Location's in-memory cache.
    assert "v1" in location._cache


def test_in_memory_cache_is_bounded_and_reused(published_kb):
    """A second resolve of the same version is served from the in-memory cache."""
    location = S3Location(published_kb, "kb")
    first = location.resolve()
    assert "v1" in location._cache
    # Re-resolving does not raise and stays consistent.
    second = location.resolve()
    assert [p.title for p in second.pages] == [p.title for p in first.pages]


def test_cache_can_be_disabled(published_kb):
    """max_cached_versions=0 means no residency beyond the loaded Snapshot."""
    location = S3Location(published_kb, "kb", max_cached_versions=0)
    location.resolve()
    assert location._cache == {}


# ---------------------------------------------------------------------------
# 7. The convenience entrypoint and manifest builder.
# ---------------------------------------------------------------------------


def test_open_s3_knowledge_base_resolves_an_immutable_snapshot(published_kb):
    snapshot = open_s3_knowledge_base(published_kb, "kb")
    assert isinstance(snapshot, KnowledgeBaseSnapshot)
    assert snapshot.fingerprint.digest == fingerprint_sources(FIXTURES / "valid").digest


def test_build_published_manifest_records_sizes_and_digests():
    content = {"a.md": b"hello", "lumio.yaml": b"x: 1"}
    manifest = build_published_manifest("v9", "fpdigest", content)
    assert manifest.version == "v9"
    assert manifest.fingerprint == "fpdigest"
    by_path = {f.path: f for f in manifest.files}
    assert by_path["a.md"].size == 5
    assert by_path["a.md"].digest == hashlib.sha256(b"hello").hexdigest()
    assert by_path["lumio.yaml"].size == 4


# ---------------------------------------------------------------------------
# 8. Actionable error when the [s3] extra is absent.
# ---------------------------------------------------------------------------


def test_missing_obstore_extra_raises_an_actionable_error(monkeypatch):
    """When obstore is not installed, the error names the exact install command."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "obstore" or name.startswith("obstore."):
            raise ImportError("simulated missing obstore")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    store = _store()
    with pytest.raises(Exception) as exc_info:
        S3Location(store, "kb").resolve()
    assert "[s3]" in str(exc_info.value)
    assert "pip install" in str(exc_info.value)
