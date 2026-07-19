"""OKF Exchange Profile 1: the public Knowledge Base export boundary.

Lumio OKF Exchange Profile 1 maps Lumio's canonical Compiled Page model onto the
Google Open Knowledge Format (OKF) v0.1 exchange unit, pinned to a single
upstream commit. It is an optional, best-effort interchange profile: it never
claims unqualified OKF compliance and never redefines Lumio's canonical domain
model. The Markdown body is preserved unchanged, OKF ``resource`` and
``timestamp`` are omitted rather than invented, and Lumio-owned semantics travel
in a versioned ``lumio`` extension. See ADR-0007 and
``docs/research/okf-comparison.md``.

Authorization boundary: :func:`export_okf_profile1` is the serializer. It
receives an **already-authorized** page sequence from the Core SDK authorization
layer (for the public MVP scope, :meth:`KnowledgeBase.public_pages`) and cannot
widen it — excluded pages are never supplied to or inspectable by the serializer.
It is pure (no filesystem I/O) and deterministic: identical authorized pages
always produce byte-identical output.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import msgspec

from lumio_wiki.knowledge_base import (
    ACTIVITY_LOG_BASENAME,
    HOT_INDEX_BASENAME,
    NAV_INDEX_ARTIFACT,
    NAV_INDEX_BASENAME,
    NAV_INDEX_VERSION,
    RESERVED_ARTIFACT_MARKERS,
    RESERVED_ARTIFACT_VERSIONS,
    FrontmatterError,
    _as_relationships,
    _as_sources,
    _as_string_list,
    _dir_index_body,
    _index_structure,
    _page_dir,
    _parse_frontmatter,
    _root_index_body,
)
from lumio_wiki.records import Relationship, Source

if TYPE_CHECKING:
    from lumio_wiki.records import CompiledPage

# Profile 1 identification, pinned to OKF v0.1 at upstream commit
# ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a (2026-06-12). Profile 1 behavior never
# changes merely because upstream ``main`` moves; an incompatible upstream change
# requires a deliberate new Lumio profile.
OKF_PROFILE_NAME = "Lumio OKF Exchange Profile 1"
OKF_PROFILE_VERSION = 1
OKF_VERSION = "v0.1"
OKF_V0_1_PIN = "ee67a5ca27044ebe7c38385f5b6cffc2305a9c1a"

# Fixed OKF ``type`` values synthesized at the exchange boundary. They describe
# the exchange document and never expand canonical Compiled Page metadata.
OKF_TYPE_COMPILED_PAGE = "Lumio Compiled Page"
OKF_TYPE_NAVIGATION_INDEX = "Lumio Navigation Index"

# The query value clients use to select Profile 1 on the export endpoint. Any
# other (or omitted) value preserves the current public Markdown export.
OKF_PROFILE1_QUERY = "okf-1"


class OkfExcludedRelationship(msgspec.Struct, frozen=True):
    """A typed relationship removed from export because its target was filtered.

    Relationships to targets outside the authorized set are dropped so an
    excluded page's title cannot travel through a kept page's structured edges.
    This diagnostic records the source page, the edge type, and a count of
    excluded edges — never the excluded target's title or content.
    """

    source_path: str
    type: str
    count: int


class OkfBrokenBodyLink(msgspec.Struct, frozen=True):
    """A Markdown body link on one source page whose target is absent from the bundle.

    Body links are emitted unchanged; this diagnostic reports links that break
    because their target page was filtered out (or is otherwise absent). It
    records the source page and a count of broken links with a non-leaking
    description — never the excluded target's path or title.
    """

    source_path: str
    count: int
    description: str = "broken Markdown body link to a target absent from the bundle"


class OkfProfile1Export(msgspec.Struct, frozen=True):
    """The logical OKF Exchange Profile 1 directory-tree export.

    ``files`` maps each exported relative path to its deterministic Markdown
    content. The mapping is the logical artifact; any client transport wraps it
    without altering paths or bytes. Compiled Pages keep their relative paths;
    regenerated root and directory Navigation Indexes occupy the reserved
    ``index.md`` paths. ``excluded_relationships`` records structured edges
    removed because their targets were outside the authorized set, and
    ``broken_body_links`` records body Markdown links whose targets are absent
    from the bundle.
    """

    profile: str
    profile_version: int
    okf_version: str
    okf_pin: str
    files: dict[str, str] = msgspec.field(default_factory=dict)
    public_page_count: int = 0
    excluded_relationships: list[OkfExcludedRelationship] = msgspec.field(
        default_factory=list
    )
    broken_body_links: list[OkfBrokenBodyLink] = msgspec.field(default_factory=list)


# YAML double-quoted escape sequences for the backslash, quote, and ASCII control
# characters. Every control character must be escaped so a title or tag that
# decodes to one (for example a YAML-encoded ``\b`` or ``\0``) re-serializes to
# valid YAML rather than emitting a raw, unpresentable byte.
_YAML_NAMED_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    '"': '\\"',
    "\x00": "\\0",
    "\x07": "\\a",
    "\x08": "\\b",
    "\x09": "\\t",
    "\x0a": "\\n",
    "\x0b": "\\v",
    "\x0c": "\\f",
    "\x0d": "\\r",
    "\x1b": "\\e",
}


def _yaml_escape_control(ch: str) -> str:
    """Escape an unnamed control character as a hex YAML escape."""
    code = ord(ch)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def _yaml_scalar(value: str) -> str:
    """Render a deterministic, always-double-quoted YAML scalar.

    Every value is double-quoted so export output stays stable regardless of
    content (colons, hashes, leading dashes). All control characters (ASCII
    controls, DEL, and C1 controls) are escaped with their YAML escape sequence,
    so a title or tag carrying a control character (e.g. a YAML-decoded ``\\b``
    or ``\\0``) re-serializes to valid YAML instead of an unpresentable raw byte.
    """
    out: list[str] = ['"']
    for ch in str(value):
        named = _YAML_NAMED_ESCAPES.get(ch)
        if named is not None:
            out.append(named)
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(_yaml_escape_control(ch))
            continue
        out.append(ch)
    out.append('"')
    return "".join(out)


def _lumio_profile_lines(indent: str) -> list[str]:
    """Return the versioned Profile 1 identification lines beneath ``lumio:``."""
    return [
        f"{indent}profile: {_yaml_scalar(OKF_PROFILE_NAME)}",
        f"{indent}profile_version: {OKF_PROFILE_VERSION}",
        f"{indent}okf_version: {_yaml_scalar(OKF_VERSION)}",
        f"{indent}okf_pin: {_yaml_scalar(OKF_V0_1_PIN)}",
    ]


def _render_compiled_page(
    page: CompiledPage,
    authorized_titles: set[str],
) -> str:
    """Render one authorized Compiled Page as an OKF Profile 1 document.

    Standard OKF fields carry ``type``, ``title``, ``description`` (from
    summary), and ``tags``. OKF ``resource`` and ``timestamp`` are omitted. The
    Markdown body is appended verbatim — no Schema, Examples, Citations, Related,
    or log sections are synthesized. Typed relationships to targets outside the
    authorized set are dropped (the caller reports them as diagnostics).
    """
    lines: list[str] = ["---"]
    lines.append(f"type: {_yaml_scalar(OKF_TYPE_COMPILED_PAGE)}")
    lines.append(f"title: {_yaml_scalar(page.title)}")
    if page.summary is not None:
        lines.append(f"description: {_yaml_scalar(page.summary)}")
    if page.tags:
        lines.append("tags:")
        for tag in page.tags:
            lines.append(f"  - {_yaml_scalar(tag)}")

    lines.append("lumio:")
    lines.extend(_lumio_profile_lines("  "))
    if page.aliases:
        lines.append("  aliases:")
        for alias in page.aliases:
            lines.append(f"    - {_yaml_scalar(alias)}")
    if page.lifecycle:
        lines.append(f"  lifecycle: {_yaml_scalar(page.lifecycle)}")
    if page.visibility:
        lines.append(f"  visibility: {_yaml_scalar(page.visibility)}")
    lines.append(f"  synthetic: {'true' if page.synthetic else 'false'}")
    if page.sources:
        lines.append("  sources:")
        for source in page.sources:
            lines.append(f"    - id: {_yaml_scalar(source.id)}")
            lines.append(f"      title: {_yaml_scalar(source.title)}")
            if source.url is not None:
                lines.append(f"      url: {_yaml_scalar(source.url)}")
    # Keep only edges whose targets survive in the authorized set; edges to
    # excluded targets are dropped and reported as diagnostics by the caller.
    included_relationships = [
        rel for rel in page.relationships if rel.target in authorized_titles
    ]
    if included_relationships:
        lines.append("  relationships:")
        for rel in included_relationships:
            lines.append(f"    - target: {_yaml_scalar(rel.target)}")
            lines.append(f"      type: {_yaml_scalar(rel.type)}")
    lines.append("---")
    header = "\n".join(lines)
    # ``page.body`` is the unchanged body (it begins with the newline(s) that
    # followed the source closing frontmatter delimiter), so appending it
    # verbatim preserves the Markdown exactly.
    return header + page.body


def _okf_nav_frontmatter() -> str:
    """Return the OKF Profile 1 Navigation Index frontmatter.

    Carries the OKF ``type`` plus the native ``lumio`` navigation-index marker
    (so a Lumio-aware loader still recognizes and excludes it) and the Profile 1
    identification.
    """
    lines: list[str] = ["---"]
    lines.append(f"type: {_yaml_scalar(OKF_TYPE_NAVIGATION_INDEX)}")
    lines.append("lumio:")
    lines.append(f"  artifact: {_yaml_scalar(NAV_INDEX_ARTIFACT)}")
    lines.append(f"  version: {NAV_INDEX_VERSION}")
    lines.extend(_lumio_profile_lines("  "))
    lines.append("---")
    return "\n".join(lines)


def _render_nav_indexes(pages: list[CompiledPage]) -> dict[str, str]:
    """Regenerate every Navigation Index from the authorized page set only.

    Reuses the shared index-body renderers so OKF indexes are byte-identical to
    native indexes apart from their OKF frontmatter, and so excluded titles and
    summaries can never leak into a regenerated index.
    """
    pages_by_dir, index_dirs = _index_structure(pages)
    files: dict[str, str] = {}
    for dir_path in sorted(index_dirs):
        relative = f"{dir_path}/index.md" if dir_path else NAV_INDEX_BASENAME
        if dir_path == "":
            body = _root_index_body(pages_by_dir)
        else:
            body = _dir_index_body(dir_path, pages_by_dir, index_dirs)
        files[relative] = _okf_nav_frontmatter() + "\n\n" + body + "\n"
    return files


# Markdown link forms parsed for broken-link diagnostics: inline
# ``[label](dest)`` / ``[label](<dest>)``, and reference-style
# ``[label][ref]`` / ``[label][]`` / ``[label]`` resolved against
# ``[ref]: dest`` definitions. Images (``![alt](src)``) are excluded. Consumed
# matches are blanked with equal-length runs so later passes keep valid offsets.
_INLINE_LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)]*)\)")
_REFERENCE_LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\[\s*([^\]]*)\s*\]")
_SHORTCUT_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\](?![\(\[])")
_LINK_DEFINITION_RE = re.compile(
    r"^ {0,3}\[([^\]]+)\]:\s*(?:<([^>]*)>|(\S+))(?:\s+.*)?$"
)


def _iter_markdown_link_targets(text: str):
    """Yield the raw destination of every Markdown link in ``text``.

    Handles inline ``[label](dest)`` and ``[label](<dest>)`` links plus
    reference-style ``[label][ref]`` / ``[label][]`` / ``[label]`` links, the
    latter resolved against ``[ref]: dest`` definitions found on their own
    lines. An optional title after the destination is dropped; angle brackets
    around a destination are stripped. Images are excluded. Reference labels
    are matched exactly (no case-folding): Lumio titles are case-sensitive and
    the diagnostic is best-effort.
    """
    definitions: dict[str, str] = {}
    scan_lines: list[str] = []
    for line in text.splitlines():
        match = _LINK_DEFINITION_RE.match(line)
        if match:
            label = match.group(1).strip()
            destination = (
                match.group(2) if match.group(2) is not None else (match.group(3) or "")
            )
            definitions[label] = destination.strip()
        else:
            scan_lines.append(line)
    body = "\n".join(scan_lines)

    def _blank(match: re.Match) -> None:
        nonlocal body
        start, end = match.span()
        body = body[:start] + " " * (end - start) + body[end:]

    # Inline links, including angle-bracket destinations.
    for match in list(_INLINE_LINK_RE.finditer(body)):
        inner = match.group(2).strip()
        _blank(match)
        if not inner:
            continue
        destination = inner.split(None, 1)[0]
        if len(destination) >= 2 and destination.startswith("<") and destination.endswith(">"):
            destination = destination[1:-1]
        if destination:
            yield destination

    # Full and collapsed reference links: [label][ref] and [label][].
    for match in list(_REFERENCE_LINK_RE.finditer(body)):
        label, ref = match.group(1), match.group(2)
        _blank(match)
        key = (ref or label).strip()
        if key in definitions:
            yield definitions[key]

    # Shortcut reference links: [label] with a matching definition.
    for match in _SHORTCUT_LINK_RE.finditer(body):
        key = match.group(1).strip()
        if key in definitions:
            yield definitions[key]


def _resolve_internal_link(target: str, source_dir: str) -> str | None:
    """Resolve a Markdown link destination to a normalized posix path, or None.

    Returns the normalized path for internal references; ``None`` for external
    URLs (whose first segment carries a scheme such as ``https:``), ``mailto:``
    links, anchor-only links, or empty destinations. A leading slash marks a
    root-relative reference resolved from the bundle root, not the source page's
    directory.
    """
    # Strip fragment and query before deciding whether the link is internal.
    for sep in ("#", "?"):
        idx = target.find(sep)
        if idx != -1:
            target = target[:idx]
    target = target.strip()
    if not target:
        return None
    if ":" in target.split("/", 1)[0]:
        return None
    # A leading slash is root-relative: resolve from the bundle root, not the
    # source page's directory.
    if target.startswith("/"):
        target = target[1:]
        base_parts: tuple[str, ...] = ()
    else:
        base_parts = PurePosixPath(source_dir).parts
    parts: list[str] = []
    for part in (*base_parts, *PurePosixPath(target).parts):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _bundle_paths(pages: list[CompiledPage]) -> set[str]:
    """Return every ``.md`` path that will exist in the exported bundle.

    Combines the authorized page paths with the regenerated Navigation Index
    paths, so a body link to a generated index is not misreported as broken.
    Derived solely from the authorized page set.
    """
    page_paths = {page.path for page in pages}
    _, index_dirs = _index_structure(pages)
    index_paths = {
        (f"{dir_path}/index.md" if dir_path else NAV_INDEX_BASENAME)
        for dir_path in index_dirs
    }
    return page_paths | index_paths


class ExportVisibilityScope(StrEnum):
    """The authorized visibility scope for an OKF Profile 1 export (issue #68).

    The scope selects exactly which Compiled Page visibility classes the
    serializer receives. It is decided by the Core SDK authorization layer
    (clamped to the caller's privilege), never by the serializer, so a caller
    cannot widen its authorized scope by changing export parameters. Raw
    Knowledge Sources live under the ingest path and are never Compiled Pages,
    so they remain excluded regardless of the requested scope.

    ``PUBLIC`` is the portable exchange boundary (public pages only). ``ALL``
    is the explicitly privileged scope that selects every visibility class;
    it is honored only when the authorization layer permits it.
    """

    PUBLIC = "public"
    ALL = "all"


# The visibility classes each scope is authorized to export. Ordering is by
# privilege set: ALL is a strict superset of PUBLIC.
_EXPORT_VISIBILITIES: dict[ExportVisibilityScope, frozenset[str]] = {
    ExportVisibilityScope.PUBLIC: frozenset({"public"}),
    ExportVisibilityScope.ALL: frozenset({"public", "internal", "restricted"}),
}


def select_export_pages(
    pages: Sequence[CompiledPage],
    scope: ExportVisibilityScope,
) -> list[CompiledPage]:
    """Select exactly the Compiled Pages authorized for ``scope`` (issue #68).

    This is the Core SDK authorization selection for OKF Profile 1 export. The
    serializer (:func:`export_okf_profile1`) receives only this already-
    authorized set and cannot widen it: excluded pages are never supplied to or
    inspectable by the serializer, so their titles and summaries cannot leak
    into the exported files, regenerated Navigation Indexes, lumio extension
    data, or generated metadata.

    Page order is preserved as supplied; the serializer sorts before rendering.
    """
    allowed = _EXPORT_VISIBILITIES[scope]
    return [page for page in pages if (page.visibility or "") in allowed]


def export_okf_profile1(pages: Sequence[CompiledPage]) -> OkfProfile1Export:
    """Serialize an already-authorized page set as an OKF Exchange Profile 1 export.

    The caller (Core SDK authorization layer) selects the authorized pages — for
    the public MVP scope, :meth:`KnowledgeBase.public_pages`. The serializer
    trusts that set and cannot widen it: it never receives excluded pages, and
    Navigation Indexes are regenerated only from the supplied set. Typed
    relationships and body links to targets outside the authorized set are
    removed/reported without modifying the body. Raw Knowledge Sources live under
    the ingest path and are never Compiled Pages, so they are inherently absent.

    Pure and deterministic: identical authorized pages always yield byte-identical
    output.
    """
    authorized = sorted(pages, key=lambda page: page.path)
    authorized_titles = {page.title for page in authorized}
    known_paths = _bundle_paths(authorized)

    files: dict[str, str] = {}
    excluded_map: dict[tuple[str, str], int] = {}
    broken_map: dict[str, int] = {}
    for page in authorized:
        files[page.path] = _render_compiled_page(page, authorized_titles)
        for rel in page.relationships:
            # Edges to targets outside the authorized set are removed and
            # reported. Empty targets are malformed rather than scoped-out, so
            # they are dropped silently.
            if rel.target and rel.target not in authorized_titles:
                key = (page.path, rel.type)
                excluded_map[key] = excluded_map.get(key, 0) + 1
        # Body links are emitted unchanged, but links that no longer resolve in
        # the exported bundle (typically to a filtered page) are reported. The
        # serializer detects them only against the authorized set it holds.
        source_dir = _page_dir(page.path)
        for raw_target in _iter_markdown_link_targets(page.body):
            resolved = _resolve_internal_link(raw_target, source_dir)
            if resolved is None:
                continue
            if not resolved.lower().endswith(".md"):
                continue
            if resolved in known_paths:
                continue
            broken_map[page.path] = broken_map.get(page.path, 0) + 1

    files.update(_render_nav_indexes(authorized))

    excluded = [
        OkfExcludedRelationship(source_path=src, type=typ, count=count)
        for (src, typ), count in sorted(excluded_map.items())
    ]
    broken = [
        OkfBrokenBodyLink(source_path=src, count=count)
        for src, count in sorted(broken_map.items())
    ]

    return OkfProfile1Export(
        profile=OKF_PROFILE_NAME,
        profile_version=OKF_PROFILE_VERSION,
        okf_version=OKF_VERSION,
        okf_pin=OKF_V0_1_PIN,
        files=dict(sorted(files.items())),
        public_page_count=len(authorized),
        excluded_relationships=excluded,
        broken_body_links=broken,
    )
# ---------------------------------------------------------------------------
# OKF Exchange Profile 1 import (issue #69): the inverse of the export boundary.
#
# A safe generic Profile 1 directory tree is scanned (never trusting an imported
# ``index.md`` as authoritative inventory), classified by reserved basename, and
# mapped onto proposed Compiled Pages. Generic pages (no recognized ``lumio``
# extension) receive safe defaults (draft / internal / non-synthetic) and
# deterministic Source provenance derived from bundle identity and the document's
# relative path; OKF ``resource`` never becomes provenance. Every case-insensitive
# ``index.md`` is navigation input and every ``log.md`` is preview-only exchange
# history — neither becomes a Compiled Page or an audit event. See ADR-0007 and
# ``docs/research/okf-comparison.md``.
# ---------------------------------------------------------------------------


class OkfImportError(ValueError):
    """Raised when an OKF Profile 1 bundle cannot be safely imported.

    Covers a missing bundle path, a non-directory path, or a path the importer
    refuses to walk. It is raised before any proposed page is produced so a
    rejected bundle never reaches the Ingest Proposal workflow.
    """


class OkfImportDiagnostic(msgspec.Struct, frozen=True):
    """One importer diagnostic naming a document path, kind, and message (issue #70).

    ``severity`` is an outcome category a Maintainer can scan at a glance:

    * ``accepted`` — the document was proposed as a Compiled Page.
    * ``mapped`` — a field was mapped onto canonical semantics (e.g.
      ``description`` -> summary, or a title fallback).
    * ``ignored`` — a file was deliberately not proposed (a reserved
      ``index.md`` / ``log.md``, or a skipped non-Markdown file).
    * ``dropped`` — an exchange-only field or unknown producer extension key
      was dropped at canonicalization while the page stayed previewable.
    * ``warning`` — a non-blocking review concern (e.g. a broken body link).
    * ``blocking`` — the outcome blocks canonical publication (e.g. an
      unsupported profile). The ordinary Core SDK validation report remains
      the canonical gate; blocking importer diagnostics are translated into
      validation errors by the Ingest Proposal assembler.

    Diagnostics name paths, kinds, and outcomes only — they never carry page
    body content.
    """

    path: str
    kind: str
    message: str
    severity: str = "info"


class OkfImportPage(msgspec.Struct, frozen=True):
    """One proposed Compiled Page distilled from an imported OKF document.

    ``markdown`` is canonical Lumio frontmatter plus the unchanged OKF body:
    exchange-only fields (``type``, ``resource``, ``timestamp``, and the
    ``lumio`` profile identification) have been mapped or dropped, and safe
    generic defaults fill any absent Lumio semantics. The relative path is the
    document's path within the bundle, preserved so a round trip keeps its
    location.
    """

    relative_path: str
    title: str
    markdown: str


class OkfProfile1Import(msgspec.Struct, frozen=True):
    """The logical OKF Exchange Profile 1 import of a directory tree.

    ``proposed_pages`` are the candidate Compiled Pages; ``diagnostics`` carry
    review-only findings; ``navigation_index_count`` and ``log_count`` record
    reserved-path artifacts treated as navigation input / exchange history
    rather than pages; ``skipped_files`` lists unsupported non-Markdown files;
    ``bundle_identity`` is the deterministic digest used to derive Source
    provenance for generic pages.
    """

    profile: str
    profile_version: int
    okf_version: str
    okf_pin: str
    bundle_identity: str
    proposed_pages: list[OkfImportPage] = msgspec.field(default_factory=list)
    diagnostics: list[OkfImportDiagnostic] = msgspec.field(default_factory=list)
    navigation_index_count: int = 0
    log_count: int = 0
    skipped_files: list[str] = msgspec.field(default_factory=list)


# The standard OKF frontmatter keys that carry canonical Lumio semantics. They
# are mapped onto the proposed page; everything else (``type``, ``resource``,
# ``timestamp``, producer extensions) is exchange-boundary and dropped at
# canonicalization.
_OKF_STANDARD_KEYS = {"title", "description", "tags"}
# The Profile 1 identification block emitted beneath ``lumio:`` by the
# exporter (:func:`_lumio_profile_lines`). These keys are recognized Profile 1
# identification — ``_classify_lumio_extension`` reads ``profile_version`` to
# identify the profile — but they carry no canonical Lumio semantics: they are
# exchange-boundary and absent from the re-imported canonical page. A Lumio
# export re-imported through the recognized extension must NOT diagnose its own
# identification block as unknown producer extensions (issue #71).
_LUMIO_IDENTIFICATION_KEYS = frozenset(
    {"profile", "profile_version", "okf_version", "okf_pin"}
)
# The semantic keys a recognized Profile 1 ``lumio`` extension may carry that
# map onto canonical Compiled Page metadata. Any key beyond these and the
# identification block is an unknown producer extension: previewable, but
# dropped at canonicalization with a diagnostic stating Profile 1 does not
# promise persistent lossless round-tripping (issue #70).
_RECOGNIZED_LUMIO_KEYS = {
    "aliases",
    "lifecycle",
    "visibility",
    "synthetic",
    "sources",
    "relationships",
}


def _okf_bundle_identity(entries: list[tuple[str, bytes]]) -> str:
    """Return a deterministic digest over the safe bundle Markdown tree.

    The digest combines each document's relative path with its content digest,
    in sorted path order, so identical bundles always yield identical identity
    (and therefore identical generic-page Source provenance).
    """
    digest = hashlib.sha256()
    for rel, content in sorted(entries, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _scan_okf_bundle(root: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return ``(markdown_files, skipped)`` under a safe bundle root (issue #70).

    ``markdown_files`` is a sorted list of ``(relative_posix_path, absolute)``
    for every case-insensitive ``.md`` file that resolves inside ``root``.
    ``skipped`` lists non-Markdown files. The scan rejects — by raising
    :class:`OkfImportError` before any page is proposed — any path whose
    resolved location escapes the bundle root (absolute paths, parent
    traversal, symlink escape), any duplicate case-insensitive path (ambiguous
    canonical destination), and any two entries that resolve to the same real
    file (colliding entries). Collision checks apply to EVERY entry, not only
    Markdown files, so non-Markdown collisions are also rejected.
    """
    markdown_files: list[tuple[str, Path]] = []
    skipped: list[str] = []
    seen_folded: dict[str, str] = {}
    seen_resolved: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise OkfImportError(
                f"bundle path escapes the bundle root: {path}"
            ) from exc
        # The relative path reflects the bundle's own directory structure (the
        # entry's own location), not a symlink target — so two distinct entries
        # that alias the same real file keep distinct relative paths and are
        # caught by the colliding-entries check below.
        rel = path.relative_to(root).as_posix()
        folded = rel.lower()
        if folded in seen_folded:
            raise OkfImportError(
                f"duplicate case-insensitive bundle path: {rel} collides "
                f"with {seen_folded[folded]}"
            )
        real = str(resolved)
        if real in seen_resolved:
            raise OkfImportError(
                f"colliding bundle entries resolve to the same file: {rel} "
                f"and {seen_resolved[real]}"
            )
        seen_folded[folded] = rel
        seen_resolved[real] = rel
        if path.suffix.lower() == ".md":
            markdown_files.append((rel, resolved))
        else:
            skipped.append(rel)
    return markdown_files, skipped


def _classify_okf_doc(rel: str) -> str:
    """Classify a Markdown document by its reserved basename.

    Returns ``"navigation-index"`` for any case-insensitive ``index.md``,
    ``"log"`` for any case-insensitive ``log.md`` (root or directory exchange
    history), ``"hot-index"`` for any case-insensitive ``hot.md`` (a reserved
    Hot Index artifact path, which conflicts with the derived-artifact role),
    and ``"page"`` otherwise. Classification is by basename only so reserved
    paths are recognized at any depth (issue #70).
    """
    basename = PurePosixPath(rel).name.lower()
    if basename == NAV_INDEX_BASENAME:
        return "navigation-index"
    if basename == ACTIVITY_LOG_BASENAME:
        return "log"
    if basename == HOT_INDEX_BASENAME:
        return "hot-index"
    return "page"


def _record_reserved_artifact(
    rel: str,
    path: Path,
    basename: str,
    role: str,
    not_page: str,
    diagnostics: list[OkfImportDiagnostic],
) -> None:
    """Record a reserved artifact as ignored navigation/history input (issue #70).

    A reserved ``index.md`` / ``log.md`` / ``hot.md`` is never a Compiled Page.
    If its frontmatter cannot be parsed, a file-specific ``malformed-reserved``
    warning is emitted. If the artifact lacks a valid Lumio marker for its
    reserved basename, a ``blocking`` diagnostic is produced so a Maintainer must
    resolve the conflict before canonical publication; otherwise it is treated as
    a recognized derived artifact and recorded as ``ignored`` for review only.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if _reserved_artifact_is_malformed(text):
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="malformed-reserved",
                severity="warning",
                message=(
                    f"{basename} has unparseable frontmatter; treated as "
                    "preview-only and not proposed as a Compiled Page"
                ),
            )
        )
    if not _reserved_artifact_marker_is_valid(text, basename):
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="unmarked-reserved",
                severity="blocking",
                message=(
                    f"{basename} occupies a reserved artifact path but lacks a "
                    "valid Lumio marker; treated as preview-only and not proposed "
                    "as a Compiled Page. Canonical publication is blocked until "
                    "the file is marked or removed."
                ),
            )
        )
    diagnostics.append(
        OkfImportDiagnostic(
            path=rel,
            kind="reserved-artifact",
            severity="ignored",
            message=f"{basename} is {role}; {not_page}",
        )
    )


def _first_level_one_heading(body: str) -> str | None:
    """Return the text of the first level-one Markdown heading, or None."""
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _humanize_filename_stem(rel: str) -> str:
    """Return a reviewable title from a document's filename stem.

    Splits the stem on ``-``/``_`` and capitalizes each word, so
    ``deep-dive.md`` becomes ``"Deep Dive"`` and ``getting_started.md`` becomes
    ``"Getting Started"``.
    """
    stem = PurePosixPath(rel).stem
    words = [word for word in re.split(r"[-_]+", stem) if word]
    return " ".join(word.capitalize() for word in words) or stem


def _okf_description(data: dict[str, Any]) -> str | None:
    """Return a non-empty OKF ``description`` mapped to summary, or None.

    A missing or blank description returns ``None`` so the importer leaves the
    summary absent and the ordinary Core SDK summary warning stands, rather
    than inventing semantic content.
    """
    value = data.get("description")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classify_lumio_extension(lumio: Any) -> str:
    """Classify a ``lumio`` frontmatter block for Profile 1 import (issue #70).

    Returns one of:

    * ``"recognized"`` — a dict declaring ``profile_version: 1``. Canonical
      Profile 1 semantics are applied; unknown keys are dropped with a
      diagnostic.
    * ``"unsupported-profile"`` — a dict declaring a different integer
      ``profile_version``. Parsed for preview only; explicit supported-profile
      selection or Maintainer approval is required before canonical publication.
    * ``"producer-extension"`` — a non-dict, or a dict without a parseable
      ``profile_version``. Treated as an unknown producer extension and dropped
      at canonicalization (Profile 1 does not promise persistent lossless
      round-tripping).
    * ``"absent"`` — no ``lumio`` block at all (a generic OKF document).
    """
    if lumio is None:
        return "absent"
    if not isinstance(lumio, dict):
        return "producer-extension"
    if "profile_version" not in lumio:
        return "producer-extension"
    raw_version = lumio.get("profile_version")
    # Strict string match: ``int()`` would accept ``true``, ``1.9``, ``"01"`` —
    # values that are not a Profile 1 declaration and must not enter the
    # recognized Lumio trust path.
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        return "producer-extension"
    if raw_version == OKF_PROFILE_VERSION:
        return "recognized"
    return "unsupported-profile"

def _diagnose_dropped_okf_fields(
    rel: str, data: dict[str, Any], diagnostics: list[OkfImportDiagnostic]
) -> None:
    """Diagnose exchange-only OKF fields dropped at canonicalization (issue #70).

    ``resource`` and ``timestamp`` are always exchange-only. A non-empty
    ``type`` that is not a fixed Lumio exchange type is an unknown value. Each
    is reported as a ``dropped`` outcome so a Maintainer sees what was set
    aside, while the page remains previewable.
    """
    okf_type = str(data.get("type") or "").strip()
    if okf_type and okf_type not in (
        OKF_TYPE_COMPILED_PAGE,
        OKF_TYPE_NAVIGATION_INDEX,
    ):
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="type",
                severity="dropped",
                message=(
                    "OKF 'type' has an unknown non-empty exchange-only value; "
                    "dropped at canonicalization while the page remains previewable"
                ),
            )
        )
    if data.get("resource") is not None:
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="resource",
                severity="dropped",
                message=(
                    "OKF 'resource' is exchange-only and is dropped at "
                    "canonicalization; it is never used as provenance"
                ),
            )
        )
    if data.get("timestamp") is not None:
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="timestamp",
                severity="dropped",
                message=(
                    "OKF 'timestamp' is exchange-only and is dropped at "
                    "canonicalization"
                ),
            )
        )


def _diagnose_broken_body_links(
    rel: str,
    body: str,
    bundle_paths: set[str],
    diagnostics: list[OkfImportDiagnostic],
) -> None:
    """Report internal Markdown body links absent from the bundle (issue #70).

    Each broken link is a ``warning``. Body links never become typed
    Relationships (those come only from a recognized ``lumio`` extension), and
    external URLs / anchor-only links are left untouched. Repeated links to the
    same absent target are reported once.
    """
    source_dir = PurePosixPath(rel).parent.as_posix()
    seen: set[str] = set()
    for target in _iter_markdown_link_targets(body):
        resolved = _resolve_internal_link(target, source_dir)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        if resolved not in bundle_paths:
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="broken-link",
                    severity="warning",
                    message=(
                        "body link target is not present in the bundle; left as "
                        "prose and not promoted to a typed Relationship"
                    ),
                )
            )


def _reserved_artifact_is_malformed(text: str) -> bool:
    """Return whether a reserved artifact's frontmatter cannot be parsed (issue #70)."""
    try:
        _data, _body, _line = _parse_frontmatter(text, Path("reserved"))
    except FrontmatterError:
        return True
    return False


def _reserved_artifact_marker_is_valid(text: str, basename: str) -> bool:
    """Return whether a reserved artifact carries a valid Lumio marker (P3.4).

    A valid marker is a ``lumio`` mapping whose ``artifact`` matches the kind the
    basename reserves and whose ``version`` is a genuine integer in that
    artifact's supported set. An unmarked or malformed marker is invalid, which
    causes the importer to produce a blocking diagnostic rather than silently
    ignoring the reserved path.
    """
    expected_artifact = RESERVED_ARTIFACT_MARKERS.get(basename)
    if expected_artifact is None:
        return True  # not a known reserved basename; no marker required
    try:
        data, _body, _line = _parse_frontmatter(text, Path(basename))
    except FrontmatterError:
        return False
    lumio_meta = data.get("lumio")
    if not isinstance(lumio_meta, dict):
        return False
    if lumio_meta.get("artifact") != expected_artifact:
        return False
    version = lumio_meta.get("version")
    # Require a genuine integer (booleans and floats are rejected).
    is_int_version = isinstance(version, int) and not isinstance(version, bool)
    if not is_int_version:
        return False
    supported_versions = RESERVED_ARTIFACT_VERSIONS.get(expected_artifact, frozenset())
    return version in supported_versions


def _generic_source(rel: str, bundle_identity: str, bundle_origin: str | None) -> Source:
    """Build the deterministic Source for a generic (no recognized lumio) page.

    The identity combines the bundle digest with the document's relative path;
    the URL is the known bundle origin only when supplied. OKF ``resource`` is
    never substituted (the caller drops it before this is called).
    """
    return Source(
        id=f"okf-profile1:{bundle_identity}:{rel}",
        title=rel,
        url=bundle_origin,
    )


def _render_canonical_page_markdown(
    *,
    title: str,
    tags: list[str],
    summary: str | None,
    aliases: list[str],
    lifecycle: str,
    visibility: str,
    synthetic: bool,
    sources: list[Source],
    relationships: list[Relationship],
    body: str,
) -> str:
    """Render canonical Lumio frontmatter plus the unchanged body.

    Exchange-only fields (OKF ``type``, ``resource``, ``timestamp``, the
    ``lumio`` profile identification) are absent. Tags are always present so an
    empty list surfaces as an explicit, reviewable blocking issue rather than a
    missing field. The body is appended verbatim.
    """
    lines: list[str] = ["---"]
    lines.append(f"title: {_yaml_scalar(title)}")
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {_yaml_scalar(tag)}")
    else:
        lines.append("tags: []")
    if summary is not None:
        lines.append(f"summary: {_yaml_scalar(summary)}")
    if aliases:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f"  - {_yaml_scalar(alias)}")
    lines.append(f"lifecycle: {_yaml_scalar(lifecycle)}")
    lines.append(f"visibility: {_yaml_scalar(visibility)}")
    lines.append(f"synthetic: {'true' if synthetic else 'false'}")
    if sources:
        lines.append("sources:")
        for source in sources:
            lines.append(f"  - id: {_yaml_scalar(source.id)}")
            lines.append(f"    title: {_yaml_scalar(source.title)}")
            if source.url is not None:
                lines.append(f"    url: {_yaml_scalar(source.url)}")
    if relationships:
        lines.append("relationships:")
        for rel in relationships:
            lines.append(f"  - target: {_yaml_scalar(rel.target)}")
            lines.append(f"    type: {_yaml_scalar(rel.type)}")
    lines.append("---")
    return "\n".join(lines) + body


def _parse_okf_page(
    rel: str,
    text: str,
    bundle_identity: str,
    bundle_origin: str | None,
    bundle_paths: set[str],
    diagnostics: list[OkfImportDiagnostic],
) -> OkfImportPage:
    """Parse one OKF Markdown document into a proposed Compiled Page (issue #70).

    Standard OKF fields map onto canonical Lumio fields. A recognized
    ``lumio`` Profile 1 extension overrides the safe generic defaults with its
    aliases, lifecycle, visibility, synthetic status, sources, and
    relationships; any key it carries beyond the recognized set is dropped with
    a diagnostic stating Profile 1 does not promise persistent lossless
    round-tripping. An unsupported or unidentified profile is parsed for
    preview only and recorded as a blocking outcome. Otherwise the page
    defaults to draft / internal / non-synthetic with deterministic Source
    provenance. A missing title yields a reviewable candidate from the first
    level-one heading or the humanized filename stem. Exchange-only OKF
    ``type``, ``resource``, and ``timestamp`` are dropped, and broken internal
    body links are reported as warnings that never become typed Relationships.
    """
    try:
        data, body, _ = _parse_frontmatter(text, Path(rel))
    except FrontmatterError:
        data = {}
        body = text
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="frontmatter",
                severity="warning",
                message="document has no usable frontmatter; treating text as body",
            )
        )

    classification = _classify_lumio_extension(data.get("lumio"))
    lumio_raw = data.get("lumio") if classification == "recognized" else {}

    # Title: OKF title, else first level-one heading, else humanized filename.
    title = str(data.get("title") or "").strip() or None
    if title is None:
        heading = _first_level_one_heading(body)
        if heading:
            title = heading
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="title",
                    severity="mapped",
                    message=(
                        "missing title; derived reviewable candidate from the "
                        "first level-one heading"
                    ),
                )
            )
        else:
            title = _humanize_filename_stem(rel)
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="title",
                    severity="mapped",
                    message=(
                        "missing title and level-one heading; derived reviewable "
                        "candidate from the filename stem"
                    ),
                )
            )

    summary = _okf_description(data)
    if summary is not None:
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="description",
                severity="mapped",
                message="OKF 'description' mapped to canonical summary",
            )
        )
    tags = _as_string_list(data.get("tags"))

    if classification == "recognized":
        aliases = _as_string_list(lumio_raw.get("aliases"))
        lifecycle = str(lumio_raw.get("lifecycle") or "").strip() or "draft"
        visibility = str(lumio_raw.get("visibility") or "").strip() or "internal"
        synthetic = bool(lumio_raw.get("synthetic"))
        sources = _as_sources(lumio_raw.get("sources"))
        relationships = _as_relationships(lumio_raw.get("relationships"))
        # The Profile 1 identification block (profile, profile_version,
        # okf_version, okf_pin) is recognized identification, not an unknown
        # producer extension; only keys beyond the identification block and the
        # recognized semantic keys are diagnosed and dropped (issues #70, #71).
        for key in lumio_raw:
            if key in _LUMIO_IDENTIFICATION_KEYS or key in _RECOGNIZED_LUMIO_KEYS:
                continue
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="extension-key",
                    severity="dropped",
                    message=(
                        f"producer extension key {key!r} is not recognized by "
                        "Profile 1; dropped at canonicalization. Profile 1 does "
                        "not promise persistent lossless round-tripping of "
                        "unknown producer extensions."
                    ),
                )
            )
    else:
        aliases = []
        lifecycle = "draft"
        visibility = "internal"
        synthetic = False
        sources = [_generic_source(rel, bundle_identity, bundle_origin)]
        relationships = []
        if classification == "unsupported-profile":
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="profile",
                    severity="blocking",
                    message=(
                        "lumio extension declares an unsupported or unidentified "
                        "profile; parsed for preview only. Explicit supported-"
                        "profile selection or Maintainer approval is required "
                        "before canonical publication."
                    ),
                )
            )
        elif classification == "producer-extension":
            diagnostics.append(
                OkfImportDiagnostic(
                    path=rel,
                    kind="extension",
                    severity="dropped",
                    message=(
                        "lumio extension is not a recognized Profile 1 block; "
                        "treated as a producer extension and dropped at "
                        "canonicalization. Profile 1 does not promise persistent "
                        "lossless round-tripping of unknown producer extensions."
                    ),
                )
            )

    # Exchange-only OKF fields are dropped at canonicalization regardless of
    # lumio classification (issue #70).
    _diagnose_dropped_okf_fields(rel, data, diagnostics)

    # Broken internal body links are warnings and never Relationships (issue #70).
    _diagnose_broken_body_links(rel, body, bundle_paths, diagnostics)

    markdown = _render_canonical_page_markdown(
        title=title,
        tags=tags,
        summary=summary,
        aliases=aliases,
        lifecycle=lifecycle,
        visibility=visibility,
        synthetic=synthetic,
        sources=sources,
        relationships=relationships,
        body=body,
    )
    diagnostics.append(
        OkfImportDiagnostic(
            path=rel,
            kind="page",
            severity="accepted",
            message="document accepted as a proposed Compiled Page",
        )
    )
    return OkfImportPage(relative_path=rel, title=title, markdown=markdown)


def import_okf_profile1(
    bundle_path: str | Path,
    bundle_origin: str | None = None,
) -> OkfProfile1Import:
    """Parse a safe OKF Exchange Profile 1 directory tree into proposed pages.

    The inverse of :func:`export_okf_profile1`. The importer scans the actual
    bundle tree (never trusting an imported ``index.md`` as authoritative
    inventory), classifies each Markdown document by reserved basename, and maps
    the standard OKF fields onto proposed Compiled Pages with safe generic
    defaults. It is pure (it reads the bundle directory but performs no
    Knowledge Base mutation) and deterministic: identical bundles yield
    identical proposed pages.

    ``bundle_origin`` is an optional known URL identifying the bundle; it may
    become the Source URL for generic pages. OKF ``resource`` is never used as
    provenance. The result feeds the ordinary Ingest Proposal workflow
    (:func:`lumio.ingest.create_okf_import_proposal`), which supplies the diff,
    validation report, Blast Radius, affected pages, and blocked state.
    """
    root = Path(bundle_path).resolve()
    if not root.exists():
        raise OkfImportError(f"bundle path does not exist: {bundle_path}")
    if not root.is_dir():
        raise OkfImportError(f"bundle path is not a directory: {bundle_path}")

    markdown_files, skipped = _scan_okf_bundle(root)
    identity_entries = [
        (rel, path.read_bytes()) for rel, path in markdown_files
    ]
    bundle_identity = _okf_bundle_identity(identity_entries)
    bundle_paths = {rel for rel, _ in markdown_files}

    diagnostics: list[OkfImportDiagnostic] = []
    proposed: list[OkfImportPage] = []
    nav_count = 0
    log_count = 0
    for rel, path in markdown_files:
        kind = _classify_okf_doc(rel)
        if kind == "navigation-index":
            nav_count += 1
            _record_reserved_artifact(rel, path, "index.md", "navigation input",
                "not proposed as a Compiled Page", diagnostics)
            continue
        if kind == "log":
            log_count += 1
            _record_reserved_artifact(rel, path, "log.md", "preview-only exchange history",
                "not a Compiled Page or audit event", diagnostics)
            continue
        if kind == "hot-index":
            _record_reserved_artifact(rel, path, "hot.md",
                "a reserved Hot Index artifact path",
                "a conflicting path that is not proposed as a Compiled Page", diagnostics)
            continue

        text = path.read_text(encoding="utf-8")
        proposed.append(
            _parse_okf_page(
                rel, text, bundle_identity, bundle_origin, bundle_paths, diagnostics
            )
        )

    # Unsupported non-Markdown files are listed as skipped and surfaced as
    # ignored outcomes rather than silently dropped (issue #70).
    for rel in skipped:
        diagnostics.append(
            OkfImportDiagnostic(
                path=rel,
                kind="skipped",
                severity="ignored",
                message=(
                    "unsupported non-Markdown file listed as skipped; not "
                    "imported as a Compiled Page"
                ),
            )
        )

    return OkfProfile1Import(
        profile=OKF_PROFILE_NAME,
        profile_version=OKF_PROFILE_VERSION,
        okf_version=OKF_VERSION,
        okf_pin=OKF_V0_1_PIN,
        bundle_identity=bundle_identity,
        proposed_pages=sorted(proposed, key=lambda page: page.relative_path),
        diagnostics=sorted(
            diagnostics, key=lambda d: (d.path, d.kind, d.severity, d.message)
        ),
        navigation_index_count=nav_count,
        log_count=log_count,
        skipped_files=sorted(skipped),
    )
