from __future__ import annotations

from pathlib import Path

import lumio_wiki as lw
import msgspec
import pytest
from lumio_wiki.ingest import (
    IngestProposal,
    IngestStore,
    SourceChangeImpact,
    SourceLifecycleChange,
    SourceProvenance,
)
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.records import ValidationReport
from lumio_wiki.source_registry import (
    KnowledgeSource,
    RetirementCandidate,
    SourceRegistry,
    SourceRegistryError,
    SourceVersion,
)


def _knowledge_base(tmp_path: Path, source_ids: list[str]):
    root = tmp_path / "kb"
    root.mkdir()
    source_lines = "\n".join(
        f'  - id: "{source_id}"\n    title: "{source_id} source"' for source_id in source_ids
    )
    (root / "policy.md").write_text(
        f"""---
title: "Policy"
aliases: []
tags:
  - "policy"
summary: "The policy."
lifecycle: "approved"
visibility: "public"
sources:
{source_lines}
relationships: []
synthetic: false
---

# Policy

Policy text.
""",
        encoding="utf-8",
    )
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


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


def test_retiring_final_support_stages_without_changing_active_source(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    proposal = pipeline.retire_source("policy")

    assert proposal.status == "staged"
    assert proposal.source_change.action == "retire"
    assert proposal.source_change.impacts == [SourceChangeImpact("Policy", "sole-source-lost")]
    assert store.source_registry.get("policy").status == "active"


def test_retiring_one_of_two_supports_is_informational(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["primary", "independent"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("primary", b"p")
    pipeline.register_source("independent", b"i")

    proposal = pipeline.retire_source("primary")

    assert proposal.source_change.impacts == [SourceChangeImpact("Policy", "still-supported")]


def test_missing_signal_creates_candidate_without_changing_support(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    assert candidate.status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_dismissing_candidate_records_decision_without_staging_proposal(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watcher missing")

    dismissed = pipeline.dismiss_retirement_candidate(candidate.id)

    assert dismissed.status == "dismissed"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_confirming_candidate_stages_retirement_and_records_decision(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watcher missing")

    proposal = pipeline.confirm_retirement_candidate(candidate.id)

    assert proposal.source_change.action == "retire"
    assert store.source_registry.get_candidate(candidate.id).status == "confirmed"
    assert store.source_registry.get("policy").status == "active"


def test_publishing_retirement_flips_state_without_removing_pages(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")

    published = pipeline.publish(proposal.id)

    assert published.status == "published"
    assert published.proposed_pages == []
    assert store.source_registry.get("policy").status == "retired"
    assert (kb.root / "policy.md").is_file()


def test_blocked_retirement_keeps_source_active(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")
    store.save_proposal(msgspec.structs.replace(proposal, blocked=True))

    with pytest.raises(lw.ProposalBlockedError):
        pipeline.publish(proposal.id)

    assert store.source_registry.get("policy").status == "active"


def test_failed_retirement_publication_keeps_source_active(tmp_path, monkeypatch) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")

    def fail_artifact_publication(_root):
        raise RuntimeError("artifact publication failed")

    monkeypatch.setattr(
        "lumio_wiki.proposal_pipeline.publish_reserved_artifacts",
        fail_artifact_publication,
    )

    with pytest.raises(RuntimeError, match="artifact publication failed"):
        pipeline.publish(proposal.id)

    assert store.source_registry.get("policy").status == "active"


def test_reactivation_appends_new_version_and_activates_only_after_publish(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    first = pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    reactivation = pipeline.reactivate_source("policy", b"policy-v2")

    retired = store.source_registry.get("policy")
    assert reactivation.source_change.action == "reactivate"
    assert reactivation.proposed_pages == []
    assert retired.status == "retired"
    assert [version.content_hash for version in retired.versions] == [first.content_hash]

    pipeline.publish(reactivation.id)

    active = store.source_registry.get("policy")
    assert active.status == "active"
    assert len(active.versions) == 2
    assert active.versions[0].source_id == active.versions[1].source_id == "policy"
    assert active.versions[0].content_hash != active.versions[1].content_hash


@pytest.mark.parametrize(
    ("source_ids", "expected_status"),
    [
        (["policy"], "sole-source-lost"),
        (["policy", "independent"], "still-supported"),
    ],
)
def test_public_reactivation_impact_uses_only_allowed_current_support_vocabulary(
    tmp_path, source_ids, expected_status
) -> None:
    kb = _knowledge_base(tmp_path, source_ids)
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    for source_id in source_ids:
        pipeline.register_source(source_id, source_id.encode())
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    reactivation = pipeline.reactivate_source("policy", b"policy-v2")

    statuses = {impact.status for impact in reactivation.source_change.impacts}
    assert statuses == {expected_status}
    assert statuses <= {"still-supported", "sole-source-lost"}


def test_terminal_proposal_persistence_failure_keeps_reactivation_state_unchanged(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)
    reactivation = pipeline.reactivate_source("policy", b"policy-v2")
    before = store.source_registry.get("policy")

    def fail_terminal_persistence(_proposal_id):
        raise OSError("terminal proposal persistence failed")

    monkeypatch.setattr(store, "publish", fail_terminal_persistence)

    with pytest.raises(OSError, match="terminal proposal persistence failed"):
        pipeline.publish(reactivation.id)

    assert store.source_registry.get("policy") == before
    assert pipeline.review(reactivation.id).status == "staged"


def test_source_transition_persistence_failure_rolls_back_terminal_proposal(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)
    reactivation = pipeline.reactivate_source("policy", b"policy-v2")
    before = store.source_registry.get("policy")

    def fail_registry_persistence():
        raise OSError("registry persistence failed")

    monkeypatch.setattr(store.source_registry, "_write", fail_registry_persistence)

    with pytest.raises(OSError, match="registry persistence failed"):
        pipeline.publish(reactivation.id)

    assert store.source_registry.get("policy") == before
    assert pipeline.review(reactivation.id).status == "staged"


def test_reactivation_requires_a_retired_source(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    with pytest.raises(SourceRegistryError, match="must be retired"):
        pipeline.reactivate_source("policy", b"policy-v2")


def test_blocked_reactivation_keeps_source_retired(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)
    reactivation = pipeline.reactivate_source("policy", b"policy-v2")
    store.save_proposal(msgspec.structs.replace(reactivation, blocked=True))

    with pytest.raises(lw.ProposalBlockedError):
        pipeline.publish(reactivation.id)

    source = store.source_registry.get("policy")
    assert source.status == "retired"
    assert len(source.versions) == 1


def test_pipeline_facing_lifecycle_contracts_are_public_but_registry_is_private():
    assert lw.SourceRegistryError is SourceRegistryError
    assert lw.SourceVersion is SourceVersion
    assert lw.KnowledgeSource is KnowledgeSource
    assert lw.RetirementCandidate is RetirementCandidate
    assert not hasattr(lw, "SourceRegistry")


def test_private_registry_activity_does_not_change_kb_or_export_bytes(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    before = (
        lw.fingerprint_sources(kb.root),
        lw.export_bundle(kb),
        msgspec.json.encode(lw.export_okf_profile1(kb.public_pages())),
    )

    pipeline.register_source("policy", b"policy-v1")
    pipeline.record_retirement_candidate("policy", "watcher missing")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    after = (
        lw.fingerprint_sources(kb.root),
        lw.export_bundle(kb),
        msgspec.json.encode(lw.export_okf_profile1(kb.public_pages())),
    )
    assert after == before


def test_discarding_retirement_releases_pending_transition(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    first = pipeline.retire_source("policy")

    discarded = pipeline.discard(first.id)
    replacement = pipeline.retire_source("policy")

    assert discarded.status == "discarded"
    assert replacement.status == "staged"
    assert store.source_registry.get("policy").status == "active"


def test_confirming_a_dismissed_candidate_does_not_stage_retirement(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watcher missing")
    pipeline.dismiss_retirement_candidate(candidate.id)

    with pytest.raises(SourceRegistryError, match="is not pending"):
        pipeline.confirm_retirement_candidate(candidate.id)

    assert pipeline.list() == []
    assert store.source_registry.get("policy").status == "active"
