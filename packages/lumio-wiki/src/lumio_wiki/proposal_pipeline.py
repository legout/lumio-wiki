"""Proposal Pipeline: stage, validate, review, publish, and discard Ingest
Proposals (issue #95, AC3; canonical owner moved into ``lumio-wiki`` by
issue #97).

The Proposal Pipeline owns the post-distillation journey of an Ingest Proposal:
assembling a proposal from distilled Markdown (page extraction, diff,
validation, blast radius), persisting raw bytes in isolation from the Knowledge
Base, and transitioning a proposal through review, publish, and discard. It
depends on no web request and no model provider: distillation happens before
the pipeline, and the store is local filesystem state. Git/shared/hybrid
storage backends and the atomic candidate-swap-with-rollback remain the full
web application's responsibility (issue #93).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from lumio_wiki import publish_reserved_artifacts
from lumio_wiki.ingest import (
    IngestProposal,
    IngestStore,
    SourceChangeImpact,
    SourceLifecycleChange,
    SourceProvenance,
    _compute_diff,
    _existing_page_markdown,
    _extract_page_records,
    _validate_page_routing,
    compute_blast_radius,
    is_reviewable_proposal,
)
from lumio_wiki.publish import apply_proposed_pages, validate_candidate_knowledge_base
from lumio_wiki.records import ValidationReport
from lumio_wiki.source_registry import (
    RetirementCandidate,
    SourceRegistryError,
    SourceVersion,
)


class ProposalPipelineError(Exception):
    """A Proposal Pipeline operation failed."""


class ProposalBlockedError(ProposalPipelineError):
    """A proposal cannot be published because validation blocks it."""


class ProposalPipeline:
    """Stage, validate, review, publish, and discard Ingest Proposals.

    Constructed over a loaded Knowledge Base (``kb``) and, optionally, a
    filesystem-backed :class:`IngestStore` for raw-byte isolation and proposal
    persistence. Every operation is independent of a web request and a model
    provider.
    """

    def __init__(self, kb, store: IngestStore | None = None) -> None:
        self._kb = kb
        self._store = store

    def _require_store(self) -> IngestStore:
        if self._store is None:
            raise RuntimeError("source lifecycle operations require an IngestStore")
        return self._store

    def register_source(self, source_id: str, raw_bytes: bytes) -> SourceVersion:
        """Register bytes under an explicit, stable Knowledge Source identity."""
        return self._require_store().source_registry.register_source(source_id, raw_bytes)

    def _source_impacts(self, source_id: str, action: str) -> list[SourceChangeImpact]:
        registry = self._require_store().source_registry
        impacts: list[SourceChangeImpact] = []
        for page in self._kb.pages:
            page_source_ids = [source.id for source in page.sources]
            if source_id not in page_source_ids:
                continue
            has_other_active_support = False
            for other_id in page_source_ids:
                if other_id == source_id:
                    continue
                try:
                    other = registry.get(other_id)
                except SourceRegistryError:
                    continue
                if other.status == "active":
                    has_other_active_support = True
                    break
            if action == "retire":
                status = "still-supported" if has_other_active_support else "sole-source-lost"
            else:
                status = "still-supported" if has_other_active_support else "support-restored"
            impacts.append(SourceChangeImpact(page.title, status))
        return impacts

    def _source_change_proposal(self, change: SourceLifecycleChange) -> IngestProposal:
        report = validate_candidate_knowledge_base([], self._kb.root)
        return IngestProposal(
            id=uuid.uuid4().hex,
            status="staged",
            created_at=datetime.now(UTC).isoformat(),
            provenance=SourceProvenance(None, None, "source-lifecycle"),
            proposed_pages=[],
            affected_pages=[impact.page_title for impact in change.impacts],
            diff="",
            validation_report=report,
            blocked=not report.is_valid,
            source_change=change,
        )

    def retire_source(self, source_id: str) -> IngestProposal:
        """Stage retirement while leaving the source active until publication."""
        store = self._require_store()
        transition = store.source_registry.stage_retirement(source_id)
        change = SourceLifecycleChange(
            action="retire",
            source_id=source_id,
            trigger=f"source {source_id} retired",
            impacts=self._source_impacts(source_id, "retire"),
        )
        proposal = self._source_change_proposal(change)
        store.save_proposal(proposal)
        store.source_registry.bind_pending(transition, proposal.id)
        return proposal

    def reactivate_source(self, source_id: str, raw_bytes: bytes) -> IngestProposal:
        """Stage a fresh version while leaving a retired source inactive."""
        store = self._require_store()
        transition = store.source_registry.stage_reactivation(source_id, raw_bytes)
        change = SourceLifecycleChange(
            action="reactivate",
            source_id=source_id,
            trigger=f"source {source_id} reactivated",
            impacts=self._source_impacts(source_id, "reactivate"),
        )
        proposal = self._source_change_proposal(change)
        store.save_proposal(proposal)
        store.source_registry.bind_pending(transition, proposal.id)
        return proposal

    def record_retirement_candidate(self, source_id: str, trigger: str) -> RetirementCandidate:
        """Record a retirement signal without staging or changing support."""
        return self._require_store().source_registry.record_retirement_candidate(source_id, trigger)

    def dismiss_retirement_candidate(self, candidate_id: str) -> RetirementCandidate:
        """Dismiss a candidate without staging a proposal."""
        return self._require_store().source_registry.dismiss_retirement_candidate(candidate_id)

    def confirm_retirement_candidate(self, candidate_id: str) -> IngestProposal:
        """Confirm a candidate by staging an ordinary retirement proposal."""
        registry = self._require_store().source_registry
        candidate = registry.get_candidate(candidate_id)
        if candidate.status != "pending":
            raise SourceRegistryError(f"retirement candidate {candidate_id!r} is not pending")
        proposal = self.retire_source(candidate.source_id)
        registry.confirm_retirement_candidate(candidate_id)
        return proposal

    def assemble(
        self,
        distilled_markdown: str,
        provenance: SourceProvenance,
        filename: str | None,
    ) -> IngestProposal:
        """Build a staged Ingest Proposal from distilled Markdown (AC3).

        Reuses the authoritative page-extraction, diff, validation, and
        blast-radius behavior from the ingest module so provenance, category/type
        routing, durability rationale, diff, and blast radius are identical to
        the web path.
        """
        proposed_pages = _extract_page_records(distilled_markdown, filename, self._kb)
        existing_pages = _existing_page_markdown(self._kb)
        diff = _compute_diff(proposed_pages, existing_pages)
        # All proposals are validated as a full candidate (existing pages and
        # Control File with proposed pages overlaid) — the same authoritative
        # gate publication uses. Isolated proposed-page validation cannot see
        # cross-Knowledge-Base invariants and falsely blocked NEW pages whose
        # typed Relationships targeted EXISTING canonical titles (and emitted
        # a spurious Legacy Flat Mode warning for categorized Knowledge
        # Bases, since the isolated temp tree carries no Control File).
        page_validation = validate_candidate_knowledge_base(proposed_pages, self._kb.root)
        routing_issues = _validate_page_routing(proposed_pages, self._kb)
        validation_report = ValidationReport(issues=list(page_validation.issues) + routing_issues)
        blast_radius = compute_blast_radius(proposed_pages, self._kb)
        return IngestProposal(
            id=uuid.uuid4().hex,
            status="staged",
            created_at=datetime.now(UTC).isoformat(),
            provenance=provenance,
            proposed_pages=proposed_pages,
            affected_pages=[page.title for page in proposed_pages],
            diff=diff,
            validation_report=validation_report,
            blocked=not validation_report.is_valid,
            blast_radius=blast_radius,
        )

    def stage(
        self,
        proposal: IngestProposal,
        *,
        raw_bytes: bytes | None = None,
        filename: str | None = None,
    ) -> IngestProposal:
        """Persist a proposal (and its raw source, isolated from the KB) for review."""
        if self._store is None:
            raise RuntimeError("ProposalPipeline.stage requires an IngestStore")
        raw_path: Path | None = None
        if raw_bytes is not None and filename is not None:
            raw_path = self._store.save_raw(proposal.id, raw_bytes, filename)
        self._store.save_proposal(proposal, raw_path)
        return self._store.get(proposal.id)

    def review(self, proposal_id: str) -> IngestProposal | None:
        """Return a staged proposal by id, or ``None`` if absent / no store."""
        if self._store is None:
            return None
        return self._store.get(proposal_id)

    def list(self) -> list[IngestProposal]:
        """List staged proposals (empty when the pipeline has no store)."""
        if self._store is None:
            return []
        return self._store.list()

    def discard(self, proposal_id: str) -> IngestProposal | None:
        """Mark a reviewable proposal as discarded."""
        if self._store is None:
            return None
        proposal = self._store.get(proposal_id)
        if proposal is None or not is_reviewable_proposal(proposal):
            return None
        discarded = self._store.discard(proposal_id)
        if discarded is not None and discarded.source_change is not None:
            self._store.source_registry.cancel_transition(proposal_id)
        return discarded

    def publish(self, proposal_id: str) -> IngestProposal:
        """Publish a reviewable proposal to the local Knowledge Base root (AC3).

        Validates the prospective candidate Knowledge Base, applies the proposed
        pages to the Knowledge Base root, regenerates reserved Navigation/Hot
        Index artifacts, then marks the proposal terminal. Provider-free and
        web-free.

        The candidate gate is authoritative: ``validate_candidate_knowledge_base``
        RETURNS a ``ValidationReport`` (it does not raise) and never mutates the
        real Knowledge Base, so the report MUST be inspected before applying
        pages. This stays identical to the web publish path, which gates on the
        same candidate report, and catches cross-Knowledge-Base invariants
        (alias uniqueness across the whole KB, relationship-target resolution)
        that the proposal's assemble-time ISOLATED validation cannot see for
        NON-compound proposals.
        """
        if self._store is None:
            raise RuntimeError("ProposalPipeline.publish requires an IngestStore")
        proposal = self._store.get(proposal_id)
        if proposal is None or not is_reviewable_proposal(proposal):
            raise ProposalPipelineError(f"proposal {proposal_id!r} is not reviewable")
        if proposal.blocked:
            raise ProposalBlockedError(f"proposal {proposal_id!r} is blocked by validation")
        candidate_report = validate_candidate_knowledge_base(proposal.proposed_pages, self._kb.root)
        if not candidate_report.is_valid:
            raise ProposalBlockedError(
                f"proposal {proposal_id!r} candidate failed validation: {candidate_report}"
            )
        apply_proposed_pages(proposal.proposed_pages, self._kb.root)
        publish_reserved_artifacts(self._kb.root)
        if proposal.source_change is not None:
            self._store.source_registry.apply_transition(proposal.id)
        published = self._store.publish(proposal_id)
        if published is None:
            raise ProposalPipelineError(f"proposal {proposal_id!r} was not publishable")
        return published


__all__ = [
    "ProposalBlockedError",
    "ProposalPipeline",
    "ProposalPipelineError",
]
