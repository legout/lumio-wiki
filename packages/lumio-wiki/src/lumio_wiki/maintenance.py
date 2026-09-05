"""Portable Maintainer workflows: lint, cross-link, and the Dream Cycle.

ADR-0015 ports the ``lint`` and ``cross-linker`` Agent Skills (issue #91,
ADR-0012) from the application package into the portable ``lumio_wiki``
surface and adds the composed **Dream Cycle** — the periodic reflection pass
a living Knowledge Base needs: deterministic health and structural
diagnostics, missing-link candidates ranked by Discovery Graph impact, and an
explicit opt-in step that stages the top repairs as ordinary reviewable
Ingest Proposals.

Everything here is model-free and proposal-first. ``run_lint`` and
``run_dream_cycle`` are read-only. Staging functions never touch the
Knowledge Base on disk: they produce ``staged`` proposals that a Maintainer
reviews and publishes through the Proposal Pipeline
(``stage -> validate -> review -> publish``).

The portable CLI carries no role gate (ADR-0015): the local operator IS the
Maintainer, and the proposal-first guardrail is the write protection. The
application-layer role gate stays in ``lumio.skills``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import msgspec

from lumio_wiki.artifact_store import SourceBindingManifest
from lumio_wiki.ingest import IngestProposal, IngestStore, SourceProvenance
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_CANONICAL,
    GRAPH_SCOPE_DISCOVERY,
    KnowledgeBase,
    Relationship,
    ValidationReport,
    due_review_pages,
    extract_references,
    load_knowledge_base,
)
from lumio_wiki.link_candidates import find_link_candidates
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.records import (
    CompiledPage,
    Entity,
    ExtractedReference,
    GraphHealthReport,
    LinkCandidate,
    RankedLinkCandidate,
    StructuralGraphReport,
)
from lumio_wiki.source_registry import SourceRegistry


class MaintenanceError(Exception):
    """A maintenance staging operation failed."""


#: Provenance marker for proposals authored by the maintenance workflows, so
#: a reviewing Maintainer can see the origin without re-deriving it.
_ORIGIN = "cross-linker"

#: Durability rationale restated on compound-revision proposals. Routing
#: validation (issue #78, P2.6) requires EVERY proposed page in a categorized
#: Knowledge Base — including revisions of existing pages — to carry a
#: non-empty durability rationale, but published pages do not retain one (it
#: is stripped at publish as review metadata). A repair revision makes no NEW
#: durability decision, so the workflow restates the status quo explicitly;
#: the statement is visible in the staged diff and the reviewing Maintainer
#: can override it. This is not a fabricated classification: the page was
#: classified when originally published.
COMPOUND_REVISION_RATIONALE = (
    "Compound revision of an existing published page; the durability "
    "classification from the original publish is unchanged."
)

#: Human-readable scope disclosure carried on every ``LintReport`` and Dream
#: Cycle report. Both graph scopes are named so a Maintainer can distinguish
#: reviewed semantic structure (canonical Relationships) from derived
#: navigation (canonical Relationships PLUS Extracted References).
SCOPE_DISCLOSURE = (
    f"canonical graph scope ({GRAPH_SCOPE_CANONICAL}): reviewed typed "
    "Relationships only. "
    f"discovery graph scope ({GRAPH_SCOPE_DISCOVERY}): canonical Relationships "
    "PLUS Extracted References derived deterministically from authored internal "
    "Markdown links. Extracted References are non-canonical: they support "
    "navigation and Discovery Graph derivation without a second approval."
)


@dataclass(frozen=True, slots=True)
class LintReport:
    """Read-only cross-page QA report with both graph scopes disclosed.

    Carries the authoritative validation report, the Discovery Graph health,
    the structural topology diagnostics for BOTH scopes, and the authored
    Extracted References (already-resolved internal links appear here as
    navigation/health, never as proposals).
    """

    kb_path: str
    validation_report: ValidationReport
    graph_health: GraphHealthReport
    extracted_references: tuple[ExtractedReference, ...]
    canonical_relationships: tuple[Relationship, ...]
    canonical_scope: str
    discovery_scope: str
    scope_disclosure: str
    canonical_structure: StructuralGraphReport
    discovery_structure: StructuralGraphReport
    page_count: int

    @property
    def is_valid(self) -> bool:
        """Whether the Knowledge Base passed authoritative validation."""
        return self.validation_report.is_valid


def _default_index_dir(kb_path: str | Path) -> Path:
    return Path(kb_path) / ".lumio" / "index"


def _run_lint_loaded(
    kb: KnowledgeBase,
    validation_report: ValidationReport,
    *,
    index_dir: str | Path | None = None,
) -> LintReport:
    """Build a lint report from one loaded Knowledge Base view."""
    derived_dir = Path(index_dir) if index_dir is not None else _default_index_dir(kb.root)
    health = kb.graph_health(derived_dir)
    canonical_structure = kb.graph_diagnostics(scope=GRAPH_SCOPE_CANONICAL)
    discovery_structure = kb.graph_diagnostics(scope=GRAPH_SCOPE_DISCOVERY)

    extracted = extract_references(kb.pages)
    canonical: list[Relationship] = []
    for page in kb.pages:
        canonical.extend(kb.related_from(page.title))

    return LintReport(
        kb_path=str(kb.root),
        validation_report=validation_report,
        graph_health=health,
        extracted_references=tuple(extracted),
        canonical_relationships=tuple(canonical),
        canonical_scope=GRAPH_SCOPE_CANONICAL,
        discovery_scope=GRAPH_SCOPE_DISCOVERY,
        scope_disclosure=SCOPE_DISCLOSURE,
        canonical_structure=canonical_structure,
        discovery_structure=discovery_structure,
        page_count=len(kb.pages),
    )


def run_lint(
    kb_path: str | Path,
    *,
    index_dir: str | Path | None = None,
) -> LintReport:
    """Run the read-only cross-page QA report over a Knowledge Base.

    Delegates to the public surface: ``load_knowledge_base``,
    ``KnowledgeBase.graph_health``, ``KnowledgeBase.graph_diagnostics`` (both
    scopes), and ``extract_references``. Model-free; never writes.

    ``index_dir`` selects where the Discovery Graph artifact is read from;
    when omitted a ``.lumio/index`` sibling of the Knowledge Base root is
    used. ``graph_health`` derives the graph in memory when no fresh artifact
    is present, so lint works on a Knowledge Base that has never
    materialized one.
    """
    kb, report = load_knowledge_base(kb_path)
    return _run_lint_loaded(kb, report, index_dir=index_dir)


def find_link_candidates_for_kb(kb: KnowledgeBase) -> list[LinkCandidate]:
    """Find missing-link candidates for an already-loaded Knowledge Base."""
    return find_link_candidates(kb.pages)


def find_ranked_link_candidates(kb: KnowledgeBase) -> list[RankedLinkCandidate]:
    """Rank missing-link candidates for an already-loaded Knowledge Base."""
    candidates = find_link_candidates_for_kb(kb)
    return kb.rank_link_candidates_by_graph_impact(candidates)


# ---------------------------------------------------------------------------
# Cross-link repair (ported from lumio.skills.cross_linker, ADR-0015).
# ---------------------------------------------------------------------------


def directory_relative_target(candidate: LinkCandidate) -> str:
    """Return the link destination relative to the SOURCE page's directory.

    ``candidate.target_path`` is relative to the Knowledge Base root, but an
    authored Markdown link resolves relative to the source page's directory.
    In a categorized (nested) Knowledge Base the two differ: a link from
    ``entities/acme_corp.md`` to ``entities/acme_rival.md`` must be written as
    ``acme_rival.md``, and a cross-category link as
    ``../entities/acme_corp.md``.
    """
    source_dir = PurePosixPath(candidate.source_path).parent
    return os.path.relpath(candidate.target_path, start=str(source_dir)).replace(os.sep, "/")


def repair_mention(page_markdown: str, candidate: LinkCandidate) -> str:
    """Wrap the candidate's mention in a Markdown link to its target.

    The mention sits at ``candidate.line`` (1-based file line) and
    ``candidate.column`` (1-based character column). The matched text is
    preserved verbatim (the finder matches case-insensitively) and wrapped in
    a link to the target page's path relative to the source page's directory.
    """
    lines = page_markdown.split("\n")
    idx = candidate.line - 1
    if idx < 0 or idx >= len(lines):
        raise MaintenanceError(
            f"candidate line {candidate.line} is out of range for {candidate.source_path}"
        )
    line = lines[idx]
    start = candidate.column - 1
    end = start + len(candidate.term)
    if start < 0 or end > len(line) or line[start:end].casefold() != candidate.term.casefold():
        raise MaintenanceError(
            f"candidate mention {candidate.term!r} not found at "
            f"{candidate.source_path}:{candidate.line}:{candidate.column}"
        )
    actual = line[start:end]
    destination = directory_relative_target(candidate)
    lines[idx] = f"{line[:start]}[{actual}]({destination}){line[end:]}"
    return "\n".join(lines)


def mark_compound_revision(
    page_markdown: str,
    *,
    category: str | None = None,
    durability_rationale: str | None = None,
) -> str:
    """Set the ``compound_revision`` directive on a page's frontmatter.

    A cross-link proposal revises an EXISTING Compiled Page. Marking the
    proposal ``compound_revision`` makes the Proposal Pipeline validate it
    against the full candidate Knowledge Base and makes the blast radius
    surface the page as a CHANGE rather than a duplicate-title collision
    (issue #79). It is a proposal-only routing flag, stripped at publish.

    ``category`` re-declares the page's existing Content Category. Published
    pages encode their category in the directory path and carry no
    ``category`` frontmatter key, but the proposal routing validator requires
    every proposed page in a categorized Knowledge Base to declare one — so
    re-proposing an existing page must restate the category its path already
    encodes. Stripped at publish like every routing field.

    ``durability_rationale`` likewise restates review metadata the routing
    validator requires on every proposed page (P2.6) but publish strips.
    """
    if not page_markdown.startswith("---"):
        raise MaintenanceError("page has no YAML frontmatter to edit")
    try:
        _leading, fm_yaml, body = page_markdown.split("---", 2)
    except ValueError as exc:
        raise MaintenanceError("page frontmatter is malformed: missing closing fence") from exc
    data = msgspec.yaml.decode(fm_yaml)
    if not isinstance(data, dict):
        raise MaintenanceError("page frontmatter did not decode to a mapping")
    data["compound_revision"] = True
    if category is not None and "category" not in data:
        data["category"] = category
    if durability_rationale is not None and not data.get("durability_rationale"):
        data["durability_rationale"] = durability_rationale
    encoded = msgspec.yaml.encode(data).decode("utf-8")
    return f"---\n{encoded}---{body}"


def category_for_page_path(kb: KnowledgeBase, page_path: str) -> str | None:
    """Return the configured Content Category a page path already encodes.

    The first path segment of a categorized page IS its category directory;
    it is returned only when it names a category configured in the Control
    File. Root-level pages (legacy flat layout) have no category.
    """
    if kb.control is None:
        return None
    configured = {category.name for category in kb.control.categories}
    parts = PurePosixPath(page_path).parts
    if len(parts) < 2:
        return None
    return parts[0] if parts[0] in configured else None


def _revision_routing_fields(kb: KnowledgeBase, page_path: str) -> dict:
    """Return the routing fields a compound revision must restate (P2.6).

    Categorized Knowledge Bases require the category the page path already
    encodes plus an explicit durability rationale; Legacy Flat Mode requires
    neither.
    """
    if kb.control is None:
        return {}
    return {
        "category": category_for_page_path(kb, page_path),
        "durability_rationale": COMPOUND_REVISION_RATIONALE,
    }


def _stage_revised_page(
    kb: KnowledgeBase,
    store: IngestStore,
    revised_markdown: str,
    source_path: str,
) -> IngestProposal:
    """Assemble and stage one compound-revision proposal through the pipeline.

    Raw bytes are NOT persisted into the ingest store: the revised Markdown is
    the KB's own page content (already on disk), and a raw ``.md`` saved under
    the default in-KB ingest store would be scanned by the page loader. The
    proposal JSON carries the full revised Markdown, which is everything the
    review surface needs.
    """
    provenance = SourceProvenance(
        original_filename=source_path,
        content_type="text/markdown",
        converted_by=_ORIGIN,
        origin=_ORIGIN,
    )
    pipeline = ProposalPipeline(kb, store=store)
    proposal = pipeline.assemble(revised_markdown, provenance, source_path)
    return pipeline.stage(proposal)


def _stage_cross_link_proposal_loaded(
    kb: KnowledgeBase,
    candidate: LinkCandidate,
    *,
    store: IngestStore,
) -> IngestProposal:
    """Stage one cross-link proposal from a loaded Knowledge Base view."""
    source_path = Path(kb.root) / candidate.source_path
    page_markdown = source_path.read_text(encoding="utf-8")
    repaired = mark_compound_revision(
        repair_mention(page_markdown, candidate),
        **_revision_routing_fields(kb, candidate.source_path),
    )
    return _stage_revised_page(kb, store, repaired, candidate.source_path)


def stage_cross_link_proposal(
    kb_path: str | Path,
    candidate: LinkCandidate,
    *,
    store: IngestStore,
) -> IngestProposal:
    """Stage a REVIEWABLE proposal that repairs one missing link.

    Reads the source page's authored Markdown, wraps the candidate mention in
    a Markdown link to its target, marks the page a compound revision, and
    stages the result through the Proposal Pipeline. The Knowledge Base on
    disk is NOT touched. Once a Maintainer publishes, the link becomes
    authored Markdown and is derived as an Extracted Reference on the next
    Discovery Graph derivation, with no second graph-specific approval
    (ADR-0011).
    """
    kb, _report = load_knowledge_base(kb_path)
    return _stage_cross_link_proposal_loaded(kb, candidate, store=store)


# ---------------------------------------------------------------------------
# Duplicate-identity candidates (t_3327f75e): read-only possible-duplicate
# Entity candidates for the Dream report. Advisory always: the explicit
# Entity Merge proposal stays the ONLY mutation path (ADR-0021 forbids
# automatic merging; issue #172 keeps resolution review-only).
# ---------------------------------------------------------------------------

#: Default bound on duplicate-identity candidates carried on a DreamReport.
DUPLICATE_CANDIDATE_LIMIT = 10

#: Signal tiers, strongest last (the tier value). ``signals_rank`` carries the
#: tier of the strongest signal on a candidate.
SIGNAL_TOKEN_OVERLAP = 1
SIGNAL_SHARED_ALIAS = 2
SIGNAL_SHARED_CANONICAL_TITLE = 3

#: Token-overlap alone never crosses an Entity Type boundary; exact surfaces
#: (aliases, titles) do.
_TOKEN_OVERLAP_NEEDS_SAME_TYPE = SIGNAL_TOKEN_OVERLAP


@dataclass(frozen=True, slots=True)
class DuplicateEntityCandidate:
    """One read-only possible-duplicate Entity pair for the Dream report.

    ``signals`` explains EXACTLY why the pair was emitted (one deterministic
    surface match per entry); ``signals_rank`` is the tier of the strongest
    signal. Advisory always: reviewing a candidate routes to the existing
    explicit ``merge-entity`` preview/proposal — nothing merges or stages.
    """

    retired_entity_id: str
    surviving_entity_id: str
    signals: tuple[str, ...]
    signals_rank: int


def _title_tokens(title: str) -> frozenset[str]:
    """Lowercase ASCII word tokens of a title (deterministic, no deps)."""
    tokens: list[str] = []
    token: list[str] = []
    for char in title.lower():
        if char.isascii() and char.isalnum():
            token.append(char)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return frozenset(tokens)


def find_duplicate_entity_candidates(
    pages: Sequence[CompiledPage],
) -> list[DuplicateEntityCandidate]:
    """Rank possible-duplicate Entity pairs from exact identity surfaces.

    Pure and I/O-free. Two DISTINCT Entities become one candidate when their
    identity surfaces collide: a shared alias (strong), a shared Canonical
    Page Title (strongest — a validation error, but an invalid KB must still
    be reportable), or a same-type title token-overlap of at least half the
    smaller title (weakest). Legacy Flat Mode pages (no Entity ID) and
    synthetic pages never enter the comparison. Deterministically ordered:
    strongest rank first, then Entity IDs.
    """
    entities = [
        Entity(
            id=page.id,
            entity_types=list(page.entity_types),
            title=page.title,
            aliases=list(page.aliases),
            path=page.path,
        )
        for page in pages
        if page.id and not page.synthetic
    ]
    found: list[DuplicateEntityCandidate] = []
    for i, a in enumerate(entities):
        for b in entities[i + 1 :]:
            if a.id == b.id:
                continue  # invalid duplicate IDs are a validation error, never a self-merge
            signals: list[str] = []
            rank = 0
            if a.title and a.title == b.title:
                signals.append(f"shared-canonical-title: {a.title}")
                rank = SIGNAL_SHARED_CANONICAL_TITLE
            for alias in sorted(set(a.aliases) & set(b.aliases)):
                if alias:
                    signals.append(f"shared-alias: {alias}")
                    rank = max(rank, SIGNAL_SHARED_ALIAS)
            a_tokens, b_tokens = _title_tokens(a.title), _title_tokens(b.title)
            if a_tokens and b_tokens:
                overlap = a_tokens & b_tokens
                if overlap and len(overlap) * 2 >= min(len(a_tokens), len(b_tokens)):
                    joined = " ".join(sorted(overlap))
                    same_type = bool(set(a.entity_types) & set(b.entity_types))
                    if rank or same_type:
                        signals.append(f"token-overlap: {joined}")
                        rank = max(rank, SIGNAL_TOKEN_OVERLAP)
            if not signals:
                continue
            first, second = sorted((a.id, b.id))
            found.append(
                DuplicateEntityCandidate(
                    retired_entity_id=first,
                    surviving_entity_id=second,
                    signals=tuple(signals),
                    signals_rank=rank,
                )
            )
    found.sort(
        key=lambda c: (-c.signals_rank, c.retired_entity_id, c.surviving_entity_id)
    )
    return found



# ---------------------------------------------------------------------------
# Source Drift (issue #196, ADR-0014 / ADR-0020): the read-only comparison
# between private source state and what currently supports published or
# working-copy knowledge.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceDriftFinding:
    """One deterministic Source Drift observation (issue #196).

    ``scope`` names which tier produced the finding: the current working
    copy or the active Published Version's private Source Binding Manifest.
    A manifest finding never carries ``page_path``: the manifest describes
    the Published Version and is never joined to the current worktree by
    title.
    """

    scope: Literal["working-copy", "published-version"]
    page_title: str
    source_id: str
    kind: Literal["retired-source", "superseded-evidence"]
    page_path: str | None = None
    published_version: str | None = None
    bound_content_hash: str | None = None
    current_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDriftReport:
    """The composed Source Drift result carried on a :class:`DreamReport`.

    ``registry_checked`` is false when no private Source Registry was
    available; ``manifest_status`` always discloses whether (and for which
    Published Version) the manifest tier was checked, or the bounded reason
    it was skipped or failed. An unknown manifest source id makes the
    manifest tier ``incomplete`` — never a clean checked result.
    """

    findings: tuple[SourceDriftFinding, ...] = ()
    registry_checked: bool = False
    manifest_status: str = "not checked: no private Source Registry"

    @property
    def count(self) -> int:
        return len(self.findings)


def run_source_drift_check(
    kb: KnowledgeBase,
    registry: SourceRegistry | None,
    *,
    manifest: SourceBindingManifest | None = None,
    manifest_status: str | None = None,
) -> SourceDriftReport:
    """Compare private source state against the working copy and a manifest.

    Pure and I/O-free: everything is passed in, nothing is read. The
    working-copy tier runs only when ``registry`` is given and iterates
    current non-synthetic pages; unregistered ``sources[].id`` values stay
    public provenance (exactly as :func:`build_binding_manifest` treats
    them). The published-version tier iterates manifest entries directly and
    never resolves titles against ``kb.pages`` — the manifest describes the
    active Published Version, so a title absent from the worktree is still
    reportable (ADR-0020). A retired source takes precedence over a hash
    mismatch for the same entry.
    """
    # Computed facts win: with no registry the working-copy tier never ran
    # and the manifest was never compared, so a caller-passed status can
    # never be echoed as a clean 'checked' result (review finding, #196).
    if registry is None:
        return SourceDriftReport(
            findings=(),
            registry_checked=False,
            manifest_status=(
                manifest_status
                if manifest_status is not None and "checked" not in manifest_status
                else "not checked: no private Source Registry"
            ),
        )

    status = {
        source.source_id: (source.status, source.versions[-1].content_hash)
        for source in registry.list()
    }
    findings: list[SourceDriftFinding] = []
    manifest_incomplete = False

    # Working-copy scope: current non-synthetic pages only.
    for page in kb.pages:
        if getattr(page, "synthetic", False):
            continue
        for source in page.sources:
            if not source.id or source.id not in status:
                continue
            source_state, current_hash = status[source.id]
            if source_state == "retired":
                findings.append(
                    SourceDriftFinding(
                        scope="working-copy",
                        page_title=page.title,
                        source_id=source.id,
                        kind="retired-source",
                        page_path=page.path,
                        current_content_hash=current_hash,
                    )
                )

    # Published-version scope: manifest entries directly, never the worktree.
    if manifest is not None:
        for entry in manifest.entries:
            if entry.synthetic_page:
                continue
            if entry.source_id not in status:
                manifest_incomplete = True
                continue
            source_state, current_hash = status[entry.source_id]
            if source_state == "retired":
                kind: Literal["retired-source", "superseded-evidence"] = "retired-source"
            elif entry.content_hash != current_hash:
                kind = "superseded-evidence"
            else:
                continue
            findings.append(
                SourceDriftFinding(
                    scope="published-version",
                    page_title=entry.page_title,
                    source_id=entry.source_id,
                    kind=kind,
                    published_version=manifest.published_version,
                    bound_content_hash=entry.content_hash,
                    current_content_hash=current_hash,
                )
            )

    # Computed facts win: 'incomplete' describes what the comparison found,
    # so it always overrides a caller's clean 'checked' status (review
    # finding, #196).
    if manifest_incomplete:
        resolved_status = (
            "incomplete: manifest references sources absent from the selected registry"
        )
    elif manifest_status is not None:
        resolved_status = manifest_status
    else:
        resolved_status = (
            f"checked (published version {manifest.published_version})"
            if manifest is not None
            else "not checked: no Published Version binding manifest"
        )

    findings.sort(
        key=lambda f: (
            f.scope,
            f.published_version or "",
            f.page_path or f.page_title,
            f.source_id,
            f.kind,
        )
    )
    return SourceDriftReport(
        findings=tuple(findings),
        registry_checked=True,
        manifest_status=resolved_status,
    )


@dataclass(frozen=True, slots=True)
class SourceCoverageReport:
    """Registered Sources no published Compiled Page declares (t_f703bd88).

    The bounded, advisory companion to Source Drift: the working-copy tier
    of the drift check asks "is every declared source healthy?", this asks
    "does every registered source support knowledge?". ``sample`` carries a
    stable (sorted) bounded sample of unreferenced ids and ``truncated`` is
    true whenever ``sample`` is shorter than the unreferenced count.
    """

    registered: int = 0
    referenced: int = 0
    unreferenced: int = 0
    sample: tuple[str, ...] = ()
    truncated: bool = False
    registry_checked: bool = False
    status: str = "not checked: no private Source Registry"

    @property
    def has_unreferenced(self) -> bool:
        return self.unreferenced > 0


#: Default cap on the unreferenced-ids sample carried on a
#: :class:`SourceCoverageReport`.
SOURCE_COVERAGE_SAMPLE_LIMIT = 10


def _read_registry_sources(
    registry: SourceRegistry,
) -> tuple[dict[str, str], str | None]:
    """Return ``{source_id: status}`` from the registry, or a bounded failure.

    The failure reason is a fixed, secret-free advisory string: raw exception
    text from a malformed private store is never echoed. The broad catch is
    deliberate (ponytail): the store is untrusted private state, and an
    advisory report must degrade instead of crashing. Bounded by design.
    """
    try:
        return {s.source_id: s.status for s in registry.list()}, None
    except Exception:  # ponytail: broad — degraded advisory over a crash
        return {}, "unavailable: registry could not be read"


def run_source_coverage_check(
    kb: KnowledgeBase,
    registry: SourceRegistry | None,
    *,
    status: str | None = None,
    sample_limit: int = SOURCE_COVERAGE_SAMPLE_LIMIT,
) -> SourceCoverageReport:
    """Join the Source Registry against what published pages declare.

    Pure and I/O-free: everything is passed in, nothing is read. A source is
    ``referenced`` when any current non-synthetic page declares its id in
    ``sources[].id`` — matching the drift check's support rule; synthetic
    pages (which may omit provenance, ADR-0014) never confer support.
    Retirement is lifecycle state, not distillation: a retired source still
    counts as unreferenced. Never a validation error; advisory by contract.

    ``status`` discloses why the registry tier was skipped when the caller
    already knows (the CLI resolver passes a fixed reason when no registry
    could be resolved); it is honored only when ``registry`` is None.
    """
    if registry is None:
        return SourceCoverageReport(
            status=status or "not checked: no private Source Registry"
        )
    sources, error = _read_registry_sources(registry)
    if error is not None:
        return SourceCoverageReport(registry_checked=False, status=error)

    referenced: set[str] = set()
    for page in kb.pages:
        if getattr(page, "synthetic", False):
            continue
        for source in page.sources:
            if source.id in sources:
                referenced.add(source.id)

    unreferenced_ids = sorted(set(sources) - referenced)
    sample = tuple(unreferenced_ids[:sample_limit])
    return SourceCoverageReport(
        registered=len(sources),
        referenced=len(referenced),
        unreferenced=len(unreferenced_ids),
        sample=sample,
        truncated=len(unreferenced_ids) > len(sample),
        registry_checked=True,
        status=f"checked ({len(sources)} registered source(s))",
    )


# ---------------------------------------------------------------------------
# The Dream Cycle (ADR-0015): reflect, then optionally stage repairs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DreamReport:
    """The read-only reflection half of the Dream Cycle.

    Composes the authoritative validation status, the Discovery Graph health,
    the structural topology diagnostics for BOTH scopes, the missing-link
    candidates ranked by Discovery Graph impact (issue #127), the pages
    due for review by ``review_after`` (ADR-0023), most overdue first, and
    the optional Source Drift diagnostic (issue #196). Read-only and
    model-free: it never mutates Compiled Pages, graph state, or the store.
    """

    lint: LintReport
    ranked_candidates: tuple[RankedLinkCandidate, ...]
    due_pages: tuple[CompiledPage, ...] = ()
    drift: SourceDriftReport = SourceDriftReport()
    coverage: SourceCoverageReport = SourceCoverageReport()
    #: Read-only possible-duplicate Entity pairs, bounded and stably ranked
    #: (t_3327f75e). Advisory: routing to the explicit Entity Merge proposal
    #: is a Maintainer decision, never an automatic step.
    duplicate_candidates: tuple[DuplicateEntityCandidate, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.lint.is_valid

    @property
    def candidate_count(self) -> int:
        return len(self.ranked_candidates)

    @property
    def due_count(self) -> int:
        return len(self.due_pages)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_candidates)


@dataclass(frozen=True, slots=True)
class DreamStagingResult:
    """The outcome of staging Dream Cycle repairs.

    ``staged`` carries the reviewable proposals created (one per repaired
    candidate, in ranked order); ``skipped`` carries ``(candidate, reason)``
    pairs for candidates whose repair could not be staged (e.g. the mention
    moved since the candidate was found).
    """

    staged: tuple[IngestProposal, ...] = ()
    skipped: tuple[tuple[LinkCandidate, str], ...] = ()


def _run_dream_cycle_loaded(
    kb: KnowledgeBase,
    validation_report: ValidationReport,
    *,
    index_dir: str | Path | None = None,
    registry: SourceRegistry | None = None,
    manifest: SourceBindingManifest | None = None,
    manifest_status: str | None = None,
    coverage_registry: SourceRegistry | None = None,
    coverage_status: str | None = None,
) -> DreamReport:
    """Build one Dream report from a loaded Knowledge Base view."""
    lint = _run_lint_loaded(kb, validation_report, index_dir=index_dir)
    candidates = find_link_candidates(kb.pages)
    ranked = kb.rank_link_candidates_by_graph_impact(candidates)
    due = due_review_pages(kb.pages)
    drift = run_source_drift_check(
        kb,
        registry,
        manifest=manifest,
        manifest_status=manifest_status,
    )
    coverage = run_source_coverage_check(kb, coverage_registry, status=coverage_status)
    duplicates = find_duplicate_entity_candidates(kb.pages)[:DUPLICATE_CANDIDATE_LIMIT]
    return DreamReport(
        lint=lint,
        ranked_candidates=tuple(ranked),
        due_pages=tuple(due),
        drift=drift,
        coverage=coverage,
        duplicate_candidates=tuple(duplicates),
    )


def run_dream_cycle(
    kb_path: str | Path,
    *,
    index_dir: str | Path | None = None,
    registry: SourceRegistry | None = None,
    manifest: SourceBindingManifest | None = None,
    manifest_status: str | None = None,
    coverage_registry: SourceRegistry | None = None,
    coverage_status: str | None = None,
) -> DreamReport:
    """Run the read-only Dream Cycle reflection over a Knowledge Base.

    Composes ``run_lint`` (validation + health + structural diagnostics for
    both scopes) with the deterministic link-candidate finder, ranked by
    Discovery Graph impact, the ``review_after`` due pages, the Source
    Drift diagnostic when the optional private inputs are provided
    (issue #196), and the Source Coverage report when a registry is provided
    (t_f703bd88). Model-free; never writes. The ranking is advisory: it
    never infers a typed Relationship from a Markdown-link proposal
    (ADR-0011).
    """
    kb, validation_report = load_knowledge_base(kb_path)
    return _run_dream_cycle_loaded(
        kb,
        validation_report,
        index_dir=index_dir,
        registry=registry,
        manifest=manifest,
        manifest_status=manifest_status,
        coverage_registry=coverage_registry,
        coverage_status=coverage_status,
    )


def _stage_ranked_link_candidates_loaded(
    kb: KnowledgeBase,
    ranked_candidates: Sequence[RankedLinkCandidate],
    *,
    store: IngestStore,
    limit: int,
) -> DreamStagingResult:
    """Stage a bounded candidate sequence from one loaded Knowledge Base view."""
    staged: list[IngestProposal] = []
    skipped: list[tuple[LinkCandidate, str]] = []
    for ranked in ranked_candidates[:limit]:
        candidate = ranked.candidate
        try:
            staged.append(_stage_cross_link_proposal_loaded(kb, candidate, store=store))
        except (MaintenanceError, OSError) as exc:
            skipped.append((candidate, str(exc)))
    return DreamStagingResult(staged=tuple(staged), skipped=tuple(skipped))


def stage_dream_repairs(
    kb_path: str | Path,
    *,
    store: IngestStore,
    limit: int = 5,
    index_dir: str | Path | None = None,
) -> DreamStagingResult:
    """Stage reviewable repair proposals for the top-ranked Dream candidates.

    Runs the read-only reflection, then stages ONE compound-revision proposal
    per candidate in ranked order (highest Discovery Graph impact first),
    bounded by ``limit``. Candidates that fail to stage (e.g. a stale
    line/column) are skipped with a reason instead of aborting the batch.

    Every staged proposal is ``staged`` and reviewable: the Knowledge Base on
    disk is untouched until a Maintainer publishes through the Proposal
    Pipeline.
    """
    if limit < 1:
        raise MaintenanceError("limit must be >= 1")
    kb, validation_report = load_knowledge_base(kb_path)
    report = _run_dream_cycle_loaded(kb, validation_report, index_dir=index_dir)
    return _stage_ranked_link_candidates_loaded(
        kb,
        report.ranked_candidates,
        store=store,
        limit=limit,
    )
