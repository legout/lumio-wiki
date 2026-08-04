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
``validate [path]``                               :func:`lumio_wiki.validate`
``search [path] <query>``                         :meth:`KnowledgeBase.search_pages`
``page [path] <title>``                           :meth:`KnowledgeBase.lookup_by_title`
``related [path] <title>``                        :meth:`KnowledgeBase.related_pages`
``paths [path] <source> <target>``               :meth:`KnowledgeBase.shortest_path`
``ingest [path] <file>``                          :func:`create_proposal_without_provider`
``proposal list [path]``                          :meth:`ProposalPipeline.list`
``proposal inspect [path] <id>``                  :meth:`ProposalPipeline.review`
``proposal validate [path] <id>``                 proposal validation report
``publish [path] <id>``                           :meth:`ProposalPipeline.publish`
``publish-s3 [path] <dest> --version <v>``        :func:`lumio_wiki.publish_s3_version`
``discard [path] <id>``                           :meth:`ProposalPipeline.discard`
``health [path]``                                 :meth:`KnowledgeBase.graph_health` + validation
``doctor``                                        install diagnostics (optionals, skill path)
``skill path``                                    packaged skill location
``skill install --agent <name>``                  install skill for a supported coding agent
================================================  ================================================

The base Distiller is the host coding agent (``PassthroughMarkdownDistiller``):
no OpenAI client is required for text and Markdown ingestion.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import lumio_wiki
from lumio_wiki import (
    RETIREMENT_CANDIDATE_TRIGGERS,
    ControlFileError,
    Distiller,
    IngestStore,
    KnowledgeBase,
    KnowledgeBaseError,
    PassthroughMarkdownDistiller,
    ProposalBlockedError,
    ProposalPipeline,
    ProposalPipelineError,
    SourceProvenance,
    SourceRegistryError,
    generate_hot_index,
    generate_navigation_indexes,
    is_reviewable_proposal,
    load_knowledge_base,
    seeded_control_file,
    validate,
    write_control_file,
)
from lumio_wiki.env_loader import (
    KB_PATH_ENV_VAR,
    discover_kb_path_from_project_env,
)
from lumio_wiki.knowledge_base import (
    DEFAULT_GRAPH_MAX_DEPTH,
    DEFAULT_GRAPH_MAX_EDGES,
    DEFAULT_GRAPH_MAX_RESULTS,
    NAV_INDEX_BASENAME,
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
    """A CLI command failed with a user-facing message and exit code.

    ``exit_code`` defaults to ``2`` (the existing CLI convention for
    user-facing errors). Source-lifecycle handlers override it to ``1`` to
    preserve the verbatim not-found contract (issue #133).
    """

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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
# Object-store (S3) Knowledge Base Locations (issue #120, ADR-0013).
#
# A read command accepts either a local directory or an object-store URI
# (``s3://bucket/path``). S3 reads resolve one immutable Published Version
# through the Location seam and operate on it with zero-index retrieval — no
# LanceDB, no managed local copy. Credentials/region/endpoint come from the
# standard LUMIO_S3_* / AWS_* environment variables, never from lumio.yaml.
# ---------------------------------------------------------------------------


# The object-store URI schemes obstore supports as S3-compatible backends.
# Restricting to these prevents misrouting ``https://`` or ``file://`` paths.
_OBJECT_STORE_SCHEMES = frozenset({"s3", "s3a", "gs", "gcs", "az", "abfs"})


def _is_object_store_uri(value: object) -> bool:
    """Return whether ``value`` is a recognized object-store URI."""
    if not isinstance(value, str) or "://" not in value:
        return False
    scheme = value.split("://", 1)[0].lower()
    return scheme in _OBJECT_STORE_SCHEMES


def _s3_config_from_env() -> tuple[dict[str, str], dict[str, object]]:
    """Build obstore ``config``/``client_options`` from environment variables."""
    config: dict[str, str] = {}
    client_options: dict[str, object] = {}
    region = (
        os.environ.get("LUMIO_S3_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if region:
        config["aws_region"] = region
    endpoint = os.environ.get("LUMIO_S3_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL_S3")
    if endpoint:
        config["aws_endpoint"] = endpoint
        if endpoint.startswith("http://"):
            client_options["allow_http"] = True
    key = os.environ.get("LUMIO_S3_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("LUMIO_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key:
        config["aws_access_key_id"] = key
    if secret:
        config["aws_secret_access_key"] = secret
    return config, client_options


def _resolve_object_store_location(uri: str) -> Any:
    """Construct an S3 Knowledge Base Location from a URI + environment config."""
    from lumio_wiki.s3_location import S3Location

    config, client_options = _s3_config_from_env()
    return S3Location.from_url(uri, config=config or None, client_options=client_options or None)


def _build_publish_store(uri: str) -> tuple[object, str]:
    """Build an ``(obstore ObjectStore, prefix)`` from a destination URI + env.

    Mirrors :meth:`S3Location.from_url` store construction but returns the raw
    store and prefix so the publisher can write under the version prefix.
    """
    from urllib.parse import urlparse

    from lumio_wiki.s3_location import _require_obstore

    obstore = _require_obstore()
    parsed = urlparse(uri)
    if not parsed.scheme:
        raise CliError(f"not an object-store destination URI: {uri!r}")
    config, client_options = _s3_config_from_env()
    authority_url = f"{parsed.scheme}://{parsed.netloc}"
    try:
        store = obstore.store.from_url(
            authority_url,
            config=config or None,
            client_options=client_options or None,
        )
    except Exception as exc:
        raise CliError(f"could not build object store from {uri!r}: {exc}") from exc
    prefix = parsed.path.lstrip("/")
    return store, prefix


def _open_read_kb(path: str | Path) -> KnowledgeBase:
    """Open a Knowledge Base for read commands from a local path or S3 URI."""
    value = str(path)
    if _is_object_store_uri(value):
        try:
            return _resolve_object_store_location(value).resolve().knowledge_base
        except KnowledgeBaseError as exc:
            raise CliError(f"could not resolve S3 Knowledge Base at {value}: {exc}") from exc
    kb, _report = _load_kb(path)
    return kb


def _validate_location(path: str | Path):
    """Validate a local path or resolve+validate an S3 URI into a report."""
    value = str(path)
    if _is_object_store_uri(value):
        try:
            snapshot = _resolve_object_store_location(value).resolve()
        except KnowledgeBaseError as exc:
            raise CliError(f"could not resolve S3 Knowledge Base at {value}: {exc}") from exc
        return snapshot.validation_report
    return validate(path)


def _format_graph_trace(*, scope: str, direction: str, outcome_fields: list[str]) -> str:
    """Format the one-line ``# trace:`` diagnostic shared by related/paths.

    Reports ONLY what the traversal actually used: the graph scope, the edge
    direction, and the caller-supplied outcome fields (bounds and the
    returned/found result). Graph traversal is always served from the
    in-memory Discovery Graph, so the trace never implies a persisted
    artifact was the traversal source — that freshness signal belongs to the
    dedicated ``health`` command (issue #110, ADR-0011).
    """
    body = " ".join(outcome_fields)
    return f"# trace: scope={scope} direction={direction} {body}"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _cmd_setup(args: argparse.Namespace) -> int:
    """One-command project setup: KB + .env + AGENTS.md + optional skill install.

    Creates the Knowledge Base if it does not exist, writes a ``.env`` file
    recording the absolute KB path as ``LUMIO_KB_PATH``, writes/appends an
    AGENTS.md section with the retrieval ladder, and optionally installs the
    Agent Skill for a coding agent. After setup, the agent harness can run
    ``lumio-wiki search "query"`` with no path argument.
    """
    kb_path = Path(args.kb_path).resolve()
    project_dir = Path.cwd()
    created = False

    # 1. Create the KB if it does not exist.
    if (kb_path / "lumio.yaml").exists() or any(kb_path.glob("*.md")):
        print(f"Knowledge Base already exists at {kb_path}")
    else:
        kb_path.mkdir(parents=True, exist_ok=True)
        write_control_file(kb_path, seeded_control_file())
        default_ingest_dir(kb_path).mkdir(parents=True, exist_ok=True)
        default_index_dir(kb_path).mkdir(parents=True, exist_ok=True)
        print(f"Created Knowledge Base at {kb_path}")
        created = True

    # 2. Write .env (create or update the LUMIO_KB_PATH line).
    env_path = project_dir / ".env"
    _upsert_env_var(env_path, "LUMIO_KB_PATH", str(kb_path))
    print(f"  .env:           {env_path} (LUMIO_KB_PATH={kb_path})")

    # 3. Write/append AGENTS.md unless --no-agents-md.
    if not getattr(args, "no_agents_md", False):
        agents_md = project_dir / "AGENTS.md"
        _write_agents_md_section(agents_md, kb_path)
        print(f"  AGENTS.md:      {agents_md}")

    # 4. Optional skill install.
    if getattr(args, "agent", None):
        from lumio_wiki.skill import SkillError, install_skill

        try:
            target = install_skill(
                agent=args.agent,
                dest=None,
                overwrite=getattr(args, "overwrite", False),
            )
        except SkillError as exc:
            print(f"  skill install:  SKIPPED ({exc})", file=sys.stderr)
        else:
            print(f"  skill install:  {target}")

    print()
    print("Setup complete. Next steps:")
    if created:
        print(f"  1. Add Compiled Pages (.md) under {kb_path}")
        print("  2. Run: lumio-wiki validate")
    else:
        print("  1. Run: lumio-wiki validate")
    print("  2. Start your agent harness in this directory.")
    print("     It will read LUMIO_KB_PATH from .env and the protocol from AGENTS.md.")
    return 0


_AGENTS_MD_SECTION = """\
## Lumio Knowledge Base

This project uses a Lumio Knowledge Base for domain knowledge. The host coding
agent IS the default Distiller (no model provider needed for base ingestion).

**KB path:** `{kb_path}` (also in `.env` as `LUMIO_KB_PATH`; the CLI reads it
automatically when no `<kb>` argument is given).

### Retrieval ladder (cheapest-first, stop when you have Evidence)

0. `lumio-wiki hot` — Maintainer-pinned entry pages. Read first.
1. `lumio-wiki index [dir]` — generated Navigation Index (all pages by directory).
2. `lumio-wiki search "<query>"` — zero-index lexical search (no external index).
3. `lumio-wiki page "<title>"` — read a page to confirm and cite the exact passage.
4. `lumio-wiki related "<title>" --scope discovery` — related pages (canonical + extracted).
5. `lumio-wiki paths "<src>" "<dst>"` — shortest directed path between two titles.

### Ingest (you are the Distiller)

1. Author a Compiled Page (YAML frontmatter + Markdown body) in a temp file.
2. `lumio-wiki ingest <file>` — stages a reviewable Ingest Proposal.
3. `lumio-wiki proposal list` → `proposal inspect <id>` → `proposal validate <id>`.
4. `lumio-wiki publish <id>` (or `lumio-wiki discard <id>`).

### Maintenance (you are the Maintainer)

- `lumio-wiki lint` — read-only cross-page QA: validation, graph health, and
  canonical/discovery structural diagnostics with scope disclosure. Exit 1 if invalid.
- `lumio-wiki cross-link` — missing-link candidates ranked by Discovery Graph
  impact. Add `--stage` to stage reviewable repair proposals (never direct-writes).
- `lumio-wiki dream` — the Dream Cycle: read-only reflection (validation +
  health + structure + ranked candidates). Add `--stage [--limit N]` to stage
  the top repairs as ordinary Ingest Proposals for review. Add opt-in `--semantic`
  with the `[llm]` extra for semantic findings; it remains proposal-first.

### Guardrails

- **Cite or refuse.** Every domain claim cites a Compiled Page (title + path +
  passage). Unsupported claims return "not covered by this knowledge base."
- **Connectivity is not support.** Graph reachability selects pages to inspect;
  it never manufactures Evidence.
- **Proposal-first.** Validation always runs before publish. Never write `.md`
  files directly to the KB root.

### Diagnostics

- `lumio-wiki doctor` — version, detected extras, skill location.
- `lumio-wiki health` — page counts, validation, Discovery Graph health.
- `lumio-wiki validate` — exit 0 if valid, 1 otherwise.
- `lumio-wiki lint` — full QA report (superset of validate + structural diagnostics).
"""

_AGENTS_MD_MARKER = "<!-- lumio-wiki-kb -->"


def _write_agents_md_section(agents_md: Path, kb_path: Path) -> None:
    """Write or update the Lumio KB section in an AGENTS.md file.

    If the file already contains the marker comment, the existing section is
    replaced in place. Otherwise the section is appended.
    """
    section = _AGENTS_MD_MARKER + "\n" + _AGENTS_MD_SECTION.format(kb_path=kb_path)

    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8")
        if _AGENTS_MD_MARKER in content:
            # Replace the existing section: the marker through the next `##`
            # heading AFTER the section's own heading. The section the writer
            # emits starts with its own `## Lumio Knowledge Base` heading, so
            # the first `##` line after the marker belongs to the section
            # itself and must not terminate the replaced span (otherwise each
            # update prepends a fresh copy and duplicates the section).
            lines = content.split("\n")
            start = None
            end = len(lines)
            own_heading_seen = False
            for i, line in enumerate(lines):
                if _AGENTS_MD_MARKER in line:
                    start = i
                    continue
                if start is None:
                    continue
                if line.startswith("## "):
                    if not own_heading_seen:
                        own_heading_seen = True
                        continue
                    end = i
                    break
            if start is not None:
                lines = lines[:start] + section.split("\n") + lines[end:]
                agents_md.write_text("\n".join(lines), encoding="utf-8")
                return
        # Append
        agents_md.write_text(content.rstrip() + "\n\n" + section + "\n", encoding="utf-8")
    else:
        agents_md.write_text(section + "\n", encoding="utf-8")


def _upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Write or update a ``KEY=value`` line in a .env file."""
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    report = _validate_location(args.path)
    print(report)
    return 0 if report.is_valid else 1


def _cmd_search(args: argparse.Namespace) -> int:
    kb = _open_read_kb(args.path)
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
    kb = _open_read_kb(args.path)
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
    kb = _open_read_kb(args.path)
    titles = kb.related_pages(
        args.title,
        direction=args.direction,
        scope=args.scope,
        relationship_type=args.relationship_type,
        max_depth=args.depth,
        max_edges=args.max_edges,
        max_results=args.max_results,
    )
    if titles:
        for title in titles:
            print(title)
    else:
        print(f"No related pages found for: {args.title}")
    if args.trace:
        print(
            _format_graph_trace(
                scope=args.scope,
                direction=args.direction,
                outcome_fields=[
                    f"max_depth={args.depth}",
                    f"max_edges={args.max_edges}",
                    f"max_results={args.max_results}",
                    f"returned={len(titles)}",
                ],
            )
        )
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    kb = _open_read_kb(args.path)
    path = kb.shortest_path(
        args.source,
        args.target,
        direction=args.direction,
        scope=args.scope,
        max_depth=args.max_depth,
        max_edges=args.max_edges,
    )
    found = path is not None
    hops = max(len(path) - 1, 0) if found else 0
    if found:
        print(" -> ".join(path))
    if args.trace:
        print(
            _format_graph_trace(
                scope=args.scope,
                direction=args.direction,
                outcome_fields=[
                    f"max_depth={args.max_depth}",
                    f"max_edges={args.max_edges}",
                    f"found={'true' if found else 'false'}",
                    f"hops={hops}",
                ],
            )
        )
    if not found:
        print(
            f"No path found from {args.source!r} to {args.target!r} "
            f"(direction={args.direction}, scope={args.scope}).",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_hot(args: argparse.Namespace) -> int:
    """Print the Maintainer-pinned Hot Index (retrieval-ladder step 0).

    Generates the Hot Index in memory from the Control File pins so it is
    always current and available without a prior publish. This is the curated
    entry surface a coding agent consults first (issue #110, ADR-0011).
    """
    kb, _report = _load_kb(args.path)
    content = generate_hot_index(kb.control, kb.pages)
    if content is None:
        print(
            "No Hot Index pins configured. Add hot_index pins to lumio.yaml, "
            "or run 'lumio-wiki index' for the full Navigation Index."
        )
        return 0
    print(content, end="")
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    """Print a generated Navigation Index (retrieval-ladder step 1).

    With no ``dir`` argument prints the root Navigation Index (the exhaustive
    page catalog grouped by directory); with ``dir`` prints that directory's
    shallow index. Generated in memory so it is always current without a
    prior publish (issue #110, ADR-0011).
    """
    kb, _report = _load_kb(args.path)
    indexes = generate_navigation_indexes(kb.pages)
    rel_dir = args.dir or ""
    key = f"{rel_dir}/index.md" if rel_dir else NAV_INDEX_BASENAME
    if key not in indexes:
        print(f"No Navigation Index at {key!r}.", file=sys.stderr)
        return 1
    print(indexes[key], end="")
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
        if suffix in {".md", ".markdown"}:
            content_type = "text/markdown"
        else:
            content_type = "text/plain"

    ingest_dir = _resolve_ingest_dir(args, kb.root)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    store = IngestStore(ingest_dir)

    # Route through select_source_processor so text/Markdown uses the
    # dependency-free processor and document sources (PDF, image, DOCX, HTML,
    # ...) use LiteParse/MarkItDown from the [documents] extra (issue #100).
    # When the extra is absent, the processor raises MissingDocumentExtraError
    # with the exact install command; we surface it as an actionable CliError.
    from lumio_wiki.ingest import select_source_processor
    from lumio_wiki.source_processor import (
        MissingDocumentExtraError,
        SourceProcessorError,
    )

    processor = select_source_processor(source_path.name, content_type)

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
    try:
        normalized = processor.process(source_path.name, content_type, raw_bytes)
    except MissingDocumentExtraError as exc:
        raise CliError(str(exc)) from exc
    except SourceProcessorError as exc:
        raise CliError(str(exc)) from exc
    provenance = SourceProvenance(
        original_filename=source_path.name,
        content_type=content_type,
        converted_by=normalized.converted_by,
        source_hash=normalized.source_hash,
    )
    distiller = _build_distiller(args)
    if kb.control is not None:
        categories = [category.name for category in kb.control.categories]
    else:
        categories = None
    distilled = distiller.distill(normalized, categories=categories)
    # Document sources produce extracted text, not authored page Markdown.
    # Wrap it in minimal frontmatter so the Proposal Pipeline can process it
    # (same logic as create_proposal_without_provider, issue #100 AC3).
    if normalized.converted_by in ("liteparse", "markitdown"):
        from lumio_wiki.ingest import _ensure_page_frontmatter

        distilled = _ensure_page_frontmatter(distilled, source_path.name)
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
    import msgspec  # type: ignore[import-not-found]

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
    # issue #135: Page Removal disclosure (AC4) — removed titles, lost-support
    # reasons, affected notes, and location-bearing body-link repair candidates.
    for removal in proposal.removed_pages:
        print(f"removed_page:    {removal.title}")
        if removal.lost_support_reason:
            print(f"  lost_support:  {removal.lost_support_reason}")
        for note in removal.affected_claim_notes:
            print(f"  affected:      {note}")
    for candidate in proposal.body_link_repairs:
        print(
            f"body_link_repair: {candidate.source_title} -> {candidate.target_title} "
            f"({candidate.origin}, line {candidate.line_start}, {candidate.source_path})"
        )
    print()
    print("Diff:")
    print(proposal.diff or "(no textual diff)")
    return 0


def _cmd_remove_page(args: argparse.Namespace) -> int:
    """Stage an explicit Page Removal proposal (issue #135)."""
    _kb, pipeline = _proposal_pipeline(args)
    try:
        proposal = pipeline.propose_page_removal(args.title, reason=args.reason or "")
    except ProposalPipelineError as exc:
        raise CliError(str(exc), exit_code=1) from exc
    print(f"Staged proposal {proposal.id}")
    print(f"  status:         {proposal.status}")
    for removal in proposal.removed_pages:
        print(f"  removed_page:   {removal.title}")
        if removal.lost_support_reason:
            print(f"  lost_support:   {removal.lost_support_reason}")
    print(f"  repairs:        {len(proposal.proposed_pages)} dependent page(s)")
    print(f"  body_links:     {len(proposal.body_link_repairs)} repair candidate(s)")
    print(f"  blocked:        {proposal.blocked}")
    print()
    print("Review with:")
    print(f"  lumio-wiki proposal inspect {args.path} {proposal.id}")
    if is_reviewable_proposal(proposal) and not proposal.blocked:
        print(f"  lumio-wiki publish {args.path} {proposal.id}")
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


def _cmd_publish_s3(args: argparse.Namespace) -> int:
    """Publish a local Knowledge Base as an immutable S3 Published Version.

    Thin, model-free orchestration: validate + write canonical content and the
    derived Discovery Graph under an immutable version prefix, then
    conditionally advance the active pointer. Credentials/region/endpoint come
    from the standard LUMIO_S3_* / AWS_* environment variables.
    """
    from lumio_wiki.s3_location import _require_obstore
    from lumio_wiki.s3_publish import S3PublicationConflict, publish_s3_version

    root = Path(args.path)
    if not root.is_dir():
        raise CliError(f"Knowledge Base source is not a directory: {root}")
    try:
        _require_obstore()
    except KnowledgeBaseError as exc:
        raise CliError(str(exc)) from exc
    store, prefix = _build_publish_store(args.destination)
    try:
        manifest = publish_s3_version(
            store,
            prefix,
            source_root=root,
            version=args.version,
            expected_pointer_version=args.expected_pointer_version,
        )
    except S3PublicationConflict as exc:
        raise CliError(f"publication conflict (pointer not advanced): {exc}") from exc
    except KnowledgeBaseError as exc:
        raise CliError(f"publication failed: {exc}") from exc
    print(f"Published {manifest.version}: {len(manifest.files)} canonical file(s)")
    print(f"  fingerprint: {manifest.fingerprint}")
    print(f"  location:    {args.destination}@{manifest.version}")
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
    if getattr(args, "rebuild", False):
        kb.materialize_graph(index_dir)
        print("graph_rebuilt:       ok")
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
    if not graph_health.graph_fresh:
        print(
            f"graph_recovery:      run 'lumio-wiki health {args.path} --rebuild' "
            f"to materialize the Discovery Graph"
        )
    # Structural topology diagnostics (issue #126, ADR-0011): read-only,
    # model-free, distinct from the artifact/runtime observability above.
    # Discloses BOTH scopes so a Maintainer can distinguish missing reviewed
    # semantic Relationships (canonical) from missing navigation topology
    # (discovery). No fixed health threshold is applied.
    for scope_name in ("canonical", "discovery"):
        structural = kb.graph_diagnostics(scope=scope_name)
        print(f"structure_scope:     {structural.scope}")
        print(f"structure_pages:     {structural.page_count}")
        print(f"structure_edges:     {structural.edge_count}")
        print(f"structure_in_orphans:  {structural.inbound_orphan_count}")
        print(f"structure_out_orphans: {structural.outbound_orphan_count}")
        print(f"structure_components: {structural.weakly_connected_component_count}")
        print(f"structure_coverage:  {structural.largest_component_coverage:.4f}")
        if structural.unresolved_references:
            unresolved_total = sum(g.count for g in structural.unresolved_references)
            print(f"structure_unresolved: {unresolved_total}")
    print(f"fingerprint:         {graph_health.fingerprint_digest}")
    for issue in issues:
        print(f"  ERROR: {issue.file}: {issue.field}: {issue.message}")
    for issue in warnings:
        print(f"  WARN:  {issue.file}: {issue.field}: {issue.message}")
    return 0 if report.is_valid else 1


# ---------------------------------------------------------------------------
# Retrieval evaluation harness (issue #138): recall@k per pipeline stage
# against a versioned gold set, over the public retrieve seam. Model-free at
# the base layer; LanceDB stages run when installed.
# ---------------------------------------------------------------------------


def _cmd_eval(args: argparse.Namespace) -> int:
    from lumio_wiki import retrieval_eval

    kb = _open_read_kb(args.path)
    gold_path = args.gold_set
    if gold_path is None:
        candidate = Path(kb.root) / "gold_set.yaml"
        if candidate.exists():
            gold_path = candidate
        else:
            raise CliError(
                "no gold set provided. Pass --gold-set <file> (a YAML query->relevant "
                "titles set) or place gold_set.yaml beside the Knowledge Base."
            )
    gold_set = retrieval_eval.load_gold_set(gold_path)

    ks = tuple(args.k) if args.k else None
    embedder = None
    synonyms: dict[str, str] = {}
    if args.semantic:
        for pair in args.synonym or []:
            if "=" not in pair:
                raise CliError(f"--synonym expects KEY=VALUE, got {pair!r}")
            key, val = pair.split("=", 1)
            synonyms[key.strip()] = val.strip()
        embedder = retrieval_eval.DeterministicHashEmbedder(synonyms=synonyms or None)

    index_dir = Path(args.index_dir) if args.index_dir else None
    if args.no_lancedb:
        stages = [retrieval_eval.ZeroIndexLexicalStage(), retrieval_eval.GraphExpansionStage()]
        report = retrieval_eval.evaluate(kb, gold_set, stages=stages, ks=ks)
    else:
        # Inject the LanceDB adapter (loaded via importlib so lumio-wiki never
        # imports lumio-lancedb; ADR-0010). When installed and no index dir was
        # given, evaluate auto-builds a temp index so the default run shows what
        # each installed stage buys (issue #138).
        adapter = retrieval_eval.load_lancedb_adapter()
        report = retrieval_eval.evaluate(
            kb,
            gold_set,
            ks=ks,
            lancedb_index_dir=index_dir,
            embedder=embedder,
            lancedb_adapter=adapter,
        )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        print(report.to_table())
        skipped = [s for s in report.stages if not s.available]
        if skipped:
            names = ", ".join(s.name for s in skipped)
            print(f"\nSkipped (unavailable): {names}")
    return 0


# ---------------------------------------------------------------------------
# Maintainer workflows (ADR-0015): lint, cross-link, dream.
# ---------------------------------------------------------------------------


def _print_structure_summary(structural) -> None:
    """Print the structural-topology lines shared by lint and dream (#126)."""
    print(f"structure_scope:     {structural.scope}")
    print(f"structure_pages:     {structural.page_count}")
    print(f"structure_edges:     {structural.edge_count}")
    print(f"structure_in_orphans:  {structural.inbound_orphan_count}")
    print(f"structure_out_orphans: {structural.outbound_orphan_count}")
    print(f"structure_components: {structural.weakly_connected_component_count}")
    print(f"structure_coverage:  {structural.largest_component_coverage:.4f}")


def _print_validation_issues(report) -> None:
    for issue in report.issues:
        marker = "ERROR" if issue.severity == "error" else "WARN "
        print(f"  {marker}: {issue.file}: {issue.field}: {issue.message}")


def _cmd_lint(args: argparse.Namespace) -> int:
    """Read-only cross-page QA report with both graph scopes disclosed (ADR-0015).

    Composes the authoritative validation report, the Discovery Graph health,
    and the canonical/discovery structural diagnostics. Never writes; exits 1
    when the Knowledge Base is invalid.
    """
    kb, _load_report = _load_kb(args.path)
    index_dir = _resolve_index_dir(args, kb.root)
    report = lumio_wiki.run_lint(args.path, index_dir=index_dir)
    errors = [i for i in report.validation_report.issues if i.severity == "error"]
    warnings = [i for i in report.validation_report.issues if i.severity == "warning"]
    print(f"path:                {report.kb_path}")
    print(f"pages:               {report.page_count}")
    print(f"valid:               {report.is_valid}")
    print(f"validation_errors:   {len(errors)}")
    print(f"validation_warnings: {len(warnings)}")
    print(f"canonical_relationships: {len(report.canonical_relationships)}")
    print(f"extracted_references:  {len(report.extracted_references)}")
    print(f"graph_fresh:         {report.graph_health.graph_fresh}")
    print(f"graph_edges:         {report.graph_health.edge_count}")
    _print_structure_summary(report.canonical_structure)
    _print_structure_summary(report.discovery_structure)
    print(f"scope_disclosure:    {report.scope_disclosure}")
    _print_validation_issues(report.validation_report)
    return 0 if report.is_valid else 1


def _print_ranked_candidates(ranked, limit: int) -> None:
    for position, entry in enumerate(ranked[:limit], start=1):
        candidate = entry.candidate
        signals = ", ".join(signal.kind for signal in entry.signals) or "none"
        print(
            f"  {position}. [impact={entry.impact_score}, signals={signals}] "
            f"{candidate.source_title} -> {candidate.target_title}"
        )
        print(
            f"     {candidate.source_path}:{candidate.line}:{candidate.column} "
            f"term={candidate.term!r}"
        )
        if candidate.snippet:
            print(f"     snippet: {candidate.snippet}")


def _stage_candidates(args: argparse.Namespace, kb, ranked, limit: int):
    """Stage one reviewable repair proposal per candidate, bounded by limit."""
    ingest_dir = _resolve_ingest_dir(args, kb.root)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    store = IngestStore(ingest_dir)
    staged = []
    skipped = []
    # Intentional parallel structure with Dream Cycle's bounded stage-with-skip loop.
    for entry in ranked[:limit]:
        candidate = entry.candidate
        try:
            proposal = lumio_wiki.stage_cross_link_proposal(args.path, candidate, store=store)
            staged.append(proposal)
        except (lumio_wiki.MaintenanceError, OSError) as exc:
            skipped.append((candidate, str(exc)))
    return staged, skipped


def _print_staging_outcome(args, staged, skipped) -> None:
    print(f"staged_proposals:    {len(staged)}")
    for proposal in staged:
        print(f"  staged: {proposal.id} pages={', '.join(proposal.affected_pages)}")
    for candidate, reason in skipped:
        print(f"  skipped: {candidate.source_path}:{candidate.line} ({reason})")
    if staged:
        print()
        print("Review with:")
        print(f"  lumio-wiki proposal list {args.path}")
        for proposal in staged:
            if is_reviewable_proposal(proposal) and not proposal.blocked:
                print(f"  lumio-wiki publish {args.path} {proposal.id}")


def _cmd_cross_link(args: argparse.Namespace) -> int:
    """List deterministic missing-link candidates, ranked by graph impact.

    Read-only by default. With ``--stage``, stages one reviewable Ingest
    Proposal per top-ranked candidate (bounded by ``--limit``); nothing is
    published without an explicit Maintainer publish (ADR-0015).
    """
    kb, _report = _load_kb(args.path)
    candidates = lumio_wiki.find_link_candidates(kb.pages)
    ranked = kb.rank_link_candidates_by_graph_impact(candidates)
    if not ranked:
        print("No missing-link candidates.")
        return 0
    print(f"link_candidates:     {len(ranked)}")
    _print_ranked_candidates(ranked, args.limit)
    if len(ranked) > args.limit:
        print(f"  ... {len(ranked) - args.limit} more (raise --limit to show)")
    if args.stage:
        print()
        staged, skipped = _stage_candidates(args, kb, ranked, args.limit)
        _print_staging_outcome(args, staged, skipped)
    else:
        print()
        print("Stage repairs with:")
        print(f"  lumio-wiki cross-link {args.path} --stage")
    return 0


def _print_semantic_findings(findings) -> None:
    """Print semantic findings in stable kind groups for Maintainer review."""
    grouped = {kind: [] for kind in ("contradiction", "stale", "summary")}
    for finding in findings:
        grouped.setdefault(finding.kind, []).append(finding)
    for kind in (
        *(("contradiction", "stale", "summary")),
        *sorted(k for k in grouped if k not in {"contradiction", "stale", "summary"}),
    ):
        entries = grouped[kind]
        print(f"semantic_{kind}: {len(entries)}")
        for finding in entries:
            print(f"  - pages: {', '.join(finding.pages)}")
            print(f"    reason: {finding.reason}")
            if finding.lifecycle:
                print(f"    lifecycle: {finding.lifecycle}")
            for validation_error in finding.validation_errors:
                print(f"    validation: {validation_error}")
            if finding.summary:
                print(f"    summary: {finding.summary}")


def _cmd_dream(args: argparse.Namespace) -> int:
    """Run deterministic reflection, then optional semantic review and staging."""
    kb, _load_report = _load_kb(args.path)
    index_dir = _resolve_index_dir(args, kb.root)
    report = lumio_wiki.run_dream_cycle(args.path, index_dir=index_dir)
    lint = report.lint
    print("# Dream Cycle")
    print(f"path:                {lint.kb_path}")
    print(f"pages:               {lint.page_count}")
    print(f"valid:               {lint.is_valid}")
    print(f"canonical_relationships: {len(lint.canonical_relationships)}")
    print(f"extracted_references:  {len(lint.extracted_references)}")
    print(f"graph_fresh:         {lint.graph_health.graph_fresh}")
    _print_structure_summary(lint.canonical_structure)
    _print_structure_summary(lint.discovery_structure)
    print(f"link_candidates:     {report.candidate_count}")
    if report.ranked_candidates:
        print("top candidates (by Discovery Graph impact):")
        _print_ranked_candidates(report.ranked_candidates, args.limit)
    _print_validation_issues(lint.validation_report)

    if args.semantic:
        from lumio_wiki.semantic_maintenance import (
            MissingSemanticExtraError,
            SemanticDreamReviewer,
            SemanticMaintenanceError,
        )

        try:
            if args.semantic_limit < 1:
                raise CliError("--semantic-limit must be >= 1")
            reviewer = SemanticDreamReviewer(
                kb, model=os.environ.get("LUMIO_PROVIDER_MODEL"), max_pages=args.semantic_limit
            )
            semantic_report = reviewer.review()
        except (MissingSemanticExtraError, SemanticMaintenanceError) as exc:
            raise CliError(str(exc)) from exc
        print()
        print("# Semantic Dream Review")
        print(f"semantic_pages:       {len(semantic_report.pages)}")
        _print_semantic_findings(semantic_report.findings)
        if args.stage and semantic_report.findings:
            ingest_dir = _resolve_ingest_dir(args, kb.root)
            ingest_dir.mkdir(parents=True, exist_ok=True)
            result = reviewer.stage_findings(store=IngestStore(ingest_dir), report=semantic_report)
            print(f"semantic_staged_proposals: {len(result.staged)}")
            for proposal in result.staged:
                print(f"  staged: {proposal.id} pages={', '.join(proposal.affected_pages)}")
            for finding, reason in result.skipped:
                print(f"  skipped: {finding.kind} ({reason})")

    if args.stage and report.ranked_candidates:
        print()
        staged, skipped = _stage_candidates(args, kb, report.ranked_candidates, args.limit)
        _print_staging_outcome(args, staged, skipped)
    return 0 if lint.is_valid else 1


def _detect_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _build_distiller(args: argparse.Namespace) -> Distiller:
    """Construct the Distiller selected by ``--distiller`` (issue #101).

    ``passthrough`` (default) uses the model-free
    :class:`PassthroughMarkdownDistiller` so the base wheel's journey is
    unchanged. ``llm`` uses the unattended :class:`OpenAIDistiller` behind the
    ``[llm]`` extra; the OpenAI client is configured from the standard
    ``LUMIO_PROVIDER_*`` environment variables so an operator points the CLI
    at an OpenAI-compatible endpoint without extra CLI plumbing. When the
    ``[llm]`` extra is absent the actionable error names the exact install
    command (PRD user story 18).
    """
    choice = getattr(args, "distiller", None) or "passthrough"
    if choice == "passthrough":
        return PassthroughMarkdownDistiller()
    if choice == "llm":
        from lumio_wiki import OpenAIDistiller, OpenAIDistillerError

        # Detect the [llm] extra BEFORE validating provider configuration so a
        # base-install request for unattended distillation fails actionably
        # with the exact install command (ADR-0010, PRD user story 18), never
        # a masked missing-config error that hides the required extra.
        if not _detect_module("openai"):
            raise CliError(
                "cannot use --distiller llm with the base install; "
                "install the unattended-distillation extra:\n"
                "  pip install 'lumio-wiki[llm]'"
            )
        base_url = os.environ.get("LUMIO_PROVIDER_BASE_URL") or None
        model = os.environ.get("LUMIO_PROVIDER_MODEL")
        api_key = os.environ.get("LUMIO_PROVIDER_API_KEY") or None
        if not model:
            raise CliError(
                "--distiller llm requires LUMIO_PROVIDER_MODEL "
                "(set LUMIO_PROVIDER_BASE_URL / LUMIO_PROVIDER_API_KEY for an "
                "OpenAI-compatible endpoint)."
            )
        try:
            return OpenAIDistiller(model=model, base_url=base_url, api_key=api_key)
        except OpenAIDistillerError as exc:
            raise CliError(str(exc)) from exc
    raise CliError(f"unsupported distiller: {choice}")


def _cmd_doctor(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import resolve_skill_path

    print(f"lumio-wiki {lumio_wiki.__version__}")
    print(f"python:    {sys.version.split()[0]}")
    optionals = {
        "documents": _detect_module("liteparse") and _detect_module("markitdown"),
        "llm": _detect_module("openai"),
        "s3": _detect_module("obstore"),
        "lancedb": _detect_module("lancedb"),
    }
    extra_hint = {
        "documents": "pip install 'lumio-wiki[documents]'",
        "llm": "pip install 'lumio-wiki[llm]'",
        "s3": "pip install 'lumio-wiki[s3]'",
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
# source subcommands: private Knowledge Source lifecycle (issue #133)
#
# Explicit, stable Knowledge Source identities live in the private Source
# Registry (outside portable KB content). These handlers delegate every
# mutation to the ProposalPipeline so retirement/reactivation stage ordinary
# reviewable proposals (publication flips source state) and candidate signals
# never mutate support. Output is safe: only proposal id, source id, trigger,
# and ``status: page title`` impacts are disclosed — never raw source bytes,
# local file paths, registry internals, or credentials.
# ---------------------------------------------------------------------------


def _source_pipeline(args: argparse.Namespace):
    """Load the KB and construct a ProposalPipeline over the resolved ingest store.

    Returns ``(kb, pipeline)``: the CLI speaks only to the public
    :class:`ProposalPipeline` seam (including source reads via
    :meth:`ProposalPipeline.list_sources`) and never reaches into the ingest
    store's private registry directly.
    """
    kb, _report = _load_kb(args.path)
    ingest_dir = _resolve_ingest_dir(args, kb.root)
    store = IngestStore(ingest_dir)
    return kb, ProposalPipeline(kb, store=store)


def _read_source_file(args: argparse.Namespace) -> bytes:
    """Return the raw bytes of ``--file`` or raise a path-free :class:`CliError`.

    The supplied path is never disclosed and existence/read races are caught:
    a missing file or an :class:`OSError` during read becomes a single generic
    user-facing error (exit code 1) with no traceback and no local path.
    """
    source_path = Path(args.file)
    try:
        if not source_path.is_file():
            raise CliError("source file could not be read", exit_code=1)
        return source_path.read_bytes()
    except OSError:
        raise CliError("source file could not be read", exit_code=1) from None


def _report_source_lifecycle_proposal(proposal) -> None:
    """Print a source-lifecycle proposal with safe, aligned fields only."""
    change = proposal.source_change
    print(f"Staged proposal {proposal.id}")
    print(f"source_id:      {change.source_id}")
    print(f"trigger:        {change.trigger}")
    if change.impacts:
        print("impacts:")
        for impact in change.impacts:
            print(f"  {impact.status}: {impact.page_title}")
    else:
        print("impacts:         (none)")


def _cmd_source_register(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    raw_bytes = _read_source_file(args)
    try:
        pipeline.register_source(args.source_id, raw_bytes)
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    source = next(
        (item for item in pipeline.list_sources() if item.source_id == args.source_id),
        None,
    )
    if source is None:
        # Impossible state: ``register_source`` reported success yet the
        # public listing omits the source. Fail with a generic, path/secret-free
        # error instead of dereferencing an unprovable ``None``.
        raise CliError("registered source could not be confirmed", exit_code=1)
    print(f"Registered Knowledge Source {args.source_id!r}")
    print(f"  source_id:      {source.source_id}")
    print(f"  status:         {source.status}")
    print(f"  versions:       {len(source.versions)}")
    return 0


def _cmd_source_list(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    sources = pipeline.list_sources()
    if not sources:
        print("No registered Knowledge Sources.")
        return 0
    print(f"{'source_id':<16} {'status':<10} {'versions':<8}")
    for source in sources:
        print(f"{source.source_id:<16} {source.status:<10} {len(source.versions):<8}")
    return 0


def _cmd_source_retire(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    try:
        proposal = pipeline.retire_source(args.source_id)
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    _report_source_lifecycle_proposal(proposal)
    return 0


def _cmd_source_candidate(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    try:
        candidate = pipeline.record_retirement_candidate(args.source_id, args.trigger)
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    print(f"candidate_id:   {candidate.id}")
    print(f"source_id:      {candidate.source_id}")
    print(f"trigger:        {candidate.trigger}")
    print(f"status:         {candidate.status}")
    return 0


def _cmd_source_dismiss_candidate(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    try:
        candidate = pipeline.dismiss_retirement_candidate(
            args.candidate_id, expected_source_id=args.source_id
        )
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    print(f"candidate_id:   {candidate.id}")
    print(f"source_id:      {candidate.source_id}")
    print(f"status:         {candidate.status}")
    return 0


def _cmd_source_confirm_candidate(args: argparse.Namespace) -> int:
    """Confirm a Retirement Candidate by staging an ordinary retirement proposal.

    Delegates to :meth:`ProposalPipeline.confirm_retirement_candidate`, which
    validates that the candidate belongs to the named Knowledge Source before
    any staging/mutation. Output is the safe staged-proposal report (proposal
    id, source id, controlled trigger, and ``status: page title`` impacts) —
    never raw source bytes, local paths, registry internals, or credentials.
    """
    _kb, pipeline = _source_pipeline(args)
    try:
        proposal = pipeline.confirm_retirement_candidate(
            args.candidate_id, expected_source_id=args.source_id
        )
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    _report_source_lifecycle_proposal(proposal)
    return 0


def _cmd_source_reactivate(args: argparse.Namespace) -> int:
    _kb, pipeline = _source_pipeline(args)
    raw_bytes = _read_source_file(args)
    try:
        proposal = pipeline.reactivate_source(args.source_id, raw_bytes)
    except (SourceRegistryError, ProposalPipelineError) as exc:
        raise CliError(str(exc), exit_code=1) from exc
    _report_source_lifecycle_proposal(proposal)
    return 0


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------


def _resolve_default_kb_path() -> str | None:
    """Resolve a default KB path when no positional ``<kb>`` was given.

    Applies the documented precedence (issue #152, ADR-0017), minus the
    positional argument which argparse already handled:

    1. an exported process ``LUMIO_KB_PATH`` (never overwritten by a file); then
    2. ``LUMIO_KB_PATH`` from the nearest trusted project ``.env``.

    Returns ``None`` when neither source yields a value so :func:`main` can
    surface a single actionable error.
    """
    if KB_PATH_ENV_VAR in os.environ:
        exported = os.environ[KB_PATH_ENV_VAR]
        return exported if exported.strip() else None
    return discover_kb_path_from_project_env()


def _add_kb_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        type=str,
        help=(
            "Knowledge Base root directory, or an S3 object-store URI "
            "(s3://bucket/path) resolved as an immutable Published Version "
            "(issue #120; requires lumio-wiki[s3]). When omitted, resolved "
            "in order: an exported LUMIO_KB_PATH, then LUMIO_KB_PATH from the "
            "nearest project .env (issue #152; ADR-0017)."
        ),
    )


def _add_ingest_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ingest-dir",
        type=Path,
        default=None,
        help=(
            f"Ingest store directory (default: <kb>/{DERIVED_DIR_NAME}/{DEFAULT_INGEST_SUBDIR})."
        ),
    )


def _add_index_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help=(
            f"Derived index directory (default: <kb>/{DERIVED_DIR_NAME}/{DEFAULT_INDEX_SUBDIR})."
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

    # setup
    setup_parser = subparsers.add_parser(
        "setup",
        help="One-command project setup: KB + .env + AGENTS.md + optional skill.",
        description=(
            "Create a Knowledge Base (or use an existing one), write .env with "
            "LUMIO_KB_PATH, write/update AGENTS.md with the retrieval-ladder "
            "protocol, and optionally install the Agent Skill. After setup, "
            "subsequent lumio-wiki commands load LUMIO_KB_PATH from .env "
            "automatically, so no <kb> argument is needed (issue #152; "
            "ADR-0017)."
        ),
    )
    setup_parser.add_argument(
        "kb_path",
        type=Path,
        help="Knowledge Base root directory (created if it does not exist).",
    )
    setup_parser.add_argument(
        "--agent",
        choices=["pi", "hermes", "codex", "claude-code"],
        default=None,
        help="Install the Agent Skill for this coding agent after setup.",
    )
    setup_parser.add_argument(
        "--no-agents-md",
        action="store_true",
        help="Skip writing/updating the AGENTS.md section.",
    )
    setup_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing skill when --agent is given.",
    )
    setup_parser.set_defaults(func=_cmd_setup)

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
    related_parser.add_argument(
        "--max-edges",
        type=int,
        default=DEFAULT_GRAPH_MAX_EDGES,
        help=f"Max edges expanded before truncation (default: {DEFAULT_GRAPH_MAX_EDGES}).",
    )
    related_parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_GRAPH_MAX_RESULTS,
        help=f"Max titles returned (default: {DEFAULT_GRAPH_MAX_RESULTS}).",
    )
    related_parser.add_argument(
        "--trace",
        action="store_true",
        help="Print a truthful diagnostic line (scope/direction/bounds/outcome).",
    )
    related_parser.set_defaults(func=_cmd_related)

    # paths
    paths_parser = subparsers.add_parser(
        "paths",
        help="Find the shortest directed path between two Canonical Page Titles.",
        description="Graph traversal: shortest directed path from source to target.",
    )
    _add_kb_argument(paths_parser)
    paths_parser.add_argument("source", type=str, help="Source Canonical Page Title.")
    paths_parser.add_argument("target", type=str, help="Target Canonical Page Title.")
    _add_graph_scope_arguments(paths_parser)
    paths_parser.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_GRAPH_MAX_DEPTH,
        help=f"Max hops in the search bound (default: {DEFAULT_GRAPH_MAX_DEPTH}).",
    )
    paths_parser.add_argument(
        "--max-edges",
        type=int,
        default=DEFAULT_GRAPH_MAX_EDGES,
        help=f"Max edges expanded before the search gives up (default: {DEFAULT_GRAPH_MAX_EDGES}).",
    )
    paths_parser.add_argument(
        "--trace",
        action="store_true",
        help="Print a truthful diagnostic line (scope/direction/bounds/found/hops).",
    )
    paths_parser.set_defaults(func=_cmd_paths)

    # hot (retrieval-ladder step 0: curated Hot Index)
    hot_parser = subparsers.add_parser(
        "hot",
        help="Print the Maintainer-pinned Hot Index.",
        description=(
            "Generate and print the Hot Index from the Control File pins. The "
            "curated entry surface a coding agent consults first in the "
            "retrieval ladder (issue #110, ADR-0011)."
        ),
    )
    _add_kb_argument(hot_parser)
    hot_parser.set_defaults(func=_cmd_hot)

    # index (retrieval-ladder step 1: generated Navigation Indexes)
    index_parser = subparsers.add_parser(
        "index",
        help="Print a generated Navigation Index.",
        description=(
            "Generate and print a Navigation Index (root catalog by default, or "
            "a directory's shallow index). The generated navigation surface a "
            "coding agent consults second in the retrieval ladder (issue #110)."
        ),
    )
    _add_kb_argument(index_parser)
    index_parser.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Optional directory whose index to print (default: root index.md).",
    )
    index_parser.set_defaults(func=_cmd_index)

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Ingest a text or Markdown Knowledge Source into a staged proposal.",
        description=(
            "Read a text or Markdown file, distill it, and stage a reviewable "
            "Ingest Proposal. The default ``passthrough`` Distiller is "
            "model-free (the host coding agent is the Distiller); "
            "``--distiller llm`` uses the unattended OpenAI-compatible "
            "Distiller behind the ``lumio-wiki[llm]`` extra (issue #101)."
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
    ingest_parser.add_argument(
        "--distiller",
        type=str,
        default="passthrough",
        choices=["passthrough", "llm"],
        help=(
            "Distiller for the source: 'passthrough' (default, model-free) or "
            "'llm' (unattended OpenAI-compatible; requires lumio-wiki[llm] and "
            "LUMIO_PROVIDER_* env vars)."
        ),
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

    # publish-s3 (issue #121, ADR-0013)
    publish_s3_parser = subparsers.add_parser(
        "publish-s3",
        help="Publish a local Knowledge Base as an immutable S3 Published Version.",
        description=(
            "Validate and publish a local Knowledge Base as an immutable S3 "
            "Published Version (canonical content + Discovery Graph), then "
            "conditionally advance the active pointer. Requires lumio-wiki[s3]. "
            "Credentials/region/endpoint come from LUMIO_S3_* / AWS_* env vars."
        ),
    )
    _add_kb_argument(publish_s3_parser)
    publish_s3_parser.add_argument(
        "destination",
        type=str,
        help="Object-store destination URI (e.g. s3://bucket/kb).",
    )
    publish_s3_parser.add_argument(
        "--version",
        required=True,
        type=str,
        help="Immutable version label for the new Published Version.",
    )
    publish_s3_parser.add_argument(
        "--expected-pointer-version",
        default=None,
        type=str,
        help=(
            "Active version expected before advancing (compare-and-swap guard). "
            "Omit for the first publication."
        ),
    )
    publish_s3_parser.set_defaults(func=_cmd_publish_s3)

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

    # remove-page (issue #135): stage an explicit Page Removal proposal.
    remove_page_parser = subparsers.add_parser(
        "remove-page",
        help="Stage an explicit Page Removal proposal.",
        description=(
            "Stage an explicit, reviewed Page Removal proposal that excludes one "
            "Compiled Page from the next Published Version and repairs every "
            "canonical Relationship that would otherwise become invalid in the "
            "same proposal (issue #135)."
        ),
    )
    _add_kb_argument(remove_page_parser)
    remove_page_parser.add_argument("title", type=str, help="Canonical Page Title to remove.")
    remove_page_parser.add_argument(
        "--reason",
        type=str,
        default="",
        help="Page-level lost-support rationale (e.g. 'sole-source-lost').",
    )
    _add_ingest_dir_argument(remove_page_parser)
    remove_page_parser.set_defaults(func=_cmd_remove_page)

    # health
    health_parser = subparsers.add_parser(
        "health",
        help="Knowledge Base health and diagnostics.",
        description="Report page counts, validation status, and Discovery Graph health.",
    )
    _add_kb_argument(health_parser)
    _add_index_dir_argument(health_parser)
    health_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Materialize the Discovery Graph artifact (actionable recovery), then report.",
    )
    health_parser.set_defaults(func=_cmd_health)

    # eval — retrieval gold-set evaluation harness (issue #138).
    eval_parser = subparsers.add_parser(
        "eval",
        help="Recall@k evaluation of retrieval stages against a versioned gold set.",
        description=(
            "Run a versioned gold set (query -> expected relevant Canonical Page "
            "Titles) through the public retrieval seam and report recall@k per "
            "pipeline stage (zero-index lexical, Discovery Graph expansion, and "
            "LanceDB BM25/semantic/hybrid when installed). Model-free at the base "
            "layer: no LLM-as-judge, no network. Suitable as a regression gate for "
            "retrieval-touching changes (issue #138)."
        ),
    )
    _add_kb_argument(eval_parser)
    eval_parser.add_argument(
        "--gold-set",
        type=Path,
        default=None,
        help="Gold set YAML (query -> relevant titles). Defaults to <kb>/gold_set.yaml.",
    )
    eval_parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=None,
        help="Recall cutoffs to report (default: 1 3 5).",
    )
    eval_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of the recall table.",
    )
    eval_parser.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help="LanceDB index directory (default: a fresh temporary directory).",
    )
    eval_parser.add_argument(
        "--no-lancedb",
        action="store_true",
        help="Skip LanceDB stages even when the adapter is installed.",
    )
    eval_parser.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Enable LanceDB semantic/hybrid stages using a deterministic, offline "
            "hash embedder (no provider, no network)."
        ),
    )
    eval_parser.add_argument(
        "--synonym",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Collapse a paraphrase to a shared token for the --semantic embedder.",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    # lint
    lint_parser = subparsers.add_parser(
        "lint",
        help="Read-only cross-page QA report with graph scope disclosed.",
        description="Report validation, Discovery Graph health, and canonical/discovery "
        "structural diagnostics. Read-only; exits 1 when the Knowledge Base is invalid "
        "(ADR-0015).",
    )
    _add_kb_argument(lint_parser)
    _add_index_dir_argument(lint_parser)
    lint_parser.set_defaults(func=_cmd_lint)

    # cross-link
    cross_link_parser = subparsers.add_parser(
        "cross-link",
        help="List missing-link candidates, ranked by Discovery Graph impact.",
        description="Surface deterministic missing-link candidates (issue #90), ranked by "
        "Discovery Graph impact (issue #127). Read-only unless --stage is given; staging "
        "produces reviewable Ingest Proposals, never direct writes (ADR-0015).",
    )
    _add_kb_argument(cross_link_parser)
    cross_link_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max candidates listed (and staged, with --stage). Default: 20.",
    )
    cross_link_parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage one reviewable repair proposal per top-ranked candidate.",
    )
    _add_ingest_dir_argument(cross_link_parser)
    cross_link_parser.set_defaults(func=_cmd_cross_link)

    # dream
    dream_parser = subparsers.add_parser(
        "dream",
        help="Run the Dream Cycle: reflect on KB health, optionally stage repairs.",
        description="Read-only reflection (validation, health, structural diagnostics for "
        "both scopes, ranked link candidates); with --stage, the top --limit repairs are "
        "staged as reviewable Ingest Proposals (ADR-0015).",
    )
    _add_kb_argument(dream_parser)
    _add_index_dir_argument(dream_parser)
    dream_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max candidates shown (and staged, with --stage). Default: 5.",
    )
    dream_parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage deterministic repairs, and semantic findings when --semantic is set.",
    )
    dream_parser.add_argument(
        "--semantic",
        action="store_true",
        help="Run the opt-in LLM-assisted contradiction, stale, and summary review.",
    )
    dream_parser.add_argument(
        "--semantic-limit",
        type=int,
        default=25,
        help="Max pages sent to one semantic review batch (default: 25).",
    )
    _add_ingest_dir_argument(dream_parser)
    dream_parser.set_defaults(func=_cmd_dream)

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

    # source (nested) — private Knowledge Source lifecycle (issue #133)
    source_parser = subparsers.add_parser(
        "source",
        help="Manage private Knowledge Source lifecycle state.",
        description=(
            "Manage private Knowledge Source identities: register, list, retire, "
            "record and dismiss retirement candidates, and reactivate. Every "
            "mutating command targets an explicit --source-id; explicit "
            "retirement and reactivation stage ordinary reviewable proposals "
            "through the Proposal Pipeline. Output is safe: no raw source bytes, "
            "local file paths, or credentials are disclosed."
        ),
    )
    source_sub = source_parser.add_subparsers(
        dest="source_command", required=True, metavar="<source-command>"
    )

    source_register = source_sub.add_parser(
        "register",
        help="Register bytes under an explicit, stable Knowledge Source identity.",
        description=(
            "Append an immutable Source Version under an explicit --source-id. "
            "The identity is private to the ingest store; it never appears in "
            "portable KB content. A retired source must be reactivated explicitly."
        ),
    )
    _add_kb_argument(source_register)
    _add_ingest_dir_argument(source_register)
    source_register.add_argument(
        "--source-id", required=True, help="Explicit stable source identity."
    )
    source_register.add_argument(
        "--file", type=Path, required=True, help="Source bytes to register."
    )
    source_register.set_defaults(func=_cmd_source_register)

    source_list = source_sub.add_parser(
        "list",
        help="List registered Knowledge Sources and their status.",
        description=(
            "List private Knowledge Source identities, statuses, and version "
            "counts. Source bytes and local paths are never disclosed."
        ),
    )
    _add_kb_argument(source_list)
    _add_ingest_dir_argument(source_list)
    source_list.set_defaults(func=_cmd_source_list)

    source_retire = source_sub.add_parser(
        "retire",
        help="Stage explicit Source Retirement.",
        description=(
            "Stage a Source Retirement proposal. The source stays active until "
            "the proposal publishes; page-level support loss is reported as "
            "reviewable impacts (sole-source-lost / still-supported)."
        ),
    )
    _add_kb_argument(source_retire)
    _add_ingest_dir_argument(source_retire)
    source_retire.add_argument("--source-id", required=True, help="Source identity to retire.")
    source_retire.set_defaults(func=_cmd_source_retire)

    source_candidate = source_sub.add_parser(
        "candidate",
        help="Record a non-mutating retirement signal.",
        description=(
            "Record a Retirement Candidate (a Maintainer-reviewable signal) "
            "without staging a proposal or changing source support."
        ),
    )
    _add_kb_argument(source_candidate)
    _add_ingest_dir_argument(source_candidate)
    source_candidate.add_argument("--source-id", required=True, help="Active source identity.")
    _candidate_trigger_help = "Signal that prompted the review. One of: " + ", ".join(
        repr(trigger) for trigger in RETIREMENT_CANDIDATE_TRIGGERS
    )
    source_candidate.add_argument("--trigger", required=True, help=_candidate_trigger_help)
    source_candidate.set_defaults(func=_cmd_source_candidate)

    source_dismiss = source_sub.add_parser(
        "dismiss-candidate",
        help="Dismiss a Retirement Candidate for an explicit source.",
        description=(
            "Dismiss a pending Retirement Candidate. Dismissal mutates state, "
            "so both the candidate id (reported by `source candidate`) and the "
            "Knowledge Source it must belong to are required; no proposal is "
            "staged."
        ),
    )
    _add_kb_argument(source_dismiss)
    _add_ingest_dir_argument(source_dismiss)
    source_dismiss.add_argument(
        "--candidate-id",
        required=True,
        help="Candidate id reported by `source candidate`.",
    )
    source_dismiss.add_argument(
        "--source-id",
        required=True,
        help="Knowledge Source the candidate must belong to.",
    )
    source_dismiss.set_defaults(func=_cmd_source_dismiss_candidate)

    source_confirm = source_sub.add_parser(
        "confirm-candidate",
        help="Confirm a Retirement Candidate by staging a retirement proposal.",
        description=(
            "Confirm a pending Retirement Candidate by staging an ordinary "
            "Source Retirement proposal through review. Both the candidate id "
            "(reported by `source candidate`) and the Knowledge Source it must "
            "belong to are required; the source stays active until the staged "
            "proposal publishes."
        ),
    )
    _add_kb_argument(source_confirm)
    _add_ingest_dir_argument(source_confirm)
    source_confirm.add_argument(
        "--candidate-id",
        required=True,
        help="Candidate id reported by `source candidate`.",
    )
    source_confirm.add_argument(
        "--source-id",
        required=True,
        help="Knowledge Source the candidate must belong to.",
    )
    source_confirm.set_defaults(func=_cmd_source_confirm_candidate)

    source_reactivate = source_sub.add_parser(
        "reactivate",
        help="Stage a new Source Version under a retired identity.",
        description=(
            "Stage a reactivation proposal that appends a new Source Version "
            "under an existing retired --source-id. The source stays retired "
            "until the proposal publishes."
        ),
    )
    _add_kb_argument(source_reactivate)
    _add_ingest_dir_argument(source_reactivate)
    source_reactivate.add_argument(
        "--source-id", required=True, help="Retired source identity to reactivate."
    )
    source_reactivate.add_argument(
        "--file", type=Path, required=True, help="Replacement source bytes."
    )
    source_reactivate.set_defaults(func=_cmd_source_reactivate)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    # Commands using _add_kb_argument can omit <kb>. Resolve the default now
    # (exported LUMIO_KB_PATH, then the nearest project .env) so the value is
    # computed once, in the live process, rather than at parser-build time
    # (issue #152, ADR-0017). If nothing yields a value, fail with guidance.
    if hasattr(args, "path") and args.path is None:
        args.path = _resolve_default_kb_path()
    if hasattr(args, "path") and args.path is None:
        print(
            "error: no Knowledge Base path provided. Pass <kb-path> as a "
            "positional argument, export LUMIO_KB_PATH, or run "
            "'lumio-wiki setup <kb>' to write .env.",
            file=sys.stderr,
        )
        return 2
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
