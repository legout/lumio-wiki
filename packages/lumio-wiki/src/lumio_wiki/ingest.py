"""Ingest pipeline: raw Knowledge Source -> staged Ingest Proposal (issue #97).

The canonical proposal model, page-extraction / diff / validation / blast-radius
glue, the model-free ingestion entry point, and the filesystem-backed
:class:`IngestStore` live here so a ``lumio-wiki``-only environment can run the
full ingestion -> distill -> propose -> validate -> review -> publish -> discard
journey. This module never imports LiteParse, MarkItDown, or an OpenAI provider
at the module level. Source-processor routing (including document formats via
the ``[documents]`` extra) is handled by :func:`select_source_processor` (issue
#100); the actual converter implementations live in
:mod:`lumio_wiki.source_processor`. An OpenAI-compatible Distiller is an
adapter in the full application (``lumio.distiller``).
"""

from __future__ import annotations

import difflib
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import msgspec
import msgspec.yaml as yaml

from lumio_wiki.knowledge_base import (
    KnowledgeBaseControlFile,
    _as_relationships,
    _as_sources,
    _as_string_list,
    _parse_frontmatter,
    extend_control_file_categories,
    validate,
)
from lumio_wiki.okf import (
    OKF_PROFILE1_QUERY,
    OkfImportDiagnostic,
    OkfImportPage,
    OkfProfile1Import,
)
from lumio_wiki.records import (
    CompiledPage,
    ValidationIssue,
    ValidationReport,
)
from lumio_wiki.source_registry import SourceRegistry

if TYPE_CHECKING:
    from lumio_wiki.source_processor import SourceProcessor


class SourceProvenance(msgspec.Struct, frozen=True):
    """Identity and routing metadata for the raw source behind a proposal."""

    original_filename: str | None
    content_type: str | None
    converted_by: str  # "liteparse" or "markitdown"
    source_hash: str | None = None
    # Lineage marker for sources promoted from a Conversation Source submission
    # (#36). Carries the original Conversation Source provenance (e.g.
    # ``reader-upload:<filename>``) so a reviewing Maintainer can see the
    # submission origin without re-deriving it. ``None`` for normal uploads.
    origin: str | None = None


class ProposedPage(msgspec.Struct, frozen=True):
    """One proposed Compiled Page distilled from a raw source.

    Ingest-review metadata (``category``, ``page_type``,
    ``durability_rationale``, ``compound_revision``) travels alongside the
    publishable ``markdown``. A categorized Knowledge Base routes each page
    to its Content Category path (``relative_path``); ``page_type`` is the
    page's free-form type semantics (distinct from the broad Content
    Category), and ``durability_rationale`` records why the page is durable
    knowledge so a reviewing Maintainer can decide per page (issue #78).

    ``compound_revision`` (issue #79) signals that this proposed page
    compounds an EXISTING Compiled Page: its Canonical Title matches a page
    already in the Knowledge Base, and publication must PRESERVE +
    DEDUPLICATE the existing page's prior Sources while ADDING the proposed
    page's newly informing Sources. It never silently replaces the evidence
    trail. A plain title collision without this flag keeps the pre-#79
    replace semantics, so additive provenance is always explicit.

    ``move_from_path`` (issue #80) signals an EXPLICIT Content Category / path
    move of an EXISTING Compiled Page: the page already lives at
    ``move_from_path`` and publication relocates it ATOMICALLY to
    ``relative_path`` (its new category path) while preserving its Canonical
    Page Title, aliases, Sources, and typed Relationships. A title match alone
    never sets this flag, so a move is always an explicit, reviewable
    operation — never an incidental side effect of a name collision.

    ``rename_from`` (issue #140) signals a reviewed title rename of an
    EXISTING Compiled Page: the page's Canonical Title changes from
    ``rename_from`` (the old title) to ``title`` (the new title), and the
    same atomic proposal repairs every canonical Relationship and every
    exactly-resolved body link that targeted the old title. The page's path,
    Sources, aliases, and typed Relationships are preserved; only the identity
    changes. A title match alone never sets this flag, so a rename is always
    an explicit, reviewable operation.
    """

    relative_path: str
    title: str
    markdown: str
    category: str | None = None
    page_type: str = ""
    durability_rationale: str = ""
    compound_revision: bool = False
    move_from_path: str | None = None
    rename_from: str | None = None


class BlastRadius(msgspec.Struct, frozen=True):
    """Semantic blast-radius review of an Ingest Proposal.

    Captures identity risks, relationship changes, visibility transitions,
    source provenance changes, and graph impact against the existing Knowledge
    Base. Duplicate candidates are reported for human review; no automatic
    merging is performed. ``category_moves`` (issue #80) surfaces explicit
    Content Category / path relocations for review. ``renames`` (issue #140)
    surfaces explicit title renames for review.
    """

    new_titles: list[str] = msgspec.field(default_factory=list)
    changed_titles: list[str] = msgspec.field(default_factory=list)
    duplicate_title_risks: list[str] = msgspec.field(default_factory=list)
    duplicate_alias_risks: list[str] = msgspec.field(default_factory=list)
    relationship_changes: list[str] = msgspec.field(default_factory=list)
    unresolved_targets: list[str] = msgspec.field(default_factory=list)
    visibility_changes: list[str] = msgspec.field(default_factory=list)
    source_changes: list[str] = msgspec.field(default_factory=list)
    affected_backlinks: list[str] = msgspec.field(default_factory=list)
    category_moves: list[str] = msgspec.field(default_factory=list)
    renames: list[str] = msgspec.field(default_factory=list)


class SourceChangeImpact(msgspec.Struct, frozen=True):
    """A page-level support result for a staged source lifecycle action."""

    page_title: str
    status: str


class SourceLifecycleChange(msgspec.Struct, frozen=True):
    """Private source action disclosed on an otherwise ordinary proposal."""

    action: str
    source_id: str
    trigger: str
    impacts: list[SourceChangeImpact] = msgspec.field(default_factory=list)


class PageRemoval(msgspec.Struct, frozen=True):
    """An explicit, reviewed removal of one Compiled Page (issue #135).

    A Page Removal is an explicit Ingest Proposal mutation that excludes a
    Compiled Page from the next Published Version and repairs every canonical
    Relationship that would otherwise become invalid in the SAME proposal. It
    is never inferred from an omitted page (ADR-0014): a page that simply does
    not appear in a proposal's proposed output is untouched.

    Per ADR-0014 the claim-level lineage design is deferred (issue #137), so
    ``lost_support_reason`` is recorded at the PAGE level — the page-level
    support classification (``sole-source-lost``) produced by a source
    lifecycle change, or a Maintainer-supplied rationale for a direct removal.
    ``affected_claim_notes`` carries optional, Maintainer-authored page-level
    notes about which knowledge on the page lost support; it never embeds raw
    source excerpts, source bytes, or Claim Lineage (which is not modeled).
    """

    title: str
    lost_support_reason: str = ""
    affected_claim_notes: list[str] = msgspec.field(default_factory=list)


class BodyLinkRepairCandidate(msgspec.Struct, frozen=True):
    """A location-bearing body-link repair candidate for a Page Removal (#135).

    One internal Markdown/wikilink in another Compiled Page's body that
    targeted the removed page. It is an explicit, location-bearing repair
    CANDIDATE or diagnostic: it is never silently redirected to a guessed page
    (ADR-0014, ADR-0016). The Maintainer decides whether to drop the link,
    re-point it, or leave it (a remaining link surfaces post-removal as a
    non-blocking broken-internal-link warning). ``origin`` is the resolver's
    link origin (``markdown-link`` or ``wikilink``); ``line_start``/``line_end``
    are 1-based and map to the source file.
    """

    source_title: str
    source_path: str
    target_title: str
    origin: str = "markdown-link"
    line_start: int = 0
    line_end: int = 0


#: Fixed, content-free label rendered for a source lifecycle trigger whose
#: action is NOT one of the controlled ``retire`` / ``reactivate`` values (#133).
#:
#: A proposal persisted *before* action-controlled triggers — or a future
#: proposal type — may carry a ``trigger`` string containing a content hash or
#: secret-bearing detail. Rendering must NEVER echo the persisted trigger:
#: the label is derived ONLY from the controlled ``action`` field. Any action
#: outside the known vocabulary renders this single generic label. This guard
#: is display-only: it neither rejects nor destroys the persisted proposal
#: trigger, which stays available for operational semantics.
SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED = "source lifecycle change"


def safe_lifecycle_trigger_display(action: str) -> str:
    """Return a display-safe, content-free label for a source lifecycle trigger.

    The label is derived ONLY from the controlled ``action`` field of a
    :class:`SourceLifecycleChange` — never from its persisted ``trigger``
    string, which may carry a content hash or secret-bearing detail on a legacy
    or future proposal. ``retire`` and ``reactivate`` map to two fixed
    descriptive labels; every other value (including a future action that
    itself carries secret material) maps to the single fixed
    :data:`SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED` label.

    This is the lumio-wiki boundary guard every Workshop rendering surface must
    use for proposal lifecycle triggers; it mirrors
    :func:`safe_candidate_trigger_display` (which masks legacy *candidate*
    triggers) but derives its output from the action rather than the stored
    trigger, because the proposal trigger is private operational state that is
    never rendered. The guard is display-only: it never raises, never mutates,
    and never destroys the persisted proposal trigger.
    """
    if action == "retire":
        return "explicit source retirement"
    if action == "reactivate":
        return "explicit source reactivation"
    return SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED


#: Fixed, content-free label rendered for a source lifecycle impact status that
#: is NOT one of the controlled ``still-supported`` / ``sole-source-lost``
#: values (#133).
#:
#: A proposal persisted *before* the controlled impact-status vocabulary — or a
#: future proposal type — may carry a ``SourceChangeImpact.status`` string
#: containing secret-bearing detail (e.g. a future status that accidentally
#: captured a credential). Rendering must NEVER echo the persisted status: the
#: label is derived ONLY from an allowlist of the two known statuses. Any value
#: outside that vocabulary renders this single generic label. This guard is
#: display-only: it neither rejects nor destroys the persisted impact status,
#: which stays available for operational semantics.
SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED = "source-impact-unknown"


def safe_lifecycle_impact_status_display(status: str) -> str:
    """Return a display-safe, content-free label for a source lifecycle impact status.

    The label is derived ONLY from an allowlist of the two controlled impact
    statuses on a :class:`SourceChangeImpact` — ``still-supported`` and
    ``sole-source-lost`` — never from an arbitrary persisted ``status`` string,
    which may carry a content hash or secret-bearing detail on a legacy or
    future proposal. Both allowlisted statuses render as their exact fixed
    labels; every other value (including a future status that itself carries
    secret material) maps to the single fixed
    :data:`SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED` label.

    This is the lumio-wiki boundary guard every Workshop rendering surface must
    use for proposal lifecycle impact statuses; it mirrors
    :func:`safe_lifecycle_trigger_display` (which derives a label from the
    controlled action) but allowlists the status directly, because the impact
    status is the value that is rendered for the reviewer. The guard is
    display-only: it never raises, never mutates, and never destroys the
    persisted impact status.
    """
    if status == "still-supported":
        return "still-supported"
    if status == "sole-source-lost":
        return "sole-source-lost"
    return SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED


class IngestProposal(msgspec.Struct, frozen=True):
    """A staged set of proposed Markdown changes with validation gate.

    ``control_file`` (issue #81) carries a proposed Knowledge Base Control
    File to establish alongside the page changes — used by a Legacy Flat Mode
    migration, which establishes the Control File and moves every page into its
    Content Category path as ONE atomic, reviewable proposal. It is ``None``
    for ordinary ingest/move proposals, which never touch the Control File.
    """

    id: str
    status: str
    created_at: str
    provenance: SourceProvenance
    proposed_pages: list[ProposedPage]
    affected_pages: list[str]
    diff: str
    validation_report: ValidationReport
    blocked: bool
    raw_source_path: str | None = None
    blast_radius: BlastRadius | None = None
    control_file: KnowledgeBaseControlFile | None = None
    okf_diagnostics: list[OkfImportDiagnostic] = msgspec.field(default_factory=list)
    source_change: SourceLifecycleChange | None = None
    # issue #135: explicit, reviewed Page Removals and their location-bearing
    # body-link repair candidates. A removal is never inferred from an omitted
    # page (ADR-0014): it is a declared mutation persisted, inspected,
    # validated, published, and discarded through this same pipeline.
    removed_pages: list[PageRemoval] = msgspec.field(default_factory=list)
    body_link_repairs: list[BodyLinkRepairCandidate] = msgspec.field(default_factory=list)


class ExternalImportCategoryMapping(msgspec.Struct, frozen=True):
    """Category-mapping result for an external-vault import (issue #87, ADR-0009).

    Carries the proposed pages routed by Content Category, a proposed Control
    File extension that adds any unmapped external categories the Maintainer
    has NOT declined, and per-category disclosure diagnostics. Shared/seeded
    categories pass through unchanged; unmapped categories become a reviewed
    Control File extension proposal rather than an automatic category or a
    silent drop (ADR-0009). Declined categories drop their content with a
    lossy-with-disclosure note (ADR-0007).
    """

    proposed_pages: list[ProposedPage] = msgspec.field(default_factory=list)
    extension_control_file: KnowledgeBaseControlFile | None = None
    diagnostics: list[OkfImportDiagnostic] = msgspec.field(default_factory=list)


TERMINAL_PROPOSAL_STATUSES = frozenset({"discarded", "published"})


def is_reviewable_proposal(proposal: IngestProposal) -> bool:
    """Return whether a proposal may still receive a maintainer decision."""
    return proposal.status not in TERMINAL_PROPOSAL_STATUSES


def _provenance_for(normalized, filename, content_type):
    """Build SourceProvenance from a NormalizedSource (shared by the entry points)."""
    return SourceProvenance(
        original_filename=filename,
        content_type=content_type,
        converted_by=normalized.converted_by,
        source_hash=normalized.source_hash,
    )


def _ensure_page_frontmatter(text: str, filename: str | None) -> str:
    """Wrap extracted document text in minimal page frontmatter.

    Document converters (LiteParse, MarkItDown) produce extracted text, not
    authored page Markdown. The Proposal Pipeline expects frontmatter; this
    helper derives a title from the filename and wraps the text so it flows
    through the same pipeline as text/Markdown sources. The host agent
    refines the page during review (issue #100, AC3).
    """
    if text.startswith("---"):
        return text
    stem = Path(filename or "Extracted Source").stem
    title = stem.replace("_", " ").replace("-", " ").strip().title() or "Extracted Source"
    return f'---\ntitle: "{title}"\n---\n\n{text}'


def select_source_processor(filename: str | None, content_type: str | None) -> SourceProcessor:
    """Route a Knowledge Source to its Source Processor (issue #100).

    Text and Markdown sources use the dependency-free
    :class:`TextMarkdownSourceProcessor`. Document sources (PDF, image, DOCX,
    HTML, and broad document formats) use :class:`PdfSourceProcessor`
    (LiteParse) or :class:`MarkItDownSourceProcessor` (MarkItDown) from the
    ``[documents]`` extra. When the extra is absent, the document processor's
    ``process`` raises :class:`MissingDocumentExtraError` with the exact
    install command.
    """
    from lumio_wiki.source_processor import (
        TextMarkdownSourceProcessor,
        select_document_processor,
    )

    document_processor = select_document_processor(filename, content_type)
    if document_processor is not None:
        return document_processor
    return TextMarkdownSourceProcessor()


def create_proposal_without_provider(
    raw_bytes: bytes,
    content_type: str | None,
    filename: str | None,
    kb,
    *,
    store: IngestStore | None = None,
) -> IngestProposal:
    """Model-free ingestion for text, Markdown, and document Knowledge Sources.

    Routes the source through :func:`select_source_processor` (text/Markdown →
    :class:`TextMarkdownSourceProcessor`; documents → LiteParse/MarkItDown via
    the ``[documents]`` extra) and distills through the
    :class:`PassthroughMarkdownDistiller` so a source reaches a reviewable
    Ingest Proposal without an OpenAI-compatible provider (issue #95, AC4;
    document support added by issue #100). When ``store`` is given, the
    proposal and raw bytes are staged for review (raw bytes isolated from the
    Knowledge Base). The host coding agent authors the Compiled Page Markdown;
    this entry packages it through the same Proposal Pipeline.
    """
    from lumio_wiki.distiller import PassthroughMarkdownDistiller
    from lumio_wiki.proposal_pipeline import ProposalPipeline

    processor = select_source_processor(filename, content_type)
    normalized = processor.process(filename, content_type, raw_bytes)
    provenance = _provenance_for(normalized, filename, content_type)
    distilled = PassthroughMarkdownDistiller().distill(normalized)
    # Document sources produce extracted text, not authored page Markdown.
    # Wrap it in minimal frontmatter so the Proposal Pipeline can process it;
    # the host agent refines the page during review (issue #100, AC3).
    if normalized.converted_by in ("liteparse", "markitdown"):
        distilled = _ensure_page_frontmatter(distilled, filename)
    pipeline = ProposalPipeline(kb, store=store)
    proposal = pipeline.assemble(distilled, provenance, filename)
    if store is not None:
        proposal = pipeline.stage(proposal, raw_bytes=raw_bytes, filename=filename)
    return proposal


def _extract_title(markdown: str) -> str | None:
    """Best-effort title extraction from YAML frontmatter or first heading."""
    if not markdown.startswith("---"):
        for line in markdown.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return None

    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        data = yaml.decode(parts[1])
        if isinstance(data, dict):
            title = data.get("title")
            if title:
                return str(title)
    except Exception:
        pass

    for line in parts[2].splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _relative_path_for(title: str | None, filename: str | None) -> str:
    if title:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in title).lower()
        return f"{safe}.md"
    if filename:
        return str(Path(filename).with_suffix(".md"))
    return "proposed.md"


def _sanitize_path_segment(name: str) -> str:
    """Return a filesystem-safe lowercase path segment for a category or title."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()
    return safe.strip("_")


def _relative_path_for_category(
    title: str | None, category: str | None, filename: str | None
) -> str:
    """Return the publishable relative path for a proposed page.

    A page routed to a configured Content Category lives under that category's
    path (``<category>/<slug>.md``); otherwise it falls back to the
    root-level slug used in Legacy Flat Mode. Callers pass ``category`` only
    when it is configured so an unconfigured category never creates a category path.
    """
    slug = _relative_path_for(title, filename)
    if category:
        safe = _sanitize_path_segment(category)
        if safe:
            return f"{safe}/{slug}"
    return slug


def _generate_summary(body: str) -> str:
    """Return the first sentence of the first non-heading body line, truncated to ~200 chars."""
    text = body.strip()
    first_line = ""
    fallback_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not fallback_line:
            fallback_line = stripped
        # Skip Markdown headings so summaries describe content, not titles.
        if not stripped.startswith("#"):
            first_line = stripped
            break
    if not first_line:
        first_line = fallback_line
    if not first_line:
        return "No summary available."

    # First sentence: stop at ., ?, or ! followed by whitespace or end of line.
    sentence = first_line
    for i, char in enumerate(first_line):
        if char in ".?!" and (i + 1 == len(first_line) or first_line[i + 1].isspace()):
            sentence = first_line[: i + 1]
            break

    max_len = 200
    if len(sentence) > max_len:
        sentence = sentence[: max_len - 3].rstrip() + "..."
    return sentence


def _ensure_summary(markdown: str) -> str:
    """Return Markdown with a non-empty summary in frontmatter.

    If the frontmatter already contains a non-empty summary, the Markdown is
    returned unchanged. Otherwise, a summary is generated from the first
    sentence of the first non-heading body line and injected into the
    frontmatter.
    """
    data, body, _ = _parse_frontmatter(markdown, Path("proposal.md"))
    summary = data.get("summary")
    if summary and str(summary).strip():
        return markdown

    data["summary"] = _generate_summary(body)
    frontmatter = yaml.encode(data).decode("utf-8").strip()
    return f"---\n{frontmatter}\n---\n{body}"


# A distiller-emitted marker that separates multiple proposed Compiled Pages
# distilled from a single Knowledge Source (issue #78). Each segment between
# markers is a complete Markdown document (frontmatter + body).
_PAGE_BREAK_MARKER = "<!-- lumio: page-break -->"


def _split_distilled_pages(markdown: str) -> list[str]:
    """Split a distilled source into one or more page documents.

    Returns one document per segment between page-break markers. Empty
    segments (e.g. a trailing marker) are dropped so they never produce a
    blank page. When no marker is present the whole source is a single page,
    preserving the original one-page behavior.
    """
    documents: list[str] = []
    for segment in markdown.split(_PAGE_BREAK_MARKER):
        text = segment.strip("\n")
        if text.strip():
            documents.append(text)
    return documents or [markdown]


def _extract_scalar(data: dict, key: str) -> str:
    """Return a trimmed non-empty string scalar from frontmatter, or empty."""
    value = data.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _safe_frontmatter(markdown: str) -> dict:
    """Best-effort frontmatter parse that returns ``{}`` on any parse failure."""
    try:
        data, _body, _ = _parse_frontmatter(markdown, Path("proposal.md"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_ingest_routing_fields(markdown: str) -> str:
    """Strip ingest-only routing/review fields from publishable frontmatter.

    ``category`` is Content Category routing (the category path encodes it; the
    Control File declares the catalog), ``durability_rationale`` is
    ingest-review metadata, ``compound_revision`` is a publication
    directive (issue #79), and ``move_from_path`` is an explicit category
    move directive (issue #80): none is source-of-truth page content. The
    page's free-form type (``type``) is retained as page semantics.
    The frontmatter is only rewritten when an ingest-only field is present,
    so sources that never declare them round-trip unchanged.
    """
    try:
        data, body, _ = _parse_frontmatter(markdown, Path("proposal.md"))
    except Exception:
        return markdown
    if not any(
        key in data
        for key in (
            "category",
            "durability_rationale",
            "compound_revision",
            "move_from_path",
        )
    ):
        return markdown
    data.pop("category", None)
    data.pop("durability_rationale", None)
    data.pop("compound_revision", None)
    data.pop("move_from_path", None)
    frontmatter = yaml.encode(data).decode("utf-8").strip()
    return f"---\n{frontmatter}\n---\n{body}"


def _configured_category_names(kb) -> set[str]:
    """Return the set of Content Category names configured by the Control File.

    Returns an empty set in Legacy Flat Mode (no Control File), where no
    category routing is enforced.
    """
    control = getattr(kb, "control", None)
    if control is None:
        return set()
    return {category.name for category in control.categories}


def _extract_page_records(markdown: str, filename: str | None, kb) -> list[ProposedPage]:
    """Slice a single distilled source into one or more proposed Compiled Pages.

    A Knowledge Source may distil into several Compiled Pages. The
    distiller signals page boundaries with the ``<!-- lumio: page-break -->``
    marker; each segment is a complete Markdown document whose frontmatter
    may declare a Content Category (``category``), a free-form type
    (``type``), a ``durability_rationale``, and a ``compound_revision``
    directive. A page is routed under its category path only when the
    category is configured in the Knowledge Base Control File; an
    unconfigured category is reported by the routing validator and never
    creates an ad-hoc category path (issue #78).

    ``compound_revision`` (issue #79) marks a segment that compounds an
    EXISTING Compiled Page by Canonical Title: publication preserves +
    deduplicates the existing page's prior Sources while adding the segment's
    newly informing Sources. ``category``, ``durability_rationale``, and
    ``compound_revision`` are ingest-review/routing metadata stripped from
    the publishable Markdown (the category path encodes the category; ``type`` is
    retained as page semantics). Legacy Flat Mode (no Control File) keeps the
    original single root-level page.
    """
    configured = _configured_category_names(kb)
    pages: list[ProposedPage] = []
    for document in _split_distilled_pages(markdown):
        ensured = _ensure_summary(document)
        data = _safe_frontmatter(ensured)
        declared_category = _extract_scalar(data, "category") or None
        page_type = _extract_scalar(data, "type")
        rationale = _extract_scalar(data, "durability_rationale")
        compound_revision = bool(data.get("compound_revision"))
        # An explicit category move (issue #80): the distiller may emit the
        # existing page's canonical path so publication relocates the page
        # to its declared category path. Blank/absent means no move.
        move_from_path = _extract_scalar(data, "move_from_path") or None
        title = _extract_title(ensured)
        # Route under the category path only when configured; an
        # unconfigured category is surfaced by ``_validate_page_routing`` and
        # must not create the category path.
        routed_category = declared_category if declared_category in configured else None
        relative_path = _relative_path_for_category(title, routed_category, filename)
        if title is None:
            title = relative_path
        # The distiller's ``durability_rationale`` is left as-is: when the
        # distiller omits it, no boilerplate is fabricated. A categorized
        # page without a rationale is blocked by ``_validate_page_routing``
        # so a reviewing Maintainer classifies it (issue #78, P2.6).
        published = _strip_ingest_routing_fields(ensured)
        pages.append(
            ProposedPage(
                relative_path=relative_path,
                title=title,
                markdown=published,
                category=declared_category,
                page_type=page_type,
                durability_rationale=rationale,
                compound_revision=compound_revision,
                move_from_path=move_from_path,
            )
        )
    return pages


def _synthesis_cross_source_issues(page: ProposedPage) -> list[ValidationIssue]:
    """Block a synthesis-category page that is not cross-source/cross-page.

    A synthesis page must draw from multiple sources or relate to other
    Compiled Pages; a single-source standalone page does not belong in the
    synthesis category. The ``synthetic`` flag stays governed by derivation
    provenance, so this never inspects or mutates it (issue #78).
    """
    data = _safe_frontmatter(page.markdown)
    sources = _as_sources(data.get("sources"))
    relationships = _as_relationships(data.get("relationships"))
    if len(sources) >= 2 or relationships:
        return []
    return [
        ValidationIssue(
            file=page.relative_path,
            field="synthesis",
            message=(
                "synthesis page must be a cross-source or cross-page conclusion; "
                "cite multiple sources or relate to other Compiled Pages"
            ),
        )
    ]


def _validate_page_routing(
    proposed_pages: Iterable[ProposedPage],
    kb,
    *,
    extra_categories: Iterable[str] = (),
) -> list[ValidationIssue]:
    """Return ingest-level Content Category routing issues for a proposal.

    File catalog is the authority on configured Content Categories, so a page
    routed to an unconfigured category (or missing its category in a
    categorized Knowledge Base) blocks the proposal rather than creating an
    ad-hoc category path. Each categorized page must also carry a non-empty
    free-form type, a non-empty durability rationale (the distiller never
    fabricates one), and a synthesis-category page must be cross-source
    or cross-page. Legacy Flat Mode (no Control File) enforces none of this.

    ``extra_categories`` widens the effective catalog with categories a
    proposal carries as a Control File *extension* (issue #87, ADR-0009), so a
    yet-to-be-published extension category does not spuriously block the
    atomic proposal that proposes it. It does not create the categories: they
    exist only when the extension Control File is published.
    """
    if getattr(kb, "control", None) is None:
        return []
    configured = _configured_category_names(kb) | set(extra_categories)
    issues: list[ValidationIssue] = []
    for page in proposed_pages:
        # Explicit category/path moves (issue #80) relocate already-valid
        # content, so they are exempt from routing re-validation here.
        # Explicit title renames (issue #140) change only the page's identity,
        # so they are exempt too: the content was already valid.
        if getattr(page, "move_from_path", None) or getattr(page, "rename_from", None):
            continue
        # Routing rules apply to EVERY page entering a categorized Knowledge
        # Base, including compound revisions and edits of existing pages
        # (P2.6). A Maintainer must be able to classify every proposal by
        # its configured category, free-form type, and durability rationale.
        category = page.category
        if category is None:
            issues.append(
                ValidationIssue(
                    file=page.relative_path,
                    field="category",
                    message=(
                        "categorized Knowledge Base requires every proposed page "
                        "to declare a configured content category"
                    ),
                )
            )
        elif category not in configured:
            issues.append(
                ValidationIssue(
                    file=page.relative_path,
                    field="category",
                    message=(
                        f"content category {category!r} is not configured; propose "
                        f"the category through the Control File before routing pages"
                    ),
                )
            )
        elif not page.page_type.strip():
            issues.append(
                ValidationIssue(
                    file=page.relative_path,
                    field="type",
                    message=("categorized page must declare a non-empty free-form type"),
                )
            )
        elif not page.durability_rationale.strip():
            issues.append(
                ValidationIssue(
                    file=page.relative_path,
                    field="durability_rationale",
                    message=(
                        "categorized page must declare a durability rationale so "
                        "a reviewing Maintainer can classify it; the distiller "
                        "omitted one"
                    ),
                )
            )
        elif category == "synthesis":
            issues.extend(_synthesis_cross_source_issues(page))
    return issues


def import_page_category(relative_path: str) -> str | None:
    """Return the Content Category implied by an imported page's path.

    A categorized Knowledge Base routes a page by the first segment of its
    path (its top-level directory), mirroring the loader's page-category
    derivation. A root-level imported page (no directory component) carries
    no category. See ADR-0009.
    """
    parts = PurePosixPath(relative_path).parts
    if len(parts) <= 1:
        return None
    return parts[0]


def _proposed_page_from_import(page: OkfImportPage, *, category: str | None = None) -> ProposedPage:
    """Build a :class:`ProposedPage` from an imported OKF page, setting its category."""
    return ProposedPage(
        relative_path=page.relative_path,
        title=page.title,
        markdown=page.markdown,
        category=category,
    )


def map_external_import_categories(
    parsed: OkfProfile1Import,
    kb,
    *,
    declined_categories: Iterable[str] = (),
) -> ExternalImportCategoryMapping:
    """Map an external import's categories against the destination catalog.

    For each imported page the category is the first segment of its path
    (ADR-0009). A category already in the destination's configured catalog
    (seeded or declared) passes through UNCHANGED — no rename, no
    relocation. A category with no configured equivalent becomes a Control
    File extension proposal: the page is kept and routed under its category
    path, and the proposed category is added to ``extension_control_file``
    for Maintainer review. No category is auto-created and no page is
    silently dropped (ADR-0009).

    A Maintainer may decline a proposed category via ``declined_categories``:
    the corresponding pages are dropped from the proposal and a
    lossy-with-disclosure note is added (ADR-0007) so the drop is reviewable
    before publish. Declining a category that is already configured has no
    effect: configured categories always pass through.

    Legacy Flat Mode (no Control File) performs no category mapping — every
    page passes through unchanged with no extension proposal, matching the
    loader's non-enforcement of category routing.
    """
    declined = {name for name in declined_categories if name}
    diagnostics: list[OkfImportDiagnostic] = list(parsed.diagnostics)

    control = getattr(kb, "control", None)
    if control is None:
        # Legacy Flat Mode: no catalog to map against; pass everything
        # through unchanged (category routing is not enforced).
        proposed_pages = [_proposed_page_from_import(page) for page in parsed.proposed_pages]
        return ExternalImportCategoryMapping(
            proposed_pages=proposed_pages,
            extension_control_file=None,
            diagnostics=diagnostics,
        )

    configured = _configured_category_names(kb)
    proposed_pages: list[ProposedPage] = []
    extension_names: list[str] = []
    for page in parsed.proposed_pages:
        category = import_page_category(page.relative_path)
        if category is None:
            # Root-level imported page: no category routing. Pass through.
            proposed_pages.append(_proposed_page_from_import(page))
            continue
        if category in configured:
            # Pass-through: a shared/seeded/declared category lands unchanged.
            proposed_pages.append(_proposed_page_from_import(page, category=category))
            continue
        # Unmapped external category.
        if category in declined:
            diagnostics.append(
                OkfImportDiagnostic(
                    path=page.relative_path,
                    kind="category",
                    severity="dropped",
                    message=(
                        f"imported content under category {category!r} dropped: "
                        "the proposed category extension was declined by the "
                        "Maintainer (ADR-0007 lossy-with-disclosure)"
                    ),
                )
            )
            continue
        # Extension proposal: keep the page and propose the category for
        # review. It is neither auto-created nor silently dropped.
        if category not in extension_names:
            extension_names.append(category)
        proposed_pages.append(_proposed_page_from_import(page, category=category))
        diagnostics.append(
            OkfImportDiagnostic(
                path=page.relative_path,
                kind="category",
                severity="mapped",
                message=(
                    f"external category {category!r} has no configured match; "
                    "proposed as a Control File extension for Maintainer review "
                    "(ADR-0009). Not auto-created and not dropped."
                ),
            )
        )

    extension_control_file: KnowledgeBaseControlFile | None = None
    if extension_names:
        extension_control_file = extend_control_file_categories(control, extension_names)
    return ExternalImportCategoryMapping(
        proposed_pages=proposed_pages,
        extension_control_file=extension_control_file,
        diagnostics=diagnostics,
    )


def propose_external_import(
    parsed: OkfProfile1Import,
    kb,
    provenance: SourceProvenance,
    *,
    declined_categories: Iterable[str] = (),
    approve_profile: str | None = None,
) -> IngestProposal:
    """Assemble an external-vault import as a staged Ingest Proposal (issue #87).

    Maps the parsed import's categories against the destination catalog
    (pass-through for shared/seeded/declared categories, a Control File
    extension proposal for unmapped categories, and a lossy-with-disclosure
    drop for declined categories), then runs the ordinary diff / validation
    / blast-radius assembly. The extension Control File travels on
    ``IngestProposal.control_file`` so it is published as ONE reviewed
    proposal alongside the pages — never auto-created (ADR-0009). The
    mapping diagnostics travel on ``okf_diagnostics``.

    Routing validates against the EXTENDED catalog (the live configured
    categories plus the proposed extension) so a yet-to-be-published
    extension category does not spuriously block the atomic proposal. The
    ordinary free-form ``type`` and ``durability_rationale`` review gates
    (issue #78) still apply to every categorized page.
    """
    mapping = map_external_import_categories(parsed, kb, declined_categories=declined_categories)
    proposed_pages = mapping.proposed_pages
    existing_pages = _existing_page_markdown(kb)
    diff = _compute_diff(proposed_pages, existing_pages)
    page_validation = _validate_proposed_pages(proposed_pages)
    # The effective catalog includes the proposed extension so the atomic
    # proposal is not blocked by a category it simultaneously proposes.
    configured = _configured_category_names(kb)
    extension_names = (
        {c.name for c in mapping.extension_control_file.categories} - configured
        if mapping.extension_control_file is not None
        else set()
    )
    routing_issues = _validate_page_routing(proposed_pages, kb, extra_categories=extension_names)
    profile_approved = approve_profile == OKF_PROFILE1_QUERY
    okf_blocking_issues = [
        ValidationIssue(
            file=diag.path,
            field="okf-import",
            severity="error",
            message=diag.message,
        )
        for diag in parsed.diagnostics
        if diag.severity == "blocking" and not (profile_approved and diag.kind == "profile")
    ]
    validation_report = ValidationReport(
        issues=list(page_validation.issues) + routing_issues + okf_blocking_issues
    )
    blast_radius = compute_blast_radius(proposed_pages, kb)
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
        control_file=mapping.extension_control_file,
        okf_diagnostics=mapping.diagnostics,
    )


def _compute_diff(
    proposed_pages: Iterable[ProposedPage],
    existing_pages: dict[str, str],
) -> str:
    """Unified Markdown diff of proposed pages against the current knowledge base."""
    lines: list[str] = []
    for page in proposed_pages:
        existing = existing_pages.get(page.title)
        if existing is not None:
            existing_lines = existing.splitlines(keepends=True)
            proposed_lines = page.markdown.splitlines(keepends=True)
            diff = difflib.unified_diff(
                existing_lines,
                proposed_lines,
                fromfile=f"a/{page.relative_path}",
                tofile=f"b/{page.relative_path}",
            )
            lines.extend(diff)
        else:
            lines.append(f"new file: {page.relative_path}")
            lines.append(f"+++ {page.relative_path}")
            lines.extend(page.markdown.splitlines())
        lines.append("")
    return "\n".join(lines)


def _validate_proposed_pages(proposed_pages: Iterable[ProposedPage]) -> ValidationReport:
    """Run Core SDK validation over the proposed pages in a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for page in proposed_pages:
            path = root / page.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page.markdown, encoding="utf-8")
        return validate(root)


def _existing_page_markdown(kb) -> dict[str, str]:
    """Return a map of canonical title -> full Markdown text for the loaded KB."""
    existing: dict[str, str] = {}
    for page in kb.pages:
        file_path = kb.root / page.path
        try:
            existing[page.title] = file_path.read_text(encoding="utf-8")
        except OSError:
            existing[page.title] = page.body
    return existing


def compute_blast_radius(proposal_pages, kb) -> BlastRadius:
    """Compare proposed pages against the existing Knowledge Base.

    Uses the public registry and lookup APIs on ``kb`` to surface identity
    risks, relationship changes, visibility transitions, source provenance
    changes, and backlinks that would be affected by accepting the proposal.
    Duplicate candidates are reported for review; no merging is performed.
    """

    registry = {entry.title: entry for entry in kb.registry()}
    existing_titles = set(registry.keys())
    existing_aliases: set[str] = set()
    for entry in registry.values():
        existing_aliases.update(entry.aliases)

    existing_by_title: dict[str, CompiledPage] = {}
    for page in kb.pages:
        existing_by_title.setdefault(page.title, page)

    # Build reverse adjacency (backlinks) from the existing KB pages.
    backlinks: dict[str, set[str]] = {}
    for page in kb.pages:
        for rel in page.relationships:
            backlinks.setdefault(rel.target, set()).add(page.title)

    new_titles: list[str] = []
    changed_titles: list[str] = []
    duplicate_title_risks: list[str] = []
    duplicate_alias_risks: list[str] = []
    relationship_changes: list[str] = []
    unresolved_targets: list[str] = []
    visibility_changes: list[str] = []
    source_changes: list[str] = []
    affected_backlinks: set[str] = set()
    category_moves: list[str] = []
    renames: list[str] = []

    proposed_titles: set[str] = set()
    proposed_aliases: set[str] = set()
    for page in proposal_pages:
        proposed_titles.add(page.title)
        for alias in _extract_aliases(page.markdown):
            proposed_aliases.add(alias)

    for page in proposal_pages:
        title = page.title
        try:
            data, _body, _ = _parse_frontmatter(page.markdown, Path(page.relative_path))
        except Exception:
            data = {}

        aliases = _as_string_list(data.get("aliases"))
        visibility = data.get("visibility")
        relationships = _as_relationships(data.get("relationships"))
        sources = _as_sources(data.get("sources"))
        # Identity risks. A compound revision (issue #79) or an explicit
        # category move (issue #80) targets an existing Compiled Page by
        # Canonical Title on purpose: the title match is the point of the
        # proposal, not a duplicate risk, so it is reported only as a change
        # for review. A plain title collision (neither flag) keeps the
        # duplicate-risk signal so a Maintainer notices a clash.
        compound = getattr(page, "compound_revision", False)
        move_from = getattr(page, "move_from_path", None)
        rename_from = getattr(page, "rename_from", None)
        if move_from:
            # An explicit category/path relocation: visible in review, never a
            # duplicate risk. The path change is the whole point of the move.
            category_moves.append(f"{title}: {move_from} -> {page.relative_path}")
        if rename_from:
            # An explicit title rename: visible in review, never a duplicate
            # risk. The identity change is the whole point of the rename.
            renames.append(f"{rename_from} -> {title}")
        if title in existing_titles:
            if not compound and not move_from and not rename_from:
                duplicate_title_risks.append(title)
            existing_page = existing_by_title.get(title)
            if existing_page is not None and not move_from:
                existing_text = _existing_page_text(kb, existing_page)
                if existing_text != page.markdown:
                    changed_titles.append(title)
        else:
            new_titles.append(title)

        for alias in aliases:
            if alias in existing_titles or alias in existing_aliases:
                duplicate_alias_risks.append(alias)

        # Visibility changes.
        if title in existing_by_title:
            existing_visibility = existing_by_title[title].visibility
            if existing_visibility and visibility and existing_visibility != str(visibility):
                visibility_changes.append(f"{title}: {existing_visibility} -> {visibility}")

        # Relationship changes.
        if title in existing_by_title:
            existing_rels = {
                (rel.target, rel.type) for rel in existing_by_title[title].relationships
            }
            proposed_rels = {(rel.target, rel.type) for rel in relationships}
            for target, rel_type in proposed_rels - existing_rels:
                relationship_changes.append(f"{title}: +{rel_type} -> {target}")
            for target, rel_type in existing_rels - proposed_rels:
                relationship_changes.append(f"{title}: -{rel_type} -> {target}")
        else:
            for rel in relationships:
                relationship_changes.append(f"{title}: +{rel.type} -> {rel.target}")

        # Unresolved relationship targets.
        all_known = existing_titles | existing_aliases | proposed_titles | proposed_aliases
        for rel in relationships:
            if rel.target not in all_known:
                unresolved_targets.append(f"{title}: {rel.target}")

        # Source changes.
        if title in existing_by_title:
            existing_sources = {
                (source.id, source.title) for source in existing_by_title[title].sources
            }
            for source in sources:
                if (source.id, source.title) not in existing_sources:
                    source_changes.append(f"{title}: new source {source.id}")
        else:
            for source in sources:
                source_changes.append(f"{title}: new source {source.id}")

        # Affected backlinks.
        if title in existing_titles:
            affected_backlinks.update(backlinks.get(title, set()))
        if rename_from:
            # A rename affects backlinks to the OLD title (rename_from), since
            # those pages referenced the page under its prior identity.
            affected_backlinks.update(backlinks.get(rename_from, set()))

    return BlastRadius(
        new_titles=new_titles,
        changed_titles=changed_titles,
        duplicate_title_risks=duplicate_title_risks,
        duplicate_alias_risks=duplicate_alias_risks,
        relationship_changes=relationship_changes,
        unresolved_targets=unresolved_targets,
        visibility_changes=visibility_changes,
        source_changes=source_changes,
        affected_backlinks=sorted(affected_backlinks),
        category_moves=category_moves,
        renames=renames,
    )


def _extract_aliases(markdown: str) -> list[str]:
    """Return aliases declared in frontmatter, if any."""
    try:
        data, _body, _ = _parse_frontmatter(markdown, Path("proposal.md"))
    except Exception:
        return []
    return _as_string_list(data.get("aliases"))


def _existing_page_text(kb, existing: CompiledPage) -> str | None:
    """Return full Markdown text for an existing CompiledPage, if available."""
    file_path = kb.root / existing.path
    try:
        return file_path.read_text(encoding="utf-8")
    except OSError:
        return existing.body


class IngestStore:
    """Filesystem-backed store for raw sources and staged proposals.

    Raw bytes are kept in a separate tree from the compiled Knowledge Base so
    they never appear in a public or published export.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.raw_dir = self.root / "raw"
        self.proposals_dir = self.root / "proposals"
        self.source_registry = SourceRegistry(self.root / "source-registry")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, IngestProposal] = {}

    def _proposal_path(self, proposal_id: str) -> Path:
        return self.proposals_dir / f"{proposal_id}.json"

    def save_raw(self, proposal_id: str, raw_bytes: bytes, filename: str) -> Path:
        dir_path = self.raw_dir / proposal_id
        dir_path.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name
        path = dir_path / safe_name
        path.write_bytes(raw_bytes)
        return path

    def save_proposal(self, proposal: IngestProposal, raw_path: Path | None = None) -> None:
        proposal_with_path = (
            msgspec.structs.replace(proposal, raw_source_path=str(raw_path))
            if raw_path is not None
            else proposal
        )
        path = self._proposal_path(proposal.id)
        path.write_bytes(msgspec.json.encode(proposal_with_path))
        self._cache[proposal.id] = proposal_with_path

    def get(self, proposal_id: str) -> IngestProposal | None:
        if proposal_id in self._cache:
            return self._cache[proposal_id]
        path = self._proposal_path(proposal_id)
        if not path.exists():
            return None
        proposal = msgspec.json.decode(path.read_bytes(), type=IngestProposal)
        self._cache[proposal_id] = proposal
        return proposal

    def list(self) -> list[IngestProposal]:
        proposals: list[IngestProposal] = []
        for path in sorted(self.proposals_dir.glob("*.json")):
            proposal = self.get(path.stem)
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    def _update_status(self, proposal: IngestProposal, status: str) -> IngestProposal:
        updated = msgspec.structs.replace(proposal, status=status)
        self._proposal_path(proposal.id).write_bytes(msgspec.json.encode(updated))
        self._cache[proposal.id] = updated
        return updated

    def publish(self, proposal_id: str) -> IngestProposal | None:
        """Mark a successfully published proposal as terminal."""
        proposal = self.get(proposal_id)
        if proposal is None or not is_reviewable_proposal(proposal):
            return proposal
        return self._update_status(proposal, "published")

    def discard(self, proposal_id: str) -> IngestProposal | None:
        """Mark a reviewable proposal as discarded."""
        proposal = self.get(proposal_id)
        if proposal is None or not is_reviewable_proposal(proposal):
            return proposal
        return self._update_status(proposal, "discarded")


__all__ = [
    "BlastRadius",
    "BodyLinkRepairCandidate",
    "ExternalImportCategoryMapping",
    "IngestProposal",
    "IngestStore",
    "PageRemoval",
    "ProposedPage",
    "SOURCE_LIFECYCLE_TRIGGER_UNRECOGNIZED",
    "SOURCE_LIFECYCLE_IMPACT_STATUS_UNRECOGNIZED",
    "SourceChangeImpact",
    "SourceLifecycleChange",
    "SourceProvenance",
    "TERMINAL_PROPOSAL_STATUSES",
    "compute_blast_radius",
    "create_proposal_without_provider",
    "import_page_category",
    "is_reviewable_proposal",
    "map_external_import_categories",
    "propose_external_import",
    "safe_lifecycle_trigger_display",
    "safe_lifecycle_impact_status_display",
]
