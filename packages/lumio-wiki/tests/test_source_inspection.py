"""Authorized Source Artifact inspection seam (issue #165, ADR-0020).

Locks in the resolution semantics behind ``source inspect|fetch|link``:
identity-oriented binding resolution (registry current version vs. one exact
Source Binding Manifest), the distinct actionable outcomes, signed-link
expiry bounds, and safe fetch destinations. Everything operates through the
public Source Artifact Store and registry seams — never private S3 key
layouts.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import msgspec
import pytest
from lumio_wiki.artifact_store import (
    InMemoryArtifactStore,
    LocalDirectoryArtifactStore,
    SourceBindingEntry,
    SourceBindingManifest,
    artifact_content_hash,
)
from lumio_wiki.source_inspection import (
    DEFAULT_LINK_EXPIRES,
    MAX_LINK_EXPIRES,
    OUTCOME_ABSENT_BINDING,
    OUTCOME_ACCESS_DENIED,
    OUTCOME_CORRUPTION,
    OUTCOME_HISTORICAL_VERSION_MISMATCH,
    OUTCOME_UNAVAILABLE,
    ResolvedSourceBinding,
    SourceInspectionError,
    fetch_verified_artifact,
    parse_expires,
    resolve_manifest_binding,
    resolve_registry_binding,
    safe_fetch_destination,
)
from lumio_wiki.source_registry import SourceRegistry

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"
RAW = b"%PDF-1.4 exact original source artifact bytes (#165)"


# ---------------------------------------------------------------------------
# parse_expires: signed-link lifetime bounds (5m default, 1h ceiling).
# ---------------------------------------------------------------------------


def test_parse_expires_defaults_to_five_minutes():
    assert parse_expires(None) == timedelta(minutes=5)
    assert parse_expires("") == timedelta(minutes=5)
    assert parse_expires(DEFAULT_LINK_EXPIRES) == MAX_LINK_EXPIRES / 12
    assert parse_expires("5M") == timedelta(minutes=5)  # case-insensitive


def test_parse_expires_accepts_units_and_bare_minutes():
    assert parse_expires("30s") == timedelta(seconds=30)
    assert parse_expires("2m") == timedelta(minutes=2)
    assert parse_expires("1h") == timedelta(hours=1)  # the ceiling itself
    assert parse_expires("10") == timedelta(minutes=10)  # bare = minutes


@pytest.mark.parametrize(
    "value",
    ["0", "0s", "00m", "61m", "2h", "7200s", "-5m", "abc", "5x", "5 m", "1.5h"],
)
def test_parse_expires_rejects_out_of_range_and_malformed(value):
    with pytest.raises(ValueError):
        parse_expires(value)


def test_parse_expires_routes_huge_durations_to_the_ceiling_error():
    # A huge digit string parses as a big int but overflows timedelta itself;
    # it must surface as the ordinary ceiling error, never OverflowError.
    with pytest.raises(ValueError, match="one hour"):
        parse_expires("99999999999999999999s")


# ---------------------------------------------------------------------------
# Registry resolution (local worktree behavior).
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> SourceRegistry:
    return SourceRegistry(tmp_path / "source-registry")


def test_resolve_registry_binding_returns_current_version(registry: SourceRegistry):
    # One active source has exactly one current version (a second version
    # only enters through the retire→reactivate proposal path, ADR-0014).
    registry.register_source("policy", RAW, filename="report.pdf", content_type="application/pdf")
    binding = resolve_registry_binding(registry, "policy")
    assert binding.source_id == "policy"
    assert binding.content_hash == artifact_content_hash(RAW)
    assert binding.filename == "report.pdf"
    assert binding.content_type == "application/pdf"
    assert binding.size == len(RAW)
    assert binding.published_version is None


def test_resolve_registry_binding_unknown_id_is_absent_binding(registry: SourceRegistry):
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_registry_binding(registry, "ghost")
    assert excinfo.value.outcome == OUTCOME_ABSENT_BINDING


def test_resolve_registry_binding_rejects_secret_bearing_id_without_echo(
    registry: SourceRegistry,
):
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_registry_binding(registry, "token=abc123")
    assert excinfo.value.outcome == OUTCOME_ABSENT_BINDING
    assert "token=abc123" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Source Binding Manifest resolution (S3 / --published-version semantics).
# ---------------------------------------------------------------------------


def _store_with_manifest(version: str, entries: list[SourceBindingEntry]):
    store = InMemoryArtifactStore()
    manifest = SourceBindingManifest(
        published_version=version, fingerprint="fp", created_at="2026-01-01T00:00:00Z"
    )
    object.__setattr__(manifest, "entries", entries)
    store.put_binding_manifest(version, msgspec.json.encode(manifest))
    return store


def test_resolve_manifest_binding_returns_the_exact_bound_hash():
    digest = artifact_content_hash(RAW)
    store = _store_with_manifest(
        "v2026",
        [
            SourceBindingEntry(
                page_title="Annual Report",
                source_id="annual-report",
                content_hash=digest,
                content_type="application/pdf",
                filename="annual.pdf",
                size=len(RAW),
            )
        ],
    )
    binding = resolve_manifest_binding(store, "v2026", "annual-report")
    assert binding.content_hash == digest
    assert binding.filename == "annual.pdf"
    assert binding.content_type == "application/pdf"
    assert binding.size == len(RAW)
    assert binding.published_version == "v2026"


def test_resolve_manifest_binding_never_substitutes_latest_version():
    # The bound hash is deliberately NOT the bytes' real digest: a manifest
    # resolution must return exactly what the manifest bound, never fall back
    # to "the latest version" or a live recomputation.
    store = _store_with_manifest(
        "v2020",
        [SourceBindingEntry(page_title="P", source_id="old", content_hash="ab" * 32)],
    )
    binding = resolve_manifest_binding(store, "v2020", "old")
    assert binding.content_hash == "ab" * 32
    assert binding.published_version == "v2020"


def test_resolve_manifest_binding_missing_manifest_is_historical_mismatch():
    store = InMemoryArtifactStore()
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_manifest_binding(store, "v-does-not-exist", "annual-report")
    assert excinfo.value.outcome == OUTCOME_HISTORICAL_VERSION_MISMATCH
    assert "latest" in str(excinfo.value)


def test_resolve_manifest_binding_unbound_source_is_absent_binding():
    store = _store_with_manifest(
        "v2026",
        [SourceBindingEntry(page_title="P", source_id="other", content_hash="cd" * 32)],
    )
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_manifest_binding(store, "v2026", "annual-report")
    assert excinfo.value.outcome == OUTCOME_ABSENT_BINDING


def test_resolve_manifest_binding_malformed_manifest_is_corruption():
    store = InMemoryArtifactStore()
    store.put_binding_manifest("v-corrupt", b"{not-json")
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_manifest_binding(store, "v-corrupt", "annual-report")
    assert excinfo.value.outcome == OUTCOME_CORRUPTION


def test_resolve_manifest_binding_rejects_secret_bearing_id_without_echo():
    store = _store_with_manifest("v2026", [])
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_manifest_binding(store, "v2026", "password=hunter2")
    assert excinfo.value.outcome == OUTCOME_ABSENT_BINDING
    assert "password=hunter2" not in str(excinfo.value)


def test_resolve_manifest_binding_identity_mismatch_is_corruption():
    # A manifest stored under one version key but naming another version is
    # misplaced/tampered state: never a usable binding (#165 review).
    store = _store_with_manifest(
        "v-wrong-identity",
        [SourceBindingEntry(page_title="P", source_id="report", content_hash="ef" * 32)],
    )
    # Re-store it under a DIFFERENT version label than it declares.
    raw = store.get_binding_manifest("v-wrong-identity")
    store.put_binding_manifest("v-requested", raw)
    with pytest.raises(SourceInspectionError) as excinfo:
        resolve_manifest_binding(store, "v-requested", "report")
    assert excinfo.value.outcome == OUTCOME_CORRUPTION


# ---------------------------------------------------------------------------
# Fetch: byte-exact retrieval with digest and size re-verification.
# ---------------------------------------------------------------------------


def _binding(
    source_id: str = "annual-report",
    content_hash: str | None = None,
    filename: str | None = "annual.pdf",
    content_type: str | None = "application/pdf",
    size: int | None = None,
    published_version: str | None = None,
) -> ResolvedSourceBinding:
    return ResolvedSourceBinding(
        source_id=source_id,
        content_hash=content_hash or artifact_content_hash(RAW),
        filename=filename,
        content_type=content_type,
        size=size if size is not None else len(RAW),
        published_version=published_version,
    )


def test_fetch_verified_artifact_returns_byte_exact_original():
    store = InMemoryArtifactStore()
    store.put_artifact(
        source_id="annual-report",
        content_hash=artifact_content_hash(RAW),
        raw_bytes=RAW,
        content_type="application/pdf",
        filename="annual.pdf",
    )
    assert fetch_verified_artifact(store, _binding()) == RAW


def test_fetch_verified_artifact_unavailable_outcome():
    store = InMemoryArtifactStore()  # nothing retained
    with pytest.raises(SourceInspectionError) as excinfo:
        fetch_verified_artifact(store, _binding())
    assert excinfo.value.outcome == OUTCOME_UNAVAILABLE


def test_fetch_verified_artifact_corruption_outcome_on_digest_mismatch(tmp_path: Path):
    # Tamper with retained bytes behind the store's back.
    store = LocalDirectoryArtifactStore(tmp_path / "store")
    digest = artifact_content_hash(RAW)
    store.put_artifact(
        source_id="annual-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type=None,
        filename=None,
    )
    victim = tmp_path / "store" / "artifacts" / "annual-report" / digest
    victim.write_bytes(RAW + b"tampered")
    with pytest.raises(SourceInspectionError) as excinfo:
        fetch_verified_artifact(store, _binding())
    assert excinfo.value.outcome == OUTCOME_CORRUPTION


def test_fetch_verified_artifact_rejects_size_mismatch_as_corruption():
    store = InMemoryArtifactStore()
    store.put_artifact(
        source_id="annual-report",
        content_hash=artifact_content_hash(RAW),
        raw_bytes=RAW,
        content_type=None,
        filename=None,
    )
    # The binding claims a size the store cannot reproduce.
    with pytest.raises(SourceInspectionError) as excinfo:
        fetch_verified_artifact(store, _binding(size=len(RAW) + 1))
    assert excinfo.value.outcome == OUTCOME_CORRUPTION


def test_fetch_verified_artifact_access_denied_outcome(tmp_path: Path):
    # A private local store whose artifact directory is unreadable: the read
    # itself (not a pre-check) must report access denial, distinct from
    # absence. Skipped for root (root ignores permission bits).
    import os

    if os.geteuid() == 0:  # pragma: no cover
        pytest.skip("permission bits are not enforced for root")
    store = LocalDirectoryArtifactStore(tmp_path / "store")
    digest = artifact_content_hash(RAW)
    store.put_artifact(
        source_id="annual-report",
        content_hash=digest,
        raw_bytes=RAW,
        content_type=None,
        filename=None,
    )
    (tmp_path / "store" / "artifacts" / "annual-report").chmod(0o000)
    try:
        with pytest.raises(SourceInspectionError) as excinfo:
            fetch_verified_artifact(store, _binding())
        assert excinfo.value.outcome == OUTCOME_ACCESS_DENIED
    finally:
        (tmp_path / "store" / "artifacts" / "annual-report").chmod(0o700)


# ---------------------------------------------------------------------------
# Safe fetch destinations (original unsafe paths reduce to safe filenames).
# ---------------------------------------------------------------------------


def test_safe_fetch_destination_uses_explicit_path_verbatim(tmp_path: Path):
    target = tmp_path / "exact" / "name.bin"
    assert safe_fetch_destination(target, _binding()) == target


def test_safe_fetch_destination_directory_receives_safe_filename(tmp_path: Path):
    out = tmp_path / "downloads"
    out.mkdir()
    assert safe_fetch_destination(out, _binding()) == out / "annual.pdf"


def test_safe_fetch_destination_reduces_unsafe_filenames_to_basenames(tmp_path: Path):
    out = tmp_path / "downloads"
    out.mkdir()
    binding = _binding(filename="../../../../etc/passwd")
    destination = safe_fetch_destination(out, binding)
    assert destination.parent == out
    assert destination.name == "passwd"
    assert ".." not in str(destination)


def test_safe_fetch_destination_falls_back_to_source_bin(tmp_path: Path):
    out = tmp_path / "downloads"
    out.mkdir()
    destination = safe_fetch_destination(out, _binding(filename=None))
    assert destination.name == "annual-report.bin"
