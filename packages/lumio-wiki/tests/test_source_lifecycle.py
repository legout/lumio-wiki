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


def test_register_creates_a_single_version_active_identity(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")

    version = registry.register_source("handbook", b"v1")

    reloaded = SourceRegistry(tmp_path / "ingest")
    source = reloaded.get("handbook")
    assert source.status == "active"
    assert [v.content_hash for v in source.versions] == [version.content_hash]


def test_second_register_of_active_source_is_refused_without_appending(tmp_path):
    # #133 final review: register establishes a NEW identity only. A second
    # registration of an already-known active id must NOT append a version —
    # unreviewed replacement cannot bypass review. Reactivation is the only
    # reviewed path that appends a new Source Version.
    registry = SourceRegistry(tmp_path / "ingest")
    first = registry.register_source("handbook", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError, match="replacement is not available"):
        registry.register_source("handbook", b"v2")

    source = registry.get("handbook")
    assert [v.content_hash for v in source.versions] == [first.content_hash]
    # No mutation at all: the live and reloaded registry are byte-identical.
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_second_register_error_does_not_echo_the_source_id(tmp_path):
    # The id may carry secret-bearing material; the generic error must never
    # echo it (mirrors the trigger-validation boundary contract).
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    secret_id = "sk-leaked-api-key"

    with pytest.raises(SourceRegistryError) as exc:
        registry.register_source(secret_id, b"v2")

    assert secret_id not in str(exc.value)
    assert "handbook" not in str(exc.value)


def test_reactivation_is_the_only_path_that_appends_a_reviewed_new_version(
    tmp_path,
) -> None:
    # register → retire publish → reactivation publish preserves the first
    # immutable version and appends the second; register cannot append it.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    first = pipeline.register_source("policy", b"policy-v1")

    # Registering again is refused even before retirement: the only way to add
    # a version is the reviewed retire→reactivate path.
    with pytest.raises(SourceRegistryError, match="replacement is not available"):
        pipeline.register_source("policy", b"policy-v2")

    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)
    reactivation = pipeline.reactivate_source("policy", b"policy-v2")
    pipeline.publish(reactivation.id)

    active = store.source_registry.get("policy")
    assert active.status == "active"
    assert len(active.versions) == 2
    assert active.versions[0].content_hash == first.content_hash
    assert active.versions[1].content_hash != first.content_hash
    assert active.versions[0].source_id == active.versions[1].source_id == "policy"


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
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    dismissed = pipeline.dismiss_retirement_candidate(candidate.id)

    assert dismissed.status == "dismissed"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_dismiss_candidate_validates_expected_source_id_before_mutating(tmp_path) -> None:
    # Issue #133: dismissing is mutating, so the caller must name the source
    # the candidate belongs to. A mismatch is refused and leaves the candidate
    # pending (no mutation, no proposal staged).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError, match="does not belong"):
        pipeline.dismiss_retirement_candidate(candidate.id, expected_source_id="other")

    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_dismiss_candidate_accepts_matching_expected_source_id(tmp_path) -> None:
    # The optional expected source id must match exactly; a matching id
    # dismisses the candidate (backward-compatible validation path).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    dismissed = pipeline.dismiss_retirement_candidate(candidate.id, expected_source_id="policy")

    assert dismissed.status == "dismissed"
    assert store.source_registry.get_candidate(candidate.id).status == "dismissed"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_confirming_candidate_stages_retirement_and_records_decision(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

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


# --- #133 final review: action-aware lifecycle impact computation ---
#
# Retirement evaluates support AFTER excluding the retiring source (a sole
# source is sole-source-lost). Reactivation evaluates support AFTER including
# the reactivated source — it will be active once the proposal publishes — so
# EVERY affected page sourced by that id is still-supported, even a page that
# would be sole-source-lost under retirement. The allowed vocabulary stays
# exactly still-supported / sole-source-lost.


def test_reactivation_impact_counts_the_reactivated_source_as_support(tmp_path) -> None:
    # Sole-source reactivation: the "Policy" page is sourced ONLY by "policy".
    # Under retirement this would be sole-source-lost; under reactivation the
    # reactivated source itself restores support, so the page is still-supported.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    pipeline.publish(pipeline.retire_source("policy").id)

    reactivation = pipeline.reactivate_source("policy", b"policy-v2")

    statuses = {impact.status for impact in reactivation.source_change.impacts}
    assert statuses == {"still-supported"}
    assert statuses <= {"still-supported", "sole-source-lost"}


def test_reactivation_impact_with_independent_support_is_still_supported(tmp_path) -> None:
    # With an independent active support, reactivation is still-supported and
    # the vocabulary never leaves the allowed set.
    kb = _knowledge_base(tmp_path, ["policy", "independent"])
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    for source_id in ("policy", "independent"):
        pipeline.register_source(source_id, source_id.encode())
    pipeline.publish(pipeline.retire_source("policy").id)

    reactivation = pipeline.reactivate_source("policy", b"policy-v2")

    statuses = {impact.status for impact in reactivation.source_change.impacts}
    assert statuses == {"still-supported"}
    assert statuses <= {"still-supported", "sole-source-lost"}


def test_retirement_impact_remains_action_aware_for_a_sole_source(tmp_path) -> None:
    # Contrast: retirement of the SAME sole source is sole-source-lost. The
    # computation is action-aware — retirement excludes the retiring source,
    # reactivation includes it — so the two actions diverge on a sole source.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = lw.IngestStore(tmp_path / "ingest")
    pipeline = lw.ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    retirement = pipeline.retire_source("policy")

    statuses = {impact.status for impact in retirement.source_change.impacts}
    assert statuses == {"sole-source-lost"}


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
    pipeline.record_retirement_candidate("policy", "watched file missing")
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
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")
    pipeline.dismiss_retirement_candidate(candidate.id)

    with pytest.raises(SourceRegistryError, match="is not pending"):
        pipeline.confirm_retirement_candidate(candidate.id)

    assert pipeline.list() == []
    assert store.source_registry.get("policy").status == "active"


# --- Task 2 durability: failure-atomic registry mutations (ADR-0014) ---


def _raise_registry_write() -> None:
    raise OSError("registry persistence failed")


def _state_snapshot(registry: SourceRegistry) -> bytes:
    """Encode the full private registry state for byte-exact comparison."""
    return msgspec.json.encode(registry._state)


def test_register_source_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    # Register a NEW identity so the write path is reached (a second register
    # of an existing id is refused before persistence — see
    # test_second_register_of_active_source_is_refused_without_appending).
    with pytest.raises(OSError, match="registry persistence failed"):
        registry.register_source("manual", b"v1")

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_bind_pending_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    transition = registry.stage_retirement("handbook")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        registry.bind_pending(transition, "proposal-1")

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_cancel_transition_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    registry.bind_pending(registry.stage_retirement("handbook"), "proposal-1")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        registry.cancel_transition("proposal-1")

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_record_retirement_candidate_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        registry.record_retirement_candidate("handbook", "watched file missing")

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_decide_candidate_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    candidate = registry.record_retirement_candidate("handbook", "watched file missing")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        registry.confirm_retirement_candidate(candidate.id)

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_apply_transition_write_failure_restores_live_and_reloaded_state(
    tmp_path, monkeypatch
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    registry.bind_pending(registry.stage_retirement("handbook"), "proposal-1")
    prior = _state_snapshot(registry)
    monkeypatch.setattr(registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        registry.apply_transition("proposal-1")

    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


# --- Task 2 durability: paired Pipeline write compensation (ADR-0014) ---


def test_retire_source_transition_bind_failure_leaves_no_reviewable_proposal(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    monkeypatch.setattr(store.source_registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        pipeline.retire_source("policy")

    assert pipeline.list() == []
    assert store.source_registry.get("policy").status == "active"
    monkeypatch.undo()
    assert pipeline.retire_source("policy").status == "staged"


def test_retire_source_proposal_save_failure_cancels_bound_transition(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    def fail_save_proposal(proposal, raw_path=None):
        raise OSError("proposal persistence failed")

    monkeypatch.setattr(store, "save_proposal", fail_save_proposal)

    with pytest.raises(OSError, match="proposal persistence failed"):
        pipeline.retire_source("policy")

    assert pipeline.list() == []
    monkeypatch.undo()
    assert pipeline.retire_source("policy").status == "staged"


def test_reactivate_source_transition_bind_failure_leaves_no_orphan_transition(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    pipeline.publish(pipeline.retire_source("policy").id)
    monkeypatch.setattr(store.source_registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        pipeline.reactivate_source("policy", b"policy-v2")

    assert [p for p in pipeline.list() if lw.is_reviewable_proposal(p)] == []
    monkeypatch.undo()
    assert pipeline.reactivate_source("policy", b"policy-v2").status == "staged"


def test_confirm_candidate_decision_failure_leaves_no_staged_transition(
    tmp_path, monkeypatch
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    def fail_decision(candidate_id):
        raise OSError("candidate decision failed")

    monkeypatch.setattr(
        store.source_registry, "confirm_retirement_candidate", fail_decision
    )

    with pytest.raises(OSError, match="candidate decision failed"):
        pipeline.confirm_retirement_candidate(candidate.id)

    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert [p for p in pipeline.list() if lw.is_reviewable_proposal(p)] == []
    monkeypatch.undo()
    assert pipeline.confirm_retirement_candidate(candidate.id).source_change.action == "retire"


def test_discard_cancel_failure_restores_reviewable_proposal(tmp_path, monkeypatch) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")
    monkeypatch.setattr(store.source_registry, "_write", _raise_registry_write)

    with pytest.raises(OSError, match="registry persistence failed"):
        pipeline.discard(proposal.id)

    restored = pipeline.review(proposal.id)
    assert restored.status == "staged"
    assert lw.is_reviewable_proposal(restored)
    with pytest.raises(SourceRegistryError, match="already has a pending transition"):
        pipeline.retire_source("policy")


# --- Task 3: ProposalPipeline as the CLI read seam for source listing ---


def test_list_sources_exposes_registered_identities_through_the_pipeline(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    assert pipeline.list_sources() == []
    pipeline.register_source("policy", b"policy-v1")
    sources = pipeline.list_sources()
    assert [source.source_id for source in sources] == ["policy"]
    assert sources[0].status == "active"
    assert len(sources[0].versions) == 1


def test_list_sources_without_store_returns_empty(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    pipeline = ProposalPipeline(kb)
    assert pipeline.list_sources() == []


# --- Task 4 fix: candidate review journey and ownership-safe confirmation ---


def test_list_retirement_candidates_exposes_recorded_candidates_through_pipeline(
    tmp_path,
) -> None:
    # A narrow read query so the Workshop can render pending candidates for
    # review. Mirrors list_sources: returns recorded candidates in registry
    # order, empty before any are recorded.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    assert pipeline.list_retirement_candidates() == []
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    listed = pipeline.list_retirement_candidates()

    assert [item.id for item in listed] == [candidate.id]
    assert listed[0].source_id == "policy"
    assert listed[0].trigger == "watched file missing"
    assert listed[0].status == "pending"


def test_list_retirement_candidates_without_store_returns_empty(tmp_path) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    pipeline = ProposalPipeline(kb)
    assert pipeline.list_retirement_candidates() == []


def test_confirm_candidate_validates_expected_source_id_before_mutating(tmp_path) -> None:
    # Issue #133: confirming is mutating (it stages a retirement proposal), so
    # the caller must name the source the candidate belongs to. A mismatch is
    # refused and leaves the candidate pending and the source active, with no
    # proposal staged (mirroring dismiss_retirement_candidate).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError, match="does not belong"):
        pipeline.confirm_retirement_candidate(candidate.id, expected_source_id="other")

    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


def test_confirm_candidate_accepts_matching_expected_source_id(tmp_path) -> None:
    # The optional expected source id must match exactly; a matching id
    # confirms the candidate (backward-compatible validation path).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    proposal = pipeline.confirm_retirement_candidate(
        candidate.id, expected_source_id="policy"
    )

    assert proposal.source_change.action == "retire"
    assert store.source_registry.get_candidate(candidate.id).status == "confirmed"
    assert store.source_registry.get("policy").status == "active"


# --- Task 4 Terra fix: controlled retirement candidate trigger vocabulary (#133) ---

# The exact controlled vocabulary a Retirement Candidate must disclose. Free
# text is rejected so secret-bearing or arbitrary input can never be persisted
# or rendered.
_EXPECTED_RETIREMENT_TRIGGERS = (
    "watched file missing",
    "failed read",
    "object store unavailable",
    "incomplete upload",
)


def test_retirement_candidate_trigger_vocabulary_is_the_controlled_set() -> None:
    # #133: the pipeline exposes ONE shared, public constant naming the only
    # accepted candidate triggers, so every surface (CLI, Workshop) shares the
    # exact safe vocabulary instead of each inventing its own.
    assert tuple(lw.RETIREMENT_CANDIDATE_TRIGGERS) == _EXPECTED_RETIREMENT_TRIGGERS


@pytest.mark.parametrize("trigger", _EXPECTED_RETIREMENT_TRIGGERS)
def test_retirement_candidate_accepts_each_controlled_trigger(trigger, tmp_path) -> None:
    # Each Maintainer-chosen signal is recorded verbatim and round-trips through
    # the private registry (the disclosed trigger stays safe and escaped).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    candidate = pipeline.record_retirement_candidate("policy", trigger)

    assert candidate.trigger == trigger
    assert candidate.status == "pending"
    assert store.source_registry.get_candidate(candidate.id).trigger == trigger


def test_retirement_candidate_rejects_trigger_outside_controlled_vocabulary(tmp_path) -> None:
    # A secret-bearing or arbitrary trigger is rejected at the registry boundary
    # WITHOUT being persisted and WITHOUT being echoed into the error message.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    secret = "password=hunter2;token=abc123"
    with pytest.raises(SourceRegistryError) as exc:
        pipeline.record_retirement_candidate("policy", secret)

    # The error is generic and never echoes the secret-bearing trigger.
    assert secret not in str(exc.value)
    # No candidate was persisted.
    assert store.source_registry.list_candidates() == []
    # The source is unaffected.
    assert store.source_registry.get("policy").status == "active"


# --- Final fix: safe source-id label contract at the registry boundary (#133) ---

# A Knowledge Source id is a stable, Maintainer-authored lowercase ASCII label.
# It must never carry a path, URL, content hash, whitespace/control character,
# or an obvious credential prefix: a pasted credential (sk-..., token=...,
# api-key...) must be rejected at the boundary without being echoed or
# persisted. Defense in depth: the registry boundary check backs up the CLI and
# Workshop so a crafted request can never bypass it.
_INVALID_SOURCE_IDS = (
    "",  # empty
    "   ",  # whitespace only
    "Policy",  # uppercase rejected (lowercase ASCII label only)
    "pol icy",  # internal whitespace
    "pol/icy",  # path separator
    "pol\\icy",  # path separator (backslash)
    "http://example.com/policy",  # URL
    "s3://bucket/policy",  # object-store URI
    "policy:v2",  # colon (scheme/credential separator)
    "a" * 49,  # exceeds the 48-char maximum
    "9policy",  # must start with a letter
    "_policy",  # must start with a letter
    "-policy",  # must start with a letter
    "1" * 64,  # 64-char hash rejected (length + not a letter-led label)
    "sk-abc123secret",  # credential prefix
    "token-policy",  # credential prefix
    "password-vault",  # credential prefix
    "secret-handbook",  # credential prefix
    "api-key-policy",  # credential prefix
)


@pytest.mark.parametrize("source_id", _INVALID_SOURCE_IDS)
def test_register_rejects_invalid_source_id_without_persisting_or_echoing(
    source_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.register_source(source_id, b"bytes")

    # The rejected value is never echoed into the generic error. (The empty /
    # whitespace cases are skipped: the empty string is trivially a substring
    # of every message, so the echo check is only meaningful for real input.)
    if source_id.strip():
        assert source_id not in str(exc.value)
    # Nothing was persisted and the live/reloaded registry is byte-identical.
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_register_accepts_the_safe_label_contract(tmp_path) -> None:
    # A Maintainer-authored lowercase ASCII label — letters, digits, dot,
    # underscore, hyphen, letter-led, at most 48 chars — is accepted.
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    registry.register_source("policy.v2", b"v1")
    registry.register_source("team_handbook", b"v1")
    registry.register_source("release-notes", b"v1")
    registry.register_source("a" * 48, b"v1")  # exactly the maximum length

    ids = {source.source_id for source in registry.list()}
    assert ids == {"handbook", "policy.v2", "team_handbook", "release-notes", "a" * 48}


def test_pipeline_register_rejects_invalid_source_id(tmp_path) -> None:
    # The ProposalPipeline delegates to the registry boundary check.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    with pytest.raises(SourceRegistryError):
        pipeline.register_source("sk-leaked-key", b"bytes")
    assert pipeline.list_sources() == []


# --- #133 final review: safe lifecycle identifiers at EVERY boundary ---
#
# _validate_source_id runs in get, retire/reactivate, candidate record, and the
# expected-source checks; candidate ids must be UUID-hex-shaped. A
# secret-bearing invalid source/candidate id is rejected at the boundary with a
# GENERIC error that never echoes the value, and never mutates state. Defense
# in depth: the registry boundary backs up the CLI and Workshop.

# Secret-bearing / structural-invalid identifiers a crafted request might use to
# probe the registry lookup or interpolate into an error message.
_SECRET_SOURCE_IDS = (
    "sk-leaked-api-key",
    "token=abc123",
    "/secret/credentials.key",
    "s3://bucket/leaked",
)
_SECRET_CANDIDATE_IDS = (
    "sk-leaked-api-key",
    "token=abc123;password=hunter2",
    "/var/lib/lumio/secret.key",
    "not-a-uuid-hex",
    "deadbeef",  # too short to be a 32-char uuid hex
)


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_get_rejects_secret_bearing_source_id_without_echoing(
    source_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")

    with pytest.raises(SourceRegistryError) as exc:
        registry.get(source_id)

    assert source_id not in str(exc.value)


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_stage_retirement_rejects_secret_bearing_source_id(
    source_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.stage_retirement(source_id)

    assert source_id not in str(exc.value)
    # No mutation: no pending transition was bound.
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_stage_reactivation_rejects_secret_bearing_source_id(
    source_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.stage_reactivation(source_id, b"bytes")

    assert source_id not in str(exc.value)
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_record_candidate_rejects_secret_bearing_source_id(
    source_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.record_retirement_candidate(source_id, "watched file missing")

    assert source_id not in str(exc.value)
    # No candidate recorded.
    assert registry.list_candidates() == []
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


@pytest.mark.parametrize("candidate_id", _SECRET_CANDIDATE_IDS)
def test_registry_get_candidate_rejects_secret_bearing_candidate_id(
    candidate_id, tmp_path
) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    real = registry.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError) as exc:
        registry.get_candidate(candidate_id)

    assert candidate_id not in str(exc.value)
    # The real candidate is untouched.
    assert registry.get_candidate(real.id).id == real.id


@pytest.mark.parametrize("candidate_id", _SECRET_CANDIDATE_IDS)
def test_registry_decide_candidate_rejects_secret_bearing_candidate_id(
    candidate_id, tmp_path
) -> None:
    # The registry-level confirm/dismiss must validate the candidate id before
    # lookup or interpolation, so a secret-bearing id is rejected generically
    # and the real candidate is left pending.
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    real = registry.record_retirement_candidate("policy", "watched file missing")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.confirm_retirement_candidate(candidate_id)
    assert candidate_id not in str(exc.value)

    with pytest.raises(SourceRegistryError) as exc:
        registry.dismiss_retirement_candidate(candidate_id)
    assert candidate_id not in str(exc.value)

    assert _state_snapshot(registry) == prior
    assert registry.get_candidate(real.id).status == "pending"


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_pipeline_dismiss_validates_expected_source_id_before_mutating(
    source_id, tmp_path
) -> None:
    # A secret-bearing expected source id must be rejected before the mismatch
    # check can interpolate it, leaving the candidate pending (no mutation).
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError) as exc:
        pipeline.dismiss_retirement_candidate(candidate.id, expected_source_id=source_id)

    assert source_id not in str(exc.value)
    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_pipeline_confirm_validates_expected_source_id_before_staging(
    source_id, tmp_path
) -> None:
    # A secret-bearing expected source id must be rejected before staging a
    # retirement proposal or mutating the candidate.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError) as exc:
        pipeline.confirm_retirement_candidate(candidate.id, expected_source_id=source_id)

    assert source_id not in str(exc.value)
    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert pipeline.list() == []


@pytest.mark.parametrize("candidate_id", _SECRET_CANDIDATE_IDS)
def test_pipeline_dismiss_rejects_secret_bearing_candidate_id(
    candidate_id, tmp_path
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError) as exc:
        pipeline.dismiss_retirement_candidate(candidate_id, expected_source_id="policy")

    assert candidate_id not in str(exc.value)
    assert store.source_registry.get_candidate(candidate.id).status == "pending"


@pytest.mark.parametrize("candidate_id", _SECRET_CANDIDATE_IDS)
def test_pipeline_confirm_rejects_secret_bearing_candidate_id(
    candidate_id, tmp_path
) -> None:
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    candidate = pipeline.record_retirement_candidate("policy", "watched file missing")

    with pytest.raises(SourceRegistryError) as exc:
        pipeline.confirm_retirement_candidate(candidate_id, expected_source_id="policy")

    assert candidate_id not in str(exc.value)
    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert pipeline.list() == []


# --- Task 4 final fix: safe display of legacy persisted candidate triggers (#133) ---

# A Retirement Candidate persisted *before* trigger validation may carry a
# secret-bearing ``trigger`` value (the registry now rejects such input at the
# boundary, but cannot rewrite history). Rendering must never echo an
# unrecognized persisted trigger: recognized values render verbatim, every
# other persisted value renders one fixed, content-free generic label.


def test_public_surface_exposes_safe_candidate_trigger_display() -> None:
    # The pipeline exposes ONE shared, public safe-display function plus the
    # fixed generic label, so every rendering surface (Workshop, future CLI)
    # masks unrecognized persisted triggers identically instead of each
    # inventing its own fallback.
    assert callable(lw.safe_candidate_trigger_display)
    assert isinstance(lw.RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED, str)
    assert lw.RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED  # non-empty


@pytest.mark.parametrize("trigger", _EXPECTED_RETIREMENT_TRIGGERS)
def test_safe_candidate_trigger_display_returns_recognized_verbatim(trigger) -> None:
    # A trigger that is in the controlled vocabulary renders exactly as stored.
    assert lw.safe_candidate_trigger_display(trigger) == trigger


def test_safe_candidate_trigger_display_masks_unrecognized_secret() -> None:
    # A legacy candidate persisted with a secret-bearing trigger must never have
    # the stored value reflected into the UI. The function returns the fixed
    # generic label and NEVER the stored value.
    secret = "password=hunter2;token=abc123"
    rendered = lw.safe_candidate_trigger_display(secret)
    assert rendered == lw.RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED
    assert secret not in rendered
    # The fallback is the same fixed label regardless of the unrecognized value.
    assert lw.safe_candidate_trigger_display("") == lw.RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED
    assert (
        lw.safe_candidate_trigger_display("upstream repo archived")
        == lw.RETIREMENT_CANDIDATE_TRIGGER_UNRECOGNIZED
    )


# --- Task 4 final Terra fix: safe display of proposal lifecycle triggers (#133) ---


def test_public_surface_exposes_safe_lifecycle_trigger_display() -> None:
    # The pipeline exposes ONE shared, public safe-display function for proposal
    # lifecycle triggers plus the fixed generic fallback label, so every
    # rendering surface (Workshop, future CLI) derives the same fixed text from
    # the controlled action and never echoes a persisted trigger string.
    assert callable(lw.safe_lifecycle_trigger_display)
    assert isinstance(lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED, str)
    assert lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED  # non-empty


def test_safe_lifecycle_trigger_display_maps_retire_action() -> None:
    # A retirement proposal renders the fixed retirement label derived ONLY
    # from the controlled action, never from the persisted trigger string.
    assert lw.safe_lifecycle_trigger_display("retire") == "explicit source retirement"


def test_safe_lifecycle_trigger_display_maps_reactivate_action() -> None:
    # A reactivation proposal renders the fixed reactivation label derived ONLY
    # from the controlled action, never from the persisted trigger string.
    assert lw.safe_lifecycle_trigger_display("reactivate") == "explicit source reactivation"


def test_safe_lifecycle_trigger_display_masks_unknown_action() -> None:
    # Any action outside the controlled retire/reactivate vocabulary renders the
    # single fixed generic label — including a legacy/future action string that
    # itself carries secret-bearing material. The stored value is never echoed.
    secret_action = "password=hunter2;token=abc123"
    rendered = lw.safe_lifecycle_trigger_display(secret_action)
    assert rendered == lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED
    assert secret_action not in rendered
    # Empty / unrecognized values share the same fixed generic fallback.
    assert lw.safe_lifecycle_trigger_display("") == lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED
    assert (
        lw.safe_lifecycle_trigger_display("purge")
        == lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED
    )


def test_safe_lifecycle_trigger_display_never_echoes_trigger_material() -> None:
    # The display label is derived from the ACTION only. A legacy/future
    # proposal whose persisted TRIGGER carries a content hash or a secret must
    # never leak it through the label: the function takes the action, so the
    # hash/secret-bearing trigger can never reach the rendered string at all.
    hash_trigger = "retired sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    secret_trigger = "retired password=hunter2;token=abc123"
    for action in ("retire", "reactivate"):
        rendered = lw.safe_lifecycle_trigger_display(action)
        assert hash_trigger not in rendered
        assert secret_trigger not in rendered
        assert "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08" not in rendered
        assert "password=hunter2" not in rendered


# --- Task 4 Terra fix: controlled impact-status vocabulary in the Workshop ---


def test_public_surface_exposes_safe_lifecycle_impact_status_display() -> None:
    # The pipeline exposes ONE shared, public safe-display function for proposal
    # lifecycle impact statuses plus the fixed generic fallback label, so every
    # rendering surface (Workshop, future CLI) derives the same allowlisted
    # text from the persisted impact status and never echoes an arbitrary
    # legacy/future status string that may carry secret-bearing material.
    assert callable(lw.safe_lifecycle_impact_status_display)
    assert isinstance(lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED, str)
    assert lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED  # non-empty


def test_safe_lifecycle_impact_status_display_passes_still_supported() -> None:
    # A page still supported after the action renders the exact fixed label.
    assert lw.safe_lifecycle_impact_status_display("still-supported") == "still-supported"


def test_safe_lifecycle_impact_status_display_passes_sole_source_lost() -> None:
    # A page that lost its sole source renders the exact fixed label.
    assert lw.safe_lifecycle_impact_status_display("sole-source-lost") == "sole-source-lost"


def test_safe_lifecycle_impact_status_display_masks_unknown_status() -> None:
    # Any status outside the controlled vocabulary renders the single fixed
    # generic label — including a legacy/future status string that itself
    # carries secret-bearing material. The stored value is never echoed.
    secret_status = "purged?token=abc123&password=hunter2"
    rendered = lw.safe_lifecycle_impact_status_display(secret_status)
    assert rendered == lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED
    assert secret_status not in rendered
    assert "token=abc123" not in rendered
    assert "password=hunter2" not in rendered
    # Empty / unrecognized values share the same fixed generic fallback.
    assert (
        lw.safe_lifecycle_impact_status_display("")
        == lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED
    )
    assert (
        lw.safe_lifecycle_impact_status_display("orphaned")
        == lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED
    )


def test_safe_lifecycle_impact_status_display_never_echoes_status_material() -> None:
    # The display label is derived ONLY from the allowlisted status. A
    # legacy/future proposal whose persisted impact status carries a content
    # hash or a secret must never leak it through the rendered label.
    hash_status = "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    secret_status = "password=hunter2;token=abc123"
    for status in (hash_status, secret_status):
        rendered = lw.safe_lifecycle_impact_status_display(status)
        assert rendered == lw.SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED
        assert status not in rendered
        assert "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08" not in rendered
        assert "password=hunter2" not in rendered
        assert "token=abc123" not in rendered
