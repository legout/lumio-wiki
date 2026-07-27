from __future__ import annotations

import msgspec
import lumio_wiki as lw

from lumio_wiki.ingest import (
    IngestProposal,
    IngestStore,
    SourceChangeImpact,
    SourceLifecycleChange,
    SourceProvenance,
)
from lumio_wiki.records import ValidationReport
from lumio_wiki.source_registry import SourceRegistry


def test_public_surface_exposes_serializable_source_lifecycle_records():
    assert lw.SourceChangeImpact is SourceChangeImpact
    assert lw.SourceLifecycleChange is SourceLifecycleChange


def test_explicit_source_registration_persists_immutable_versions(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")

    first = registry.register_source("handbook", b"v1")
    second = registry.register_source("handbook", b"v2")

    reloaded = SourceRegistry(tmp_path / "ingest")
    source = reloaded.get("handbook")

    assert [version.content_hash for version in source.versions] == [
        first.content_hash,
        second.content_hash,
    ]
    assert source.status == "active"
    assert source.versions[0].content_hash != source.versions[1].content_hash


def test_ingest_store_keeps_private_registry_outside_raw_and_proposal_trees(tmp_path):
    store = IngestStore(tmp_path / "ingest")

    store.source_registry.register_source("handbook", b"v1")

    assert store.source_registry.root == (tmp_path / "ingest" / "source-registry").resolve()
    assert (store.source_registry.root / "sources.json").is_file()
    assert not list(store.raw_dir.rglob("sources.json"))
    assert not list(store.proposals_dir.rglob("sources.json"))


def test_proposal_source_lifecycle_metadata_round_trips_without_page_changes():
    change = SourceLifecycleChange(
        action="retire",
        source_id="handbook",
        trigger="source handbook retired",
        impacts=[SourceChangeImpact(page_title="Handbook", status="sole-source-lost")],
    )
    proposal = IngestProposal(
        id="proposal",
        status="staged",
        created_at="2026-07-27T00:00:00+00:00",
        provenance=SourceProvenance(None, None, "source-lifecycle"),
        proposed_pages=[],
        affected_pages=["Handbook"],
        diff="",
        validation_report=ValidationReport(),
        blocked=False,
        source_change=change,
    )

    decoded = msgspec.json.decode(msgspec.json.encode(proposal), type=IngestProposal)

    assert decoded.proposed_pages == []
    assert decoded.source_change == change
