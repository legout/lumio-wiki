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

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import msgspec
import msgspec.yaml as yaml

from lumio_wiki import publish_reserved_artifacts
from lumio_wiki.ingest import (
    BodyLinkRepairCandidate,
    EntityMerge,
    IngestProposal,
    IngestStore,
    PageRemoval,
    PathPrecondition,
    ProposedPage,
    SourceChangeImpact,
    SourceLifecycleChange,
    SourceProvenance,
    _compute_diff,
    _existing_page_markdown,
    _extract_page_records,
    _recomputed_mutation_identity,
    _validate_page_routing,
    compute_blast_radius,
    is_reviewable_proposal,
)
from lumio_wiki.knowledge_base import (
    CONTROL_FILE_BASENAME,
    HotIndexPin,
    KnowledgeBaseControlFile,
    append_activity_log_entry,
    make_activity_log_entry,
)
from lumio_wiki.knowledge_base import _extract_references as _extract_references
from lumio_wiki.knowledge_base import _parse_frontmatter as parse_frontmatter
from lumio_wiki.mutation import mutation_lock
from lumio_wiki.publish import (
    _capture_mutation_preconditions,
    _expected_precondition_pairs,
    _precondition_drift,
    _precondition_set_mismatch,
    apply_proposed_pages,
    validate_candidate_knowledge_base,
)
from lumio_wiki.records import EntityRedirect, Ontology, ValidationIssue, ValidationReport
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


class ProposalPreconditionError(ProposalPipelineError):
    """A reviewed precondition no longer holds; the proposal must be re-staged.

    Raised by :meth:`ProposalPipeline.publish` (Plan 02 / P4, B03/B04) under
    the mutation lock, BEFORE any candidate is constructed or applied, when
    the durable proposal's private reviewed preconditions no longer match the
    Knowledge Base: an affected path's reviewed bytes changed or vanished, an
    expected-absent destination is now occupied, the Control File changed, or
    the proposal carries no reviewed preconditions at all (staged before they
    were captured). Newer on-disk content is preserved and the proposal stays
    reviewable: discard and re-stage it against the current state — never a
    silent rebase, never an overwrite of newer content.
    """


def _as_path_preconditions(expectations) -> list[PathPrecondition]:
    """Convert publish-side capture expectations into durable records (P4)."""
    return [
        PathPrecondition(
            path=expectation.relative_path,
            role=expectation.role,
            kind=expectation.kind,
            digest=expectation.digest,
        )
        for expectation in expectations
    ]


def _drop_claims_for_entity(data: dict, removed_entity_id: str) -> bool:
    """Drop frontmatter Claims whose object is ``removed_entity_id``.

    The default, reviewed repair for a Page Removal (issue #135, AC5, as
    revised by ADR-0021): every canonical Claim whose object Entity would
    otherwise dangle is removed in the same proposal. Body links are repaired
    separately (AC6): exactly-resolved links are unwrapped and ambiguous ones
    are never guessed. Returns whether any Claim was dropped.
    """
    changed = False
    claims = data.get("claims")
    if isinstance(claims, list):
        kept: list = []
        for claim in claims:
            if (
                isinstance(claim, dict)
                and str(claim.get("object", "")).strip() == removed_entity_id
            ):
                changed = True
                continue
            kept.append(claim)
        if changed:
            data["claims"] = kept
    return changed


def _repair_page_for_removal(
    markdown: str,
    removed_entity_id: str,
    source_path: str,
    body_start_line: int,
    retired_title: str,
    pages,
) -> tuple[str, bool, list[str]]:
    """Return a page's Markdown repaired for a Page Removal (issue #135).

    Two reviewed repairs in one revision (ADR-0021): Claims whose object
    Entity would dangle are dropped, and every internal body link that
    resolves — with the shared resolver's precedence — EXACTLY to the removed
    page is unwrapped to its readable text (``[[T]]``/``[[T|L]]`` -> ``T``/``L``,
    ``[L](t.md)`` -> ``L``). External URLs, images, inline-code spans,
    unresolved links, and ambiguous destinations are never touched: an
    ambiguous destination (the removed page AND another page through the same
    resolution key) is reported, never guessed. Returns
    ``(rewritten_markdown, changed, ambiguous)``.
    """
    try:
        data, body, _ = parse_frontmatter(markdown, Path(source_path))
    except Exception:
        return markdown, False, []
    changed = _drop_claims_for_entity(data, removed_entity_id)

    # Unwrap exactly-resolved body links against the ORIGINAL body spans, so
    # repeated links on one line are each repaired exactly once.
    from lumio_wiki.knowledge_base import _scan_body_links

    source_dir = str(PurePosixPath(source_path).parent) if "/" in source_path else ""
    body_lines = body.splitlines(keepends=True)
    ambiguous: list[str] = []
    for dest, line_start, line_end in _scan_body_links(body, body_start_line):
        kind = _classify_retired_link(dest, pages, source_dir, retired_title)
        if kind is None:
            continue
        if kind == "ambiguous":
            ambiguous.append(
                f"{source_path}: line {line_start}: '{dest}' matches the removed "
                f"page and another page; leaving it unresolved"
            )
            continue
        lo = max(line_start - body_start_line, 0)
        hi = min(line_end - body_start_line, len(body_lines) - 1)
        # Delimiter-bounded patterns (like the Entity Merge repair) so a shared
        # prefix is never rewritten by accident; group 1 carries the readable
        # label/alias, and the ``!`` lookbehind keeps images untouched. The
        # Markdown pattern also matches the optional ``"title"`` tail the
        # scanner excludes from ``dest``.
        escaped = re.escape(dest)
        patterns = (
            re.compile(r"\[\[" + escaped + r"\]\]"),
            re.compile(r"\[\[" + escaped + r"\|([^\]]*)\]\]"),
            re.compile(r"(?<!\!)\[([^\]]*)\]\(" + escaped + r"(?:\s+\"[^\"]*\")?\)"),
        )
        for index in range(lo, hi + 1):
            line = body_lines[index]
            for pattern in patterns:
                if pattern.search(line):
                    replacement = dest if pattern.groups == 0 else lambda m: m.group(1)
                    body_lines[index] = pattern.sub(replacement, line, count=1)
                    changed = True
                    break
    body = "".join(body_lines)

    if not changed:
        return markdown, False, ambiguous
    frontmatter = yaml.encode(data).decode("utf-8").strip()
    return f"---\n{frontmatter}\n---\n{body}", True, ambiguous


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


def _retarget_claim_objects(data: dict, from_id: str, to_id: str) -> bool:
    """Rewrite Claim objects pointing at ``from_id`` to ``to_id`` in place.

    The reviewed Entity Merge repair (issue #169, ADR-0021): every Claim on a
    page whose object Entity is being retired is retargeted to the surviving
    Entity ID in the same proposal. Returns whether any Claim was rewritten.
    """
    claims = data.get("claims")
    changed = False
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict) and str(claim.get("object", "")).strip() == from_id:
                claim["object"] = to_id
                changed = True
    return changed


def _classify_retired_link(dest: str, pages, source_dir: str, retired_title: str) -> str | None:
    """Classify a body-link destination against the retired page.

    Mirrors the shared resolver's lookup precedence (Canonical Title, then
    alias, then path stem) so "exactly resolved" means exactly what the
    resolver means. Returns ``"title"``/``"alias"``/``"path"`` when the
    destination resolves to exactly the retired page through that key
    (repair it), ``"ambiguous"`` when it matches the retired page AND
    another page through the same key (never guess — block the merge), or
    ``None`` when it resolves elsewhere or not at all (leave it alone).
    Titles are the set identity because CompiledPage records are unhashable;
    a valid Knowledge Base has unique titles.
    """
    from lumio_wiki.knowledge_base import _destination_stem

    stem = _destination_stem(dest).casefold().lstrip("/")
    if not stem:
        return None
    title_matches = {page.title for page in pages if page.title and page.title.casefold() == stem}
    if title_matches:
        if retired_title not in title_matches:
            return None
        # A unique Canonical-Title match wins over any alias or path match —
        # exactly like the resolver — so a same-named alias on another page
        # must not make this repair ambiguous. Case-colliding titles
        # (``Beta``/``BETA``) casefold onto this one stem: never guess.
        return "title" if len(title_matches) == 1 else "ambiguous"
    alias_matches = {
        page.title for page in pages for alias in page.aliases if alias and alias.casefold() == stem
    }
    if alias_matches:
        if retired_title not in alias_matches:
            return None
        return "alias" if len(alias_matches) == 1 else "ambiguous"
    # Path stems, mirroring the resolver: source-dir-relative first, then
    # global. Each lookup keys a unique page path, so a match is never
    # ambiguous by itself.
    by_path: dict[str, str] = {}
    for page in pages:
        if page.path:
            pstem = page.path
            if pstem.lower().endswith(".md"):
                pstem = pstem[: -len(".md")]
            by_path.setdefault(pstem.casefold(), page.title)
    target: str | None = None
    if source_dir:
        target = by_path.get(f"{source_dir.casefold()}/{stem}")
    if target is None:
        target = by_path.get(stem)
    if target is None:
        return None
    return "path" if target == retired_title else None


def _repair_body_links_for_merge(
    markdown: str,
    source_path: str,
    body_start_line: int,
    retired_title: str,
    surviving_title: str,
    surviving_path: str,
    pages,
) -> tuple[str, bool, list[str]]:
    """Rewrite exactly-resolved body links targeting the retired page (issue #169).

    A reviewed Entity Merge repairs exactly resolved links in the same
    proposal (ADR-0021): every internal link that resolves — with the shared
    resolver's precedence — to exactly the retired page is rewritten to
    target the surviving page (wikilinks and title-resolved links to the
    surviving Canonical Title, path-resolved links to a root-absolute
    surviving path). A destination that matches the retired page AND another
    page through the same resolution key is an AMBIGUOUS repair: it is never
    guessed, reported verbatim, and blocks staging. Returns
    ``(rewritten_markdown, changed, ambiguous)``.
    """
    from lumio_wiki.knowledge_base import _scan_body_links

    source_dir = str(PurePosixPath(source_path).parent) if "/" in source_path else ""
    body_lines = markdown.splitlines(keepends=True)
    ambiguous: list[str] = []
    changed = False
    # Split once: repairs shift no characters on their own line, so scan the
    # ORIGINAL body and apply one exact substring replacement per scanned link.
    for dest, line_start, line_end in _scan_body_links(markdown, body_start_line):
        kind = _classify_retired_link(dest, pages, source_dir, retired_title)
        if kind is None:
            continue
        if kind == "ambiguous":
            ambiguous.append(
                f"{source_path}: line {line_start}: '{dest}' matches the retired "
                f"page and another page; refusing to guess a repair target"
            )
            continue
        # Title/alias resolutions rewrite to the surviving Canonical Title;
        # path resolutions rewrite to a root-absolute surviving path (which
        # resolves from every source directory).
        rel = surviving_path if kind == "path" else surviving_title
        lo = max(line_start - body_start_line, 0)
        hi = min(line_end - body_start_line, len(body_lines) - 1)
        # Wikilinks always rewrite to the surviving title; Markdown links use
        # the resolution-derived destination. Delimiter-bounded patterns so a
        # shared prefix (``[[beta`` inside ``[[beta2]]``) is never rewritten
        # by accident.
        replacements = (
            (f"[[{dest}]]", f"[[{surviving_title}]]"),
            (f"[[{dest}|", f"[[{surviving_title}|"),
            (f"]({dest})", f"]({rel})"),
            (f"]({dest} ", f"]({rel} "),
        )
        for index in range(lo, hi + 1):
            line = body_lines[index]
            for old, new in replacements:
                if old in line:
                    body_lines[index] = line.replace(old, new, 1)
                    changed = True
                    break
    return "".join(body_lines), changed, ambiguous


class ProposalPipeline:
    """Stage, validate, review, publish, and discard Ingest Proposals.

    Constructed over a loaded Knowledge Base (``kb``) and, optionally, a
    filesystem-backed :class:`IngestStore` for raw-byte isolation and proposal
    persistence. Every operation is independent of a web request and a model
    provider.
    """

    def __init__(self, kb, store: IngestStore | None = None, artifact_store=None) -> None:
        self._kb = kb
        self._store = store
        # Optional private Source Artifact Store (issue #164, ADR-0020):
        # when configured, managed ingest retains the ORIGINAL bytes as an
        # immutable artifact bound to the exact Source Version. ``None`` keeps
        # today's hash-only behavior.
        self._artifact_store = artifact_store

    def _require_store(self) -> IngestStore:
        if self._store is None:
            raise RuntimeError("source lifecycle operations require an IngestStore")
        return self._store

    def _assembly_preconditions(
        self, proposed_pages: list[ProposedPage], removed_titles: list[str] | None
    ) -> list[PathPrecondition]:
        """Capture the assembly-time reviewed snapshot (Plan 02 / P4, B03/B04).

        Every assemble route binds this snapshot to its returned proposal so
        the reviewed content and the base it was reviewed against are ONE
        snapshot even though assembly runs outside any lock. The staging
        boundary re-verifies the snapshot under the Knowledge Base + store
        locks (:meth:`_bind_reviewed_state`) and refuses a drifted assembly
        — a recapture there would bless stale content instead.
        """
        return _as_path_preconditions(
            _capture_mutation_preconditions(
                proposed_pages, self._kb.root, removed_titles=removed_titles
            )
        )

    def _bind_reviewed_state(self, proposal: IngestProposal) -> IngestProposal:
        """Bind reviewed preconditions and content identity (Plan 02 / P4).

        Called under the Knowledge Base + store mutation locks at staging, so
        the durable proposal and its reviewed base are ONE consistent
        snapshot. Two binding shapes:

        - A proposal WITHOUT preconditions (built outside the pipeline —
          graph exchange, external import, hand-assembled) gets a freshly
          captured reviewed base at this first stage boundary: the affected
          paths (replaced/merged/renamed bytes, move source bytes and
          expected-absent target, created destinations, removal bytes) plus
          the consumed/rewritten Control File state, as resolved by the same
          destination resolution live application uses.
        - A proposal CARRYING an assembly snapshot (every pipeline assemble
          route binds one) has that snapshot VERIFIED against the current
          filesystem/control state instead of being recaptured: any drift or
          structural divergence raises :class:`ProposalPreconditionError` so
          the stale assembly is re-assembled against the current state —
          never silently rebased (a recapture here would bless content
          assembled from an older snapshot).

        Finally the deterministic content identity covering every
        publish-relevant mutation field is bound next to the preconditions,
        so durable metadata and durable content can no longer be separated:
        publication recomputes it from the durable record under the mutation
        lock (:meth:`_reject_stale_base`) and fails closed on a mismatch.
        """
        mutates_knowledge_base = bool(
            proposal.proposed_pages or proposal.removed_pages or proposal.control_file is not None
        )
        if not proposal.preconditions:
            if mutates_knowledge_base:
                expectations = _capture_mutation_preconditions(
                    proposal.proposed_pages,
                    self._kb.root,
                    removed_titles=[removal.title for removal in proposal.removed_pages] or None,
                )
                preconditions = _as_path_preconditions(expectations)
            else:
                # A source-lifecycle proposal mutates no Knowledge Base path:
                # its captured-nothing precondition set stays deliberately
                # empty (distinct from ``None``, which means "never captured").
                preconditions: list[PathPrecondition] = []
        else:
            preconditions = self._verify_assembly_snapshot(proposal, list(proposal.preconditions))
        return msgspec.structs.replace(
            proposal,
            preconditions=preconditions,
            reviewed_identity=_recomputed_mutation_identity(proposal),
        )

    def _verify_assembly_snapshot(
        self, proposal: IngestProposal, snapshot: list[PathPrecondition]
    ) -> list[PathPrecondition]:
        """Verify a proposal's assembly snapshot against the current state (P4).

        Never recaptures: the snapshot describes the state the proposal
        content was assembled (and reviewed) against, so any divergence means
        the assembly is STALE and must be redone — recapturing current state
        would bless stale content. Two checks run here, both under the
        Knowledge Base + store mutation locks:

        - drift: every recorded reviewed byte/absence must still hold;
        - structure: the recorded (path, role) set must still describe
          exactly the affected paths the proposal's own content resolves to,
          so content edited after assembly cannot ride a foreign snapshot.
        """
        root = Path(self._kb.root)
        drift = _precondition_drift(snapshot, root)
        if drift:
            details = "; ".join(drift)
            raise ProposalPreconditionError(
                f"proposal {proposal.id!r} was assembled against a Knowledge Base "
                f"state that has since changed: {details}. Refusing to stage a "
                "review whose base already drifted — re-assemble the proposal "
                "against the current state, then review and publish it; the "
                "current files are never guessed as the reviewed base"
            )
        expected_pairs = _expected_precondition_pairs(
            proposal.proposed_pages,
            root,
            removed_titles=[removal.title for removal in proposal.removed_pages] or None,
        )
        mismatch = _precondition_set_mismatch(snapshot, expected_pairs)
        if mismatch:
            details = "; ".join(mismatch)
            raise ProposalPreconditionError(
                f"proposal {proposal.id!r} carries preconditions that do not "
                f"describe its own affected paths: {details}. Refusing to stage "
                "content that no longer matches its reviewed snapshot — "
                "re-assemble the proposal against the current state, then "
                "review and publish it"
            )
        return list(snapshot)

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

    def managed_ingest(
        self,
        raw_bytes: bytes,
        content_type: str | None,
        filename: str | None,
        source_id: str,
        authored_markdown: str,
        *,
        source_url: str | None = None,
        retrieved_at: str | None = None,
        consulted_sources: list | None = None,
    ) -> IngestProposal:
        """Bind an original raw Knowledge Source and an authored page (issue #149).

        The ONE deep, proposal-first host-Distiller operation over the Source
        Processor, Source Registry, and Proposal Pipeline. It records private
        provenance over the ORIGINAL bytes (converter name derived from routing
        — the ``[documents]`` extra never runs because the host agent authored
        the page), resolves the explicit ``source_id`` against the private
        registry, blocks staging unless the authored page declares that id in
        ``sources[].id``, and assembles + stages a single reviewable proposal
        from the AUTHORED Markdown. Raw bytes are never written under the
        Knowledge Base root: the registry retains identity/version hashes
        privately (ADR-0014), so an identical retry reuses the current Source
        Version without duplication and a changed-bytes retry is rejected
        without registry or proposal mutation.

        issue #178: ``source_url``/``retrieved_at`` carry the truthful FINAL
        URL provenance of a fetched Knowledge Source (recorded in proposal
        provenance only — never page content), and ``consulted_sources``
        records a research bundle's consulted URLs as provenance.
        """
        from lumio_wiki.ingest import (
            ManagedIngestError,
            _authored_page_declares_source,
            _managed_provenance,
        )

        # 1. Provenance over the ORIGINAL bytes; no converter runs.
        provenance = _managed_provenance(raw_bytes, content_type, filename, source_id)
        if source_url is not None or retrieved_at is not None or consulted_sources:
            provenance = msgspec.structs.replace(
                provenance,
                source_url=source_url,
                retrieved_at=retrieved_at,
                consulted_sources=list(consulted_sources or []),
            )
        # 2. Resolve the explicit source identity in PRIVATE registry state
        #    (register new / reuse identical / reject changed / reject retired)
        #    and — when a Source Artifact Store is configured — retain the
        #    original bytes as a private artifact through the idempotent
        #    upload+binding saga (issue #164, ADR-0020: create-only upload,
        #    verify size/digest, then record the binding; a failed upload
        #    never reports retention and a retry recovers). Without a store
        #    the hash-only behavior of #149 is unchanged.
        registry = self._require_store().source_registry
        registry.register_or_reuse(
            source_id, raw_bytes, filename=filename, content_type=content_type
        )
        if self._artifact_store is not None:
            from lumio_wiki.artifact_store import ArtifactStoreError, retain_artifact

            try:
                retain_artifact(
                    self._artifact_store,
                    registry,
                    source_id=source_id,
                    raw_bytes=raw_bytes,
                    content_type=content_type,
                    filename=filename,
                )
            except ArtifactStoreError as exc:
                raise ManagedIngestError(
                    f"Source Artifact retention failed for {source_id!r}: {exc} "
                    "— retry the ingest to recover; the Source Version is "
                    "registered but not reported as retained"
                ) from exc
        # 3. The authored page must cite this source_id; a missing/mismatched
        #    id blocks staging with an actionable diagnostic.
        _authored_page_declares_source(authored_markdown, source_id)
        # 4. The AUTHORED Markdown (not distilled text) determines page content.
        proposal = self.assemble(authored_markdown, provenance, filename)
        # 5. Stage WITHOUT raw bytes: identity/version hashes live privately in
        #    the registry; raw bytes never reach the KB root or Reader/export.
        return self.stage(proposal)

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
            # Plan 02 / P4: a source-lifecycle proposal mutates no Knowledge
            # Base path (the registry transition is private state), so its
            # captured precondition set is deliberately empty — distinct from
            # ``None``, which marks a proposal staged before preconditions
            # were captured and is refused at publish.
            preconditions=[],
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

        Plan 02 / P3: the store and registry resources are held under their
        interprocess mutation locks (one consistent global order — see
        :func:`lumio_wiki.mutation.mutation_lock`) through bind, persist, and
        the cancel compensation, so cooperating processes can never interleave
        a second bind for the same source (the registry re-checks the
        no-pending rule under the lock) nor a transition/proposal teardown.

        ``bind_pending`` also re-validates the staged transition against the
        durable source state re-read under those locks: a transition whose
        precondition was invalidated between stage and bind (a concurrent
        retire/reactivate published in the window) is refused BEFORE any
        mutation, so the rejection compensates cleanly — no orphan pending
        transition, and ``save_proposal`` is never reached, so no stale
        proposal is persisted. The caller re-stages against the new state.
        """
        store = self._require_store()
        registry = store.source_registry
        with mutation_lock(store.root, registry.root):
            registry.bind_pending(transition, proposal.id)
            try:
                # Plan 02 / P4: the source-lifecycle proposal mutates no
                # Knowledge Base path (its captured-nothing precondition set
                # is already bound), so only the content identity is bound
                # here — under the same locks that persist the record.
                store.save_proposal(self._bind_reviewed_state(proposal))
            except Exception:
                registry.cancel_transition(proposal.id)
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
        the next Published Version and repairs every dependent page that
        would otherwise reference it invalidly in the SAME proposal: canonical
        Relationships to the removed title are dropped, and internal body
        links that resolve exactly to it are unwrapped to their readable text
        (ambiguous destinations are never guessed). A Page
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
        # Plan 02 / P4 (B03/B04): assembly (which binds the reviewed snapshot),
        # snapshot verification, and persistence run under the Knowledge Base +
        # store mutation locks, so the reviewed content and its captured base
        # are one consistent snapshot and cooperating writers cannot interleave
        # a publish.
        with mutation_lock(self._kb.root, store.root):
            proposal = self._assemble_page_removal(
                title, reason=reason, affected_claim_notes=affected_claim_notes or []
            )
            store.save_proposal(self._bind_reviewed_state(proposal))
            persisted = store.get(proposal.id)
            if persisted is None:  # pragma: no cover - just persisted under the store lock
                raise ProposalPipelineError(f"proposal {proposal.id!r} disappeared after staging")
        return persisted

    def _assemble_page_removal(
        self,
        title: str,
        *,
        reason: str,
        affected_claim_notes: list[str],
    ) -> IngestProposal:
        """Assemble a Page Removal proposal without persisting it (#135).

        Resolves the page by Canonical Title, generates the dependent-page
        repair revisions (dropping Relationships to the removed title and
        unwrapping exactly-resolved body links to their readable text),
        collects location-bearing body-link repair disclosures, drops a stale
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
            raise ProposalPipelineError(f"no Compiled Page found for Canonical Title {title!r}")
        removed_entity_id = target_page.id

        # Claim repair (AC5, ADR-0021): every OTHER page with a canonical
        # Relationship targeting the removed title gets a revision that DROPS
        # those edges. Redirect is an explicit Maintainer edit to the staged
        # proposal; the default, reviewed repair is to drop the dangling edge.
        # AC6: the same revision unwraps exactly-resolved body links to their
        # readable text, so a page is proposed when either a Claim or an
        # exactly-resolved body link targets the removed page; an ambiguous
        # destination is reported for review, never guessed.
        # AC6: location-bearing disclosure of exactly-resolved body links,
        # derived from the shared Extracted Reference extraction. Replaced
        # pages keep their location line (reviewer visibility); ambiguous
        # destinations never appear (they are not exactly resolved).
        _refs, diagnostics = _extract_references(self._kb.pages)
        proposed_pages: list[ProposedPage] = []
        body_link_repairs: list[BodyLinkRepairCandidate] = []
        validation_issues: list = []
        for page in self._kb.pages:
            if page.title == title or not page.path:
                continue
            try:
                page_markdown = (self._kb.root / page.path).read_text(encoding="utf-8")
            except OSError:
                continue
            _data, _body, body_start_line = parse_frontmatter(
                page_markdown, self._kb.root / page.path
            )
            repaired, changed, ambiguous = _repair_page_for_removal(
                page_markdown,
                removed_entity_id,
                page.path,
                body_start_line,
                title,
                self._kb.pages,
            )
            for note in ambiguous:
                validation_issues.append(
                    ValidationIssue(
                        file=page.path,
                        field="body-links",
                        message=note,
                        severity="warning",
                    )
                )
            if not changed:
                continue
            proposed_pages.append(
                ProposedPage(relative_path=page.path, title=page.title, markdown=repaired)
            )

        # Disclosure (AC6): every exactly-resolved body link is disclosed
        # once for reviewer visibility. Pages with a Claim drop the edge and
        # the link in ONE revision; body-only pages get the unwrap-only one.
        for ref in _refs:
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
        # pin atomically via a proposed Control File. The ontology travels
        # through UNCHANGED (Plan 02 / P4 review): the pin-drop rewrites only
        # the Hot Index section, so proposing a Control File without the
        # reviewed ontology would silently erase every Entity Type, Predicate,
        # and redirect on publication — the reviewed redirect/pin state the
        # preconditions capture is preserved verbatim except for the pins.
        control_file: KnowledgeBaseControlFile | None = None
        kb_control = getattr(self._kb, "control", None)
        if kb_control is not None and any(pin.title == title for pin in kb_control.hot_index):
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
                ontology=kb_control.ontology,
            )

        # Validate the removal + repairs (+ pin drop) as ONE candidate. The
        # authoritative gate is the full-candidate validation below; the
        # ingest-routing validator is SKIPPED for a removal proposal because
        # every proposed page is a REPAIR of an existing, already-classified,
        # already-published page (it drops a relationship edge and unwraps
        # exactly-resolved body links) — exactly as category moves and title
        # renames skip routing re-validation.
        page_validation = validate_candidate_knowledge_base(
            proposed_pages,
            self._kb.root,
            removed_titles=[title],
            control_file=control_file,
        )
        validation_report = ValidationReport(
            issues=list(page_validation.issues) + validation_issues
        )

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
            # Plan 02 / P4 (B03/B04): the assembly binds the reviewed
            # snapshot (removed bytes, repair destinations, Control File
            # state) so the persisted record and its base are ONE snapshot;
            # the caller verifies it under the staging locks.
            preconditions=self._assembly_preconditions(proposed_pages, [title]),
        )

    def propose_entity_merge(
        self,
        retired_entity_id: str,
        surviving_entity_id: str,
        *,
        reason: str = "",
    ) -> IngestProposal:
        """Stage an explicit, reviewed Entity Merge proposal (issue #169).

        One atomic proposal that retires one Entity: the surviving Entity
        keeps its stable ID and page; the retired Entity's Claims migrate to
        the surviving page; every Claim on any page whose object is the
        retired ID is retargeted to the surviving ID; every exactly-resolved
        body link targeting the retired page is repaired to the survivor; the
        retired ID is recorded as an ontology redirect; and the retired page
        is excluded from the next Published Version. Automatic or ambiguous
        merges are forbidden (ADR-0021): alias/vector/FTS similarity may
        surface merge CANDIDATES but never mutates a proposal — the
        surviving Entity is always an explicit, reviewed choice.

        Ambiguous link repair (a destination matching the retired page AND
        another page) refuses to stage: it is never guessed. Merge
        collisions (duplicate Claim IDs after migration), redirect cycles,
        and any invalid resulting ontology are caught by the authoritative
        candidate gate and block publication.
        """
        store = self._require_store()
        # Plan 02 / P4 (B03/B04): same lock + snapshot boundary as the removal
        # journey — the merge's reviewed pages, redirect/pin Control File
        # state, and retired-page bytes are captured by the assembly, verified
        # under the locks that persist them, and bound to the content
        # identity.
        with mutation_lock(self._kb.root, store.root):
            proposal = self._assemble_entity_merge(
                retired_entity_id, surviving_entity_id, reason=reason
            )
            store.save_proposal(self._bind_reviewed_state(proposal))
            persisted = store.get(proposal.id)
            if persisted is None:  # pragma: no cover - just persisted under the store lock
                raise ProposalPipelineError(f"proposal {proposal.id!r} disappeared after staging")
        return persisted

    def _assemble_entity_merge(
        self,
        retired_entity_id: str,
        surviving_entity_id: str,
        *,
        reason: str,
    ) -> IngestProposal:
        """Assemble an Entity Merge proposal without persisting it (#169)."""
        retired_entity_id = retired_entity_id.strip()
        surviving_entity_id = surviving_entity_id.strip()
        if not retired_entity_id or not surviving_entity_id:
            raise ProposalPipelineError("entity merge requires two Entity IDs")
        if retired_entity_id == surviving_entity_id:
            raise ProposalPipelineError("cannot merge an entity into itself")
        control = getattr(self._kb, "control", None)
        if control is None:
            raise ProposalPipelineError(
                "entity merge requires a version-2 Knowledge Base with a Control File "
                "(Legacy Flat Mode has no ontology redirects)"
            )
        page_by_entity = {page.id: page for page in self._kb.pages if page.id}
        retired_page = page_by_entity.get(retired_entity_id)
        surviving_page = page_by_entity.get(surviving_entity_id)
        if retired_page is None or not retired_page.path:
            raise ProposalPipelineError(
                f"no Compiled Page found for retired Entity {retired_entity_id!r}"
            )
        if surviving_page is None or not surviving_page.path:
            raise ProposalPipelineError(
                f"no Compiled Page found for surviving Entity {surviving_entity_id!r}"
            )
        retired_title = retired_page.title
        surviving_title = surviving_page.title

        try:
            retired_markdown = (self._kb.root / retired_page.path).read_text(encoding="utf-8")
            surviving_markdown = (self._kb.root / surviving_page.path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProposalPipelineError(f"could not read a merge page: {exc}") from exc

        all_pages = [page for page in self._kb.pages if page.path]
        ambiguous_links: list[str] = []
        proposed_pages: list[ProposedPage] = []

        def _merge_page_markdown(markdown: str, path: str) -> tuple[str, bool]:
            """Retarget Claim objects + repair exactly-resolved body links."""
            data, body, body_start = parse_frontmatter(markdown, Path(path))
            claims_changed = _retarget_claim_objects(data, retired_entity_id, surviving_entity_id)
            repaired_body, links_changed, ambiguous = _repair_body_links_for_merge(
                body,
                path,
                body_start,
                retired_title,
                surviving_title,
                surviving_page.path,
                all_pages,
            )
            ambiguous_links.extend(ambiguous)
            if not claims_changed and not links_changed:
                return markdown, False
            frontmatter = yaml.encode(data).decode("utf-8").strip()
            return f"---\n{frontmatter}\n---\n{repaired_body}", True

        # 1. Claim repair + body-link repair on every OTHER page. The
        #    surviving page is assembled separately (it also absorbs the
        #    retired Entity's Claims).
        for page in all_pages:
            if page.title in (retired_title, surviving_title):
                continue
            try:
                markdown = (self._kb.root / page.path).read_text(encoding="utf-8")
            except OSError:
                continue
            merged, changed = _merge_page_markdown(markdown, page.path)
            if changed:
                proposed_pages.append(
                    ProposedPage(relative_path=page.path, title=page.title, markdown=merged)
                )
        if ambiguous_links:
            raise ProposalPipelineError(
                "ambiguous link repair blocks this entity merge "
                "(never guessed; resolve the collision and retry): " + "; ".join(ambiguous_links)
            )

        # 2. Surviving page revision: its own Claims (retargeted) plus the
        #    retired page's Claims (retargeted), and its body links repaired.
        #    A Claim ID that exists on both pages becomes a duplicate in the
        #    candidate — the merge-collision gate blocks publication.
        surviving_data, surviving_body, surviving_line = parse_frontmatter(
            surviving_markdown, Path(surviving_page.path)
        )
        _retarget_claim_objects(surviving_data, retired_entity_id, surviving_entity_id)
        merged_claims = list(surviving_data.get("claims") or [])
        retired_data, _retired_body, _retired_line = parse_frontmatter(
            retired_markdown, Path(retired_page.path)
        )
        if isinstance(retired_data.get("claims"), list):
            _retarget_claim_objects(retired_data, retired_entity_id, surviving_entity_id)
            merged_claims.extend(retired_data["claims"])
        repaired_survivor_body, links_changed, ambiguous = _repair_body_links_for_merge(
            surviving_body,
            surviving_page.path,
            surviving_line,
            retired_title,
            surviving_title,
            surviving_page.path,
            all_pages,
        )
        if ambiguous:
            raise ProposalPipelineError(
                "ambiguous link repair blocks this entity merge "
                "(never guessed; resolve the collision and retry): " + "; ".join(ambiguous)
            )
        if merged_claims:
            surviving_data["claims"] = merged_claims
        frontmatter = yaml.encode(surviving_data).decode("utf-8").strip()
        merged_markdown = f"---\n{frontmatter}\n---\n{repaired_survivor_body}"
        if merged_markdown != surviving_markdown:
            proposed_pages.insert(
                0,
                ProposedPage(
                    relative_path=surviving_page.path,
                    title=surviving_title,
                    markdown=merged_markdown,
                ),
            )

        # 3. Control File: record the retired Entity ID as a redirect to the
        #    surviving ID. A resulting redirect cycle or Claim-ID collision is
        #    caught by the authoritative candidate gate below and blocks
        #    publication.
        ontology = control.ontology or Ontology()
        redirects = [
            redirect for redirect in ontology.redirects if redirect.from_id != retired_entity_id
        ]
        redirects.append(EntityRedirect(from_id=retired_entity_id, to_id=surviving_entity_id))
        new_ontology = Ontology(
            entity_types=ontology.entity_types,
            predicates=ontology.predicates,
            redirects=redirects,
        )
        # An unresolved Hot Index pin is a blocking validation error, so a pin
        # on the retired title drops atomically with the merge (as in #135).
        kept_pins = [
            HotIndexPin(title=pin.title, note=pin.note)
            for pin in control.hot_index
            if pin.title != retired_title
        ]
        control_file = KnowledgeBaseControlFile(
            version=control.version,
            categories=list(control.categories),
            hot_index=kept_pins,
            mode=control.mode,
            path=control.path,
            ontology=new_ontology,
        )

        # Validate the merge + repairs as ONE candidate Knowledge Base.
        page_validation = validate_candidate_knowledge_base(
            proposed_pages,
            self._kb.root,
            removed_titles=[retired_title],
            control_file=control_file,
        )
        validation_report = ValidationReport(issues=list(page_validation.issues))

        existing_pages = _existing_page_markdown(self._kb)
        diff = _compute_removal_diff(
            retired_title, retired_page.path, retired_markdown, proposed_pages, existing_pages
        )
        blast_radius = compute_blast_radius(proposed_pages, self._kb)

        removal = PageRemoval(
            title=retired_title,
            lost_support_reason=reason or f"merged into {surviving_title}",
        )
        entity_merge = EntityMerge(
            retired_entity_id=retired_entity_id,
            surviving_entity_id=surviving_entity_id,
            retired_title=retired_title,
            surviving_title=surviving_title,
        )
        provenance = SourceProvenance(
            original_filename=None,
            content_type=None,
            converted_by="lumio-entity-merge",
            origin="lumio:entity-merge",
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
            entity_merges=[entity_merge],
            # Plan 02 / P4 (B03/B04): same assembly-bound reviewed snapshot
            # as the removal route (repaired/surviving revisions, retired
            # page bytes, Control File state).
            preconditions=self._assembly_preconditions(proposed_pages, [retired_title]),
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

        Plan 02 / P4 (B03/B04): assembly runs outside the staging locks, so
        the returned proposal carries the ASSEMBLY-TIME reviewed snapshot
        (affected paths + Control File state) next to the reviewed content.
        :meth:`stage` re-verifies that snapshot under the Knowledge Base +
        store locks and refuses a drifted assembly — an overlapping change
        landing between assemble and stage can never become the captured
        base while the review still describes the older snapshot.
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
            # Plan 02 / P4 (B03/B04): the assembly-time reviewed snapshot
            # rides on the unstaged proposal; the staging boundary verifies
            # it under the locks instead of recapturing current state.
            preconditions=self._assembly_preconditions(proposed_pages, None),
        )

    def stage(
        self,
        proposal: IngestProposal,
        *,
        raw_bytes: bytes | None = None,
        filename: str | None = None,
    ) -> IngestProposal:
        """Persist a proposal (and its raw source, isolated from the KB) for review.

        Plan 02 / P3: raw-byte isolation and proposal persistence happen under
        the store's interprocess mutation lock, and the returned proposal is
        re-read from durable state.

        Plan 02 / P3 review: the DURABLE proposal is re-read and checked
        BEFORE any write. A stale instance re-staging an object whose
        proposal has already reached a terminal ``published``/``discarded``
        state is refused with :class:`ProposalPipelineError` — no raw bytes
        are written and durable terminal state is never replaced (the
        store's :meth:`IngestStore.save_proposal` refuses that overwrite
        too; only the pipeline's explicit rollback compensation may restore
        a reviewable proposal over terminal state).

        Plan 02 / P4 (B03/B04): staging binds the proposal's reviewed state
        and persists it with it, holding the Knowledge Base + store
        interprocess mutation locks (one consistent global order — see
        :func:`lumio_wiki.mutation.mutation_lock`) across verification,
        capture, and persistence so the reviewed base and the durable record
        are one consistent snapshot of the Knowledge Base. A proposal that
        carries an ASSEMBLY snapshot (every pipeline assemble route binds
        one) is verified against the current state under these locks: an
        overlapping page/control change that landed between assembly and
        staging refuses with :class:`ProposalPreconditionError` — never a
        silent recapture that would bless stale content. A proposal WITHOUT
        a snapshot (graph exchange, external import, hand-assembled) gets a
        freshly captured reviewed base at this boundary. Either way the
        deterministic content identity is bound next to the preconditions.
        """
        if self._store is None:
            raise RuntimeError("ProposalPipeline.stage requires an IngestStore")
        with mutation_lock(self._kb.root, self._store.root):
            durable = self._store.get(proposal.id)
            if durable is not None and not is_reviewable_proposal(durable):
                raise ProposalPipelineError(
                    f"proposal {proposal.id!r} is already {durable.status} and cannot be re-staged"
                )
            reviewed = self._bind_reviewed_state(proposal)
            raw_path: Path | None = None
            if raw_bytes is not None and filename is not None:
                raw_path = self._store.save_raw(proposal.id, raw_bytes, filename)
            self._store.save_proposal(reviewed, raw_path)
            staged = self._store.get(proposal.id)
            if staged is None:  # pragma: no cover - just persisted under the store lock
                raise ProposalPipelineError(f"proposal {proposal.id!r} disappeared after staging")
            return staged

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
        """Mark a reviewable proposal as discarded.

        Plan 02 / P3: the whole read/check/mutate/compensate sequence runs
        under the store + registry interprocess mutation locks (one consistent
        global order). The reviewable check reads DURABLE proposal state — a
        stale instance cannot discard over a terminal transition — and the
        registry transition cancellation and its proposal-restore compensation
        stay inside the same critical section.
        """
        if self._store is None:
            return None
        store = self._store
        with mutation_lock(store.root, store.source_registry.root):
            proposal = store.get(proposal_id)
            if proposal is None or not is_reviewable_proposal(proposal):
                return None
            discarded = store.discard(proposal_id)
            if discarded is not None and discarded.source_change is not None:
                try:
                    store.source_registry.cancel_transition(proposal_id)
                except Exception:
                    # Cancellation failed: restore the original reviewable proposal
                    # so the discard can be retried with its transition still bound.
                    # Authorized rollback compensation (Plan 02 / P3 review): the
                    # restored object was re-read from durable state and verified
                    # reviewable at the start of THIS critical section, so the
                    # private restore deliberately overwrites the just-written
                    # terminal state — a stale caller's ordinary save/re-stage is
                    # still refused and cannot reach this path.
                    store._restore_reviewable(proposal)
                    raise
        return discarded

    def _reject_stale_base(self, proposal_id: str, proposal: IngestProposal) -> None:
        """Refuse to publish over a base that no longer matches the review (P4).

        Runs under the Knowledge Base + store + registry mutation locks,
        immediately before candidate construction (Plan 02 / B03/B04). The
        DURABLE proposal's private reviewed state is checked against the
        CURRENT filesystem/control state, in fail-closed layers that apply
        to EVERY durable proposal — source-lifecycle records included:

        1. no captured reviewed preconditions at all (``None`` — a proposal
           staged before preconditions were captured, or stripped by the
           store after an altering save) is refused; source-lifecycle
           proposals carry an explicit EMPTY captured set instead;
        2. no reviewed content identity, or the identity recomputed from the
           DURABLE record's mutation content differs from the stored one —
           the reviewed content and its metadata were separated after review
           (an altered proposal retaining its old claim), so unreviewed
           content can never publish;
        3. the recorded precondition path-set does not match the proposal —
           for Knowledge Base mutations the affected-path set freshly
           resolved from the durable record (changed destinations, removals,
           or moves cannot retain records captured for the original
           structure), for source-lifecycle proposals exactly the EMPTY set
           (a mutating-nothing record carrying Knowledge Base precondition
           rows is altered after review);
        4. drift — a recorded reviewed byte vanished/changed, an
           expected-absent destination is occupied, or the reviewed Control
           File state changed (Knowledge Base mutations only: a
           source-lifecycle proposal has no filesystem base to drift).

        Every failure raises :class:`ProposalPreconditionError` with restage
        guidance, preserving newer on-disk content. Never a silent rebase,
        never a guess from the current files.
        """
        mutates_knowledge_base = bool(
            proposal.proposed_pages or proposal.removed_pages or proposal.control_file is not None
        )
        if proposal.preconditions is None:
            raise ProposalPreconditionError(
                f"proposal {proposal_id!r} carries no captured reviewed "
                "preconditions: it was staged before reviewed bases were "
                "captured (Plan 02 / P4) or its reviewed claim was stripped "
                "after an altering save. Inspect it, then discard and restage "
                "it against the current Knowledge Base — the current files "
                "are never guessed as the reviewed base"
            )
        if not proposal.reviewed_identity:
            raise ProposalPreconditionError(
                f"proposal {proposal_id!r} carries no reviewed content identity: "
                "its mutation content was never bound to its reviewed metadata "
                "(staged before Plan 02 / P4's durable-tampering defense, or "
                "altered afterwards). Inspect it, then discard and restage it "
                "against the current Knowledge Base — the current files are "
                "never guessed as the reviewed base"
            )
        identity = _recomputed_mutation_identity(proposal)
        if identity != proposal.reviewed_identity:
            raise ProposalPreconditionError(
                f"proposal {proposal_id!r} no longer matches its reviewed content "
                "identity: the durable mutation content (proposed pages, "
                "removals, moves, body repairs, Control File, source-lifecycle "
                "change) was altered after review while its reviewed metadata "
                "was retained. Refusing to publish unreviewed content: discard "
                "this proposal and restage a fresh one against the current "
                "state, then review and publish it"
            )
        if not mutates_knowledge_base:
            # A source-lifecycle proposal mutates only private registry state:
            # its reviewed base is exactly the EMPTY captured-nothing set
            # bound at staging. Any recorded Knowledge Base precondition row
            # means the durable record was altered after review.
            if proposal.preconditions:
                raise ProposalPreconditionError(
                    f"proposal {proposal_id!r} carries reviewed Knowledge Base "
                    "precondition records but its durable content mutates no "
                    "Knowledge Base path: the durable record was altered after "
                    "review while its reviewed metadata was retained. Refusing "
                    "to publish: discard this proposal and restage a fresh "
                    "one against the current state, then review and publish it"
                )
            return
        expected_pairs = _expected_precondition_pairs(
            proposal.proposed_pages,
            Path(self._kb.root),
            removed_titles=[removal.title for removal in proposal.removed_pages] or None,
        )
        mismatch = _precondition_set_mismatch(proposal.preconditions, expected_pairs)
        if mismatch:
            details = "; ".join(mismatch)
            raise ProposalPreconditionError(
                f"proposal {proposal_id!r} no longer matches its reviewed base: "
                f"{details}. Refusing to silently rebase or overwrite newer "
                "content: discard this proposal and restage a fresh one against "
                "the current state, then review and publish it"
            )
        drift = _precondition_drift(proposal.preconditions, Path(self._kb.root))
        if drift:
            details = "; ".join(drift)
            raise ProposalPreconditionError(
                f"proposal {proposal_id!r} no longer matches its reviewed base: "
                f"{details}. Refusing to silently rebase or overwrite newer "
                "content: discard this proposal and restage a fresh one against "
                "the current state, then review and publish it"
            )

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

        Plan 02 / P3 (B02/B03): the whole read/check/mutate/commit/rollback
        sequence runs under the interprocess mutation locks of the three
        resources it touches — Knowledge Base root, ingest store, and Source
        Registry — acquired in the one consistent global order
        (:func:`lumio_wiki.mutation.mutation_lock` sorts identities, so no
        cooperating writer can deadlock). The proposal is re-read from DURABLE
        store state under the lock and re-checked for reviewability, so a
        proposal another instance already discarded or published can never be
        published from a stale view, and exactly one of publish/discard wins.
        The registry transition applies inside the same critical section, and
        its failure compensation (restore the reviewable proposal) runs there
        too, so in-memory and durable state agree after any exception.

        Plan 02 / P4 (B03/B04): still under the same locks and BEFORE the
        candidate is constructed, the durable proposal's private reviewed
        state is verified against the current filesystem/control state
        (:meth:`_reject_stale_base`) in four fail-closed layers — reviewed
        preconditions present, reviewed content identity present and equal
        to the identity recomputed from the durable record (an altered
        proposal retaining its old reviewed claim is refused), the
        freshly-resolved affected-path set matching the stored records
        (changed destinations/removals/moves cannot retain old records),
        and no drift in the recorded reviewed bytes/absences/Control File
        state. The FULL current candidate is then revalidated from the real
        root (never a stale in-memory ``self._kb`` snapshot), so disjoint
        intervening changes that introduce alias/entity/Claim conflicts are
        still blocked.
        """
        if self._store is None:
            raise RuntimeError("ProposalPipeline.publish requires an IngestStore")
        store = self._store
        registry = store.source_registry
        with mutation_lock(self._kb.root, store.root, registry.root):
            proposal = store.get(proposal_id)
            if proposal is None or not is_reviewable_proposal(proposal):
                raise ProposalPipelineError(f"proposal {proposal_id!r} is not reviewable")
            if proposal.blocked:
                raise ProposalBlockedError(f"proposal {proposal_id!r} is blocked by validation")
            # Plan 02 / P4 (B03/B04): under the locks and before any candidate
            # construction, the durable proposal's reviewed base must still
            # match the current filesystem/control state.
            self._reject_stale_base(proposal_id, proposal)
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
            # (claim-level lineage is not modeled, ADR-0014). Plan 02 / P4: the
            # categorized decision reads the CURRENT Control File presence from
            # the root under the lock — never the possibly stale in-memory
            # ``self._kb`` snapshot (a legacy→categorized migration publishes
            # before the stale object would know about it).
            if (Path(self._kb.root) / CONTROL_FILE_BASENAME).is_file():
                if proposal.entity_merges:
                    # issue #169: a reviewed Entity Merge logs its own transition
                    # (retired Entity -> surviving Entity), not a page-removal.
                    append_activity_log_entry(
                        self._kb.root,
                        make_activity_log_entry(
                            operation="entity-merge",
                            description="merged entity(s): "
                            + ", ".join(
                                f"{merge.retired_entity_id} -> {merge.surviving_entity_id}"
                                for merge in proposal.entity_merges
                            ),
                        ),
                    )
                elif proposal.removed_pages:
                    append_activity_log_entry(
                        self._kb.root,
                        make_activity_log_entry(
                            operation="page-removal",
                            description="removed page(s): "
                            + ", ".join(removal.title for removal in proposal.removed_pages),
                        ),
                    )
            published = store.publish(proposal_id)
            if published is None:
                raise ProposalPipelineError(f"proposal {proposal_id!r} was not publishable")
            if proposal.source_change is not None:
                try:
                    registry.apply_transition(proposal.id)
                except Exception:
                    # Authorized rollback compensation (Plan 02 / P3 review): the
                    # restored object was re-read from durable state and verified
                    # reviewable at the start of THIS critical section, so the
                    # private restore deliberately overwrites the just-written
                    # terminal state for retry — a stale caller's ordinary
                    # save/re-stage is still refused and cannot reach this path.
                    store._restore_reviewable(proposal)
                    raise
        return published


__all__ = [
    "ProposalBlockedError",
    "ProposalPipeline",
    "ProposalPipelineError",
    "ProposalPreconditionError",
]
