"""Standalone ``lumio-wiki`` command-line interface (issue #98).

A coding agent initializes, inspects, retrieves from, ingests into, reviews,
and publishes a Knowledge Base through this CLI without cloning the Lumio
repository or importing the full web application. Every command calls the
public :mod:`lumio_wiki` Python surface — no internal application modules.

The dispatcher follows the same ``argparse`` + ``set_defaults(func=...)``
convention the existing ``lumio`` CLI uses. Commands map one-to-one onto
public functions:

================================================  ================================================
Command                                           Public function
================================================  ================================================
``init <path>``                                   :func:`lumio_wiki.write_control_file`
``validate <path>``                               :func:`lumio_wiki.validate`
``search <path> <query>``                         :meth:`KnowledgeBase.search_pages`
``page <path> <title>``                           :meth:`KnowledgeBase.lookup_by_title`
``related <path> <title>``                        :meth:`KnowledgeBase.related_pages`
``paths <path> <source> <target>``               :meth:`KnowledgeBase.shortest_path`
``ingest <path> <file>``                          :func:`create_proposal_without_provider`
``proposal list <path>``                          :meth:`ProposalPipeline.list`
``proposal inspect <path> <id>``                  :meth:`ProposalPipeline.review`
``proposal validate <path> <id>``                 proposal validation report
``publish <path> <id>``                           :meth:`ProposalPipeline.publish`
``discard <path> <id>``                           :meth:`ProposalPipeline.discard`
``health <path>``                                 :meth:`KnowledgeBase.graph_health` + validation
``doctor``                                        install diagnostics (optionals, skill path)
``skill path``                                    packaged skill location
``skill install --agent <name>``                  install skill for a supported coding agent
================================================  ================================================

The base Distiller is the host coding agent (``PassthroughMarkdownDistiller``):
no OpenAI client is required for text and Markdown ingestion.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import lumio_wiki
from lumio_wiki import (
    ControlFileError,
    IngestStore,
    KnowledgeBase,
    KnowledgeBaseError,
    PassthroughMarkdownDistiller,
    ProposalBlockedError,
    ProposalPipeline,
    ProposalPipelineError,
    SourceProvenance,
    TextMarkdownSourceProcessor,
    is_reviewable_proposal,
    load_knowledge_base,
    seeded_control_file,
    validate,
    write_control_file,
)

# Derived-state directory name inside a Knowledge Base root. Holds the
# filesystem-backed ingest store (proposal JSON) and the materialized graph
# artifact. It is not a Compiled Page tree: the Markdown walker only scans
# ``.md`` files, so the ``.json`` proposals and graph artifact under here are
# invisible to loading, validation, and fingerprinting.
DERIVED_DIR_NAME = ".lumio"
DEFAULT_INGEST_SUBDIR = "ingest"
DEFAULT_INDEX_SUBDIR = "index"


class CliError(Exception):
    """A CLI command failed with a user-facing message and exit code."""


def default_ingest_dir(kb_root: str | Path) -> Path:
    """Return the default ingest-store directory for a Knowledge Base root."""
    return Path(kb_root, DERIVED_DIR_NAME, DEFAULT_INGEST_SUBDIR)


def default_index_dir(kb_root: str | Path) -> Path:
    """Return the default derived-index directory for a Knowledge Base root."""
    return Path(kb_root, DERIVED_DIR_NAME, DEFAULT_INDEX_SUBDIR)


def _resolve_ingest_dir(args: argparse.Namespace, kb_root: Path) -> Path:
    raw = getattr(args, "ingest_dir", None)
    return Path(raw) if raw else default_ingest_dir(kb_root)


def _resolve_index_dir(args: argparse.Namespace, kb_root: Path) -> Path:
    raw = getattr(args, "index_dir", None)
    return Path(raw) if raw else default_index_dir(kb_root)


def _load_kb(path: str | Path) -> tuple[KnowledgeBase, object]:
    """Load a Knowledge Base and return ``(kb, report)`` or raise :class:`CliError`."""
    try:
        return load_knowledge_base(path)
    except (KnowledgeBaseError, ControlFileError) as exc:
        raise CliError(f"could not load Knowledge Base at {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    target.mkdir(parents=True, exist_ok=True)
    if (target / "lumio.yaml").exists():
        raise CliError(f"Knowledge Base already exists at {target} (lumio.yaml present)")
    write_control_file(target, seeded_control_file())
    # Pre-create the derived-state directory so a fresh ``health`` command
    # has somewhere deterministic to materialize the graph artifact.
    default_ingest_dir(target).mkdir(parents=True, exist_ok=True)
    default_index_dir(target).mkdir(parents=True, exist_ok=True)
    print(f"Initialized empty Knowledge Base at {target}")
    print(f"  control file:   {target / 'lumio.yaml'}")
    print(f"  ingest store:   {default_ingest_dir(target)}")
    print(f"  derived index:  {default_index_dir(target)}")
    print("Add Compiled Pages (.md) under this directory, then run `lumio-wiki validate`.")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate(args.path)
    print(report)
    return 0 if report.is_valid else 1


def _cmd_search(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    results = kb.search_pages(args.query, limit=args.limit)
    if not results:
        print("No pages matched the query.")
        return 0
    for result in results:
        page = result.page
        print(f"## {page.title}")
        if page.summary:
            print(f"summary: {page.summary}")
        print(f"path:    {page.path}")
        print(f"score:   {result.score}")
        if result.matched_fields:
            print(f"matched: {', '.join(result.matched_fields)}")
        if result.snippet:
            print(f"snippet: {result.snippet}")
        print()
    return 0


def _cmd_page(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    pages = kb.lookup_by_title(args.title)
    if not pages:
        # Fall back to alias resolution so the command reads naturally.
        pages = kb.lookup_by_alias(args.title)
    if not pages:
        print(f"No Compiled Page found for title or alias: {args.title}", file=sys.stderr)
        return 1
    for page in pages:
        print(f"# {page.title}")
        print(f"path:        {page.path}")
        if page.aliases:
            print(f"aliases:     {', '.join(page.aliases)}")
        if page.tags:
            print(f"tags:        {', '.join(page.tags)}")
        if page.summary:
            print(f"summary:     {page.summary}")
        if page.lifecycle:
            print(f"lifecycle:   {page.lifecycle}")
        if page.visibility:
            print(f"visibility:  {page.visibility}")
        if page.sources:
            for source in page.sources:
                url_part = f" ({source.url})" if source.url else ""
                print(f"source:      {source.id} — {source.title}{url_part}")
        if page.synthetic:
            print("synthetic:   true")
        if page.relationships:
            print("relationships:")
            for rel in page.relationships:
                print(f"  - {rel.type}: {rel.target}")
        print()
        print(page.body)
    return 0


def _cmd_related(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    titles = kb.related_pages(
        args.title,
        direction=args.direction,
        scope=args.scope,
        relationship_type=args.relationship_type,
        max_depth=args.depth,
    )
    if not titles:
        print(f"No related pages found for: {args.title}")
        return 0
    for title in titles:
        print(title)
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    path = kb.shortest_path(
        args.source,
        args.target,
        direction=args.direction,
        scope=args.scope,
    )
    if not path:
        print(
            f"No path found from {args.source!r} to {args.target!r} "
            f"(direction={args.direction}, scope={args.scope}).",
            file=sys.stderr,
        )
        return 1
    print(" -> ".join(path))
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    source_path = Path(args.file)
    if not source_path.is_file():
        raise CliError(f"source file not found: {source_path}")
    raw_bytes = source_path.read_bytes()
    content_type = args.content_type
    if content_type is None:
        suffix = source_path.suffix.lower()
        content_type = "text/markdown" if suffix in {".md", ".markdown"} else "text/plain"

    ingest_dir = _resolve_ingest_dir(args, kb.root)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    store = IngestStore(ingest_dir)

    # The base wheel handles only text and Markdown Knowledge Sources. A
    # non-text file requires a document converter (LiteParse / MarkItDown);
    # name the exact extra the user must install rather than attempting a
    # UTF-8 decode that would produce a garbage proposal (ADR-0010, PRD #93
    # user story 18).
    document_suffixes = {".pdf", ".docx", ".doc", ".html", ".htm", ".png", ".jpg",
                         ".jpeg", ".gif", ".bmp", ".tiff", ".xls", ".xlsx",
                         ".ppt", ".pptx", ".odt", ".ods", ".odp"}
    if source_path.suffix.lower() in document_suffixes and not _detect_module("liteparse"):
        raise CliError(
            f"cannot ingest {source_path.suffix} file with the base install; "
            f"install the document-converter extra:\n"
            f"  pip install 'lumio-wiki[documents]'"
        )

    # Stage through the public pipeline WITHOUT persisting raw bytes into the
    # ingest store. The default ingest store lives under ``<kb>/.lumio/ingest``
    # (inside the KB root), and a raw ``.md`` upload saved there would be
    # scanned by the page loader — ``apply_proposed_pages`` would then resolve
    # the canonical page path to the raw source file and overwrite it instead
    # of creating the authored page. The source file already lives on the
    # user's filesystem; the proposal JSON carries the distilled Markdown and
    # provenance, which is everything the review surface needs. Callers who
    # need raw-byte isolation can point ``--ingest-dir`` outside the KB root
    # and use ``create_proposal_without_provider`` directly.
    normalized = TextMarkdownSourceProcessor().process(
        source_path.name, content_type, raw_bytes
    )
    provenance = SourceProvenance(
        original_filename=source_path.name,
        content_type=content_type,
        converted_by=normalized.converted_by,
        source_hash=normalized.source_hash,
    )
    distilled = PassthroughMarkdownDistiller().distill(normalized)
    pipeline = ProposalPipeline(kb, store=store)
    proposal = pipeline.assemble(distilled, provenance, source_path.name)
    # Stage without raw_bytes so no .md file is written inside the KB root.
    proposal = pipeline.stage(proposal)
    print(f"Staged proposal {proposal.id}")
    print(f"  status:         {proposal.status}")
    print(f"  converted_by:   {proposal.provenance.converted_by}")
    print(f"  affected_pages: {', '.join(proposal.affected_pages) or '(none)'}")
    print(f"  blocked:        {proposal.blocked}")
    if proposal.blast_radius is not None:
        br = proposal.blast_radius
        if br.new_titles:
            print(f"  new_titles:     {', '.join(br.new_titles)}")
        if br.changed_titles:
            print(f"  changed_titles: {', '.join(br.changed_titles)}")
        if br.category_moves:
            print(f"  category_moves: {', '.join(br.category_moves)}")
    print()
    print("Review with:")
    print(f"  lumio-wiki proposal inspect {args.path} {proposal.id}")
    print(f"  lumio-wiki proposal validate {args.path} {proposal.id}")
    if is_reviewable_proposal(proposal) and not proposal.blocked:
        print(f"  lumio-wiki publish {args.path} {proposal.id}")
    return 0


def _proposal_pipeline(args: argparse.Namespace):
    """Load the KB and construct a ProposalPipeline over the resolved ingest store."""
    kb, _report = _load_kb(args.path)
    ingest_dir = _resolve_ingest_dir(args, kb.root)
    store = IngestStore(ingest_dir)
    return kb, ProposalPipeline(kb, store=store)


def _cmd_proposal_list(args: argparse.Namespace) -> int:
    kb, pipeline = _proposal_pipeline(args)
    proposals = pipeline.list()
    if not proposals:
        print("No staged proposals.")
        return 0
    for proposal in proposals:
        print(
            f"{proposal.id}  {proposal.status:10}  blocked={proposal.blocked}  "
            f"pages={','.join(proposal.affected_pages) or '(none)'}"
        )
    return 0


def _encode_proposal(proposal) -> str:
    """Serialize a proposal to indented JSON via the canonical msgspec codec."""
    import msgspec

    return msgspec.json.format(msgspec.json.encode(proposal), indent=2).decode("utf-8")
def _cmd_proposal_inspect(args: argparse.Namespace) -> int:
    _kb, pipeline = _proposal_pipeline(args)
    proposal = pipeline.review(args.proposal_id)
    if proposal is None:
        print(f"No proposal found with id: {args.proposal_id}", file=sys.stderr)
        return 1
    if args.json:
        print(_encode_proposal(proposal))
        return 0
    print(f"id:              {proposal.id}")
    print(f"status:          {proposal.status}")
    print(f"created_at:      {proposal.created_at}")
    print(f"blocked:         {proposal.blocked}")
    print(f"affected_pages:  {', '.join(proposal.affected_pages) or '(none)'}")
    print(f"converted_by:    {proposal.provenance.converted_by}")
    if proposal.provenance.original_filename:
        print(f"original_file:   {proposal.provenance.original_filename}")
    if proposal.raw_source_path:
        print(f"raw_source:      {proposal.raw_source_path}")
    if proposal.blast_radius is not None:
        br = proposal.blast_radius
        print(
            f"blast_radius:    new={len(br.new_titles)} changed={len(br.changed_titles)} "
            f"dup_title={len(br.duplicate_title_risks)} moves={len(br.category_moves)}"
        )
    print()
    print("Diff:")
    print(proposal.diff or "(no textual diff)")
    return 0


def _cmd_proposal_validate(args: argparse.Namespace) -> int:
    _kb, pipeline = _proposal_pipeline(args)
    proposal = pipeline.review(args.proposal_id)
    if proposal is None:
        print(f"No proposal found with id: {args.proposal_id}", file=sys.stderr)
        return 1
    print(proposal.validation_report)
    return 0 if proposal.validation_report.is_valid else 1


def _cmd_publish(args: argparse.Namespace) -> int:
    _kb, pipeline = _proposal_pipeline(args)
    try:
        published = pipeline.publish(args.proposal_id)
    except ProposalBlockedError as exc:
        print(f"Proposal is blocked by validation: {exc}", file=sys.stderr)
        return 1
    except ProposalPipelineError as exc:
        print(f"Could not publish proposal: {exc}", file=sys.stderr)
        return 1
    print(f"Published proposal {published.id}")
    print(f"  status:         {published.status}")
    print(f"  affected_pages: {', '.join(published.affected_pages) or '(none)'}")
    return 0


def _cmd_discard(args: argparse.Namespace) -> int:
    _kb, pipeline = _proposal_pipeline(args)
    discarded = pipeline.discard(args.proposal_id)
    if discarded is None:
        print(
            f"Could not discard proposal {args.proposal_id} (absent or already terminal).",
            file=sys.stderr,
        )
        return 1
    print(f"Discarded proposal {discarded.id} (status={discarded.status}).")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    report = validate(args.path)
    try:
        kb, _load_report = load_knowledge_base(args.path)
    except (KnowledgeBaseError, ControlFileError) as exc:
        print(f"validation: {report}")
        print(f"load_error: {exc}", file=sys.stderr)
        return 1
    index_dir = _resolve_index_dir(args, kb.root)
    index_dir.mkdir(parents=True, exist_ok=True)
    graph_health = kb.graph_health(index_dir)
    page_count = len(kb.pages)
    public_count = len(kb.public_pages())
    print(f"path:                {kb.root}")
    print(f"mode:                {kb.control.mode if kb.control else 'legacy'}")
    print(f"pages:               {page_count}")
    print(f"public_pages:        {public_count}")
    print(f"valid:               {report.is_valid}")
    issues = [i for i in report.issues if i.severity == "error"]
    warnings = [i for i in report.issues if i.severity == "warning"]
    print(f"validation_errors:   {len(issues)}")
    print(f"validation_warnings: {len(warnings)}")
    print(f"graph_fresh:         {graph_health.graph_fresh}")
    print(f"graph_materialized:  {graph_health.materialized}")
    print(f"graph_edges:         {graph_health.edge_count}")
    if graph_health.materialized_size_bytes is not None:
        print(f"graph_artifact_bytes: {graph_health.materialized_size_bytes}")
    print(f"fingerprint:         {graph_health.fingerprint_digest}")
    for issue in issues:
        print(f"  ERROR: {issue.file}: {issue.field}: {issue.message}")
    for issue in warnings:
        print(f"  WARN:  {issue.file}: {issue.field}: {issue.message}")
    return 0 if report.is_valid else 1


def _detect_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _cmd_doctor(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import resolve_skill_path

    print(f"lumio-wiki {lumio_wiki.__version__}")
    print(f"python:    {sys.version.split()[0]}")
    optionals = {
        "documents": _detect_module("liteparse") and _detect_module("markitdown"),
        "llm": _detect_module("openai"),
        "lancedb": _detect_module("lancedb"),
    }
    extra_hint = {
        "documents": "pip install 'lumio-wiki[documents]'",
        "llm": "pip install 'lumio-wiki[llm]'",
        "lancedb": "pip install lumio-lancedb",
    }
    for name, present in optionals.items():
        if present:
            print(f"extra[{name}]: installed")
        else:
            print(f"extra[{name}]: not installed  ({extra_hint[name]})")
    skill_path = resolve_skill_path()
    print(f"skill_md:      {skill_path}")
    print(f"skill_exists:  {skill_path.is_file()}")
    return 0


# ---------------------------------------------------------------------------
# Skill subcommands
# ---------------------------------------------------------------------------


def _cmd_skill_path(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import resolve_skill_path

    skill_path = resolve_skill_path()
    print(skill_path)
    if not skill_path.is_file():
        print(f"warning: packaged skill not found at {skill_path}", file=sys.stderr)
        return 1
    return 0


def _cmd_skill_protocol(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import resolve_protocol_path

    protocol_path = resolve_protocol_path()
    print(protocol_path)
    if not protocol_path.is_file():
        print(f"warning: packaged protocol not found at {protocol_path}", file=sys.stderr)
        return 1
    return 0


def _cmd_skill_install(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import SkillError, install_skill

    try:
        target = install_skill(agent=args.agent, dest=args.dest, overwrite=args.overwrite)
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Installed lumio-wiki Agent Skill for {args.agent!r}:")
    print(f"  {target}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------


def _add_kb_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", type=Path, help="Knowledge Base root directory.")


def _add_ingest_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ingest-dir",
        type=Path,
        default=None,
        help=(
            f"Ingest store directory (default: <kb>/{DERIVED_DIR_NAME}/"
            f"{DEFAULT_INGEST_SUBDIR})."
        ),
    )


def _add_index_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help=(
            f"Derived index directory (default: <kb>/{DERIVED_DIR_NAME}/"
            f"{DEFAULT_INDEX_SUBDIR})."
        ),
    )


def _add_graph_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=["canonical", "discovery"],
        default="canonical",
        help="Graph scope: canonical Relationships only, or discovery (canonical + "
        "Extracted References). Default: canonical.",
    )
    parser.add_argument(
        "--direction",
        choices=["outgoing", "incoming", "both"],
        default="outgoing",
        help="Edge direction to follow. Default: outgoing.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``lumio-wiki`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="lumio-wiki",
        description=(
            "Portable Lumio Knowledge Base toolkit. Initialize, validate, search, "
            "read, traverse, ingest, review, and publish a compiled Markdown "
            "Knowledge Base. Model-free by default: the host coding agent is the "
            "Distiller."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {lumio_wiki.__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=False, metavar="<command>")

    # init
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new categorized Knowledge Base at a path.",
        description="Create a new categorized Knowledge Base root with a seeded Control File.",
    )
    init_parser.add_argument("path", type=Path, help="Directory to initialize (created if absent).")
    init_parser.set_defaults(func=_cmd_init)

    # validate
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a Knowledge Base.",
        description="Load and validate every page, Control File, link, and reserved artifact.",
    )
    _add_kb_argument(validate_parser)
    validate_parser.set_defaults(func=_cmd_validate)

    # search
    search_parser = subparsers.add_parser(
        "search",
        help="Lexical page search over titles, aliases, tags, summaries, and bodies.",
        description="Deterministic zero-index lexical search. No external index required.",
    )
    _add_kb_argument(search_parser)
    search_parser.add_argument("query", type=str, help="Search query.")
    search_parser.add_argument("--limit", type=int, default=20, help="Max results (default: 20).")
    search_parser.set_defaults(func=_cmd_search)

    # page
    page_parser = subparsers.add_parser(
        "page",
        help="Read a Compiled Page by Canonical Page Title (or alias).",
        description="Print the full Compiled Page (frontmatter + body) for a title or alias.",
    )
    _add_kb_argument(page_parser)
    page_parser.add_argument("title", type=str, help="Canonical Page Title (or alias).")
    page_parser.set_defaults(func=_cmd_page)

    # related
    related_parser = subparsers.add_parser(
        "related",
        help="List pages related to a Canonical Page Title.",
        description="Traverse typed Relationships (and optionally Extracted References) "
        "from a page title.",
    )
    _add_kb_argument(related_parser)
    related_parser.add_argument("title", type=str, help="Canonical Page Title.")
    related_parser.add_argument(
        "--relationship-type",
        type=str,
        default=None,
        help="Restrict to a single Relationship type.",
    )
    related_parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Max traversal depth in hops (default: 1).",
    )
    _add_graph_scope_arguments(related_parser)
    related_parser.set_defaults(func=_cmd_related)

    # paths
    paths_parser = subparsers.add_parser(
        "paths",
        help="Find the shortest typed path between two Canonical Page Titles.",
        description="Graph traversal: shortest directed path from source to target.",
    )
    _add_kb_argument(paths_parser)
    paths_parser.add_argument("source", type=str, help="Source Canonical Page Title.")
    paths_parser.add_argument("target", type=str, help="Target Canonical Page Title.")
    _add_graph_scope_arguments(paths_parser)
    paths_parser.set_defaults(func=_cmd_paths)

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Ingest a text or Markdown Knowledge Source into a staged proposal.",
        description=(
            "Read a text or Markdown file, distill it through the model-free "
            "PassthroughMarkdownDistiller (the host coding agent is the Distiller), "
            "and stage a reviewable Ingest Proposal. No OpenAI client required."
        ),
    )
    _add_kb_argument(ingest_parser)
    ingest_parser.add_argument("file", type=Path, help="Path to the text or Markdown source.")
    ingest_parser.add_argument(
        "--content-type",
        type=str,
        default=None,
        help="Override content type (default: inferred from file extension).",
    )
    _add_ingest_dir_argument(ingest_parser)
    ingest_parser.set_defaults(func=_cmd_ingest)

    # proposal (nested)
    proposal_parser = subparsers.add_parser(
        "proposal",
        help="Inspect, validate, or list staged Ingest Proposals.",
        description="Review staged proposals before publishing or discarding.",
    )
    proposal_sub = proposal_parser.add_subparsers(
        dest="proposal_command", required=False, metavar="<proposal-command>"
    )

    proposal_list = proposal_sub.add_parser(
        "list",
        help="List staged proposals.",
        description="List staged proposals in the ingest store.",
    )
    _add_kb_argument(proposal_list)
    _add_ingest_dir_argument(proposal_list)
    proposal_list.set_defaults(func=_cmd_proposal_list)

    proposal_inspect = proposal_sub.add_parser(
        "inspect",
        help="Inspect a staged proposal by id.",
        description="Print proposal metadata, blast radius, and diff.",
    )
    _add_kb_argument(proposal_inspect)
    proposal_inspect.add_argument("proposal_id", type=str, help="Proposal id.")
    proposal_inspect.add_argument(
        "--json",
        action="store_true",
        help="Emit the proposal as JSON instead of the human-readable summary.",
    )
    _add_ingest_dir_argument(proposal_inspect)
    proposal_inspect.set_defaults(func=_cmd_proposal_inspect)

    proposal_validate = proposal_sub.add_parser(
        "validate",
        help="Print the validation report for a staged proposal.",
        description="Print the proposal's validation report.",
    )
    _add_kb_argument(proposal_validate)
    proposal_validate.add_argument("proposal_id", type=str, help="Proposal id.")
    _add_ingest_dir_argument(proposal_validate)
    proposal_validate.set_defaults(func=_cmd_proposal_validate)

    # publish
    publish_parser = subparsers.add_parser(
        "publish",
        help="Publish a reviewable proposal to the Knowledge Base.",
        description="Apply a proposal's pages to the KB root and mark it terminal.",
    )
    _add_kb_argument(publish_parser)
    publish_parser.add_argument("proposal_id", type=str, help="Proposal id to publish.")
    _add_ingest_dir_argument(publish_parser)
    publish_parser.set_defaults(func=_cmd_publish)

    # discard
    discard_parser = subparsers.add_parser(
        "discard",
        help="Discard a reviewable proposal.",
        description="Mark a reviewable proposal as discarded (terminal).",
    )
    _add_kb_argument(discard_parser)
    discard_parser.add_argument("proposal_id", type=str, help="Proposal id to discard.")
    _add_ingest_dir_argument(discard_parser)
    discard_parser.set_defaults(func=_cmd_discard)

    # health
    health_parser = subparsers.add_parser(
        "health",
        help="Knowledge Base health and diagnostics.",
        description="Report page counts, validation status, and Discovery Graph health.",
    )
    _add_kb_argument(health_parser)
    _add_index_dir_argument(health_parser)
    health_parser.set_defaults(func=_cmd_health)

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Install diagnostics: version, optionals, and packaged skill location.",
        description="Print version, detected optional capabilities, and skill path.",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    # skill (nested)
    skill_parser = subparsers.add_parser(
        "skill",
        help="Locate or install the packaged Agent Skill.",
        description="Resolve the packaged Agent Skill path or install it for a coding agent.",
    )
    skill_sub = skill_parser.add_subparsers(
        dest="skill_command", required=False, metavar="<skill-command>"
    )

    skill_path = skill_sub.add_parser(
        "path",
        help="Print the absolute path of the packaged SKILL.md.",
        description="Print the absolute path of the packaged SKILL.md inside the installed wheel.",
    )
    skill_path.set_defaults(func=_cmd_skill_path)

    skill_protocol = skill_sub.add_parser(
        "protocol",
        help="Print the absolute path of the packaged coding-agent protocol.",
        description="Print the absolute path of the packaged protocol document.",
    )
    skill_protocol.set_defaults(func=_cmd_skill_protocol)

    skill_install = skill_sub.add_parser(
        "install",
        help="Install the packaged Agent Skill for a supported coding agent.",
        description=(
            "Copy the packaged Agent Skill into a coding agent's skill directory. "
            "Supported agents: pi, hermes, codex, claude-code."
        ),
    )
    skill_install.add_argument(
        "--agent",
        required=True,
        choices=["pi", "hermes", "codex", "claude-code"],
        help="Target coding agent.",
    )
    skill_install.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Override destination directory (default: agent's conventional skill dir).",
    )
    skill_install.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing skill at the destination.",
    )
    skill_install.set_defaults(func=_cmd_skill_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
