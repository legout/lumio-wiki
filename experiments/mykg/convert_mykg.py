"""Experiment-only converter: myKG extraction session -> Lumio Compiled Page tree.

Standalone, standard-library only. This is NOT a ``lumio-wiki`` dependency and is
deliberately kept out of the package graph (issue #146,
``docs/research/mykg-fit.md``).

What it does
------------
* Reads a myKG session (``output/nodes.jsonl``, ``output/edges.jsonl``,
  ``intermediate/schema.json``).
* Emits one CANDIDATE Compiled Page per selected node into an external Markdown
  tree, with safe defaults: lifecycle ``review``, visibility ``internal``, the
  myKG ontology class mapped to the free-form page ``type`` (never a Content
  Category), and provenance mapped to opaque Lumio ``Source`` entries.
* Emits a PRIVATE relationship-candidate sidecar
  (``relationship-candidates.jsonl``) carrying myKG edges, confidence, method,
  and source files for Maintainer review.

Architecture guardrails (issue #146)
------------------------------------
* No myKG edge becomes a canonical Relationship without Maintainer review. Edges
  are NEVER written into page frontmatter ``relationships:``; they live only in
  the private sidecar.
* myKG confidence never maps to Lifecycle, Visibility, or publication status.
* Raw source paths, source text/chunks, method, and per-attribute confidence
  stay in the private sidecar / conversion report — never in the Compiled Page
  body or frontmatter. Page provenance uses opaque, stable source ids that the
  Maintainer resolves against the private Knowledge Source Registry.

The emitted tree is then staged through Lumio's normal external-import proposal
path (``import_external_compiled_markdown`` -> ``propose_external_import`` ->
``ProposalPipeline``), exactly as the issue's proposed experiment path requires.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_NODES = "output/nodes.jsonl"
SESSION_EDGES = "output/edges.jsonl"
SESSION_SCHEMA = "intermediate/schema.json"

# Lifecycle is deliberately restricted: a myKG candidate must never be `approved`
# (issue #146 architecture constraint). `deprecated` is not a candidate state.
ALLOWED_LIFECYCLES = ("draft", "review")
ALLOWED_VISIBILITIES = ("public", "internal", "restricted")
DEFAULT_CATEGORY = "entities"
DEFAULT_LIFECYCLE = "review"
DEFAULT_VISIBILITY = "internal"
DEFAULT_TAG = "entity"
DEFAULT_SOURCE_ID_PREFIX = "mykg"

# Seeded Content Category catalog (CONTEXT.md). Pages are routed under one of
# these directory names so Lumio's external-import category mapping is pass-through.
SEED_CATEGORIES = (
    "concepts",
    "entities",
    "references",
    "procedures",
    "tables",
    "datasets",
    "synthesis",
)


@dataclass
class Settings:
    """Conversion options (all safe-by-default)."""

    category: str = DEFAULT_CATEGORY
    default_lifecycle: str = DEFAULT_LIFECYCLE
    default_visibility: str = DEFAULT_VISIBILITY
    default_tag: str = DEFAULT_TAG
    source_id_prefix: str = DEFAULT_SOURCE_ID_PREFIX
    kb_root: Path | None = None


@dataclass
class PageSource:
    """A provenance entry on a candidate Compiled Page (opaque, stable)."""

    id: str
    title: str
    url: str | None = None


@dataclass
class PageSpec:
    """A single candidate Compiled Page, ready to render."""

    source_node_id: str
    title: str
    aliases: list[str]
    tags: list[str]
    lifecycle: str
    visibility: str
    type: str
    sources: list[PageSource]
    category: str
    slug: str
    body: str


@dataclass
class CandidateSpec:
    """A PRIVATE relationship candidate derived from a myKG edge."""

    edge_id: str
    from_node_id: str
    from_candidate_title: str | None
    to_node_id: str
    to_candidate_title: str | None
    relation_type: str
    confidence: float | None
    method: str | None
    source_files: list[str]
    attributes: dict[str, Any]
    # Maintainer fills this during review: accepted | modified | rejected.
    disposition: str = "pending"
    # False when either endpoint node was skipped (no name) -> cannot resolve.
    resolvable: bool = True


@dataclass
class ConversionResult:
    """Everything the converter produced, plus private review diagnostics."""

    pages: list[PageSpec] = field(default_factory=list)
    candidates: list[CandidateSpec] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # PRIVATE map: opaque source id -> raw myKG source file(s). Stays out of the
    # published tree; written only to conversion-report.json for Maintainer use.
    private_provenance: dict[str, list[str]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# myKG session loading
# --------------------------------------------------------------------------- #


def load_session(
    session_root: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Read ``(nodes, edges, schema)`` from a myKG session directory.

    Mirrors myKG's own ``load_session`` layout:
    ``output/nodes.jsonl``, ``output/edges.jsonl``, ``intermediate/schema.json``.
    ``schema.json`` is optional for conversion (used only for context).
    """
    root = Path(session_root)
    nodes = _read_jsonl(root / SESSION_NODES)
    edges = _read_jsonl(root / SESSION_EDGES)
    schema_path = root / SESSION_SCHEMA
    schema: dict[str, Any] = {}
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {schema_path}: {exc}") from exc
        if not isinstance(schema, dict):
            raise ValueError(f"{schema_path}: schema must be a JSON object")
    return nodes, edges, schema


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required myKG session file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{lineno}: {exc}") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


# --------------------------------------------------------------------------- #
# Field derivation (matches myKG's own accessors)
# --------------------------------------------------------------------------- #


def node_name(node: dict[str, Any]) -> str:
    """A node's display name: ``attributes.name.value`` (or scalar ``name``).

    Byte-for-byte compatible with myKG's ``_node_name`` helper.
    """
    attrs = node.get("attributes") or {}
    name_attr = attrs.get("name")
    if isinstance(name_attr, dict):
        value = name_attr.get("value")
        return str(value) if value is not None else ""
    return str(name_attr) if name_attr is not None else ""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def _stable_id(raw: Any, length: int = 12) -> str:
    """Short, stable, non-cryptographic digest for opaque ids."""
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:length]


def _source_id(raw_source: Any, prefix: str) -> str:
    """Opaque, stable provenance id. Does NOT leak the raw path or filename."""
    return f"{prefix}-{_stable_id(raw_source)}"


def _tag_for_type(node_type: str, default_tag: str) -> str:
    tokens = node_type.strip().split()
    token = re.sub(r"[^a-z0-9-]+", "", tokens[0].lower()) if tokens else ""
    return token or default_tag


# --------------------------------------------------------------------------- #
# Building pages + candidates
# --------------------------------------------------------------------------- #


def _taken_titles_from_kb(kb_root: Path) -> set[str]:
    """Best-effort scan of an existing KB for lowercase titles + aliases.

    The authoritative collision gate is Lumio's own validation on import; this
    only provides an early, pre-stage warning so obvious collisions are fixed
    before the proposal is assembled.
    """
    taken: set[str] = set()
    for md in Path(kb_root).rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        in_aliases = False
        for line in text[3:end].splitlines():
            stripped = line.strip()
            if stripped.startswith("aliases:"):
                in_aliases = True
                continue
            if in_aliases:
                if stripped.startswith("- "):
                    value = stripped[2:].strip().strip('"')
                    if value:
                        taken.add(value.lower())
                    continue
                in_aliases = False
            match = re.match(r'title:\s*"?(.*?)"?\s*$', line)
            if match:
                taken.add(match.group(1).lower())
    return taken


def build_pages(
    nodes: list[dict[str, Any]],
    settings: Settings,
    taken: set[str],
) -> tuple[list[PageSpec], dict[str, str], ConversionResult]:
    """Map myKG nodes -> candidate Compiled Page specs.

    Returns ``(pages, node_id_to_title, partial_result)``.
    """
    result = ConversionResult()
    used_titles: set[str] = set(taken)
    used_slugs: set[str] = set()
    node_id_to_title: dict[str, str] = {}

    for node in nodes:
        node_id = str(node.get("id", ""))
        name = node_name(node)
        if not name:
            result.skipped.append(
                {
                    "node_id": node_id,
                    "type": node.get("type"),
                    "reason": "node has no name attribute",
                }
            )
            continue

        node_type = str(node.get("type") or "")
        title = _unique_title(name, node_type, used_titles, result)
        used_titles.add(title.lower())
        node_id_to_title[node_id] = title

        kept_aliases: list[str] = []
        for alias in (str(a) for a in (node.get("aliases") or [])):
            if alias.lower() in used_titles:
                result.warnings.append(
                    f'node {node_id!r}: dropped alias {alias!r} (collides with an existing '
                    "title/alias); re-add manually after review if intended"
                )
            else:
                kept_aliases.append(alias)
                used_titles.add(alias.lower())

        source_files = [str(s) for s in (node.get("source_files") or [])]
        if not source_files:
            # A myKG node derives from raw sources, so it is NOT a Lumio Synthetic
            # Page (those are derived from other compiled knowledge). Without
            # source_files it simply lacks provenance -> skip it rather than emit a
            # non-synthetic page with no Source (CONTEXT.md requires >=1 Source).
            result.skipped.append(
                {
                    "node_id": node_id,
                    "type": node.get("type"),
                    "reason": "no source_files provenance; cannot map to a non-synthetic page",
                }
            )
            continue
        sources: list[PageSource] = []
        for raw in source_files:
            sid = _source_id(raw, settings.source_id_prefix)
            if sid not in {src.id for src in sources}:
                sources.append(PageSource(id=sid, title=sid))
            result.private_provenance.setdefault(sid, []).append(raw)

        slug = _unique_slug(slugify(title), used_slugs)
        used_slugs.add(slug)

        body = _render_body(title, kept_aliases)

        result.pages.append(
            PageSpec(
                source_node_id=node_id,
                title=title,
                aliases=kept_aliases,
                tags=[_tag_for_type(node_type, settings.default_tag)],
                lifecycle=settings.default_lifecycle,
                visibility=settings.default_visibility,
                type=node_type,
                sources=sources,
                category=settings.category,
                slug=slug,
                body=body,
            )
        )
    return result.pages, node_id_to_title, result


def _unique_title(name: str, node_type: str, used: set[str], result: ConversionResult) -> str:
    """Disambiguate a candidate Canonical Page Title on collision."""
    if name.lower() not in used:
        return name
    result.collisions.append(f'Canonical Page Title collision on "{name}"; disambiguating')
    suffixes: list[str] = [f"({node_type})"] if node_type else []
    suffixes += [f"({i})" for i in range(2, 1000)]
    for suffix in suffixes:
        candidate = f"{name} {suffix}"
        if candidate.lower() not in used:
            return candidate
    # Extremely unlikely fallback.
    return f"{name} ({_stable_id(name, 8)})"


def _unique_slug(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    for i in range(2, 1000):
        candidate = f"{base}-{i}"
        if candidate not in used:
            return candidate
    return f"{base}-{_stable_id(base, 8)}"


def _render_body(title: str, aliases: list[str]) -> str:
    """Neutral, privacy-safe body. No confidence, method, or source text."""
    lines = [
        f"# {title}",
        "",
        "Candidate page proposed from a private myKG extraction session. Review",
        "provenance and relationship candidates in the private sidecar before this",
        "page may become canonical.",
    ]
    if aliases:
        lines.append("")
        lines.append(f"**Also known as:** {', '.join(aliases)}")
    return "\n".join(lines)


def build_candidates(
    edges: list[dict[str, Any]],
    node_id_to_title: dict[str, str],
) -> list[CandidateSpec]:
    """Map myKG edges -> PRIVATE relationship candidates (never page relationships)."""
    candidates: list[CandidateSpec] = []
    for edge in edges:
        from_id = str(edge.get("from", ""))
        to_id = str(edge.get("to", ""))
        from_title = node_id_to_title.get(from_id)
        to_title = node_id_to_title.get(to_id)
        candidates.append(
            CandidateSpec(
                edge_id=str(edge.get("id") or f"{from_id}->{to_id}"),
                from_node_id=from_id,
                from_candidate_title=from_title,
                to_node_id=to_id,
                to_candidate_title=to_title,
                relation_type=str(edge.get("type") or ""),
                confidence=edge.get("confidence"),
                method=edge.get("method"),
                source_files=[str(s) for s in (edge.get("source_files") or [])],
                attributes=edge.get("attributes") or {},
                disposition="pending",
                resolvable=from_title is not None and to_title is not None,
            )
        )
    return candidates


def convert_session(session_root: str | Path, settings: Settings) -> ConversionResult:
    """Run the full conversion: nodes -> pages, edges -> candidates."""
    if settings.default_lifecycle not in ALLOWED_LIFECYCLES:
        raise ValueError(
            f"default_lifecycle must be one of {ALLOWED_LIFECYCLES} (never 'approved'); "
            f"got {settings.default_lifecycle!r}"
        )
    if settings.default_visibility not in ALLOWED_VISIBILITIES:
        raise ValueError(
            f"default_visibility must be one of {ALLOWED_VISIBILITIES}; "
            f"got {settings.default_visibility!r}"
        )
    if not settings.default_tag.strip():
        raise ValueError(
            "default_tag must be non-empty (tags are required and non-empty)"
        )

    nodes, _edges, _schema = load_session(session_root)
    taken = _taken_titles_from_kb(settings.kb_root) if settings.kb_root else set()
    pages, node_id_to_title, result = build_pages(nodes, settings, taken)
    candidates = build_candidates(_edges, node_id_to_title)

    dangling = [c for c in candidates if not c.resolvable]
    if dangling:
        result.warnings.append(
            f"{len(dangling)} relationship candidate(s) reference a skipped/missing node "
            "(cannot resolve to a Canonical Page Title); review in the sidecar"
        )

    result.pages = pages
    result.candidates = candidates
    return result


# --------------------------------------------------------------------------- #
# Rendering + writing
# --------------------------------------------------------------------------- #


def _yaml_quote_scalar(value: Any) -> str:
    """Render a scalar as a double-quoted YAML string with escapes."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_page(spec: PageSpec) -> str:
    """Render a candidate Compiled Page as Markdown with YAML frontmatter.

    ``relationships:`` is intentionally NEVER emitted: myKG edges are candidates
    only and live in the private sidecar until a Maintainer accepts them. Every
    page is non-synthetic and carries at least one provenance ``Source``.
    """
    lines: list[str] = ["---"]
    lines.append(f"title: {_yaml_quote_scalar(spec.title)}")
    if spec.aliases:
        lines.append("aliases:")
        for alias in spec.aliases:
            lines.append(f"  - {_yaml_quote_scalar(alias)}")
    lines.append("tags:")
    for tag in spec.tags:
        lines.append(f"  - {_yaml_quote_scalar(tag)}")
    lines.append(f"lifecycle: {_yaml_quote_scalar(spec.lifecycle)}")
    lines.append(f"visibility: {_yaml_quote_scalar(spec.visibility)}")
    if spec.type:
        lines.append(f"type: {_yaml_quote_scalar(spec.type)}")
    lines.append("sources:")
    for source in spec.sources:
        lines.append(f"  - id: {_yaml_quote_scalar(source.id)}")
        lines.append(f"    title: {_yaml_quote_scalar(source.title)}")
        if source.url:
            lines.append(f"    url: {_yaml_quote_scalar(source.url)}")
    # NOTE: no `relationships:` block — by design (issue #146).
    lines.append("---")
    lines.append("")
    lines.append(spec.body)
    return "\n".join(lines) + "\n"


def candidate_to_dict(candidate: CandidateSpec) -> dict[str, Any]:
    return {
        "edge_id": candidate.edge_id,
        "from_node_id": candidate.from_node_id,
        "from_candidate_title": candidate.from_candidate_title,
        "to_node_id": candidate.to_node_id,
        "to_candidate_title": candidate.to_candidate_title,
        "relation_type": candidate.relation_type,
        "confidence": candidate.confidence,
        "method": candidate.method,
        "source_files": candidate.source_files,
        "attributes": candidate.attributes,
        "resolvable": candidate.resolvable,
        "disposition": candidate.disposition,
    }


def write_tree(result: ConversionResult, out_dir: str | Path, settings: Settings) -> Path:
    """Write the candidate Markdown tree + private sidecar + conversion report."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    category_dir = out / settings.category
    category_dir.mkdir(parents=True, exist_ok=True)

    for page in result.pages:
        (category_dir / f"{page.slug}.md").write_text(render_page(page), encoding="utf-8")

    sidecar = out / "relationship-candidates.jsonl"
    with sidecar.open("w", encoding="utf-8") as handle:
        for candidate in result.candidates:
            handle.write(json.dumps(candidate_to_dict(candidate), ensure_ascii=False) + "\n")

    report = {
        "purpose": "PRIVATE Maintainer review material. Do not publish.",
        "summary": {
            "pages": len(result.pages),
            "candidates": len(result.candidates),
            "skipped_nodes": len(result.skipped),
            "title_collisions": len(result.collisions),
            "warnings": len(result.warnings),
        },
        "collisions": result.collisions,
        "warnings": result.warnings,
        "skipped_nodes": result.skipped,
        # PRIVATE: opaque source id -> raw myKG source file(s). Resolve against the
        # Knowledge Source Registry; never copy into a Compiled Page.
        "private_provenance": result.private_provenance,
    }
    (out / "conversion-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert_mykg.py",
        description=(
            "Convert a myKG extraction session into a candidate Lumio Compiled Page "
            "tree + a private relationship-candidate sidecar. Experiment only (issue #146)."
        ),
    )
    parser.add_argument("--session", required=True, help="myKG session root directory.")
    parser.add_argument("--out", required=True, help="output Markdown tree directory.")
    parser.add_argument(
        "--category",
        default=DEFAULT_CATEGORY,
        choices=SEED_CATEGORIES,
        help="seeded Content Category directory to place pages under.",
    )
    parser.add_argument(
        "--default-lifecycle",
        default=DEFAULT_LIFECYCLE,
        choices=ALLOWED_LIFECYCLES,
        help="candidate lifecycle (never 'approved').",
    )
    parser.add_argument(
        "--default-visibility",
        default=DEFAULT_VISIBILITY,
        choices=ALLOWED_VISIBILITIES,
        help="candidate visibility (conservative default 'internal').",
    )
    parser.add_argument(
        "--default-tag",
        default=DEFAULT_TAG,
        help="fallback tag when a node has no type.",
    )
    parser.add_argument(
        "--source-id-prefix",
        default=DEFAULT_SOURCE_ID_PREFIX,
        help="prefix for opaque, stable provenance source ids.",
    )
    parser.add_argument(
        "--kb-root",
        default=None,
        help="optional existing KB root for best-effort title/alias collision warnings.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    settings = Settings(
        category=args.category,
        default_lifecycle=args.default_lifecycle,
        default_visibility=args.default_visibility,
        default_tag=args.default_tag,
        source_id_prefix=args.source_id_prefix,
        kb_root=Path(args.kb_root) if args.kb_root else None,
    )
    try:
        result = convert_session(args.session, settings)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    write_tree(result, args.out, settings)
    dangling = sum(1 for c in result.candidates if not c.resolvable)
    print(
        f"converted {len(result.pages)} page(s), {len(result.candidates)} relationship "
        f"candidate(s) ({dangling} dangling), {len(result.skipped)} node(s) skipped, "
        f"{len(result.collisions)} collision(s) -> {args.out}"
    )
    if result.warnings:
        print("warnings:", file=sys.stderr)
        for warning in result.warnings:
            print(f"  - {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
