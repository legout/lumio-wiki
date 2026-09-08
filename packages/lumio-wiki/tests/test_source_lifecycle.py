from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
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
    _is_self_consistent_restage,
    _recomputed_mutation_identity,
)
from lumio_wiki.mutation import (
    MutationLockError,
    lock_directory,
    mutation_lock,
    resource_identity,
)
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.records import ValidationReport
from lumio_wiki.source_registry import (
    SourceRegistry,
    SourceRegistryError,
    _RegistryState,
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


# --- Plan 02 / P4 FINAL remediation: lifecycle impacts derive from the ---
# --- CURRENT durable Knowledge Base under the staging boundary          ---


def _publish_new_page_citing(pipeline, source_id: str, title: str) -> IngestProposal:
    """Assemble, stage, and publish a NEW page citing ``source_id``.

    Deterministic helper for the stale-``self._kb`` regressions: the page
    has the same Compiled Page shape as ``_knowledge_base``'s and is
    published through the ordinary pipeline journey, so the durable
    Knowledge Base root gains a page another (already-open) pipeline's
    in-memory snapshot does not know about.
    """
    markdown = textwrap.dedent(
        f"""\
        ---
        title: "{title}"
        aliases: []
        tags:
          - "{title.lower()}"
        summary: "The {title.lower()}."
        lifecycle: "approved"
        visibility: "public"
        sources:
          - id: "{source_id}"
            title: "{source_id} source"
        synthetic: false
        ---

        # {title}

        {title} text.
        """
    )
    assembled = pipeline.assemble(
        markdown, SourceProvenance(None, None, "test-helper"), f"{title.lower()}.md"
    )
    return pipeline.publish(pipeline.stage(assembled).id)


def test_retire_source_impacts_cover_pages_published_after_pipeline_open(tmp_path) -> None:
    # Plan 02 / P4 FINAL blocker: an already-open pipeline used to derive
    # lifecycle impacts from its stale in-memory Knowledge Base snapshot, so
    # a page another pipeline published (citing the same source) after this
    # pipeline opened was silently missing from the staged retirement's
    # impacts — and the lifecycle proposal's deliberately empty precondition
    # set let that stale metadata publish. Lifecycle staging now reloads the
    # CURRENT durable Knowledge Base under the Knowledge Base + store +
    # registry boundary locks and derives the impacts from that state;
    # publishing the retirement keeps every impact. Two separately opened
    # pipelines, direct durable assertions, no sleeps.
    kb_a = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline_a = ProposalPipeline(kb_a, store)
    pipeline_a.register_source("policy", b"policy-v1")

    # A separately opened pipeline publishes a NEW page citing the source.
    kb_b, report_b = lw.load_knowledge_base(kb_a.root)
    assert report_b.is_valid
    pipeline_b = ProposalPipeline(kb_b, IngestStore(tmp_path / "ingest"))
    published = _publish_new_page_citing(pipeline_b, "policy", "Handbook")
    assert published.status == "published"
    assert (kb_a.root / "handbook.md").is_file()

    # Pipeline A retires while carrying its stale snapshot: the staged
    # impacts must cover BOTH pages, derived from the reloaded current
    # Knowledge Base (ADR-0014: page-level impacts for every affected page).
    retirement = pipeline_a.retire_source("policy")
    assert retirement.source_change is not None
    assert {impact.page_title: impact.status for impact in retirement.source_change.impacts} == {
        "Policy": "sole-source-lost",
        "Handbook": "sole-source-lost",
    }

    # Publishing the retirement must not lose the new-page impact; the
    # DURABLE record is the authority a Maintainer reviews.
    pipeline_a.publish(retirement.id)
    durable = pipeline_a.review(retirement.id)
    assert durable is not None and durable.status == "published"
    assert durable.source_change is not None
    assert {impact.page_title for impact in durable.source_change.impacts} == {
        "Policy",
        "Handbook",
    }
    assert store.source_registry.get("policy").status == "retired"


def test_reactivate_source_impacts_cover_pages_published_after_pipeline_open(tmp_path) -> None:
    # Same stale-snapshot shape for reactivation: a page another pipeline
    # published (citing the retired source) after this pipeline opened must
    # appear among the staged reactivation's still-supported impacts, and
    # publishing the reactivation must keep it.
    kb_a = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline_a = ProposalPipeline(kb_a, store)
    pipeline_a.register_source("policy", b"policy-v1")
    pipeline_a.publish(pipeline_a.retire_source("policy").id)

    kb_b, report_b = lw.load_knowledge_base(kb_a.root)
    assert report_b.is_valid
    pipeline_b = ProposalPipeline(kb_b, IngestStore(tmp_path / "ingest"))
    published = _publish_new_page_citing(pipeline_b, "policy", "Handbook")
    assert published.status == "published"

    reactivation = pipeline_a.reactivate_source("policy", b"policy-v2")
    assert reactivation.source_change is not None
    assert {impact.page_title: impact.status for impact in reactivation.source_change.impacts} == {
        "Policy": "still-supported",
        "Handbook": "still-supported",
    }

    pipeline_a.publish(reactivation.id)
    durable = pipeline_a.review(reactivation.id)
    assert durable is not None and durable.status == "published"
    assert durable.source_change is not None
    assert {impact.page_title for impact in durable.source_change.impacts} == {
        "Policy",
        "Handbook",
    }
    active = store.source_registry.get("policy")
    assert active.status == "active"
    assert len(active.versions) == 2


def test_confirm_retirement_candidate_impacts_derive_from_current_kb(tmp_path) -> None:
    # The candidate confirm route stages through retire_source's ONE coherent
    # boundary — there is no separate confirm-side impact derivation to keep
    # in sync — so a page published after the pipeline opened is covered
    # there too, and the candidate decision still records cleanly.
    kb_a = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline_a = ProposalPipeline(kb_a, store)
    pipeline_a.register_source("policy", b"policy-v1")
    candidate = pipeline_a.record_retirement_candidate("policy", "watched file missing")

    kb_b, report_b = lw.load_knowledge_base(kb_a.root)
    assert report_b.is_valid
    pipeline_b = ProposalPipeline(kb_b, IngestStore(tmp_path / "ingest"))
    _publish_new_page_citing(pipeline_b, "policy", "Handbook")

    proposal = pipeline_a.confirm_retirement_candidate(candidate.id)
    assert proposal.source_change is not None
    assert {impact.page_title for impact in proposal.source_change.impacts} == {
        "Policy",
        "Handbook",
    }
    assert store.source_registry.get_candidate(candidate.id).status == "confirmed"
    assert store.source_registry.get("policy").status == "active"


def test_lifecycle_staging_refuses_an_invalid_current_knowledge_base(tmp_path) -> None:
    # Fail-closed reload: a durable Knowledge Base corrupted by a direct
    # external edit after the pipeline opened must refuse lifecycle staging
    # — no impacts derived from a broken load, no staged transition, no
    # persisted proposal, the registry byte-untouched. Direct durable
    # assertions, no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    # Phase 1 — retirement staging fails closed on the invalid reload.
    (kb.root / "broken.md").write_text("---\ntitle: [broken\n", encoding="utf-8")
    prior_registry = msgspec.json.encode(store.source_registry._state)
    with pytest.raises(lw.ProposalPipelineError, match="no longer validates"):
        pipeline.retire_source("policy")
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    assert pipeline.list() == []

    # Phase 2 — the repaired Knowledge Base stages cleanly again.
    (kb.root / "broken.md").unlink()
    retirement = pipeline.retire_source("policy")
    assert retirement.status == "staged"
    pipeline.publish(retirement.id)

    # Phase 3 — the same gate guards reactivation staging.
    (kb.root / "broken.md").write_text("---\ntitle: [broken\n", encoding="utf-8")
    prior_registry = msgspec.json.encode(store.source_registry._state)
    with pytest.raises(lw.ProposalPipelineError, match="no longer validates"):
        pipeline.reactivate_source("policy", b"policy-v2")
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    assert [item for item in pipeline.list() if lw.is_reviewable_proposal(item)] == []


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
    re_staged = pipeline.review(reactivation.id)
    assert re_staged is not None and re_staged.status == "staged"


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
    re_staged = pipeline.review(reactivation.id)
    assert re_staged is not None and re_staged.status == "staged"


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


# --- Plan 02 / P4 remediation: source-lifecycle records fail closed too ---
#
# The reviewed-metadata checks are publish-time gates over the DURABLE
# record, so they must apply to EVERY durable proposal. A source-lifecycle
# proposal mutates only private registry state, but its reviewed base is
# still bound at staging: an explicit EMPTY precondition set plus the
# content identity over its ``source_change``. Legacy (pre-P4) records
# lacking both — and durable records whose lifecycle content was altered
# after review — must be refused at publish with the registry untouched.


def test_publish_refuses_legacy_source_lifecycle_record_without_reviewed_metadata(
    tmp_path,
) -> None:
    # P4 remediation (BLOCKER): source-lifecycle proposals used to return
    # from the stale-base check before any reviewed-metadata validation, so
    # a legacy serialized record with ``preconditions=None`` and
    # ``reviewed_identity=None`` still published and flipped the registry.
    # Every durable proposal now carries its reviewed metadata: publish
    # refuses before any mutation, the source stays active, and the record
    # stays reviewable (old JSON remains decodable and inspectable).
    # Deterministic: direct durable-JSON edits, no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")
    durable = store.get(proposal.id)
    assert durable is not None
    assert durable.preconditions == []  # explicit captured-nothing base
    assert durable.reviewed_identity is not None

    proposal_path = store.proposals_dir / f"{proposal.id}.json"
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    record.pop("preconditions", None)
    record.pop("reviewed_identity", None)
    proposal_path.write_text(json.dumps(record), encoding="utf-8")
    prior_registry = msgspec.json.encode(store.source_registry._state)

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(proposal.id)
    assert "restage" in str(excinfo.value).lower()

    source = store.source_registry.get("policy")
    assert source.status == "active"
    assert len(source.versions) == 1
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    persisted = store.get(proposal.id)
    assert persisted is not None and persisted.status == "staged"


def test_publish_refuses_tampered_source_lifecycle_content(tmp_path) -> None:
    # Second remediation layer: a durable source-lifecycle record rewritten
    # BELOW the public API (its reviewed lifecycle content altered while the
    # reviewed identity was retained verbatim) cannot publish either — the
    # identity is recomputed from the DURABLE record under the publish lock
    # and must equal the stored one. The forged content never flips the
    # registry. Deterministic: direct durable-JSON edits, no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")

    proposal_path = store.proposals_dir / f"{proposal.id}.json"
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    record["source_change"]["trigger"] = "TAMPERED: forged retirement trigger"
    proposal_path.write_text(json.dumps(record), encoding="utf-8")
    prior_registry = msgspec.json.encode(store.source_registry._state)

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(proposal.id)
    assert "content identity" in str(excinfo.value)
    assert "restage" in str(excinfo.value).lower()

    assert store.source_registry.get("policy").status == "active"
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    persisted = store.get(proposal.id)
    assert persisted is not None and persisted.status == "staged"


def test_publish_refuses_source_lifecycle_record_carrying_precondition_records(
    tmp_path,
) -> None:
    # Third remediation layer: the reviewed base of a source-lifecycle
    # proposal is exactly the EMPTY captured-nothing set. A durable record
    # that carries Knowledge Base precondition records (a foreign snapshot
    # injected after review) cannot publish even when its content identity
    # was re-forged to match: the precondition path-set must match the
    # proposal. Deterministic: direct durable-JSON edits, no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")

    proposal_path = store.proposals_dir / f"{proposal.id}.json"
    record = json.loads(proposal_path.read_text(encoding="utf-8"))
    record["preconditions"] = [
        {
            "path": "policy.md",
            "role": "revision",
            "kind": "file",
            "digest": hashlib.sha256((kb.root / "policy.md").read_bytes()).hexdigest(),
        }
    ]
    # Re-forge the identity so ONLY the path-set check can catch the forgery.
    forged = msgspec.json.decode(msgspec.json.encode(record), type=IngestProposal)
    record["reviewed_identity"] = _recomputed_mutation_identity(forged)
    proposal_path.write_text(json.dumps(record), encoding="utf-8")
    prior_registry = msgspec.json.encode(store.source_registry._state)

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(proposal.id)
    assert "no Knowledge Base path" in str(excinfo.value)
    assert "restage" in str(excinfo.value).lower()

    assert store.source_registry.get("policy").status == "active"
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    persisted = store.get(proposal.id)
    assert persisted is not None and persisted.status == "staged"


def test_public_save_cannot_rebind_reviewed_identity_for_altered_lifecycle_content(
    tmp_path,
) -> None:
    # P4 FINAL review blocker, source-lifecycle side: an ordinary save that
    # alters the reviewed lifecycle content (here: the trigger prose) used to
    # KEEP its reviewed claim whenever the caller recomputed the PUBLIC
    # deterministic identity of the altered content and retained the old
    # captured-empty preconditions — publish then accepted the forged
    # identity against the unchanged EMPTY base and flipped the registry for
    # unreviewed lifecycle content. Caller-side recomputation is not a
    # reviewed binding: the altering save now strips the reviewed metadata,
    # publication refuses with restage guidance, and the registry state is
    # byte-untouched. Deterministic: direct public-API calls, no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")
    durable = store.get(proposal.id)
    assert durable is not None and durable.source_change is not None
    assert durable.preconditions == [] and durable.reviewed_identity is not None
    prior_registry = msgspec.json.encode(store.source_registry._state)

    tampered = msgspec.structs.replace(
        durable,
        source_change=msgspec.structs.replace(
            durable.source_change, trigger="TAMPERED: forged retirement trigger"
        ),
    )
    forged = msgspec.structs.replace(
        tampered, reviewed_identity=_recomputed_mutation_identity(tampered)
    )
    assert forged.preconditions == []  # old captured-nothing base retained
    assert _is_self_consistent_restage(forged)  # the old bypass accepted this record

    store.save_proposal(forged)

    saved = store.get(proposal.id)
    assert saved is not None and saved.status == "staged"
    assert saved.source_change is not None
    assert saved.source_change.trigger == "TAMPERED: forged retirement trigger"
    # ...but the reviewed claim is gone: a caller-supplied identity — however
    # self-consistent — is never a freshly reviewed binding.
    assert saved.preconditions is None
    assert saved.reviewed_identity is None

    with pytest.raises(lw.ProposalPreconditionError) as excinfo:
        pipeline.publish(proposal.id)
    assert "restage" in str(excinfo.value).lower()

    # The registry is untouched: the forged retirement never applied.
    assert store.source_registry.get("policy").status == "active"
    assert msgspec.json.encode(store.source_registry._state) == prior_registry


def test_public_save_refuses_incoming_terminal_status_and_keeps_decision_open(
    tmp_path,
) -> None:
    # Plan 02 / P3 (incoming terminal-status guard), source-lifecycle side: a
    # public save of a staged RETIREMENT proposal carrying an incoming
    # ``published``/``discarded`` status must be refused BEFORE any write.
    # Writing it would mark the proposal terminal without applying the
    # pending registry transition — stranding the retirement behind a
    # terminal record the pipeline's publish/discard paths refuse. The
    # refusal preserves the staged proposal AND the bound transition, so the
    # maintainer decision stays open in BOTH directions: discarding releases
    # the transition, publishing applies it. Deterministic: direct ordered
    # public-API calls, no sleeps, no threads.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    proposal = pipeline.retire_source("policy")
    durable = store.get(proposal.id)
    assert durable is not None and durable.status == "staged"
    assert durable.source_change is not None
    prior_registry = msgspec.json.encode(store.source_registry._state)

    for terminal_status in ("published", "discarded"):
        with pytest.raises(lw.ProposalTerminalStateError):
            store.save_proposal(msgspec.structs.replace(durable, status=terminal_status))

    # Nothing was written: the proposal keeps its reviewable staged status
    # and the registry keeps its bound pending transition (a second
    # retirement for the same source is still refused while it is bound).
    reloaded = IngestStore(tmp_path / "ingest")
    preserved = reloaded.get(proposal.id)
    assert preserved is not None and preserved.status == "staged"
    assert preserved == durable
    assert msgspec.json.encode(store.source_registry._state) == prior_registry
    with pytest.raises(SourceRegistryError, match="already has a pending transition"):
        pipeline.retire_source("policy")

    # The decision stays open: discarding releases the transition ...
    discarded = pipeline.discard(proposal.id)
    assert discarded is not None and discarded.status == "discarded"
    replacement = pipeline.retire_source("policy")
    assert replacement.status == "staged"
    # ... and publishing the replacement applies it: the source retires.
    published = pipeline.publish(replacement.id)
    assert published.status == "published"
    assert store.source_registry.get("policy").status == "retired"


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

    assert discarded is not None and discarded.status == "discarded"
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


def test_bind_pending_write_failure_restores_live_and_reloaded_state(tmp_path, monkeypatch) -> None:
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

    def fail_reviewed_write(proposal):
        raise OSError("proposal persistence failed")

    # The staging seam persists through the store's authorized reviewed
    # write (Plan 02 / P4 final review): simulating its failure must still
    # cancel the bound transition so no reviewable proposal is left behind.
    monkeypatch.setattr(store, "_write_reviewed_proposal", fail_reviewed_write)

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

    monkeypatch.setattr(store.source_registry, "confirm_retirement_candidate", fail_decision)

    with pytest.raises(OSError, match="candidate decision failed"):
        pipeline.confirm_retirement_candidate(candidate.id)

    assert store.source_registry.get_candidate(candidate.id).status == "pending"
    assert store.source_registry.get("policy").status == "active"
    assert [p for p in pipeline.list() if lw.is_reviewable_proposal(p)] == []
    monkeypatch.undo()
    confirmed = pipeline.confirm_retirement_candidate(candidate.id)
    assert confirmed.source_change is not None
    assert confirmed.source_change.action == "retire"


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
    assert restored is not None and restored.status == "staged"
    assert lw.is_reviewable_proposal(restored)
    with pytest.raises(SourceRegistryError, match="already has a pending transition"):
        pipeline.retire_source("policy")


# --- Task 3: ProposalPipeline as the CLI read seam for source listing ---


# --- Task 4 fix: candidate review journey and ownership-safe confirmation ---


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

    proposal = pipeline.confirm_retirement_candidate(candidate.id, expected_source_id="policy")

    assert proposal.source_change is not None
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
def test_registry_get_rejects_secret_bearing_source_id_without_echoing(source_id, tmp_path) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")

    with pytest.raises(SourceRegistryError) as exc:
        registry.get(source_id)

    assert source_id not in str(exc.value)


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_stage_retirement_rejects_secret_bearing_source_id(source_id, tmp_path) -> None:
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
def test_registry_stage_reactivation_rejects_secret_bearing_source_id(source_id, tmp_path) -> None:
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError) as exc:
        registry.stage_reactivation(source_id, b"bytes")

    assert source_id not in str(exc.value)
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


@pytest.mark.parametrize("source_id", _SECRET_SOURCE_IDS)
def test_registry_record_candidate_rejects_secret_bearing_source_id(source_id, tmp_path) -> None:
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
def test_registry_get_candidate_rejects_secret_bearing_candidate_id(candidate_id, tmp_path) -> None:
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
def test_pipeline_dismiss_validates_expected_source_id_before_mutating(source_id, tmp_path) -> None:
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
def test_pipeline_confirm_validates_expected_source_id_before_staging(source_id, tmp_path) -> None:
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
def test_pipeline_dismiss_rejects_secret_bearing_candidate_id(candidate_id, tmp_path) -> None:
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
def test_pipeline_confirm_rejects_secret_bearing_candidate_id(candidate_id, tmp_path) -> None:
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
    assert lw.safe_lifecycle_trigger_display("purge") == lw.SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED


# --- Task 4 Terra fix: controlled impact-status vocabulary in the Workshop ---


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


# ---------------------------------------------------------------------------
# register_or_reuse — the managed host-Distiller identity rule (issue #149).
# ---------------------------------------------------------------------------


def test_register_or_reuse_creates_a_new_active_identity(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")

    version, action = registry.register_or_reuse("handbook", b"v1")

    assert action == "registered"
    source = registry.get("handbook")
    assert source.status == "active"
    assert source.source_id == "handbook"
    assert [v.content_hash for v in source.versions] == [version.content_hash]
    # The version hash is over the original raw bytes (ADR-0014).
    import hashlib

    assert version.content_hash == hashlib.sha256(b"v1").hexdigest()
    assert version.source_id == "handbook"


def test_register_or_reuse_reuses_identical_bytes_idempotently(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")
    first, first_action = registry.register_or_reuse("handbook", b"v1")
    assert first_action == "registered"
    prior = _state_snapshot(registry)

    reused, action = registry.register_or_reuse("handbook", b"v1")

    assert action == "reused"
    assert reused.content_hash == first.content_hash
    assert reused.source_id == "handbook"
    # No duplicate Source Version: the identity still has exactly one version.
    source = registry.get("handbook")
    assert [v.content_hash for v in source.versions] == [first.content_hash]
    # No mutation at all — the retry is byte-identical to the first registration.
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_register_or_reuse_changed_bytes_is_rejected_without_mutation(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")
    first, _ = registry.register_or_reuse("handbook", b"v1")
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError, match="retire and reactivate"):
        registry.register_or_reuse("handbook", b"v2-changed")

    # No registry or version mutation: the single immutable version is intact.
    source = registry.get("handbook")
    assert [v.content_hash for v in source.versions] == [first.content_hash]
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior


def test_register_or_reuse_retired_identity_is_rejected_until_reactivation(tmp_path):
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    registry = store.source_registry
    prior = _state_snapshot(registry)

    with pytest.raises(SourceRegistryError, match="retired"):
        registry.register_or_reuse("policy", b"policy-v1")

    # Retired status is unchanged; reactivation is the only path back.
    assert registry.get("policy").status == "retired"
    assert _state_snapshot(registry) == prior


def test_register_or_reuse_refusal_never_echoes_the_source_id(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_or_reuse("handbook", b"v1")
    secret_id = "sk-leaked-api-key"

    with pytest.raises(SourceRegistryError) as exc:
        registry.register_or_reuse(secret_id, b"v2")

    assert secret_id not in str(exc.value)
    assert "handbook" not in str(exc.value)


def test_register_or_reuse_rejects_an_unsafe_id_at_the_boundary(tmp_path):
    registry = SourceRegistry(tmp_path / "ingest")

    with pytest.raises(SourceRegistryError):
        registry.register_or_reuse("reports/secret.md", b"v1")
    assert registry.list() == []


# --- Plan 02 / P3: one durable lock/reload boundary (B02) ---

# The writer each subprocess runs: it opens its own SourceRegistry instance
# FIRST, signals the parent through a pipe, and waits on a second pipe until
# the parent releases it. The pipes are the controlled barrier — there is no
# sleep and no polling: a hung child is a failed rendezvous, not a timing
# assumption.
_REGISTRY_WRITER_CHILD = textwrap.dedent(
    """
    import os
    import sys

    from lumio_wiki.source_registry import SourceRegistry

    root, source_id, raw = sys.argv[1], sys.argv[2], sys.argv[3]
    release_fd, signal_fd = int(sys.argv[4]), int(sys.argv[5])

    # Open this process's registry instance BEFORE the barrier so both
    # writers hold an already-open pre-mutation view when they race.
    registry = SourceRegistry(root)
    os.write(signal_fd, b"open")
    os.read(release_fd, 1)
    registry.register_source(source_id, raw.encode("utf-8"))
    os.write(signal_fd, b"done")
    os.read(release_fd, 1)
    """
)


def _signal_reader(fd: int, results: queue.Queue) -> None:
    try:
        results.put(os.read(fd, 8))
    except OSError as exc:  # pragma: no cover - defensive
        results.put(exc)


def _await_signal(fd: int, expected: bytes, timeout: float = 120.0) -> None:
    """Block until a writer subprocess signals ``expected`` through ``fd``.

    The blocking pipe read IS the barrier rendezvous; the deadline only
    converts a hung child into a test failure instead of a CI stall.
    """
    results: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_signal_reader, args=(fd, results), daemon=True)
    reader.start()
    try:
        signal: object = results.get(timeout=timeout)
    except queue.Empty:
        signal = None
    assert signal == expected, (
        f"registry writer subprocess did not signal {expected!r} (got {signal!r}): "
        "a hung or crashed writer is a failed rendezvous, not a timing assumption"
    )


def test_source_registry_process_writers_preserve_successful_updates(tmp_path):
    # B02 (Plan 02 / P3): two SEPARATE processes, each with its own
    # already-open SourceRegistry instance, register a different source.
    # Mutations are serialized by the durable per-registry interprocess lock
    # and each registration is recomputed from the state read UNDER that
    # lock, so BOTH successful registrations survive a fresh reload —
    # neither writer loses the other's committed update.
    root = tmp_path / "ingest"
    writers: list[tuple[subprocess.Popen, int, int]] = []
    try:
        for source_id in ("alpha", "beta"):
            release_read, release_write = os.pipe()
            signal_read, signal_write = os.pipe()
            writer = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _REGISTRY_WRITER_CHILD,
                    str(root),
                    source_id,
                    f"{source_id}-v1",
                    str(release_read),
                    str(signal_write),
                ],
                # The rendezvous pipes must survive the child's default
                # close_fds=True descriptor hygiene.
                pass_fds=(release_read, signal_write),
            )
            os.close(release_read)
            os.close(signal_write)
            writers.append((writer, release_write, signal_read))

        # Barrier 1: both processes hold an already-open registry instance.
        for _writer, _release, signal_read in writers:
            _await_signal(signal_read, b"open")
        # Release both; each process registers a different source.
        for _writer, release_write, _signal in writers:
            os.write(release_write, b"1")
        # Barrier 2: both registrations reported successful commit.
        for _writer, _release, signal_read in writers:
            _await_signal(signal_read, b"done")
        for _writer, release_write, _signal in writers:
            os.write(release_write, b"1")
    finally:
        for _writer, release_write, signal_read in writers:
            os.close(release_write)
            os.close(signal_read)
    for writer, _release, _signal in writers:
        assert writer.wait(timeout=120) == 0

    reloaded = SourceRegistry(root)
    assert {source.source_id for source in reloaded.list()} == {"alpha", "beta"}
    for source_id in ("alpha", "beta"):
        expected_hash = hashlib.sha256(f"{source_id}-v1".encode()).hexdigest()
        assert [v.content_hash for v in reloaded.get(source_id).versions] == [expected_hash]


def test_atomic_write_failure_cleans_temp_files_and_restores_state(tmp_path, monkeypatch):
    # Plan 02 / P3 resource-bound behavior: a persistence failure inside the
    # atomic write path (unique temp file + os.replace) must leave the private
    # registry directory holding EXACTLY the durable file — no leaked temp
    # artifacts a later writer or a portable export could trip over — and the
    # live in-memory state must still agree with the durable state.
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("handbook", b"v1")
    prior = _state_snapshot(registry)

    def fail_replace(src, dst, *args, **kwargs):
        raise OSError("atomic replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic replace failed"):
        registry.register_source("manual", b"v1")
    monkeypatch.undo()

    # Live state agrees with durable state; the durable file is intact.
    assert _state_snapshot(registry) == prior
    assert _state_snapshot(SourceRegistry(tmp_path / "ingest")) == prior
    # No temp file survived the failed write.
    leftovers = sorted(p.name for p in (tmp_path / "ingest").iterdir())
    assert leftovers == ["sources.json"]
    # A retry through the same path succeeds and persists exactly one file.
    registry.register_source("manual", b"v1")
    assert SourceRegistry(tmp_path / "ingest").get("manual").status == "active"
    assert sorted(p.name for p in (tmp_path / "ingest").iterdir()) == ["sources.json"]


def test_registry_lock_is_reentrant_for_nested_reads_and_writes(tmp_path):
    # Lock ownership covers read/check/mutate/commit/rollback, and registry
    # operations legitimately nest (a mutation method reads through get()).
    # Acquiring the same resource lock from nested calls in one thread must
    # reenter rather than deadlock; the durable result is one writer state.
    registry = SourceRegistry(tmp_path / "ingest")
    with mutation_lock(registry.root):
        registry.register_source("handbook", b"v1")
        assert registry.get("handbook").status == "active"
        registry.register_source("policy", b"v1")
    reloaded = SourceRegistry(tmp_path / "ingest")
    assert {source.source_id for source in reloaded.list()} == {"handbook", "policy"}


def test_mutation_lock_files_stay_outside_resource_roots(tmp_path):
    # Lock files must never land in portable Knowledge Base or private store
    # artifacts: they live in the dedicated temp-directory lock directory and
    # are keyed by the normalized resource identity.
    resource = tmp_path / "ingest"
    registry = SourceRegistry(resource)
    registry.register_source("handbook", b"v1")
    with mutation_lock(resource):
        lock_dir = lock_directory()
        assert lock_dir == Path(tempfile.gettempdir()) / "lumio-mutation-locks"
        resolved_resource = resource.resolve()
        assert resolved_resource not in lock_dir.resolve().parents
        assert lock_dir.resolve() not in resolved_resource.parents
        # The private store itself holds only durable registry state.
        assert sorted(p.name for p in resource.iterdir()) == ["sources.json"]


# --- Plan 02 / P3 review remediation: stale-transition, nested-lock, and
# --- case-identity regressions ---


def _pending_transitions(registry: SourceRegistry) -> list:
    state = msgspec.json.decode(_state_snapshot(registry), type=_RegistryState)
    return state.pending_transitions


def test_bind_pending_rejects_retirement_stale_after_a_concurrent_publish(tmp_path) -> None:
    # Review blocker 1: stage_retirement validates the source OUTSIDE the
    # bind/persist critical section. A concurrent retire+publish that commits
    # in the stage→bind window must be caught by bind_pending's durable
    # re-read UNDER the registry lock: the stale transition is refused before
    # any mutation, so _bind_and_persist persists no stale proposal and binds
    # no orphan transition.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    stale = store.source_registry.stage_retirement("policy")
    # The conflicting writer completes a FULL retire + publish in the window.
    conflict = ProposalPipeline(kb, IngestStore(tmp_path / "ingest"))
    winner = conflict.retire_source("policy")
    conflict.publish(winner.id)

    stale_proposal = pipeline._source_change_proposal(
        SourceLifecycleChange(
            action="retire", source_id="policy", trigger="source policy retired", impacts=[]
        )
    )
    with pytest.raises(SourceRegistryError, match="no longer active"):
        pipeline._bind_and_persist(stale, stale_proposal)

    # Clean compensation: the published retirement is the only durable
    # proposal, the source is retired, and no stale/orphan transition remains.
    assert [proposal.id for proposal in pipeline.list()] == [winner.id]
    published_winner = pipeline.review(winner.id)
    assert published_winner is not None and published_winner.status == "published"
    assert store.source_registry.get("policy").status == "retired"
    assert _pending_transitions(store.source_registry) == []


def test_bind_pending_rejects_reactivation_stale_after_a_concurrent_publish(tmp_path) -> None:
    # Review blocker 1, reactivation side: a reactivation staged while the
    # source was retired is refused at bind time once a concurrent
    # reactivation+publish has made the source active again.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    stale = store.source_registry.stage_reactivation("policy", b"policy-v2")
    conflict = ProposalPipeline(kb, IngestStore(tmp_path / "ingest"))
    winner = conflict.reactivate_source("policy", b"policy-v2-conflict")
    conflict.publish(winner.id)

    stale_proposal = pipeline._source_change_proposal(
        SourceLifecycleChange(
            action="reactivate",
            source_id="policy",
            trigger="source policy reactivated",
            impacts=[],
        )
    )
    with pytest.raises(SourceRegistryError, match="no longer retired"):
        pipeline._bind_and_persist(stale, stale_proposal)

    # Clean compensation: the conflicting reactivation won durably, and the
    # active source carries no orphan transition (a fresh retirement can be
    # staged immediately).
    persisted_ids = {proposal.id for proposal in pipeline.list()}
    assert persisted_ids == {retirement.id, winner.id}
    assert stale_proposal.id not in persisted_ids
    assert store.source_registry.get("policy").status == "active"
    assert _pending_transitions(store.source_registry) == []
    assert pipeline.retire_source("policy").status == "staged"


def test_bind_pending_rejects_a_reactivation_without_a_valid_staged_version(tmp_path) -> None:
    # Review blocker 1, version validation: a reactivation transition must
    # carry a new Source Version for ITS OWN source id; a decoded or
    # hand-assembled transition without one is refused before any mutation.
    registry = SourceRegistry(tmp_path / "ingest")
    registry.register_source("policy", b"policy-v1")
    retirement = registry.stage_retirement("policy")
    registry.bind_pending(retirement, "retire-proposal")
    registry.apply_transition("retire-proposal")
    broken = msgspec.structs.replace(
        registry.stage_reactivation("policy", b"policy-v2"), version=None
    )

    with pytest.raises(SourceRegistryError, match="no valid new Source Version"):
        registry.bind_pending(broken, "reactivate-proposal")

    assert _pending_transitions(registry) == []


def _gated_pipeline_stale_writer(pipeline, method_name, args, outcomes):
    """Run one pipeline lifecycle call parked inside its staging boundary.

    The gate parks the call inside ``_source_change_proposal`` — which,
    since the Plan 02 / P4 final remediation, runs INSIDE the one coherent
    staging boundary (current-KB reload, registry staging, impact
    derivation, reviewed binding, persistence under the Knowledge Base +
    store + registry locks). Parking there therefore holds the whole
    boundary open, so a conflicting writer's own lifecycle staging blocks
    on the boundary locks until the gate releases. Coordination is
    ``threading.Event`` barriers; there are no sleeps. Returns
    (parked_event, release_event, started thread). The gated ``pipeline``
    is a test-local instance, so the attribute assignment does not leak;
    after both events are set the gate is a pass-through.
    """
    parked_at_bind_window = threading.Event()
    conflict_may_commit = threading.Event()
    real_assemble = pipeline._source_change_proposal

    def gated_assemble(change):
        parked_at_bind_window.set()
        assert conflict_may_commit.wait(timeout=120), (
            "conflicting writer never committed; the barrier rendezvous failed"
        )
        return real_assemble(change)

    pipeline._source_change_proposal = gated_assemble

    def runner() -> None:
        try:
            getattr(pipeline, method_name)(*args)
        except BaseException as exc:  # relayed to the main thread for asserting
            outcomes.put(exc)
        else:
            outcomes.put(None)

    thread = threading.Thread(target=runner, daemon=True)
    return parked_at_bind_window, conflict_may_commit, thread


def _run_conflicting_lifecycle_writer(pipeline, method_name, args, outcomes):
    """Run a conflicting lifecycle staging on a daemon thread.

    The conflicting writer is a separately opened pipeline over the same
    durable roots; its result (``None`` on success, the exception otherwise)
    is relayed through ``outcomes`` for order-independent assertions.
    """

    def conflicting_runner() -> None:
        try:
            getattr(pipeline, method_name)(*args)
        except BaseException as exc:  # relayed to the main thread for asserting
            outcomes.put(exc)
        else:
            outcomes.put(None)

    return threading.Thread(target=conflicting_runner, daemon=True)


def test_retire_source_boundary_serializes_a_conflicting_writer(tmp_path) -> None:
    # Plan 02 / P4 FINAL blocker: the whole retire staging — current-KB
    # reload, registry staging, impact derivation, reviewed binding,
    # persistence — is ONE critical section under the Knowledge Base +
    # store + registry locks. A separately opened conflicting writer can no
    # longer commit a full retire+publish inside it: it blocks on the
    # boundary locks, and its own staging is then refused because the gated
    # writer's transition is already bound (one pending transition per
    # source). Deterministic via event barriers — no sleeps.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")

    outcomes: queue.Queue = queue.Queue()
    parked, release, writer = _gated_pipeline_stale_writer(
        pipeline, "retire_source", ("policy",), outcomes
    )
    writer.start()
    assert parked.wait(timeout=120), "writer never reached its staging boundary"

    conflict = ProposalPipeline(kb, IngestStore(tmp_path / "ingest"))
    conflicting = _run_conflicting_lifecycle_writer(
        conflict, "retire_source", ("policy",), outcomes
    )
    conflicting.start()
    release.set()

    writer.join(timeout=120)
    conflicting.join(timeout=120)
    assert not writer.is_alive() and not conflicting.is_alive()
    results = [outcomes.get(timeout=120), outcomes.get(timeout=120)]
    errors = [result for result in results if isinstance(result, BaseException)]
    successes = [result for result in results if result is None]
    assert len(successes) == 1 and len(errors) == 1
    assert isinstance(errors[0], SourceRegistryError)
    assert "already has a pending transition" in str(errors[0])

    # Exactly one durable staged retirement with its transition bound — the
    # conflicting staging never persisted a proposal or an orphan state.
    staged = [proposal for proposal in pipeline.list() if proposal.status == "staged"]
    assert len(staged) == 1
    assert staged[0].source_change is not None
    assert staged[0].source_change.action == "retire"
    pending = _pending_transitions(store.source_registry)
    assert [transition.proposal_id for transition in pending] == [staged[0].id]
    assert store.source_registry.get("policy").status == "active"
    # Discarding the staged retirement releases the pending transition, so
    # the next lifecycle journey stages cleanly (P3 compensation intact).
    discarded = pipeline.discard(staged[0].id)
    assert discarded is not None and discarded.status == "discarded"
    assert _pending_transitions(store.source_registry) == []
    assert pipeline.retire_source("policy").status == "staged"


def test_reactivate_source_boundary_serializes_a_conflicting_writer(tmp_path) -> None:
    # Same one-boundary shape for the reactivation journey: the conflicting
    # reactivation cannot interleave with the gated staging; it is refused
    # once the gated writer's transition is durably bound.
    kb = _knowledge_base(tmp_path, ["policy"])
    store = IngestStore(tmp_path / "ingest")
    pipeline = ProposalPipeline(kb, store)
    pipeline.register_source("policy", b"policy-v1")
    retirement = pipeline.retire_source("policy")
    pipeline.publish(retirement.id)

    outcomes: queue.Queue = queue.Queue()
    parked, release, writer = _gated_pipeline_stale_writer(
        pipeline, "reactivate_source", ("policy", b"policy-v2"), outcomes
    )
    writer.start()
    assert parked.wait(timeout=120), "writer never reached its staging boundary"

    conflict = ProposalPipeline(kb, IngestStore(tmp_path / "ingest"))
    conflicting = _run_conflicting_lifecycle_writer(
        conflict, "reactivate_source", ("policy", b"policy-v2-conflict"), outcomes
    )
    conflicting.start()
    release.set()

    writer.join(timeout=120)
    conflicting.join(timeout=120)
    assert not writer.is_alive() and not conflicting.is_alive()
    results = [outcomes.get(timeout=120), outcomes.get(timeout=120)]
    errors = [result for result in results if isinstance(result, BaseException)]
    successes = [result for result in results if result is None]
    assert len(successes) == 1 and len(errors) == 1
    assert isinstance(errors[0], SourceRegistryError)
    assert "already has a pending transition" in str(errors[0])

    staged = [proposal for proposal in pipeline.list() if proposal.status == "staged"]
    assert len(staged) == 1
    assert staged[0].source_change is not None
    assert staged[0].source_change.action == "reactivate"
    pending = _pending_transitions(store.source_registry)
    assert [transition.proposal_id for transition in pending] == [staged[0].id]
    assert store.source_registry.get("policy").status == "retired"
    # Discarding the staged reactivation releases the pending transition, so
    # the next lifecycle journey stages cleanly (P3 compensation intact).
    discarded = pipeline.discard(staged[0].id)
    assert discarded is not None and discarded.status == "discarded"
    assert _pending_transitions(store.source_registry) == []
    assert pipeline.reactivate_source("policy", b"policy-v3").status == "staged"


def test_mutation_lock_refuses_unsafe_nested_expansion_without_deadlock(tmp_path):
    # Review blocker 2: sorting each call cannot order a NESTED call that
    # expands an already-held set DOWNWARD in the canonical order — two
    # writers each holding one resource and nesting the wider set wait on
    # each other forever (ABBA). The lock refuses that expansion up front
    # with an actionable MutationLockError instead of entering a possible
    # deadlock; the refusal acquires nothing and the held lock stays intact.
    early = tmp_path / "a-early"
    late = tmp_path / "z-late"
    early.mkdir()
    late.mkdir()
    assert resource_identity(early) < resource_identity(late)

    with mutation_lock(late):
        # Reentrant re-request of the held resource stays legal.
        with mutation_lock(late):
            pass
        # A nested call adding the EARLIER identity is refused before any
        # acquisition — it raises promptly (no timeout/sleep involved).
        with pytest.raises(MutationLockError, match="unsafe nested mutation_lock expansion"):
            with mutation_lock(early, late):
                raise AssertionError("unsafe expansion must never be entered")
    # After the outer hold ends, the refused resource acquires cleanly: the
    # refusal left nothing half-acquired behind.
    with mutation_lock(early):
        pass


def test_mutation_lock_allows_order_preserving_nested_expansion(tmp_path):
    # Review blocker 2, safe side: a nested call may re-request held
    # resources (reentrant) or add resources that sort AFTER everything its
    # thread already holds — canonical order is preserved, so cooperating
    # writers can never cycle.
    early = tmp_path / "a-early"
    late = tmp_path / "z-late"
    early.mkdir()
    late.mkdir()

    with mutation_lock(early):
        with mutation_lock(early, late):
            # Both held: further reentrant requests of each still work.
            with mutation_lock(late):
                pass
            with mutation_lock(early):
                pass
    # Release is complete: both resources re-acquire from scratch.
    with mutation_lock(late, early):
        pass


def test_resource_identity_canonicalizes_case_variants_of_one_location(tmp_path):
    # Review blocker 3: os.path.normcase is a no-op on POSIX, so on a
    # case-insensitive macOS filesystem differently cased spellings of one
    # location would take separate lock identities and race. The identity
    # lower-cases explicitly (conservatively on every platform: over-
    # serializing distinct case-sensitive paths only adds mutual exclusion).
    root = tmp_path / "CamelCase-Store"
    root.mkdir()
    variants = [
        root,
        tmp_path / "camelcase-store",
        tmp_path / "CAMELCASE-STORE",
        tmp_path / "CamelCase-Store",
    ]
    identities = {resource_identity(variant) for variant in variants}
    assert len(identities) == 1
    assert resource_identity(root) == resource_identity(root).lower()

    # One canonical identity means one lock file for every spelling...
    digest = hashlib.sha256(resource_identity(root).encode("utf-8")).hexdigest()
    assert lock_directory() / f"{digest}.lock" == lock_directory() / (
        hashlib.sha256(resource_identity(tmp_path / "camelcase-store").encode("utf-8")).hexdigest()
        + ".lock"
    )

    # ...and one lock end to end: a mutation_lock taken over one spelling is
    # reentered (not re-acquired) through a differently cased spelling.
    registry = SourceRegistry(root)
    registry.register_source("handbook", b"v1")
    with mutation_lock(tmp_path / "camelcase-store"):
        with mutation_lock(root):
            registry.register_source("policy", b"v1")
    reloaded = SourceRegistry(root)
    assert {source.source_id for source in reloaded.list()} == {"handbook", "policy"}


def test_save_raw_performs_every_filesystem_mutation_inside_the_store_lock(tmp_path, monkeypatch):
    # Review blocker (Plan 02 / P3 remediation v3): save_raw used to create
    # the raw source directory BEFORE acquiring the store mutation lock, so a
    # direct raw writer whose lock acquisition failed still mutated the
    # filesystem outside the locked atomic boundary. Every raw mutation —
    # directory creation, safe-name/path derivation, and the atomic byte
    # write — must happen INSIDE the critical section: a refused lock leaves
    # zero filesystem state behind, and the success path persists durable
    # bytes at the returned path. Nested acquisition reenters (the store lock
    # is reentrant), exactly as :meth:`ProposalPipeline.stage` stages raw
    # bytes while already holding the lock.
    from typing import NoReturn

    import lumio_wiki.ingest as ingest_module

    store = IngestStore(tmp_path / "ingest")
    raw_dir = tmp_path / "ingest" / "raw"

    def refuse_lock(*args: object, **kwargs: object) -> NoReturn:
        raise MutationLockError("store mutation lock unavailable")

    # save_raw resolves mutation_lock from the ingest module's globals, so
    # patching it there simulates a refused lock acquisition deterministically.
    monkeypatch.setattr(ingest_module, "mutation_lock", refuse_lock)
    with pytest.raises(MutationLockError, match="store mutation lock unavailable"):
        store.save_raw("proposal-1", b"raw bytes", "notes.md")
    # The refused lock left ZERO filesystem mutation: no raw tree, no
    # proposal artifacts, no temp files — the store root was never created.
    assert not (tmp_path / "ingest").exists()

    monkeypatch.undo()
    path = store.save_raw("proposal-1", b"raw bytes", "notes.md")
    # The success path keeps the raw bytes isolated under the proposal's own
    # raw directory, flattens the filename, and returns that durable path.
    assert path == raw_dir / "proposal-1" / "notes.md"
    assert path.read_bytes() == b"raw bytes"
    assert sorted(p.name for p in raw_dir.iterdir()) == ["proposal-1"]

    # A direct raw writer called while the store lock is already held
    # (nested stage) reenters instead of deadlocking and lands the same
    # durable atomic write.
    with mutation_lock(store.root):
        nested = store.save_raw("proposal-1", b"raw bytes 2", "notes.md")
    assert nested == path
    assert nested.read_bytes() == b"raw bytes 2"
    # save_raw touches ONLY the raw tree; the registry stays write-lazy
    # (no sources.json until a registry mutation) and no temp files leak.
    assert sorted(p.name for p in (tmp_path / "ingest").iterdir()) == ["raw"]
    assert sorted(p.name for p in raw_dir.iterdir()) == ["proposal-1"]
    assert sorted(p.name for p in (raw_dir / "proposal-1").iterdir()) == ["notes.md"]
