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

import msgspec.yaml as yaml

from lumio_wiki import publish_reserved_artifacts
from lumio_wiki.ingest import (
    BodyLinkRepairCandidate,
    IngestProposal,
    IngestStore,
    PageRemoval,
    ProposedPage,
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
from lumio_wiki.knowledge_base import (
    HotIndexPin,
    KnowledgeBaseControlFile,
    append_activity_log_entry,
    extract_references,
    make_activity_log_entry,
)
from lumio_wiki.knowledge_base import _parse_frontmatter as parse_frontmatter
from lumio_wiki.publish import apply_proposed_pages, validate_candidate_knowledge_base
from lumio_wiki.records import ValidationReport
from lumio_wiki.source_registry import (
    KnowledgeSource,
    PendingSourceTransition,
    RetirementCandidate,
    SourceRegistryError,
    SourceVersion,
    _validate_source_id,
)


class ProposalPipelineError(Exception):
    """A Proposal Pipeline operation failed."""


class ProposalBlockedError(ProposalPipelineError):
    """A proposal cannot be published because validation blocks it."""


def _drop_relationship_edges(data: dict, removed_title: str) -> bool:
    """Drop frontmatter Relationship edges targeting ``removed_title``.

    The default, reviewed repair for a Page Removal (issue #135, AC5): every
    canonical Relationship whose target would otherwise disappear is removed
    in the same proposal. Only edges are dropped; the page body is untouched
    (body links become location-bearing repair candidates, never guessed —
    AC6). Returns whether any edge was dropped.
    """
    changed = False
    relationships = data.get("relationships")
    if isinstance(relationships, list):
        kept: list = []
        for rel in relationships:
            if isinstance(rel, dict) and str(rel.get("target", "")).strip() == removed_title:
                changed = True
                continue
            kept.append(rel)
        if changed:
            data["relationships"] = kept
    return changed


def _repair_page_for_removal(
    markdown: str, removed_title: str, source_path: str
) -> tuple[str, bool]:
    """Return a page's Markdown with Relationship edges to a removed page dropped.

    Mirrors the rename reference-repair pattern (ADR-0016) but REMOVES the
    edges instead of retargeting them: a removal has no chosen destination, so
    the safe, reviewed repair is to drop the dangling canonical edge. Returns
    ``(rewritten_markdown, was_changed)``.
    """
    try:
        data, body, _ = parse_frontmatter(markdown, Path(source_path))
    except Exception:
        return markdown, False
    changed = _drop_relationship_edges(data, removed_title)
    if not changed:
        return markdown, False
    frontmatter = yaml.encode(data).decode("utf-8").strip()
    return f"---\n{frontmatter}\n---\n{body}", True


def _compute_removal_diff(
    removed_title: str,
    removed_page_path: str,
    removed_page_markdown: str,
    proposed_pages: list,
    existing_pages: dict[str, str],
) -> str:
    """Unified diff for a Page Removal: a deletion block plus repair diffs.

    The removed page is rendered as a unified-diff deletion (every line
    prefixed ``-``) so a Maintainer sees exactly what is excluded from the
    next Published Version, followed by the ordinary diffs of the dependent
    pages whose Relationships were repaired.
    """
    lines: list[str] = [
        f"deleted file: {removed_page_path}",
        f"--- a/{removed_page_path}",
    ]
    for line in removed_page_markdown.splitlines():
        lines.append(f"-{line}")
    lines.append("")
    repair_diff = _compute_diff(proposed_pages, existing_pages)
    if repair_diff:
        lines.append(repair_diff)
    return "\n".join(lines)



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
        """Register bytes under an explicit, stable Knowledge Source identity.

        #133 final review: explicit ``source register`` is the managed-lifecycle
        boundary for this iteration — the reviewed path that establishes a
        private Source identity (and the only ordinary-ingest path that records
        a ``source.register`` audit event). Ordinary file ingest (the legacy
        ``ingest`` CLI / Workshop upload / protocol) is intentionally OUTSIDE
        #133 and is left unchanged: it never establishes or mutates a private
        Source identity.
        """
        return self._require_store().source_registry.register_source(source_id, raw_bytes)

    def _source_impacts(self, source_id: str, action: str) -> list[SourceChangeImpact]:
        # Page-impact lookup matches the EXPLICIT registry ``source_id`` against
        # the ALREADY-PUBLIC ``CompiledPage.sources[].id`` declared on each page
        # (ADR-0014, decision A). The provenance id is part of portable public
        # content; only the registry records/status/version history/candidates
        # are private. No private source-to-page mapping is maintained: a
        # registry id produces an impact only for a page that publicly declares
        # it, so renaming a registered file can never create or retract support.
        #
        # The computation is ACTION-AWARE (#133 final review). Retirement
        # evaluates support AFTER excluding the retiring source: a page whose
        # only active support is that source is sole-source-lost. Reactivation
        # evaluates support AFTER including the reactivated source — it will be
        # active once the proposal publishes — so EVERY affected page sourced by
        # that id is still-supported (the reactivated source itself restores
        # support). The allowed vocabulary stays exactly still-supported /
        # sole-source-lost.
        registry = self._require_store().source_registry
        impacts: list[SourceChangeImpact] = []
        for page in self._kb.pages:
            page_source_ids = [source.id for source in page.sources]
            if source_id not in page_source_ids:
                continue
            if action == "reactivate":
                # The reactivated source itself provides support after
                # publication, so the page is always still-supported.
                impacts.append(SourceChangeImpact(page.title, "still-supported"))
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
            status = "still-supported" if has_other_active_support else "sole-source-lost"
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

    def _bind_and_persist(
        self, transition: PendingSourceTransition, proposal: IngestProposal
    ) -> None:
        """Bind a source transition then persist its reviewable proposal.

        The registry transition is bound first, so a reviewable proposal is never
        left without one. If proposal persistence then raises, the bound
        transition is cancelled so the source can be re-staged (no orphan
        transition). Both writes are single-file atomic; this is exception
        compensation, not a transaction journal (ADR-0014).
        """
        store = self._require_store()
        store.source_registry.bind_pending(transition, proposal.id)
        try:
            store.save_proposal(proposal)
        except Exception:
            store.source_registry.cancel_transition(proposal.id)
            raise

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
        self._bind_and_persist(transition, proposal)
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
        self._bind_and_persist(transition, proposal)
        return proposal

    def record_retirement_candidate(self, source_id: str, trigger: str) -> RetirementCandidate:
        """Record a retirement signal without staging or changing support."""
        return self._require_store().source_registry.record_retirement_candidate(source_id, trigger)

    def dismiss_retirement_candidate(
        self, candidate_id: str, expected_source_id: str | None = None
    ) -> RetirementCandidate:
        """Dismiss a candidate without staging a proposal.

        When ``expected_source_id`` is given, the candidate must belong to that
        Knowledge Source or the dismissal is refused without mutating state.
        Mutating callers (the CLI) use this to require explicit source identity;
        programmatic callers that omit it keep the original behavior.

        #133 final review: ``expected_source_id`` is validated at the boundary
        BEFORE the mismatch check so a secret-bearing value is rejected
        generically and never interpolated into the mismatch error.
        """
        registry = self._require_store().source_registry
        if expected_source_id is not None:
            _validate_source_id(expected_source_id)
            candidate = registry.get_candidate(candidate_id)
            if candidate.source_id != expected_source_id:
                raise SourceRegistryError(
                    f"retirement candidate {candidate_id!r} does not belong to "
                    f"Knowledge Source {expected_source_id!r}"
                )
        return registry.dismiss_retirement_candidate(candidate_id)

    def confirm_retirement_candidate(
        self, candidate_id: str, expected_source_id: str | None = None
    ) -> IngestProposal:
        """Confirm a candidate by staging an ordinary retirement proposal.

        When ``expected_source_id`` is given, the candidate must belong to that
        Knowledge Source or the confirmation is refused WITHOUT staging a
        proposal or mutating the candidate/source — mirroring
        :meth:`dismiss_retirement_candidate`. The web confirm route uses this to
        require explicit source identity from its route parameter so a mismatch
        can never stage a retirement for (or audit) the wrong source.
        """
        registry = self._require_store().source_registry
        if expected_source_id is not None:
            _validate_source_id(expected_source_id)
        candidate = registry.get_candidate(candidate_id)
        if expected_source_id is not None and candidate.source_id != expected_source_id:
            raise SourceRegistryError(
                f"retirement candidate {candidate_id!r} does not belong to "
                f"Knowledge Source {expected_source_id!r}"
            )
        if candidate.status != "pending":
            raise SourceRegistryError(f"retirement candidate {candidate_id!r} is not pending")
        proposal = self.retire_source(candidate.source_id)
        try:
            registry.confirm_retirement_candidate(candidate_id)
        except Exception:
            # The retirement was staged but the candidate decision did not
            # persist: undo the staging so no reviewable proposal or bound
            # transition is left behind. The candidate stays pending (retryable).
            self.discard(proposal.id)
            raise
        return proposal

    def propose_page_removal(
        self,
        title: str,
        *,
        reason: str = "",
        affected_claim_notes: list[str] | None = None,
    ) -> IngestProposal:
        """Stage an explicit, reviewed Page Removal proposal (issue #135).

        Builds and persists a proposal that excludes one Compiled Page from
        the next Published Version and repairs every canonical Relationship
        that would otherwise become invalid in the SAME proposal. A Page
        Removal is never inferred from an omitted page (ADR-0014): it is an
        explicit, declared mutation persisted, inspected, validated,
        published, and discarded through this same Proposal Pipeline.

        ``reason`` is the page-level lost-support rationale (the
        ``sole-source-lost`` classification from a source lifecycle change, or
        a Maintainer-supplied rationale for a direct removal). Per ADR-0014 the
        claim-level lineage design is deferred (#137), so this records the
        page-level rationale and optional Maintainer notes — never raw source
        excerpts or Claim Lineage.
        """
        store = self._require_store()
        proposal = self._assemble_page_removal(
            title, reason=reason, affected_claim_notes=affected_claim_notes or []
        )
        store.save_proposal(proposal)
        return store.get(proposal.id)

    def _assemble_page_removal(
        self,
        title: str,
        *,
        reason: str,
        affected_claim_notes: list[str],
    ) -> IngestProposal:
        """Assemble a Page Removal proposal without persisting it (#135).

        Resolves the page by Canonical Title, generates the dependent-edge
        repair revisions (dropping Relationships to the removed title),
        collects location-bearing body-link repair candidates, drops a stale
        Hot Index pin atomically when needed, and validates the removal + its
        repairs as ONE candidate Knowledge Base. Mirrors the rename (#140)
        and retire/reactivate source-lifecycle flows.
        """
        title = title.strip()
        if not title:
            raise ProposalPipelineError("page removal requires a Canonical Page Title")
        target_page = None
        for page in self._kb.pages:
            if page.title == title:
                target_page = page
                break
        if target_page is None or not target_page.path:
            raise ProposalPipelineError(
                f"no Compiled Page found for Canonical Title {title!r}"
            )

        # Relationship repair (AC5): every OTHER page with a canonical
        # Relationship targeting the removed title gets a revision that DROPS
        # those edges. Redirect is an explicit Maintainer edit to the staged
        # proposal; the default, reviewed repair is to drop the dangling edge.
        proposed_pages: list[ProposedPage] = []
        for page in self._kb.pages:
            if page.title == title or not page.path:
                continue
            if not any(rel.target == title for rel in page.relationships):
                continue
            try:
                page_markdown = (self._kb.root / page.path).read_text(encoding="utf-8")
            except OSError:
                continue
            repaired, changed = _repair_page_for_removal(page_markdown, title, page.path)
            if not changed:
                continue
            proposed_pages.append(
                ProposedPage(relative_path=page.path, title=page.title, markdown=repaired)
            )

        # Body-link repair candidates (AC6): location-bearing, never silently
        # redirected to a guessed page. A remaining link surfaces post-removal
        # as a non-blocking broken-internal-link warning.
        body_link_repairs: list[BodyLinkRepairCandidate] = []
        for ref in extract_references(self._kb.pages):
            if ref.target_title == title and ref.source_title != title:
                body_link_repairs.append(
                    BodyLinkRepairCandidate(
                        source_title=ref.source_title,
                        source_path=ref.source_path,
                        target_title=ref.target_title,
                        origin=ref.origin,
                        line_start=ref.line_start,
                        line_end=ref.line_end,
                    )
                )

        # Hot Index pin (AC7): an unresolved pin is a blocking validation
        # error, so when the removed title is pinned the proposal drops the
        # pin atomically via a proposed Control File.
        control_file: KnowledgeBaseControlFile | None = None
        kb_control = getattr(self._kb, "control", None)
        if kb_control is not None and any(
            pin.title == title for pin in kb_control.hot_index
        ):
            kept_pins = [
                HotIndexPin(title=pin.title, note=pin.note)
                for pin in kb_control.hot_index
                if pin.title != title
            ]
            control_file = KnowledgeBaseControlFile(
                version=kb_control.version,
                categories=list(kb_control.categories),
                hot_index=kept_pins,
                mode=kb_control.mode,
                path=kb_control.path,
            )

        # Validate the removal + repairs (+ pin drop) as ONE candidate. The
        # authoritative gate is the full-candidate validation below; the
        # ingest-routing validator is SKIPPED for a removal proposal because
        # every proposed page is a REPAIR of an existing, already-classified,
        # already-published page (it drops a relationship edge) — exactly as
        # category moves and title renames skip routing re-validation.
        page_validation = validate_candidate_knowledge_base(
            proposed_pages,
            self._kb.root,
            removed_titles=[title],
            control_file=control_file,
        )
        validation_report = ValidationReport(issues=list(page_validation.issues))

        existing_pages = _existing_page_markdown(self._kb)
        removed_markdown: str = existing_pages.get(title) or target_page.body
        diff = _compute_removal_diff(
            title, target_page.path, removed_markdown, proposed_pages, existing_pages
        )
        blast_radius = compute_blast_radius(proposed_pages, self._kb)

        removal = PageRemoval(
            title=title,
            lost_support_reason=reason,
            affected_claim_notes=list(affected_claim_notes),
        )
        provenance = SourceProvenance(
            original_filename=None,
            content_type=None,
            converted_by="lumio-page-removal",
            origin="lumio:page-removal",
        )
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
            control_file=control_file,
            removed_pages=[removal],
            body_link_repairs=body_link_repairs,
        )

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

    def list_sources(self) -> list[KnowledgeSource]:
        """List registered private Knowledge Source identities (read query).

        Mirrors :meth:`list`: returns an empty list when the pipeline has no
        store, so the CLI reads source identities through the public pipeline
        seam instead of reaching into the ingest store's private registry.
        """
        if self._store is None:
            return []
        return self._store.source_registry.list()

    def list_retirement_candidates(self) -> list[RetirementCandidate]:
        """List recorded retirement candidates for review (read query).

        Mirrors :meth:`list_sources`: returns an empty list when the pipeline
        has no store, so the Workshop reads candidates through the public
        pipeline seam. Returns every recorded candidate; the reviewing caller
        filters pending vs. decided (the Workshop renders pending candidates
        with confirm/dismiss actions).
        """
        if self._store is None:
            return []
        return self._store.source_registry.list_candidates()

    def discard(self, proposal_id: str) -> IngestProposal | None:
        """Mark a reviewable proposal as discarded."""
        if self._store is None:
            return None
        proposal = self._store.get(proposal_id)
        if proposal is None or not is_reviewable_proposal(proposal):
            return None
        discarded = self._store.discard(proposal_id)
        if discarded is not None and discarded.source_change is not None:
            try:
                self._store.source_registry.cancel_transition(proposal_id)
            except Exception:
                # Cancellation failed: restore the original reviewable proposal
                # so the discard can be retried with its transition still bound.
                self._store.save_proposal(proposal)
                raise
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
        # issue #135: a Page Removal proposal carries removed titles and an
        # optional Control File (Hot Index pin drop). Both are threaded
        # through the authoritative candidate gate AND the apply step so the
        # removal and its dependent-edge repairs publish as ONE atomic unit.
        removed_titles = [removal.title for removal in proposal.removed_pages]
        candidate_report = validate_candidate_knowledge_base(
            proposal.proposed_pages,
            self._kb.root,
            removed_titles=removed_titles or None,
            control_file=proposal.control_file,
        )
        if not candidate_report.is_valid:
            raise ProposalBlockedError(
                f"proposal {proposal_id!r} candidate failed validation: {candidate_report}"
            )
        apply_proposed_pages(
            proposal.proposed_pages,
            self._kb.root,
            removed_titles=removed_titles or None,
            control_file=proposal.control_file,
        )
        publish_reserved_artifacts(self._kb.root)
        # AC7: record the transition in the append-only Activity Log. Only a
        # categorized Knowledge Base (one with a Control File) carries a
        # portable Activity Log; legacy flat KBs do not. The entry records only
        # the removed Canonical Titles and operation — never Claim Lineage
        # (claim-level lineage is not modeled, ADR-0014).
        if proposal.removed_pages and getattr(self._kb, "control", None) is not None:
            append_activity_log_entry(
                self._kb.root,
                make_activity_log_entry(
                    operation="page-removal",
                    description="removed page(s): "
                    + ", ".join(removal.title for removal in proposal.removed_pages),
                ),
            )
        published = self._store.publish(proposal_id)
        if published is None:
            raise ProposalPipelineError(f"proposal {proposal_id!r} was not publishable")
        if proposal.source_change is not None:
            try:
                self._store.source_registry.apply_transition(proposal.id)
            except Exception:
                self._store.save_proposal(proposal)
                raise
        return published


__all__ = [
    "ProposalBlockedError",
    "ProposalPipeline",
    "ProposalPipelineError",
]
