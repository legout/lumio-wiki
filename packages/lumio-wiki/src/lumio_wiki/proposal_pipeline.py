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
    SourceProvenance,
    _compute_diff,
    _existing_page_markdown,
    _extract_page_records,
    _validate_page_routing,
    _validate_proposed_pages,
    compute_blast_radius,
    is_reviewable_proposal,
)
from lumio_wiki.publish import apply_proposed_pages, validate_candidate_knowledge_base
from lumio_wiki.records import ValidationReport


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
        # Compound revisions integrate with EXISTING Compiled Pages, so they are
        # validated as a full candidate (existing pages with proposed pages
        # overlaid). Plain proposals keep the lighter isolated validation.
        if any(page.compound_revision for page in proposed_pages):
            page_validation = validate_candidate_knowledge_base(
                proposed_pages, self._kb.root
            )
        else:
            page_validation = _validate_proposed_pages(proposed_pages)
        routing_issues = _validate_page_routing(proposed_pages, self._kb)
        validation_report = ValidationReport(
            issues=list(page_validation.issues) + routing_issues
        )
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
        return self._store.discard(proposal_id)

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
            raise ProposalPipelineError(
                f"proposal {proposal_id!r} is not reviewable"
            )
        if proposal.blocked:
            raise ProposalBlockedError(
                f"proposal {proposal_id!r} is blocked by validation"
            )
        candidate_report = validate_candidate_knowledge_base(
            proposal.proposed_pages, self._kb.root
        )
        if not candidate_report.is_valid:
            raise ProposalBlockedError(
                f"proposal {proposal_id!r} candidate failed validation: {candidate_report}"
            )
        apply_proposed_pages(proposal.proposed_pages, self._kb.root)
        publish_reserved_artifacts(self._kb.root)
        published = self._store.publish(proposal_id)
        if published is None:
            raise ProposalPipelineError(
                f"proposal {proposal_id!r} was not publishable"
            )
        return published


__all__ = [
    "ProposalBlockedError",
    "ProposalPipeline",
    "ProposalPipelineError",
]
