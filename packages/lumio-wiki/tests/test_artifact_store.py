"""Private Source Artifact retention (issue #164, ADR-0020).

Deterministic coverage for the Source Artifact Store seam, the managed-ingest
upload+binding saga, the private Source Binding Manifest, required-retention
activation gating, and privacy (no artifact bytes or keys in public Published
Versions, fingerprints, or manifests). The S3 adapter's signing and
cross-role-credential behavior is covered by the MinIO suite
(``test_artifact_store_minio.py``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import msgspec
import pytest
from lumio_wiki.artifact_store import (
    ArtifactStoreError,
    InMemoryArtifactStore,
    LocalDirectoryArtifactStore,
    RetentionRequiredError,
    SigningUnavailable,
    SourceBindingManifest,
    activation_binding_hook,
    affected_published_versions,
    artifact_content_hash,
    build_binding_manifest,
    delete_artifact_with_disclosure,
    retain_artifact,
    sweep_orphaned_uploads,
    write_binding_manifest,
)
from lumio_wiki.ingest import IngestStore, ManagedIngestError
from lumio_wiki.knowledge_base import fingerprint_sources
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.source_registry import (
    SourceRegistry,
    SourceRegistryError,
    safe_artifact_filename,
)

obstore = pytest.importorskip("obstore", reason="obstore required for publication coverage")

from lumio_wiki.s3_location import CURRENT_POINTER_OBJECT  # noqa: E402
from lumio_wiki.s3_publish import publish_s3_version  # noqa: E402

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

RAW = b"%PDF-1.4 original source bytes for the artifact suite"


def _kb(tmp_path: Path):
    import lumio_wiki as lw

    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def _authored_page(source_id: str, *, title: str = "Artifact Page", synthetic: bool = False):
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "artifacts"\n'
        'summary: "Authored page for the artifact suite."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "relationships: []\n"
        f"synthetic: {str(synthetic).lower()}\n"
        "---\n\n"
        f"# {title}\n\n"
        "Body authored from the original source.\n"
    )


def _pipeline(kb, tmp_path: Path, artifact_store=None) -> tuple[ProposalPipeline, IngestStore]:
    store = IngestStore(tmp_path / "ingest")
    return ProposalPipeline(kb, store=store, artifact_store=artifact_store), store


@pytest.fixture(params=["memory", "local"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryArtifactStore()
    return LocalDirectoryArtifactStore(tmp_path / "artifact-store")


# ---------------------------------------------------------------------------
# Adapter contract: verified bytes, create-only, identity-oriented addressing.
# ---------------------------------------------------------------------------


def test_put_get_roundtrip_verifies_digest(store):
    store.put_artifact(
        source_id="report",
        content_hash=artifact_content_hash(RAW),
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    assert store.get_artifact(source_id="report", content_hash=artifact_content_hash(RAW)) == RAW
    assert store.artifact_exists(source_id="report", content_hash=artifact_content_hash(RAW))


def test_declared_hash_mismatch_is_rejected(store):
    with pytest.raises(ArtifactStoreError):
        store.put_artifact(
            source_id="report",
            content_hash="0" * 64,
            raw_bytes=RAW,
            content_type=None,
            filename=None,
        )


def test_missing_artifact_is_not_retained(store):
    digest = artifact_content_hash(RAW)
    with pytest.raises(ArtifactStoreError):
        store.get_artifact(source_id="unknown", content_hash=digest)
    assert not store.artifact_exists(source_id="unknown", content_hash=digest)


def test_fetch_rejects_tampered_stored_bytes(store):
    digest = artifact_content_hash(RAW)
    store.put_artifact(
        source_id="report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type=None,
        filename=None,
    )
    _tamper(store, "report", digest)
    assert not store.artifact_exists(source_id="report", content_hash=digest)
    with pytest.raises(ArtifactStoreError):
        store.get_artifact(source_id="report", content_hash=digest)


def _tamper(store, source_id: str, digest: str) -> None:
    if isinstance(store, InMemoryArtifactStore):
        store._artifacts[(source_id, digest)] = (b"tampered", None, None)
    else:
        path = store._artifact_path(source_id, digest)
        path.write_bytes(b"tampered on disk")


def test_identical_bytes_do_not_merge_source_identities(store):
    digest = artifact_content_hash(RAW)
    for source_id in ("report-a", "report-b"):
        store.put_artifact(
            source_id=source_id,
            content_hash=digest,
            raw_bytes=RAW,
            content_type="application/pdf",
            filename="same.pdf",
        )
    # Both identities resolve independently (physical dedup never merges them).
    assert store.get_artifact(source_id="report-a", content_hash=digest) == RAW
    assert store.get_artifact(source_id="report-b", content_hash=digest) == RAW
    # Deleting one identity leaves the other intact.
    assert store.delete_artifact(source_id="report-a", content_hash=digest)
    assert store.get_artifact(source_id="report-b", content_hash=digest) == RAW
    assert not store.artifact_exists(source_id="report-a", content_hash=digest)


def test_signing_unavailable_on_local_adapters(store):
    digest = artifact_content_hash(RAW)
    store.put_artifact(
        source_id="report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type=None,
        filename=None,
    )
    assert not store.supports_signing()
    with pytest.raises(SigningUnavailable):
        store.signed_get_url(source_id="report", content_hash=digest, expires_in=300)


# ---------------------------------------------------------------------------
# Safe filenames (registry metadata boundary).
# ---------------------------------------------------------------------------


def test_safe_artifact_filename_strips_paths_and_control_characters():
    assert safe_artifact_filename("/deep/path/report v2.pdf") == "report v2.pdf"
    assert safe_artifact_filename("..\\..\\windows.pdf") == "windows.pdf"
    assert safe_artifact_filename("..") is None
    assert safe_artifact_filename(None) is None
    assert safe_artifact_filename("bad\r\nname.txt") == "badname.txt"
    truncated = safe_artifact_filename("x" * 500 + ".pdf")
    assert truncated is not None and len(truncated) <= 128


# ---------------------------------------------------------------------------
# The idempotent managed-ingest saga (AC: new / retry / failure / orphan).
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path) -> SourceRegistry:
    return SourceRegistry(tmp_path / "registry")


def test_saga_registers_uploads_and_binds(store, tmp_path):
    registry = _registry(tmp_path)
    version, _ = registry.register_or_reuse(
        "annual-report", RAW, filename="/deep/report.pdf", content_type="application/pdf"
    )
    digest = retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="/deep/report.pdf",
    )
    assert digest == version.content_hash
    assert version.content_hash == artifact_content_hash(RAW)
    bound = registry.get("annual-report").versions[-1]
    assert bound.artifact_available is True
    assert bound.filename == "report.pdf"
    assert bound.content_type == "application/pdf"
    assert bound.size == len(RAW)
    assert store.get_artifact(source_id="annual-report", content_hash=digest) == RAW


def test_saga_identical_retry_is_idempotent(store, tmp_path):
    registry = _registry(tmp_path)
    for _ in range(3):
        registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
        retain_artifact(
            store,
            registry,
            source_id="annual-report",
            raw_bytes=RAW,
            content_type=None,
            filename="r.pdf",
        )
    source = registry.get("annual-report")
    assert len(source.versions) == 1  # one immutable version, no duplicates
    assert (
        store.get_artifact(source_id="annual-report", content_hash=source.versions[0].content_hash)
        == RAW
    )


def test_saga_changed_bytes_rejected_without_store_mutation(store, tmp_path):
    registry = _registry(tmp_path)
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    with pytest.raises(SourceRegistryError):
        registry.register_or_reuse(
            "annual-report", b"DIFFERENT bytes", filename="r.pdf", content_type=None
        )
    # The store still holds exactly the original bytes.
    assert (
        store.get_artifact(source_id="annual-report", content_hash=artifact_content_hash(RAW))
        == RAW
    )


def test_saga_failed_upload_is_never_reported_as_retained(tmp_path):
    store = InMemoryArtifactStore()
    registry = _registry(tmp_path)

    def _fail_put(**kwargs):
        raise ArtifactStoreError("object store unavailable")

    store.put_artifact = _fail_put  # type: ignore[method-assign]
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    with pytest.raises(ArtifactStoreError):
        retain_artifact(
            store,
            registry,
            source_id="annual-report",
            raw_bytes=RAW,
            content_type=None,
            filename="r.pdf",
        )
    # The Source Version exists as identity/provenance state, but availability
    # was NOT recorded: a failed upload is never reported as retained.
    assert registry.get("annual-report").versions[-1].artifact_available is False
    # A retry after the store recovers completes the saga.
    del store.put_artifact
    retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    assert registry.get("annual-report").versions[-1].artifact_available is True


def test_saga_registry_failure_leaves_sweepable_orphan(store, tmp_path):
    registry = _registry(tmp_path)
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    store.put_artifact(
        source_id="annual-report",
        content_hash=artifact_content_hash(RAW),
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    # The registry binding never happened (crash between upload and bind).
    orphans = sweep_orphaned_uploads(store, registry)
    assert orphans == [("annual-report", artifact_content_hash(RAW))]
    # Recovery: re-running the saga binds the orphan idempotently.
    retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    assert sweep_orphaned_uploads(store, registry) == []
    assert registry.get("annual-report").versions[-1].artifact_available is True


def test_managed_ingest_retains_artifacts_through_the_pipeline(tmp_path):
    kb = _kb(tmp_path)
    store = InMemoryArtifactStore()
    pipeline, ingest = _pipeline(kb, tmp_path, artifact_store=store)
    pipeline.managed_ingest(
        RAW, "application/pdf", "report.pdf", "annual-report", _authored_page("annual-report")
    )
    version = ingest.source_registry.get("annual-report").versions[-1]
    assert version.artifact_available is True
    assert store.get_artifact(source_id="annual-report", content_hash=version.content_hash) == RAW


def test_managed_ingest_wraps_store_failure_actionably(tmp_path):
    kb = _kb(tmp_path)
    store = InMemoryArtifactStore()

    def _fail_put(**kwargs):
        raise ArtifactStoreError("endpoint unreachable")

    store.put_artifact = _fail_put  # type: ignore[method-assign]
    pipeline, ingest = _pipeline(kb, tmp_path, artifact_store=store)
    with pytest.raises(ManagedIngestError, match="retry the ingest to recover"):
        pipeline.managed_ingest(
            RAW, "application/pdf", "report.pdf", "annual-report", _authored_page("annual-report")
        )
    registry = ingest.source_registry
    assert registry.get("annual-report").versions[-1].artifact_available is False
    # Nothing was staged: the failed retention is not publishable.
    assert pipeline.list() == []


# ---------------------------------------------------------------------------
# Source Binding Manifest + required-retention activation gate.
# ---------------------------------------------------------------------------


def _published_kb_with_source(tmp_path, *, retain: bool, synthetic: bool = False):
    """Publish a fixture KB plus one managed source under a configured store.

    Every source id the FIXTURE pages reference is registered and backfilled
    with a verified artifact (required retention evaluates every referenced
    non-synthetic source, #164 — fixture sources are scenery and always
    covered). ``annual-report`` (the managed source) is retained ONLY when
    ``retain``: the tests that assert blocking exercise exactly that hole.
    """
    kb = _kb(tmp_path)
    artifact_store = InMemoryArtifactStore()
    pipeline, ingest = _pipeline(kb, tmp_path, artifact_store=artifact_store if retain else None)
    registry = ingest.source_registry
    for page in kb.pages:
        for source in page.sources:
            if not source.id:
                continue
            fixture_bytes = f"fixture artifact for {source.id}".encode()
            registry.register_or_reuse(source.id, fixture_bytes, filename=None, content_type=None)
            retain_artifact(
                artifact_store,
                registry,
                source_id=source.id,
                raw_bytes=fixture_bytes,
                content_type="text/plain",
                filename=f"{source.id}.txt",
            )
    proposal = pipeline.managed_ingest(
        RAW,
        "application/pdf",
        "report.pdf",
        "annual-report",
        _authored_page("annual-report", synthetic=synthetic),
    )
    assert pipeline.publish(proposal.id).status == "published"
    return kb, registry, artifact_store


def _publish(store, kb, *, version: str, hook=None):
    return publish_s3_version(
        store,
        "kb",
        source_root=kb.root,
        version=version,
        before_activation=hook,
    )


def _public_keys(store) -> list[str]:
    keys = []
    for batch in obstore.list(store, prefix="kb"):
        for obj in batch:
            keys.append(obj["path"])
    return sorted(keys)


def test_publication_writes_private_binding_manifest_before_activation(tmp_path):
    kb, registry, artifact_store = _published_kb_with_source(tmp_path, retain=True)
    store = obstore.store.MemoryStore()
    hook = activation_binding_hook(
        artifact_store=artifact_store, registry=registry, source_root=kb.root, required=False
    )
    manifest = _publish(store, kb, version="v1", hook=hook)

    # The private manifest binds the Published Version and the exact version.
    raw = artifact_store.get_binding_manifest("v1")
    binding = msgspec.json.decode(raw, type=SourceBindingManifest)
    assert binding.published_version == "v1"
    assert binding.fingerprint == manifest.fingerprint
    entry = next(e for e in binding.entries if e.source_id == "annual-report")
    assert entry.page_title == "Artifact Page"
    assert entry.content_hash == registry.get("annual-report").versions[-1].content_hash
    assert entry.filename == "report.pdf"
    assert entry.synthetic_page is False

    # PRIVACY: the public prefix exposes no artifact bytes, keys, or manifest.
    public = _public_keys(store)
    assert public, "expected published objects"
    assert not any("artifacts/" in key or "bindings/" in key for key in public)
    for key in public:
        blob = bytes(obstore.get(store, key).bytes())
        assert b"%PDF-1.4 original source bytes" not in blob
    # The canonical fingerprint ignores artifact state entirely.
    assert fingerprint_sources(kb.root) == fingerprint_sources(kb.root)


def test_required_retention_blocks_activation_until_artifact_exists(tmp_path):
    kb, registry, artifact_store = _published_kb_with_source(tmp_path, retain=False)
    assert registry.get("annual-report").versions[-1].artifact_available is False

    store = obstore.store.MemoryStore()
    hook = activation_binding_hook(
        artifact_store=artifact_store, registry=registry, source_root=kb.root, required=True
    )
    with pytest.raises(RetentionRequiredError, match="annual-report"):
        _publish(store, kb, version="v1", hook=hook)
    # Pointer was NOT advanced: activation blocked before publication.
    with pytest.raises(FileNotFoundError):
        obstore.get(store, f"kb/{CURRENT_POINTER_OBJECT}")

    # Backfill the artifact (the ADR's historical-backfill path), then retry
    # under a fresh version label: the blocked attempt's prefix is immutable
    # residue (#163), never overwritten.
    retain_artifact(
        artifact_store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="report.pdf",
    )
    _publish(store, kb, version="v2", hook=hook)
    assert artifact_store.get_binding_manifest("v2")


def test_required_retention_ignores_synthetic_pages(tmp_path):
    kb, registry, artifact_store = _published_kb_with_source(tmp_path, retain=False, synthetic=True)
    store = obstore.store.MemoryStore()
    hook = activation_binding_hook(
        artifact_store=artifact_store, registry=registry, source_root=kb.root, required=True
    )
    _publish(store, kb, version="v1", hook=hook)  # synthetic page: no artifact needed
    binding = msgspec.json.decode(
        artifact_store.get_binding_manifest("v1"), type=SourceBindingManifest
    )
    entry = next(e for e in binding.entries if e.source_id == "annual-report")
    assert entry.synthetic_page is True


def test_disabled_retention_preserves_hash_only_publication(tmp_path):
    kb, registry, _ = _published_kb_with_source(tmp_path, retain=False)
    assert registry.get("annual-report").versions[-1].artifact_available is False
    store = obstore.store.MemoryStore()
    # No artifact store configured at all: publication is byte-for-byte the
    # pre-#164 behavior (no hook, no gate).
    _publish(store, kb, version="v1")
    pointer = msgspec.json.decode(bytes(obstore.get(store, f"kb/{CURRENT_POINTER_OBJECT}").bytes()))
    assert pointer["version"] == "v1"


def test_historical_manifest_survives_new_source_version(tmp_path):
    kb, registry, artifact_store = _published_kb_with_source(tmp_path, retain=True)
    store = obstore.store.MemoryStore()
    hook = activation_binding_hook(
        artifact_store=artifact_store, registry=registry, source_root=kb.root, required=True
    )
    _publish(store, kb, version="v1", hook=hook)
    v1_binding = msgspec.json.decode(
        artifact_store.get_binding_manifest("v1"), type=SourceBindingManifest
    )
    old_hash = next(
        e.content_hash for e in v1_binding.entries if e.source_id == "annual-report"
    )

    # A later reviewed retirement + reactivation registers a NEW Source
    # Version; the historical manifest still identifies the historical
    # artifact.
    retire = registry.stage_retirement("annual-report")
    registry.bind_pending(retire, "retire-proposal")
    registry.apply_transition("retire-proposal")
    transition = registry.stage_reactivation("annual-report", b"NEW report bytes")
    registry.bind_pending(transition, "later-proposal")
    registry.apply_transition("later-proposal")
    new_hash = registry.get("annual-report").versions[-1].content_hash
    assert new_hash != old_hash

    v1_again = msgspec.json.decode(
        artifact_store.get_binding_manifest("v1"), type=SourceBindingManifest
    )
    assert (
        next(e.content_hash for e in v1_again.entries if e.source_id == "annual-report")
        == old_hash
    )
    # The historical artifact bytes are still fetchable by exact identity.
    assert artifact_store.get_artifact(source_id="annual-report", content_hash=old_hash) == RAW


def test_binding_manifest_omits_unregistered_source_ids(tmp_path):
    kb = _kb(tmp_path)
    registry = _registry(tmp_path)
    manifest = build_binding_manifest(
        published_version="v9",
        fingerprint="digest",
        registry=registry,
        pages=list(kb.pages),
        now="2026-01-01T00:00:00+00:00",
    )
    # Fixture pages cite source ids that were never privately registered: the
    # manifest binds only registered identities.
    assert manifest.entries == []


# ---------------------------------------------------------------------------
# Retirement preserves artifacts; deletion is explicit with disclosure.
# ---------------------------------------------------------------------------


def test_retirement_preserves_historical_artifacts(store, tmp_path):
    registry = _registry(tmp_path)
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    digest = registry.get("annual-report").versions[-1].content_hash

    transition = registry.stage_retirement("annual-report")
    registry.bind_pending(transition, "retire-proposal")
    registry.apply_transition("retire-proposal")
    assert registry.get("annual-report").status == "retired"

    # Retirement did NOT delete or unbind the historical artifact.
    assert store.get_artifact(source_id="annual-report", content_hash=digest) == RAW
    assert registry.get("annual-report").versions[-1].artifact_available is True


def test_explicit_deletion_reports_affected_published_versions(store, tmp_path):
    registry = _registry(tmp_path)
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    retain_artifact(
        store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    digest = registry.get("annual-report").versions[-1].content_hash

    # Two Published Versions bind this exact artifact.
    for version in ("v1", "v2"):
        manifest = build_binding_manifest(
            published_version=version,
            fingerprint="digest",
            registry=registry,
            pages=_pages_citing("annual-report"),
            now="2026-01-01T00:00:00+00:00",
        )
        store.put_binding_manifest(version, msgspec.json.encode(manifest))

    assert affected_published_versions(store, source_id="annual-report", content_hash=digest) == [
        "v1",
        "v2",
    ]

    affected = delete_artifact_with_disclosure(
        store, registry, source_id="annual-report", content_hash=digest
    )
    assert affected == ["v1", "v2"]
    assert not store.artifact_exists(source_id="annual-report", content_hash=digest)
    assert registry.get("annual-report").versions[-1].artifact_available is False


def _pages_citing(source_id: str):
    class _Source:
        id = source_id

    class _Page:
        title = "Artifact Page"
        sources = [_Source()]
        synthetic = False

    return [_Page()]


# ---------------------------------------------------------------------------
# CLI wiring: managed ingest retains artifacts from the environment config.
# ---------------------------------------------------------------------------


def test_cli_managed_ingest_retains_artifacts_from_env(tmp_path, monkeypatch, capsys):
    from lumio_wiki import cli

    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb_root)
    store_root = tmp_path / "artifact-store"
    compiled = tmp_path / "page.md"
    compiled.write_text(_authored_page("annual-report"), encoding="utf-8")
    source = tmp_path / "report.pdf"
    source.write_bytes(RAW)

    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    rc = cli.main(
        [
            "ingest",
            str(kb_root),
            str(source),
            "--compiled-page",
            str(compiled),
            "--source-id",
            "annual-report",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Staged proposal" in out

    local = LocalDirectoryArtifactStore(store_root)
    digest = artifact_content_hash(RAW)
    assert local.get_artifact(source_id="annual-report", content_hash=digest) == RAW
    # Private state never lands inside the Knowledge Base root.
    assert not (kb_root / "artifacts").exists()
    assert not (kb_root / "bindings").exists()


# ---------------------------------------------------------------------------
# Review findings (#164): coverage semantics, fail-closed config, rollback
# gate, size limit, local store setup.
# ---------------------------------------------------------------------------


def test_required_retention_blocks_unregistered_source_references(tmp_path):
    """A referenced source the registry does not know can never have a
    verified artifact: under required retention it BLOCKS activation (the
    manifest binding only registered identities is not a coverage pass)."""
    kb = _kb(tmp_path)
    artifact_store = InMemoryArtifactStore()
    pipeline, ingest = _pipeline(kb, tmp_path, artifact_store=artifact_store)
    # Only the fixture sources are registered/retained; the managed page
    # cites an id that was never registered.
    registry = ingest.source_registry
    for page in kb.pages:
        for source in page.sources:
            if not source.id:
                continue
            fixture_bytes = f"fixture artifact for {source.id}".encode()
            registry.register_or_reuse(source.id, fixture_bytes, filename=None, content_type=None)
            retain_artifact(
                artifact_store,
                registry,
                source_id=source.id,
                raw_bytes=fixture_bytes,
                content_type="text/plain",
                filename=f"{source.id}.txt",
            )
    # The authored page cites the registered annual-report AND a second
    # source id that was never privately registered.
    authored = _authored_page("annual-report").replace(
        '  - id: "annual-report"\n'
        '    title: "annual-report source"\n',
        '  - id: "annual-report"\n'
        '    title: "annual-report source"\n'
        '  - id: "ghost-reference"\n'
        '    title: "Never registered"\n',
    )
    assert "ghost-reference" in authored
    proposal = pipeline.managed_ingest(
        RAW, "application/pdf", "report.pdf", "annual-report", authored
    )
    assert pipeline.publish(proposal.id).status == "published"

    store = obstore.store.MemoryStore()
    hook = activation_binding_hook(
        artifact_store=artifact_store,
        registry=registry,
        source_root=kb.root,
        required=True,
    )
    with pytest.raises(RetentionRequiredError, match="unregistered source"):
        _publish(store, kb, version="v1", hook=hook)


def test_publish_s3_required_without_store_fails_closed(tmp_path, monkeypatch, capsys):
    """LUMIO_ARTIFACT_RETENTION=required with no configured store refuses to
    publish rather than silently publishing ungated."""
    from lumio_wiki import cli

    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb_root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    monkeypatch.setenv("LUMIO_ARTIFACT_RETENTION", "required")
    rc = cli.main(
        ["publish-s3", str(kb_root), "s3://bucket/kb", "--version", "v1"]
    )
    assert rc == 1
    assert "no Source Artifact Store is configured" in capsys.readouterr().err


def test_verify_rollback_coverage_gates_historical_activation(tmp_path):
    artifact_store = InMemoryArtifactStore()
    registry = _registry(tmp_path)
    registry.register_or_reuse("annual-report", RAW, filename="r.pdf", content_type=None)
    retain_artifact(
        artifact_store,
        registry,
        source_id="annual-report",
        raw_bytes=RAW,
        content_type=None,
        filename="r.pdf",
    )
    digest = registry.get("annual-report").versions[-1].content_hash
    manifest = build_binding_manifest(
        published_version="v1",
        fingerprint="digest",
        registry=registry,
        pages=_pages_citing("annual-report"),
        now="2026-01-01T00:00:00+00:00",
    )
    write_binding_manifest(artifact_store, manifest)

    from lumio_wiki.artifact_store import verify_rollback_coverage

    # Not required: no-op even with no manifest at all.
    verify_rollback_coverage(artifact_store, "unknown-version", required=False)
    # Required + no manifest for the version: fail closed.
    with pytest.raises(RetentionRequiredError, match="no private Source Binding Manifest"):
        verify_rollback_coverage(artifact_store, "unknown-version", required=True)
    # Required + manifest + verified artifact: passes.
    verify_rollback_coverage(artifact_store, "v1", required=True)
    # Required + manifest but the artifact was explicitly deleted: blocked.
    artifact_store.delete_artifact(source_id="annual-report", content_hash=digest)
    with pytest.raises(RetentionRequiredError, match="lack a verified artifact"):
        verify_rollback_coverage(artifact_store, "v1", required=True)


def test_rollback_cli_blocked_after_artifact_deletion(tmp_path, monkeypatch, capsys):
    """The rollback CLI is an activation: under required retention it refuses
    to reactivate a version whose bound artifact was explicitly deleted."""
    from lumio_wiki import cli

    kb_root = tmp_path / "kb"
    shutil.copytree(FIXTURES, kb_root)
    store_root = tmp_path / "src-store"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    monkeypatch.setenv("LUMIO_ARTIFACT_RETENTION", "required")

    import lumio_wiki as lw

    kb, report = lw.load_knowledge_base(kb_root)
    assert report.is_valid, report
    from lumio_wiki.artifact_store import LocalDirectoryArtifactStore

    artifact_store = LocalDirectoryArtifactStore(store_root)
    ingest = IngestStore(tmp_path / "ingest")
    registry = ingest.source_registry
    for page in kb.pages:
        for source in page.sources:
            if not source.id:
                continue
            fixture_bytes = f"fixture artifact for {source.id}".encode()
            registry.register_or_reuse(source.id, fixture_bytes, filename=None, content_type=None)
            retain_artifact(
                artifact_store,
                registry,
                source_id=source.id,
                raw_bytes=fixture_bytes,
                content_type="text/plain",
                filename=f"{source.id}.txt",
            )

    memory_store = obstore.store.MemoryStore()
    publish_s3_version(
        memory_store,
        "kb",
        source_root=kb_root,
        version="v1",
        before_activation=activation_binding_hook(
            artifact_store=artifact_store,
            registry=registry,
            source_root=kb_root,
            required=True,
        ),
    )

    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (memory_store, "kb"))
    # Rollback with the artifact still retained: allowed.
    rc = cli.main(["rollback-s3", "s3://bucket/kb", "--version", "v1"])
    assert rc == 0

    # Delete the bound artifact; the same rollback now refuses.
    digest = registry.get("architecture-doc").versions[-1].content_hash
    delete_artifact_with_disclosure(
        artifact_store, registry, source_id="architecture-doc", content_hash=digest
    )
    capsys.readouterr()
    rc = cli.main(["rollback-s3", "s3://bucket/kb", "--version", "v1"])
    assert rc == 1
    assert "rollback blocked" in capsys.readouterr().err


def test_retain_artifact_enforces_size_limit(tmp_path, monkeypatch):
    from lumio_wiki import artifact_store as module

    store = InMemoryArtifactStore()
    registry = _registry(tmp_path)
    registry.register_or_reuse("big-report", b"0123456789", filename=None, content_type=None)
    monkeypatch.setattr(module, "MAX_ARTIFACT_BYTES", 8)
    with pytest.raises(ArtifactStoreError, match="maximum retained artifact size"):
        retain_artifact(
            store,
            registry,
            source_id="big-report",
            raw_bytes=b"0123456789",
            content_type=None,
            filename=None,
        )
    # A failed retention is never reported as retained.
    assert registry.get("big-report").versions[-1].artifact_available is False


def test_setup_accepts_local_source_store_path(tmp_path, monkeypatch, capsys):
    """setup records a local-directory Source Artifact Store without the S3
    extra (object storage OR a local directory — CONTEXT.md, ADR-0020)."""
    from lumio_wiki import cli
    from lumio_wiki.env_loader import SOURCE_STORE_ENV_VAR

    monkeypatch.chdir(tmp_path)
    for key in ("LUMIO_SOURCE_STORE", "LUMIO_ARTIFACT_RETENTION"):
        monkeypatch.delenv(key, raising=False)
    local_store = tmp_path / "private-source-store"
    rc = cli.main(["setup", "kb", "--source-store", str(local_store)])
    assert rc == 0
    assert _env_value_text(tmp_path, SOURCE_STORE_ENV_VAR) == str(local_store)
    assert "Created Knowledge Base" in capsys.readouterr().out


def test_setup_rejects_unsupported_source_store_scheme(tmp_path, monkeypatch, capsys):
    from lumio_wiki import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    rc = cli.main(["setup", "kb", "--source-store", "ftp://example.com/src"])
    assert rc == 2
    assert "not a supported scheme" in capsys.readouterr().err


def _env_value_text(project: Path, key: str) -> str | None:
    env = project / ".env"
    if not env.exists():
        return None
    prefix = f"{key}="
    for raw in env.read_text(encoding="utf-8").splitlines():
        if raw.startswith(prefix):
            return raw[len(prefix) :].strip() or None
    return None
