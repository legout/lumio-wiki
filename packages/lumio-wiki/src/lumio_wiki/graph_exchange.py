"""Graph exchange export and stub import (issue #193, ADR-0024, PRD-0005).

A third documented exchange surface alongside OKF Profiles 1/2: a
structure-only, tool-interop projection of the Discovery Graph.

**Export** (:func:`export_graph`) deterministically serializes an
ALREADY-AUTHORIZED page set to two artifacts:

- ``graph.json`` — NetworkX node_link format (the de-facto interchange
  standard; what wiki-export/wiki-import and most Python tooling speak);
- ``graph.graphml`` — GraphML for Gephi/yEd/Cytoscape.

Nodes carry page identity (stable Entity ID, or Canonical Page Title for
Legacy Flat Mode pages without one), the Canonical Page Title and label,
page path, Content Category, Entity Types, tags, and summary — never body
content, Sources, or private registry data. Edges are typed canonical
Relationships (accepted entity-to-entity Claims) plus untyped Extracted
References, marked by ``kind`` so consumers can distinguish reviewed
knowledge from derived navigation. Visibility filtering is ENFORCED, NOT
DISCLOSED (ADR-0024): the serializer receives only the authorized set,
exactly as at the OKF export boundary (ADR-0007), so an export can never
leak a title or summary the caller is not authorized to see. Excluded
edges are dropped silently.

**Import** (:func:`import_graph`) loads a ``graph.json`` (Lumio or
wiki-export lineage) and stages stub Compiled Pages — frontmatter skeletons
plus link structure, no bodies — as ONE ordinary reviewable Ingest
Proposal. There are no merge/skip/overwrite modes: the Proposal Pipeline
replaces them entirely (review, validate, publish, or discard). Broken
links (edges to nodes absent from the graph) are tolerated and disclosed
as proposal diagnostics. Edges arrive as Markdown link structure — typed
relationships carry their predicate as a link title — and NEVER as Claims:
semantics are Maintainer-authored at review, never inferred from structure.
Stubs carry a minimal Entity contract (Lumio lineage keeps the original
Entity ID and Entity Types; foreign nodes get a synthesized ID and the
``stub`` type) because every version-2 Knowledge Base page declares one;
missing Entity Types and foreign categories are proposed as ONE Control
File extension (the ADR-0009 pattern), never silently created or dropped.

Deferred with a named trigger (ADR-0024): ``cypher.txt``, ``postgres.sql``,
``graph.html`` — add when a real integration asks.

Stability note: ``graph.json`` node fields are additive-only once shipped.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import msgspec

from lumio_wiki.ingest import (
    IngestProposal,
    ProposedPage,
    SourceProvenance,
    _compute_diff,
    _existing_page_markdown,
    _validate_page_routing,
    compute_blast_radius,
    import_page_category,
)
from lumio_wiki.knowledge_base import (
    ENTITY_ID_PREFIX,
    IDENTIFIER_SLUG_RE,
    EntityTypeDefinition,
    Ontology,
    extend_control_file_categories,
    extract_references,
)
from lumio_wiki.okf import OkfImportDiagnostic, _yaml_scalar
from lumio_wiki.publish import validate_candidate_knowledge_base
from lumio_wiki.records import CompiledPage, ValidationReport

#: Edge kind: a typed canonical Relationship (an accepted entity-to-entity
#: Claim). Reviewed knowledge.
EDGE_KIND_RELATIONSHIP = "relationship"
#: Edge kind: an untyped Extracted Reference (derived navigation only).
EDGE_KIND_REFERENCE = "reference"

#: The ``graph.graph`` format identifier carried by every Lumio export.
GRAPH_EXCHANGE_FORMAT = "lumio-graph-exchange"

#: Version of the graph exchange projection. Bumped only when the serialized
#: layout changes in a way an older reader could misinterpret; node fields
#: are additive-only once shipped (PRD-0005).
GRAPH_EXCHANGE_VERSION = 1

_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"

#: Entity Type assigned to stub pages whose graph node carried no Entity
#: Types (foreign wiki-export lineage). Proposed into the destination
#: ontology through the Control File extension when missing.
STUB_ENTITY_TYPE = "stub"

_STUB_PAGE_TYPE = "stub"
_STUB_DURABILITY_RATIONALE = (
    "Structure-only stub imported through graph exchange; awaiting content "
    "distillation and semantic review."
)
_ENTITY_ID_RE = re.compile(rf"^{re.escape(ENTITY_ID_PREFIX)}[a-z0-9][a-z0-9-]*[a-z0-9]$")


class GraphExchangeExport(msgspec.Struct, frozen=True):
    """The two deterministic graph-exchange artifacts over an authorized page set.

    ``graph_json`` is a complete NetworkX node_link document and ``graphml``
    a complete GraphML document (both str). Both are byte-deterministic for
    identical authorized pages.
    """

    graph_json: str
    graphml: str
    node_count: int
    relationship_count: int
    reference_count: int


class GraphImportError(ValueError):
    """Raised when a graph.json input cannot be parsed as a graph exchange.

    Covers missing/unreadable files, invalid JSON, and malformed payload
    shapes (missing ``nodes``, non-mapping node entries, nodes without an
    ``id``) so every caller surfaces one consistent error shape.
    """


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _node_id(page: CompiledPage) -> str:
    """Return the graph key for a page: stable Entity ID, else Canonical Title."""
    return page.id or page.title


def export_graph(pages: Sequence[CompiledPage]) -> GraphExchangeExport:
    """Serialize an already-authorized page set to graph.json + graph.graphml.

    The caller (Core SDK authorization layer) selects the authorized pages —
    e.g. :meth:`KnowledgeBase.public_pages` or
    :func:`lumio_wiki.select_export_pages`. The serializer trusts that set
    and cannot widen it: it never receives excluded pages, so their titles,
    summaries, and edges cannot leak into either artifact (ADR-0007 export
    boundary; enforced, not disclosed — ADR-0024).

    Pure and deterministic: identical authorized pages always yield
    byte-identical output. Nodes are ordered by id; edges by
    ``(source, target, kind, predicate)``.
    """
    authorized = sorted(pages, key=_node_id)
    id_set = {_node_id(page) for page in authorized}
    id_by_title = {page.title: _node_id(page) for page in authorized}

    nodes: list[dict[str, Any]] = []
    for page in authorized:
        node: dict[str, Any] = {
            "id": _node_id(page),
            "label": page.title,
            "title": page.title,
            "path": page.path,
            "tags": list(page.tags),
            "summary": page.summary or "",
        }
        if page.entity_types:
            node["entity_types"] = list(page.entity_types)
        category = import_page_category(page.path)
        if category is not None:
            node["category"] = category
        nodes.append(node)

    links: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    def add_edge(source: str, target: str, kind: str, predicate: str = "") -> None:
        key = (source, target, kind, predicate)
        if key in seen_edges:
            return
        seen_edges.add(key)
        link: dict[str, Any] = {"source": source, "target": target, "kind": kind}
        if predicate:
            link["predicate"] = predicate
        links.append(link)

    # Typed canonical Relationships: accepted entity-to-entity Claims whose
    # object resolves inside the authorized set. Dangling objects and edges
    # to filtered-out pages are dropped silently (enforced, not disclosed).
    for page in authorized:
        source = _node_id(page)
        for claim in sorted(page.claims, key=lambda c: (c.predicate, c.object or "")):
            if claim.status != "accepted" or claim.object is None:
                continue
            if claim.object in id_set and claim.object != source:
                add_edge(source, claim.object, EDGE_KIND_RELATIONSHIP, claim.predicate)

    # Untyped Extracted References, derived over the authorized set ONLY so
    # a reference to a filtered-out page can never produce an edge.
    for ref in extract_references(authorized):
        source_id = id_by_title.get(ref.source_title)
        target_id = id_by_title.get(ref.target_title)
        if source_id is None or target_id is None or source_id == target_id:
            continue
        add_edge(source_id, target_id, EDGE_KIND_REFERENCE)

    def link_sort_key(link: dict[str, Any]) -> tuple[str, str, str, str]:
        return (link["source"], link["target"], link["kind"], link.get("predicate", ""))

    links.sort(key=link_sort_key)
    relationship_count = sum(1 for link in links if link["kind"] == EDGE_KIND_RELATIONSHIP)
    reference_count = len(links) - relationship_count

    payload = {
        "directed": True,
        "multigraph": False,
        "graph": {
            "format": GRAPH_EXCHANGE_FORMAT,
            "version": GRAPH_EXCHANGE_VERSION,
            "node_count": len(nodes),
            "edge_count": len(links),
        },
        "nodes": nodes,
        "links": links,
    }
    graph_json = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    graphml = _render_graphml(nodes, links)
    return GraphExchangeExport(
        graph_json=graph_json,
        graphml=graphml,
        node_count=len(nodes),
        relationship_count=relationship_count,
        reference_count=reference_count,
    )


def _render_graphml(nodes: Sequence[dict[str, Any]], links: Sequence[dict[str, Any]]) -> str:
    """Render the same node/edge set as deterministic GraphML."""
    ns = f"{{{_GRAPHML_NS}}}"
    node_keys = ("label", "title", "path", "category", "entity_types", "tags", "summary")
    edge_keys = ("kind", "predicate")
    root = ET.Element(f"{ns}graphml")
    for key_id in node_keys:
        key = ET.SubElement(root, f"{ns}key", {"id": key_id, "for": "node"})
        key.set("attr.name", key_id)
        key.set("attr.type", "string")
    for key_id in edge_keys:
        key = ET.SubElement(root, f"{ns}key", {"id": key_id, "for": "edge"})
        key.set("attr.name", key_id)
        key.set("attr.type", "string")
    graph = ET.SubElement(root, f"{ns}graph", {"id": "lumio", "edgedefault": "directed"})
    for node in nodes:
        element = ET.SubElement(graph, f"{ns}node", {"id": node["id"]})
        for field in node_keys:
            value = node.get(field)
            if value is None:
                continue
            if isinstance(value, list):
                value = ", ".join(value)
            data = ET.SubElement(element, f"{ns}data", {"key": field})
            data.text = str(value)
    for index, link in enumerate(links):
        element = ET.SubElement(
            graph,
            f"{ns}edge",
            {"id": f"e{index}", "source": link["source"], "target": link["target"]},
        )
        data = ET.SubElement(element, f"{ns}data", {"key": "kind"})
        data.text = link["kind"]
        if "predicate" in link:
            data = ET.SubElement(element, f"{ns}data", {"key": "predicate"})
            data.text = link["predicate"]
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _read_graph_document(graph_path: str | Path) -> tuple[dict[str, Any], bytes]:
    """Load and shape-check a graph.json document, or raise GraphImportError."""
    path = Path(graph_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        detail = getattr(exc, "strerror", None) or str(exc)
        raise GraphImportError(f"graph file could not be read: {path} ({detail})") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueError
        raise GraphImportError(f"graph file is not valid JSON: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise GraphImportError(f"graph document must be a JSON object: {path}")
    if "nodes" not in payload:
        raise GraphImportError(f"graph document has no 'nodes' array: {path}")
    if not isinstance(payload["nodes"], list):
        raise GraphImportError(f"graph document 'nodes' must be an array: {path}")
    seen_node_ids: set[str] = set()
    for node in payload["nodes"]:
        if not isinstance(node, dict):
            raise GraphImportError(f"graph document node entries must be objects: {path}")
        if not isinstance(node.get("id"), str) or not node["id"].strip():
            raise GraphImportError(f"every graph node requires a non-empty string 'id': {path}")
        if node["id"] in seen_node_ids:
            raise GraphImportError(
                f"graph document contains a duplicate node id: {node['id']!r} ({path})"
            )
        seen_node_ids.add(node["id"])
    return payload, raw


def _stub_relative_path(node: dict[str, Any]) -> str:
    """Derive the stub's relative path: Lumio ``path`` field, else node id.

    Foreign (wiki-export) node ids ARE vault-relative paths, so ``<id>.md``
    preserves the source's category layout. Lumio node ids are Entity IDs,
    which are not paths; those nodes carry an explicit ``path`` field.
    """
    node_path = node.get("path")
    raw = node_path if isinstance(node_path, str) and node_path else f"{node['id']}.md"
    candidate = raw.lstrip("/")
    if not candidate.endswith(".md"):
        candidate = f"{candidate}.md"
    parts = PurePosixPath(candidate).parts
    if not parts or any(part in {"..", "."} for part in parts):
        raise GraphImportError(f"graph node id/path is not a safe relative path: {raw!r}")
    return candidate


def _stub_entity_id(node: dict[str, Any], relative_path: str) -> str:
    """Return the stub's Entity ID: a Lumio lineage ID, else a path slug.

    Entity IDs must use the ``entity:`` prefix and slug shape; a node whose
    id already conforms (a Lumio export) keeps its identity, everything
    else is derived deterministically from the stub's path.
    """
    node_id = node["id"]
    if _ENTITY_ID_RE.match(node_id):
        return node_id
    stem = PurePosixPath(relative_path).stem or "stub"
    while "--" in stem:
        stem = stem.replace("--", "-")
    slug = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch == "-")) else "-" for ch in stem.lower()
    ).strip("-")
    if not IDENTIFIER_SLUG_RE.match(slug):
        slug = "stub"
    return f"{ENTITY_ID_PREFIX}{slug}"


def _relative_link(source_path: str, target_path: str) -> str:
    """Return a POSIX relative Markdown link target from one stub to another."""
    source_parts = PurePosixPath(source_path).parts[:-1]
    target_parts = PurePosixPath(target_path).parts
    common = 0
    for a, b in zip(source_parts, target_parts, strict=False):
        if a != b:
            break
        common += 1
    asc = [".."] * (len(source_parts) - common)
    desc = list(target_parts[common:])
    return "/".join(asc + desc) if (asc or desc) else PurePosixPath(target_path).name


def _render_stub_markdown(
    title: str,
    entity_id: str | None,
    entity_types: Sequence[str],
    tags: Sequence[str],
    summary: str,
    link_lines: Sequence[str],
) -> str:
    """Render one stub Compiled Page: frontmatter skeleton + link structure.

    A categorized destination requires every page to declare an Entity
    contract, so ``entity_id`` carries it; a Legacy Flat Mode destination
    gets a contract-free skeleton (an Entity contract without a Control
    File ontology can never validate).
    """
    lines = ["---", f"title: {_yaml_scalar(title)}"]
    if entity_id is not None:
        lines.append(f"id: {_yaml_scalar(entity_id)}")
        lines.append("entity_types:")
        lines.extend(f"  - {_yaml_scalar(kind)}" for kind in entity_types)
    lines.append("tags:")
    lines.extend(f"  - {_yaml_scalar(tag)}" for tag in tags)
    if summary:
        lines.append(f"summary: {_yaml_scalar(summary)}")
    lines.extend(['lifecycle: "draft"', 'visibility: "public"', "synthetic: true", "---", ""])
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        "Structure-only stub imported through graph exchange (ADR-0024). No "
        "body content traveled with the graph; author the page and its Claims "
        "at review."
    )
    if link_lines:
        lines.extend(["", "## Links", ""])
        lines.extend(link_lines)
    lines.append("")
    return "\n".join(lines)


def _extend_control_file_ontology(
    control,
    entity_types: Sequence[str],
) -> Any:
    """Return a proposed Control File adding missing Entity Types to the ontology.

    Mirrors :func:`extend_control_file_categories` (ADR-0009): the types are
    PROPOSED, never silently created; a reviewing Maintainer may fill in
    descriptions or discard the proposal. Existing declarations win.
    """
    ontology = control.ontology or Ontology()
    merged = dict(ontology.entity_types)
    for kind in entity_types:
        merged.setdefault(kind, EntityTypeDefinition())
    return msgspec.structs.replace(
        control,
        ontology=msgspec.structs.replace(ontology, entity_types=merged),
    )


def import_graph(graph_path: str | Path, kb) -> IngestProposal:
    """Stage stub Compiled Pages from a graph.json as ONE reviewable proposal.

    Accepts Lumio exports and foreign wiki-export lineage: nodes resolve by
    ``id`` (Entity ID or path), titles by ``label``/``title``, categories by
    the directory convention (first path segment, ADR-0009). Edges become
    Markdown link structure — typed relationships carry their predicate as a
    link title, extracted references stay unmarked — and never Claims.

    Stubs keep a Lumio lineage node's original Entity ID and Entity Types;
    foreign nodes get a synthesized ID and the ``stub`` type. A categorized
    destination with unconfigured foreign categories or undeclared Entity
    Types receives ONE Control File extension proposal (the ADR-0009
    pattern) instead of a blocked import. Edges to nodes absent from the
    graph are tolerated and disclosed as ``broken-link`` diagnostics. There
    are no merge/skip/overwrite modes; review replaces them.
    """
    payload, raw = _read_graph_document(graph_path)
    links = payload.get("links") or []
    if not isinstance(links, list):
        raise GraphImportError(f"graph document 'links' must be an array: {graph_path}")

    nodes: list[dict[str, Any]] = payload["nodes"]
    path_by_id: dict[str, str] = {}
    for node in nodes:
        resolved = _stub_relative_path(node)
        if resolved in path_by_id.values():
            raise GraphImportError(
                f"two graph nodes resolve to the same stub path: {resolved!r} (node {node['id']!r})"
            )
        path_by_id[node["id"]] = resolved
    title_by_id = {
        node["id"]: str(node.get("title") or node.get("label") or node["id"]) for node in nodes
    }

    # Edges per source node. Unresolvable endpoints are tolerated and
    # disclosed (broken links), never fatal.
    edges_by_source: dict[str, list[tuple[str, str]]] = {}
    diagnostics: list[OkfImportDiagnostic] = []
    for link in links:
        if not isinstance(link, dict):
            diagnostics.append(
                OkfImportDiagnostic(
                    path=str(graph_path),
                    kind="broken-link",
                    severity="warning",
                    message="malformed link entry skipped (must be an object)",
                )
            )
            continue
        source_id = link.get("source")
        target_id = link.get("target")
        if not isinstance(source_id, str) or not isinstance(target_id, str):
            diagnostics.append(
                OkfImportDiagnostic(
                    path=str(graph_path),
                    kind="broken-link",
                    severity="warning",
                    message=(
                        f"malformed link entry skipped (source/target must be "
                        f"strings, got source={source_id!r} target={target_id!r})"
                    ),
                )
            )
            continue
        if source_id not in path_by_id or target_id not in path_by_id:
            diagnostics.append(
                OkfImportDiagnostic(
                    path=path_by_id.get(str(source_id), str(graph_path)),
                    kind="broken-link",
                    severity="warning",
                    message=(
                        f"edge {source_id!r} -> {target_id!r} does not resolve to a "
                        "node in the graph; tolerated and dropped (PRD-0005)"
                    ),
                )
            )
            continue
        predicate = link.get("predicate")
        if not isinstance(predicate, str) or not predicate.strip():
            relation = link.get("relation")
            # wiki-export marks plain wikilinks with relation "wikilink";
            # only a typed edge carries a real predicate.
            predicate = relation if isinstance(relation, str) and relation != "wikilink" else ""
        edges_by_source.setdefault(str(source_id), []).append((str(target_id), predicate.strip()))

    control = getattr(kb, "control", None)
    extension_categories: list[str] = []
    extension_entity_types: list[str] = []
    if control is not None:
        configured = {category.name for category in control.categories}
        declared_types = set(control.ontology.entity_types) if control.ontology else set()
        for node in nodes:
            category = import_page_category(path_by_id[node["id"]])
            if (
                category is not None
                and category not in configured
                and category not in extension_categories
            ):
                extension_categories.append(category)
            raw_types = node.get("entity_types")
            for kind in raw_types if isinstance(raw_types, list) else ():
                if (
                    isinstance(kind, str)
                    and kind not in declared_types
                    and kind not in extension_entity_types
                ):
                    extension_entity_types.append(kind)
        if (
            STUB_ENTITY_TYPE not in declared_types
            and STUB_ENTITY_TYPE not in extension_entity_types
        ):
            extension_entity_types.append(STUB_ENTITY_TYPE)

    # Entity IDs must stay unique within the proposal AND against the
    # destination. Collisions get a deterministic numeric suffix.
    taken_ids = {page.id for page in kb.pages if page.id}
    entity_id_by_node: dict[str, str] = {}
    for node in nodes:
        base = _stub_entity_id(node, path_by_id[node["id"]])
        entity_id = base
        suffix = 2
        while entity_id in taken_ids:
            entity_id = f"{base}-{suffix}"
            suffix += 1
        taken_ids.add(entity_id)
        entity_id_by_node[node["id"]] = entity_id

    proposed_pages: list[ProposedPage] = []
    for node in nodes:
        node_id = node["id"]
        relative_path = path_by_id[node_id]
        title = title_by_id[node_id]
        raw_tags = node.get("tags")
        tags = (
            [str(tag) for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
        )
        if not tags:
            tags = [STUB_ENTITY_TYPE]
        raw_types = node.get("entity_types")
        entity_types = (
            [str(kind) for kind in raw_types if str(kind).strip()]
            if isinstance(raw_types, list)
            else []
        )
        if not entity_types:
            entity_types = [STUB_ENTITY_TYPE]

        link_lines: list[str] = []
        for target_id, predicate in sorted(
            edges_by_source.get(node_id, []), key=lambda edge: (title_by_id[edge[0]], edge[1])
        ):
            target = _relative_link(relative_path, path_by_id[target_id])
            label = title_by_id[target_id]
            if predicate:
                link_lines.append(f"- [{label}]({target} {_yaml_scalar(predicate)})")
            else:
                link_lines.append(f"- [{label}]({target})")

        markdown = _render_stub_markdown(
            title,
            # A categorized destination requires the Entity contract; a
            # Legacy Flat Mode destination cannot validate one.
            entity_id_by_node[node_id] if control is not None else None,
            entity_types,
            tags,
            str(node.get("summary") or ""),
            link_lines,
        )
        proposed_pages.append(
            ProposedPage(
                relative_path=relative_path,
                title=title,
                markdown=markdown,
                category=import_page_category(relative_path),
                page_type=_STUB_PAGE_TYPE,
                durability_rationale=_STUB_DURABILITY_RATIONALE,
            )
        )

    extension_control_file = None
    if control is not None and (extension_categories or extension_entity_types):
        extended = control
        if extension_categories:
            extended = extend_control_file_categories(extended, extension_categories)
        if extension_entity_types:
            extended = _extend_control_file_ontology(extended, extension_entity_types)
        extension_control_file = extended

    existing_pages = _existing_page_markdown(kb)
    diff = _compute_diff(proposed_pages, existing_pages)
    # The authoritative candidate gate (the same one publication uses):
    # proposed pages overlaid on the destination tree WITH the proposed
    # Control File extension, so the stubs' Entity contracts validate
    # against the ontology they propose.
    page_validation = validate_candidate_knowledge_base(
        proposed_pages, kb.root, control_file=extension_control_file
    )
    routing_issues = _validate_page_routing(
        proposed_pages, kb, extra_categories=extension_categories
    )
    validation_report = ValidationReport(issues=list(page_validation.issues) + routing_issues)
    return IngestProposal(
        id=uuid.uuid4().hex,
        status="staged",
        created_at=datetime.now(UTC).isoformat(),
        provenance=SourceProvenance(
            original_filename=str(Path(graph_path)),
            content_type="application/json",
            converted_by="graph-exchange",
            source_hash=hashlib.sha256(raw).hexdigest(),
        ),
        proposed_pages=proposed_pages,
        affected_pages=[page.title for page in proposed_pages],
        diff=diff,
        validation_report=validation_report,
        blocked=not validation_report.is_valid,
        blast_radius=compute_blast_radius(proposed_pages, kb),
        okf_diagnostics=diagnostics,
        control_file=extension_control_file,
    )
