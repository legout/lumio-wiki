"""Standalone ``lumio-wiki`` command-line interface (issue #98).

A coding agent initializes, inspects, retrieves from, ingests into, reviews,
and publishes a Knowledge Base through this CLI without cloning the Lumio
repository or importing the full web application. Every command calls the
public :mod:`lumio_wiki` Python surface — no internal application modules.

The dispatcher follows the same ``argparse`` + ``set_defaults(func=...)``
convention the existing ``lumio`` CLI uses. Core operations (validate,
search, page, related, paths, ingest, proposal, publish, health, lint,
dream) call public ``lumio_wiki`` functions directly. CLI-only commands do
not map one-to-one to a public function: ``setup`` composes KB creation with
project wiring (``.env`` and ``AGENTS.md``), ``skill`` installs the Agent
Skill, and ``doctor`` reports install diagnostics.

The optional ``<kb>`` positional argument can be omitted for read and write
commands once ``setup`` has wired the project: it resolves from an exported
``LUMIO_KB_PATH`` and then the nearest project ``.env`` (issue #152;
ADR-0017).

The base Distiller is the host coding agent (``PassthroughMarkdownDistiller``):
no OpenAI client is required for text and Markdown ingestion.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import lumio_wiki
from lumio_wiki import (
    PREFERRED_RELATIONSHIP_TYPES,
    RETIREMENT_CANDIDATE_TRIGGERS,
    ControlFileError,
    Distiller,
    IngestStore,
    KnowledgeBase,
    KnowledgeBaseError,
    MaintenanceError,
    ManagedIngestError,
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
    ARTIFACT_RETENTION_ENV_VAR,
    KB_PATH_ENV_VAR,
    PUBLISH_TO_ENV_VAR,
    RETRIEVAL_BACKEND_ENV_VAR,
    RETRIEVAL_MODE_ENV_VAR,
    SOURCE_STORE_ENV_VAR,
    discover_kb_path_from_project_env,
    load_project_config,
)
from lumio_wiki.knowledge_base import (
    DEFAULT_GRAPH_MAX_DEPTH,
    DEFAULT_GRAPH_MAX_EDGES,
    DEFAULT_GRAPH_MAX_RESULTS,
    NAV_INDEX_BASENAME,
)
from lumio_wiki.source_inspection import (
    DEFAULT_LINK_EXPIRES,
    OUTCOME_ACCESS_DENIED,
    OUTCOME_CORRUPTION,
    SourceInspectionError,
    fetch_verified_artifact,
    parse_expires,
    resolve_manifest_binding,
    resolve_registry_binding,
    safe_fetch_destination,
    validate_published_version,
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


def _infer_content_type(path: Path) -> str:
    """Infer a stable MIME type for CLI provenance, with a text fallback."""
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    return mimetypes.guess_type(path.name)[0] or "text/plain"


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
    """Build obstore ``config``/``client_options`` from environment variables.

    ``LUMIO_S3_REGION`` / ``LUMIO_S3_ENDPOINT`` may also be recorded in the
    project ``.env`` allowlist (issue #161, ADR-0019): exported process values
    retain precedence, and the Lumio-specific key beats the generic ``AWS_*``
    fallback. Credentials are never read from ``.env`` — standard AWS
    credential resolution stays authoritative.
    """
    project = load_project_config()
    config: dict[str, str] = {}
    client_options: dict[str, object] = {}
    region = (
        os.environ.get("LUMIO_S3_REGION")
        or project.get("LUMIO_S3_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if region:
        config["aws_region"] = region
    endpoint = (
        os.environ.get("LUMIO_S3_ENDPOINT")
        or project.get("LUMIO_S3_ENDPOINT")
        or os.environ.get("AWS_ENDPOINT_URL_S3")
    )
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


def _build_publish_store(uri: str) -> tuple[Any, str]:
    """Build an ``(obstore ObjectStore, prefix)`` from a destination URI + env.

    Mirrors :meth:`S3Location.from_url` store construction but returns the raw
    store and prefix so the publisher can write under the version prefix.
    """
    from urllib.parse import urlparse, urlunsplit

    from lumio_wiki.s3_location import _require_obstore

    obstore = _require_obstore()
    parsed = urlparse(uri)
    if not parsed.scheme:
        raise CliError(f"not an object-store destination URI: {uri!r}")
    config, client_options = _s3_config_from_env()
    authority_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
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


# ---------------------------------------------------------------------------
# Remote LanceDB binding for S3 Published Versions (issue #162, ADR-0019).
#
# A read command resolves the active S3 pointer exactly once and retains the
# selected Published Version identity and canonical fingerprint. When the
# configured retrieval backend is ``lancedb``, the CLI dynamically binds
# ``lumio-lancedb`` to that exact version's remote index through the Snapshot's
# dependency-neutral descriptor — object keys are never reconstructed in the
# caller, the URI is never coerced through ``Path``, and no managed local
# index/cache is materialized. Backend and mode stay separate choices so
# LanceDB BM25 can serve lexical mode.
# ---------------------------------------------------------------------------

#: The retrieval backends a read command accepts (ADR-0019).
_RETRIEVAL_BACKENDS = ("zero-index", "lancedb")

#: The retrieval modes ``search`` accepts (ADR-0019).
_RETRIEVAL_MODES = ("lexical", "semantic", "hybrid")


def _retrieval_backend() -> str:
    """Resolve the retrieval backend: ``zero-index`` (default) or ``lancedb``.

    Read from ``LUMIO_RETRIEVAL_BACKEND`` with exported-process precedence
    over the project ``.env`` allowlist (written by ``setup --retrieval``).
    An unknown value is an actionable configuration error, never a silent
    default (issue #162).
    """
    configured = load_project_config().get(RETRIEVAL_BACKEND_ENV_VAR)
    if configured is None:
        return "zero-index"
    if configured not in _RETRIEVAL_BACKENDS:
        raise CliError(
            f"LUMIO_RETRIEVAL_BACKEND must be one of "
            f"{', '.join(_RETRIEVAL_BACKENDS)}; got {configured!r}"
        )
    return configured


def _resolve_search_mode(args: argparse.Namespace) -> str:
    """Resolve the retrieval mode: ``--mode``, else ``LUMIO_RETRIEVAL_MODE``, else lexical."""
    if args.mode is not None:
        return args.mode
    configured = load_project_config().get(RETRIEVAL_MODE_ENV_VAR)
    if configured is None:
        return "lexical"
    if configured not in _RETRIEVAL_MODES:
        raise CliError(
            f"LUMIO_RETRIEVAL_MODE must be one of {', '.join(_RETRIEVAL_MODES)}; got {configured!r}"
        )
    return configured


def _bind_remote_lancedb(snapshot) -> tuple[Any, Any]:
    """Dynamically bind ``lumio-lancedb`` to a resolved Snapshot's remote index.

    The adapter is imported only when the configured backend is ``lancedb``
    (``lumio-wiki`` never imports it statically; the base wheel stays
    LanceDB-free, ADR-0010). The remote index location is constructed from the
    Snapshot's dependency-neutral descriptor — the CLI never reconstructs S3
    keys or coerces the URI through ``Path`` — and LanceDB receives the same
    credentials/region/endpoint configuration as canonical S3 reads through
    its own ``storage_options`` (issue #162, ADR-0013).

    Returns ``(lumio_lancedb_module, RemoteIndexLocation)``.
    """
    import importlib

    from lumio_wiki import retrieval_eval

    if not retrieval_eval.lancedb_available():
        raise CliError(
            "LUMIO_RETRIEVAL_BACKEND=lancedb needs lumio-lancedb; install with:  "
            "pip install 'lumio-lancedb[s3]'"
        )
    descriptor = snapshot.remote_derived_index
    if descriptor is None:
        raise CliError(
            "the resolved Snapshot has no remote derived index; publish the "
            "Published Version with 'publish-s3 --retrieval lancedb' so this "
            "version carries one, or use the zero-index backend"
        )
    module = importlib.import_module("lumio_lancedb")
    location = module.RemoteIndexLocation(
        descriptor.uri,
        storage_options=_lance_storage_options_from_env() or None,
        store=descriptor.store,
        sidecar_prefix=descriptor.sidecar_prefix,
    )
    return module, location


def _remote_lance_page_search(
    module: Any,
    location: Any,
    snapshot: Any,
    query: str,
    limit: int,
) -> tuple[list, str | None]:
    """Serve lexical search from the published remote index via LanceDB BM25.

    Returns ``(results, note)``; ``note`` is ``None`` on the healthy path. A
    missing, corrupt, stale (fingerprint-mismatched), or unavailable remote
    index degrades truthfully: zero-index page search over the same Published
    Version with a disclosed note (issue #162, ADR-0013). Embedding errors
    propagate — they are configuration errors, not degradations.
    """
    from lumio_wiki.embeddings import EmbeddingError
    from lumio_wiki.fingerprint_store import FINGERPRINT_FILE

    def _fallback(note: str) -> tuple[list, str]:
        return snapshot.knowledge_base.search_pages(query, limit=limit), note

    describe = location.describe
    try:
        if not location.has_index():
            return _fallback(
                f"LanceDB index missing at {describe}; "
                f"zero-index page search over the same Published Version"
            )
        raw = location.read_sidecar(FINGERPRINT_FILE)
        stored = None
        if raw is not None:
            import msgspec

            from lumio_wiki.records import SourceFingerprint

            stored = msgspec.json.decode(raw, type=SourceFingerprint)
        if stored is not None and stored.digest != snapshot.fingerprint.digest:
            return _fallback(
                f"LanceDB index stale at {describe} (fingerprint mismatch); "
                f"zero-index page search over the same Published Version"
            )
        if hasattr(location, "connect") and hasattr(module, "PAGE_TABLE_NAME"):
            present = set(location.connect().list_tables().tables)
            required = {
                getattr(module, "TABLE_NAME", "evidence"),
                module.PAGE_TABLE_NAME,
            }
            missing = required - present
            if missing:
                return _fallback(
                    f"LanceDB index incomplete at {describe} (missing table(s): "
                    f"{', '.join(sorted(missing))}); "
                    f"zero-index page search over the same Published Version"
                )
        results = module.search_pages(list(snapshot.pages), query, limit=limit, index_dir=location)
        return results, None
    except EmbeddingError:
        raise
    except Exception as exc:  # transport/auth/corruption — degrade truthfully
        return _fallback(
            f"LanceDB index unavailable at {describe} "
            f"({type(exc).__name__}: {exc}); "
            f"zero-index page search over the same Published Version"
        )


def _index_fallback_note(results: list) -> str | None:
    """Return the disclosed fallback detail recorded in the results' traces."""
    for result in results:
        for stage in getattr(result.trace, "stages", ()):
            if stage.name == "index-fallback":
                return stage.detail
    return None


def _print_page_search_results(results: list) -> None:
    """Print page-oriented lexical search results (the ``search`` output contract)."""
    if not results:
        print("No pages matched the query.")
        return
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


def _print_evidence_results(results: list) -> None:
    """Print citation-ready Evidence retrieval results (semantic/hybrid contract)."""
    if not results:
        print("No Evidence matched the query.")
        return
    for result in results:
        cite = result.citation
        print(f"## {cite.page_title}")
        print(f"path:   {cite.relative_path}")
        source = getattr(cite, "source", None)
        if source:
            print(f"source: {source}")
        line_start = getattr(cite, "line_start", None)
        if line_start is not None:
            line_end = getattr(cite, "line_end", line_start)
            print(f"lines:  {line_start}-{line_end}")
        print(f"score:  {result.score}")
        if getattr(result, "reason", None):
            print(f"reason: {result.reason}")
        if getattr(result, "snippet", None):
            print(f"\n{result.snippet}\n")
        print()


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


def _validate_object_store_uri(flag: str, value: str) -> None:
    """Reject non-object-store URIs for the setup location flags (issue #161)."""
    if not _is_object_store_uri(value):
        raise CliError(
            f"{flag} requires an object-store URI (e.g. s3://bucket/path), got {value!r}"
        )


def _require_extra(module: str, install_command: str, reason: str) -> None:
    """Fail with one exact install command when an optional capability is missing.

    Setup never mutates the active Python environment (ADR-0019); it detects a
    missing optional distribution and prints the exact command to run.
    """
    if not _detect_module(module):
        raise CliError(
            f"{reason} requires an optional capability that is not installed.\n"
            f"Install it with this exact command:\n"
            f"  {install_command}\n"
            f"setup never modifies your Python environment."
        )


def _env_is_tracked_by_git(project_dir: Path) -> bool:
    """Return whether the project's ``.env`` is tracked by git (issue #161).

    Used as the privacy guard for private Source Artifact Store URIs
    (ADR-0020): a tracked ``.env`` would leak the private URI through normal
    commits. Missing git binary or a non-repo directory means there is no VCS
    leak surface, so they are allowed. Only an actually-tracked ``.env``
    (``git ls-files --error-unmatch .env`` succeeds) refuses.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "ls-files", "--error-unmatch", ".env"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _wizard_ask(
    prompt: str,
    *,
    default: str | None = None,
    optional: bool = False,
    validate=None,
    choices: dict[str, str | None] | None = None,
) -> str | None:
    """Ask one setup-wizard question with bounded retries."""
    for _ in range(3):
        try:
            raw = input(prompt).strip()
        except EOFError:
            raise CliError(
                "setup wizard needs interactive input; pass <local-kb> or "
                "--from <s3-uri> explicitly"
            ) from None
        if not raw:
            if default is not None or optional:
                return default
            print("  A value is required.")
            continue
        if choices is not None:
            if raw.lower() not in choices:
                print(f"  Answer one of: {', '.join(sorted(choices))}")
                continue
            return choices[raw.lower()]
        if validate is not None:
            error = validate(raw)
            if error:
                print(f"  {error}")
                continue
        return raw
    raise CliError("setup wizard: aborted after repeated invalid answers")


def _run_setup_wizard() -> dict[str, Any]:
    """Interactive fallback when no location was passed (ADR-0019).

    Asks the same questions the flags answer, so the wizard and flag flows
    produce equivalent configuration.
    """
    print("Lumio Knowledge Base setup wizard (Ctrl+C aborts; Enter accepts [bracketed] defaults)")

    def require_s3_uri(flag: str):
        def check(value: str) -> str | None:
            if not _is_object_store_uri(value):
                return f"{flag} requires an object-store URI (e.g. s3://bucket/path)."
            return None

        return check

    role = _wizard_ask(
        "Configure as [m]aintainer (local KB, optional S3 publish) or [r]eader (read-only S3 KB)? ",
        default="maintainer",
        choices={
            "m": "maintainer",
            "maintainer": "maintainer",
            "r": "reader",
            "reader": "reader",
        },
    )
    if role == "maintainer":
        kb_raw = (
            _wizard_ask(
                "Local Knowledge Base directory (created if absent) [./knowledge-base]: ",
                default="./knowledge-base",
            )
            or "./knowledge-base"
        )
        kb_path: Path | None = Path(kb_raw)
        from_uri = None
        publish_to = _wizard_ask(
            "Publish destination object-store URI, e.g. s3://bucket/kb (Enter to skip): ",
            optional=True,
            validate=require_s3_uri("--publish-to"),
        )
    else:
        from_uri = _wizard_ask(
            "Existing S3 Knowledge Base URI, e.g. s3://bucket/kb: ",
            validate=require_s3_uri("--from"),
        )
        kb_path = None
        publish_to = None
    retrieval = _wizard_ask(
        "Retrieval backend [z]ero-index (always available) or [l]anceDB? ",
        default="zero-index",
        choices={
            "z": "zero-index",
            "zero-index": "zero-index",
            "l": "lancedb",
            "lancedb": "lancedb",
        },
    )
    def require_source_store(flag: str):
        def check(value: str) -> str | None:
            if _is_object_store_uri(value) or "://" not in value:
                return None
            scheme = value.split("://", 1)[0]
            return (
                f"{flag} must be an object-store URI (e.g. s3://bucket/path) "
                f"or a local directory path; {scheme!r} is not supported."
            )

        return check

    source_store = _wizard_ask(
        "Private source artifact store (s3:// URI or local path; Enter to skip): ",
        optional=True,
        validate=require_source_store("--source-store"),
    )
    skill_scope = _wizard_ask(
        "Install the Agent Skill at [u]ser scope, [p]roject scope, or [n]one? ",
        optional=True,
        choices={"u": "user", "user": "user", "p": "project", "project": "project", "n": None},
    )
    return {
        "kb_path": kb_path,
        "from_uri": from_uri,
        "publish_to": publish_to,
        "retrieval": retrieval,
        "source_store": source_store,
        "skill_scope": skill_scope,
    }


def _cmd_setup(args: argparse.Namespace) -> int:
    """One-command project setup: KB + .env + AGENTS.md + optional skill install.

    Maintainer form: ``setup <local-kb> [--publish-to <s3-uri>]`` creates the
    Knowledge Base if it does not exist and records optional S3 publication,
    retrieval-backend, and private source-store configuration in ``.env``.
    Reader form: ``setup --from <s3-uri>`` configures a read-only project
    against an existing S3 Knowledge Base Location (issue #161, ADR-0019).
    With no location in an interactive terminal a short wizard asks the same
    questions; non-interactively it fails with the required flags. Both forms
    write/update ``AGENTS.md`` and optionally install the Agent Skill
    explicitly, so a fresh agent harness session can run pathless commands.
    """
    project_dir = Path.cwd()
    kb_path: Path | None = getattr(args, "kb_path", None)
    from_uri: str | None = getattr(args, "from_uri", None)
    publish_to: str | None = getattr(args, "publish_to", None)
    source_store: str | None = getattr(args, "source_store", None)
    artifact_retention: str | None = getattr(args, "artifact_retention", None)
    retrieval: str | None = getattr(args, "retrieval", None)
    created = False

    # 1. Resolve the location: a local Maintainer worktree or a read-only S3
    # Knowledge Base Location. Never both.
    if kb_path is not None and from_uri is not None:
        raise CliError(
            "choose one Knowledge Base location: a local <kb> path or --from <s3-uri>, not both"
        )
    if kb_path is None and from_uri is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise CliError(
                "setup requires a Knowledge Base location. Either:\n"
                "  lumio-wiki setup <local-kb> [--publish-to <s3-uri>]  (Maintainer)\n"
                "  lumio-wiki setup --from <s3-uri>                     (read-only)\n"
                "In an interactive terminal, plain 'lumio-wiki setup' runs a short wizard."
            )
        try:
            answers = _run_setup_wizard()
        except KeyboardInterrupt:
            print("\nsetup wizard aborted; nothing was written", file=sys.stderr)
            return 1
        kb_path = answers["kb_path"]
        from_uri = answers["from_uri"]
        publish_to = publish_to or answers["publish_to"]
        source_store = source_store or answers["source_store"]
        retrieval = retrieval or answers["retrieval"]
        if getattr(args, "skill_scope", None) is None and getattr(args, "agent", None) is None:
            args.skill_scope = answers["skill_scope"]
    if publish_to is not None and from_uri is not None:
        raise CliError(
            "--publish-to records an S3 publication destination for a local "
            "Maintainer worktree; it cannot be combined with --from"
        )
    for flag, value in (("--from", from_uri), ("--publish-to", publish_to)):
        if value is not None:
            _validate_object_store_uri(flag, value)
    if source_store is not None:
        # A Source Artifact Store is object storage OR a local directory
        # (CONTEXT.md / ADR-0020): accept either; only the URI form needs
        # the obstore extra.
        if not _is_object_store_uri(source_store) and "://" in source_store:
            scheme = source_store.split("://", 1)[0]
            raise CliError(
                f"--source-store must be an object-store URI (s3://...) or a "
                f"local directory path; {scheme!r} is not a supported scheme"
            )
    if artifact_retention == "required" and source_store is None:
        raise CliError(
            "--artifact-retention required needs --source-store: required "
            "retention blocks publication while a referenced source lacks a "
            "verified artifact (issue #164, ADR-0020)"
        )

    # 2. Optional capabilities: detect BEFORE any write; print one exact
    # install command. Never mutate the active Python environment (ADR-0019).
    if (
        from_uri is not None
        or publish_to is not None
        or (source_store is not None and _is_object_store_uri(source_store))
    ):
        _require_extra("obstore", "pip install 'lumio-wiki[s3]'", "S3 configuration")
    if retrieval == "lancedb":
        # Detect the Lumio adapter package, not any importable `lancedb`:
        # the install command names the distribution that owns the capability.
        _require_extra("lumio_lancedb", "pip install 'lumio-lancedb[s3]'", "LanceDB retrieval")

    # 3. Privacy guard: never record a private Source Artifact Store URI in a
    # .env git tracks (ADR-0020).
    if source_store is not None and _env_is_tracked_by_git(project_dir):
        raise CliError(
            "refusing to record the private Source Artifact Store URI: this "
            "project's .env is tracked by git. Add '.env' to .gitignore, run "
            "'git rm --cached .env', commit, then re-run setup — or keep the "
            "source store out of this project."
        )

    env_path = project_dir / ".env"
    if from_uri is not None:
        # Read-only project: no local KB is created; pathless reads resolve
        # the immutable S3 Published Version from .env.
        print(f"Read-only Knowledge Base location: {from_uri}")
        kb_display = from_uri
    else:
        if kb_path is None:
            # Unreachable: the location resolution above guarantees one form.
            raise CliError("no Knowledge Base location resolved")
        kb_path = kb_path.resolve()
        if (kb_path / "lumio.yaml").exists() or any(kb_path.glob("*.md")):
            print(f"Knowledge Base already exists at {kb_path}")
        else:
            kb_path.mkdir(parents=True, exist_ok=True)
            write_control_file(kb_path, seeded_control_file())
            default_ingest_dir(kb_path).mkdir(parents=True, exist_ok=True)
            default_index_dir(kb_path).mkdir(parents=True, exist_ok=True)
            print(f"Created Knowledge Base at {kb_path}")
            created = True
        kb_display = kb_path

    # 4. Write .env (create or update each line only when configured this run).
    _upsert_env_var(env_path, KB_PATH_ENV_VAR, str(kb_display))
    print(f"  .env:           {env_path} ({KB_PATH_ENV_VAR}={kb_display})")
    if publish_to is not None:
        _upsert_env_var(env_path, PUBLISH_TO_ENV_VAR, publish_to)
        print(f"  .env:           {env_path} ({PUBLISH_TO_ENV_VAR}={publish_to})")
    if retrieval is not None:
        _upsert_env_var(env_path, RETRIEVAL_BACKEND_ENV_VAR, retrieval)
        print(f"  .env:           {env_path} ({RETRIEVAL_BACKEND_ENV_VAR}={retrieval})")
    if source_store is not None:
        _upsert_env_var(env_path, SOURCE_STORE_ENV_VAR, source_store)
        print(f"  .env:           {env_path} ({SOURCE_STORE_ENV_VAR}={source_store})")
    if artifact_retention is not None:
        _upsert_env_var(env_path, ARTIFACT_RETENTION_ENV_VAR, artifact_retention)
        print(f"  .env:           {env_path} ({ARTIFACT_RETENTION_ENV_VAR}={artifact_retention})")

    # 5. Write/append AGENTS.md unless --no-agents-md.
    if not getattr(args, "no_agents_md", False):
        agents_md = project_dir / "AGENTS.md"
        _write_agents_md_section(
            agents_md,
            kb_display,
            publish_to=publish_to,
            retrieval=retrieval,
            source_store=source_store,
        )
        print(f"  AGENTS.md:      {agents_md}")

    # 6. Optional, explicit skill install. Project bootstrap never writes into
    # an agent trust surface unless one target was requested (ADR-0017).
    agent = getattr(args, "agent", None)
    scope = getattr(args, "skill_scope", None)
    if agent is not None or scope is not None:
        from lumio_wiki.skill import SkillError, install_skill, skill_status

        try:
            status = skill_status(agent, scope=scope, project_dir=project_dir)
            if status.state == "current":
                print(f"  skill install:  CURRENT ({status.target})")
            elif status.state == "missing" or getattr(args, "overwrite", False):
                target = install_skill(
                    agent,
                    scope=scope,
                    project_dir=project_dir,
                    overwrite=getattr(args, "overwrite", False),
                )
                print(f"  skill install:  {target}")
                print("  skill discovery: restart the agent or start a new session")
            else:
                print(
                    f"  skill install:  SKIPPED ({status.state}; run "
                    f"'lumio-wiki skill update' for this target)",
                    file=sys.stderr,
                )
        except SkillError as exc:
            print(f"  skill install:  SKIPPED ({exc})", file=sys.stderr)

    print()
    print("Setup complete. Next steps:")
    if created:
        print(f"  1. Add Compiled Pages (.md) under {kb_path}")
        print("  2. Run: lumio-wiki validate")
    elif from_uri is not None:
        print('  1. Run: lumio-wiki search "query" (pathless; reads the S3 Published Version)')
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
automatically when no `<kb>` argument is given).{s3_config}

### Retrieval ladder (cheapest-first, stop when you have Evidence)

0. `lumio-wiki hot` — Maintainer-pinned entry pages. Read first.
1. `lumio-wiki index [dir]` — generated Navigation Index (all pages by directory).
2. `lumio-wiki search "<query>"` — zero-index lexical search (no external index).
3. `lumio-wiki page "<title>"` — read a page to confirm and cite the exact passage.
4. `lumio-wiki related "<title>" --scope discovery` — related pages (canonical + extracted).
5. `lumio-wiki paths "<src>" "<dst>"` — shortest directed path between two titles.

### Ingest (you are the Distiller)

1. Author a Compiled Page (YAML frontmatter + Markdown body) that declares the
   source identity in `sources[].id`.
2. `lumio-wiki ingest <original-source> --compiled-page <page.md> --source-id <id>`
   — bind the ORIGINAL raw source to your authored page under one stable
   identity and stage a single reviewable Ingest Proposal. No `[documents]`
   extra required (the converter name is derived from routing without running
   it). Plain `lumio-wiki ingest <file>` stays available for text/Markdown
   passthrough but does NOT establish a Source identity.
3. `lumio-wiki proposal list` → `proposal inspect <id>` → `proposal validate <id>`.
4. `lumio-wiki publish <id>` (or `lumio-wiki discard <id>`).

### Maintenance (you are the Maintainer)

- `lumio-wiki lint` — read-only cross-page QA: validation, graph health, and
  canonical/discovery structural diagnostics with scope disclosure. Exit 1 if invalid.
- `lumio-wiki cross-link` — missing-link candidates ranked by Discovery Graph
  impact. `--stage` repairs candidates as authored Markdown links (Extracted
  References, Discovery Graph only); it never creates typed Relationships.
- `lumio-wiki relationship stage <source> <target> --type T` — stage a typed
  canonical Relationship proposal (e.g. `--type uses`). Distinct from
  `cross-link --stage`; reviewed through the same proposal pipeline.
- `lumio-wiki dream` — the Dream Cycle: read-only reflection (validation +
  health + structure + ranked candidates). Add `--stage [--limit N]` to stage
  the top repairs as ordinary Ingest Proposals for review. Add opt-in `--semantic`
  with the `[llm]` extra for semantic findings; it remains proposal-first.

### Source Artifact inspection (authorized, not Evidence)

1. `lumio-wiki source inspect [<kb>] --source-id <id> [--published-version <v>]`
   — secret-free metadata for the ONE exact Source Version bound to the id
   (the registry's current version locally; the private Source Binding
   Manifest for S3 KBs or explicit history). Never falls back to the latest
   version when a binding is absent.
2. `lumio-wiki source fetch [<kb>] --source-id <id> [--published-version <v>]
   --output <path>` — byte-exact original, digest and size re-verified, to an
   explicit destination. Prefer verified `fetch` over `link` (signed URLs can
   leak through conversation history).
3. `lumio-wiki source link [<kb>] --source-id <id> [--published-version <v>]
   [--expires 5m]` — an
   explicit short-lived signed GET URL for the exact artifact (5 min default,
   1 h max) when the store supports signing. Treat the URL as a bearer
   secret: never persist, log, or paste it.
4. After fetch, read bounded text directly (text/CSV/JSON/XML) or use a
   sandboxed document tool for PDF/Office/image content. Quote exact original
   text with source/version and stable coordinates; label decoded/OCR/
   converted output as DERIVED. Never execute active content, never dump a
   large private artifact wholesale into context without an explicit user
   request. Inspection supports provenance review but is NOT Evidence and
   does not promise claim-level passage highlighting.

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


def _agents_md_s3_config(
    publish_to: str | None,
    retrieval: str | None,
    source_store: str | None,
) -> str:
    """Render the optional project S3 configuration block (issue #161).

    Empty when no S3 setting is configured, so plain local setup writes the
    exact section it wrote before this feature.
    """
    lines: list[str] = []
    if publish_to is not None:
        lines.append(
            f"- Publication destination: `{publish_to}` (`LUMIO_PUBLISH_TO`); "
            "publish with `lumio-wiki publish-s3 --version <v>` (destination "
            "is read from `.env` when omitted)."
        )
    if retrieval is not None:
        lines.append(
            f"- Retrieval backend: `{retrieval}` (`LUMIO_RETRIEVAL_BACKEND`); "
            "the retrieval *mode* (lexical/semantic/hybrid) is a separate "
            "setting."
        )
    if source_store is not None:
        # ADR-0020: AGENTS.md is a shared, typically tracked project file, so
        # the private Source Artifact Store URI itself never appears here —
        # only the fact that `.env` holds it (guarded by the setup privacy
        # check).
        lines.append(
            "- Private source artifact store configured in `.env` "
            "(`LUMIO_SOURCE_STORE`) — optional and private; never commit `.env` "
            "or copy its value into tracked files."
        )
    if not lines:
        return ""
    return (
        "\n\n**Project S3 configuration** (recorded in `.env`; setup never "
        "writes credentials there):\n" + "\n".join(lines)
    )


def _write_agents_md_section(
    agents_md: Path,
    kb_path: Path | str,
    *,
    publish_to: str | None = None,
    retrieval: str | None = None,
    source_store: str | None = None,
) -> None:
    """Write or update the Lumio KB section in an AGENTS.md file.

    If the file already contains the marker comment, the existing section is
    replaced in place. Otherwise the section is appended.
    """
    section = (
        _AGENTS_MD_MARKER
        + "\n"
        + _AGENTS_MD_SECTION.format(
            kb_path=kb_path,
            s3_config=_agents_md_s3_config(publish_to, retrieval, source_store),
        )
    )

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


# ---------------------------------------------------------------------------
# Embedder resolution for semantic/hybrid search (ADR-0010: lazy heavy deps).
#
# The base CLI never imports sentence-transformers / openai / lumio_lancedb at
# module load; they are pulled in only when a caller asks for --mode
# semantic|hybrid. lumio-lancedb ships no concrete Embedder, so the CLI builds
# one here from either an OpenAI-compatible provider or a local model (#75).
# ---------------------------------------------------------------------------


class _LocalEmbedder:
    """Sentence-transformers backed Embedder (lumio-lancedb[embeddings])."""

    def __init__(self, model_name: str) -> None:
        import importlib

        from lumio_wiki.embeddings import EmbeddingModelInfo  # stdlib-only record

        # Lazy: lumio-lancedb[embeddings] only. Loaded via importlib (a string
        # lookup) so the base package keeps no static sentence-transformers
        # import (ADR-0010) and static analysis doesn't flag the optional dep.
        SentenceTransformer = importlib.import_module("sentence_transformers").SentenceTransformer
        self._model = SentenceTransformer(model_name)
        dim_fn = getattr(
            self._model,
            "get_embedding_dimension",
            getattr(self._model, "get_sentence_embedding_dimension", lambda: 384),
        )
        self._info = EmbeddingModelInfo(model_name, int(dim_fn()))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    @property
    def model_info(self):
        return self._info


class _ProviderEmbedder:
    """OpenAI-compatible ``/embeddings`` backed Embedder (lumio-wiki[llm])."""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        import importlib

        from lumio_wiki.embeddings import EmbeddingModelInfo

        try:
            openai = importlib.import_module("openai")
        except ImportError as exc:  # pragma: no cover - only hit with provider env
            raise CliError(
                "provider embedder needs the 'openai' package; install "
                "'lumio-wiki[llm]' or 'openai'."
            ) from exc
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        probe = self._client.embeddings.create(model=model, input=["lumio"])
        self._info = EmbeddingModelInfo(model, len(probe.data[0].embedding))

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        ordered = sorted(resp.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]

    @property
    def model_info(self):
        return self._info


def _resolve_embedder(model: str | None):
    """Build a concrete Embedder for semantic/hybrid search.

    Provider-first (``LUMIO_PROVIDER_*`` -> OpenAI-compatible ``/embeddings``),
    else a local sentence-transformers model (``lumio-lancedb[embeddings]``).
    Heavy deps are imported lazily so the base CLI stays framework-free
    (ADR-0010).
    """
    import importlib.util

    base_url = os.environ.get("LUMIO_PROVIDER_BASE_URL")
    api_key = os.environ.get("LUMIO_PROVIDER_API_KEY")
    provider_model = (
        model or os.environ.get("LUMIO_EMBEDDING_MODEL") or os.environ.get("LUMIO_PROVIDER_MODEL")
    )
    if base_url and api_key and provider_model:
        return _ProviderEmbedder(base_url=base_url, api_key=api_key, model=provider_model)

    if importlib.util.find_spec("sentence_transformers") is None:
        raise CliError(
            "semantic/hybrid search needs an embedder: install "
            "'lumio-lancedb[embeddings]' (local) or set LUMIO_PROVIDER_BASE_URL + "
            "LUMIO_PROVIDER_API_KEY + (LUMIO_EMBEDDING_MODEL or --model) for a "
            "remote endpoint."
        )
    return _LocalEmbedder(model or "all-MiniLM-L6-v2")


def _cmd_search(args: argparse.Namespace) -> int:
    value = str(args.path)
    if _is_object_store_uri(value):
        try:
            # Resolve the active S3 pointer exactly once and retain the
            # selected Published Version identity and fingerprint (issue #162).
            snapshot = _resolve_object_store_location(value).resolve()
        except KnowledgeBaseError as exc:
            raise CliError(f"could not resolve S3 Knowledge Base at {value}: {exc}") from exc
        return _search_object_store(args, snapshot)

    kb = _open_read_kb(args.path)
    mode = _resolve_search_mode(args)

    # Default lexical path: zero-index, model-free, no derived index. Preserves
    # the original ``search`` behaviour and the offline invariant (PRD-0002:22).
    if mode == "lexical":
        _print_page_search_results(kb.search_pages(args.query, limit=args.limit))
        return 0

    # semantic / hybrid — needs lumio-lancedb + an Embedder (ADR-0010, #75).
    import importlib

    from lumio_wiki import retrieval_eval

    if not retrieval_eval.lancedb_available():
        raise CliError(
            f"--mode {mode} needs lumio-lancedb; install with:  "
            "pip install lumio-lancedb  (or lumio-lancedb[embeddings] for local "
            "sentence-transformers)."
        )
    embedder = _resolve_embedder(args.model)
    index_dir = args.index_dir or str(Path(args.path) / ".lumio" / "lance")
    # Build/refresh the derived LanceDB index; binds the adapter to the KB and
    # writes BM25 + embedding tables under index_dir (issue #75, #138).
    kb = importlib.import_module("lumio_lancedb").build_lancedb_index(
        kb, index_dir, embedder=embedder
    )
    results = kb.retrieve(
        args.query,
        limit=args.limit,
        index_dir=index_dir,
        mode=mode,
        embedder=embedder,
    )
    _print_evidence_results(results)
    return 0


def _search_object_store(args: argparse.Namespace, snapshot: Any) -> int:
    """Search an S3 Published Version: zero-index, or bound remote LanceDB (#162).

    Backend and mode are separate choices: LanceDB BM25 may serve lexical
    mode, while semantic/hybrid additionally use the configured embedder and
    the exact remote index model identity. Every degradable failure falls back
    to truthful zero-index retrieval over the same Snapshot with a disclosed
    note; configuration and model errors remain actionable errors.
    """
    from lumio_wiki.embeddings import EmbeddingError

    backend = _retrieval_backend()
    mode = _resolve_search_mode(args)

    if mode == "lexical" and backend == "zero-index":
        _print_page_search_results(
            snapshot.knowledge_base.search_pages(args.query, limit=args.limit)
        )
        return 0

    if backend != "lancedb":
        raise CliError(
            f"--mode {mode} over an S3 Knowledge Base needs the LanceDB "
            "backend. Record it with 'lumio-wiki setup --retrieval lancedb' "
            "(.env: LUMIO_RETRIEVAL_BACKEND=lancedb) or search --mode lexical."
        )
    if args.index_dir is not None:
        raise CliError(
            "--index-dir selects a local derived index; an S3 Knowledge Base "
            "reads its published remote index instead (never a managed local "
            "copy). Remove --index-dir to use the Published Version's index."
        )

    module, location = _bind_remote_lancedb(snapshot)

    if mode == "lexical":
        results, note = _remote_lance_page_search(
            module, location, snapshot, args.query, args.limit
        )
        if note:
            print(f"note: {note}")
        _print_page_search_results(results)
        return 0

    # semantic / hybrid through the adapter bound to the exact remote index.
    embedder = _resolve_embedder(args.model)
    adapter = module.LanceDBRetrievalAdapter(
        index_location=location,
        expected_fingerprint=snapshot.remote_derived_index.fingerprint,
    )
    try:
        results = adapter.retrieve(
            list(snapshot.pages),
            args.query,
            limit=args.limit,
            mode=mode,
            embedder=embedder,
        )
    except EmbeddingError as exc:
        # Model identity / index configuration errors are actionable, never
        # silently degraded (issue #162).
        raise CliError(str(exc)) from exc
    note = _index_fallback_note(results) or adapter.last_fallback_detail
    if note:
        print(f"note: {note}")
    _print_evidence_results(results)
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


def _artifact_store_from_env():
    """Resolve the optional private Source Artifact Store (issue #164).

    ``LUMIO_SOURCE_STORE`` (exported process value, then the project
    ``.env`` allowlist written by ``setup --source-store``) selects the
    adapter: an object-store URI builds the S3 adapter over an obstore client
    with the standard LUMIO_S3_*/AWS_* credential resolution (a private
    prefix in the same bucket works for development; production should use a
    separate bucket and KMS key — ADR-0020), and a filesystem path builds the
    local-directory adapter. ``None`` keeps the hash-only behavior.
    """
    from lumio_wiki.artifact_store import LocalDirectoryArtifactStore, S3ArtifactStore

    uri = os.environ.get(SOURCE_STORE_ENV_VAR) or load_project_config().get(SOURCE_STORE_ENV_VAR)
    if not uri:
        return None
    if _is_object_store_uri(uri):
        store, prefix = _build_publish_store(uri)
        return S3ArtifactStore(store, prefix)
    path = Path(uri).expanduser()
    if not path.is_absolute():
        project = discover_kb_path_from_project_env()
        base = Path(project).parent if project else Path.cwd()
        path = (base / path).resolve()
    try:
        return LocalDirectoryArtifactStore(path)
    except OSError as exc:
        raise CliError(f"cannot open Source Artifact Store at {uri!r}: {exc}") from exc


def _artifact_retention_required() -> bool:
    """Whether artifact retention is configured as required (issue #164).

    ``LUMIO_ARTIFACT_RETENTION=required`` blocks publication activation
    while a referenced non-synthetic source lacks a verified artifact;
    unset/``disabled`` preserves the hash-only behavior (ADR-0020).
    """
    value = os.environ.get(ARTIFACT_RETENTION_ENV_VAR) or load_project_config().get(
        ARTIFACT_RETENTION_ENV_VAR
    )
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized not in {"required", "disabled"}:
        raise CliError(
            f"{ARTIFACT_RETENTION_ENV_VAR} must be 'required' or 'disabled', got {value!r}"
        )
    return normalized == "required"


def _cmd_ingest(args: argparse.Namespace) -> int:
    kb, _report = _load_kb(args.path)
    compiled_page = getattr(args, "compiled_page", None)
    source_id = getattr(args, "source_id", None)
    # issue #149: --compiled-page and --source-id select the managed
    # host-Distiller mode and are required together. With neither present the
    # ordinary text/Markdown/document ingest path is unchanged.
    if (compiled_page is None) != (source_id is None):
        raise CliError(
            "--compiled-page and --source-id must be supplied together (issue #149 managed ingest)."
        )
    source_path = Path(args.file)
    if not source_path.is_file():
        raise CliError(f"source file not found: {source_path}")
    raw_bytes = source_path.read_bytes()
    content_type = args.content_type or _infer_content_type(source_path)

    ingest_dir = _resolve_ingest_dir(args, kb.root)
    ingest_dir.mkdir(parents=True, exist_ok=True)
    store = IngestStore(ingest_dir)
    pipeline = ProposalPipeline(kb, store=store)

    if compiled_page is not None and source_id is not None:
        # Managed host-Distiller ingest (issue #149): bind the ORIGINAL raw
        # Knowledge Source to the host-agent-authored Compiled Page under one
        # explicit source identity. The converter name is derived from routing
        # without invoking it, so PDF/DOCX/HTML sources do NOT require the
        # [documents] extra here — the host agent already authored the page.
        # When a private Source Artifact Store is configured (issue #164,
        # ADR-0020), the ORIGINAL bytes are additionally retained as an
        # immutable Source Artifact bound to the exact Source Version.
        if not compiled_page.is_file():
            raise CliError(f"compiled-page file not found: {compiled_page}")
        authored_markdown = compiled_page.read_text(encoding="utf-8")
        artifact_store = _artifact_store_from_env()
        pipeline = ProposalPipeline(kb, store=store, artifact_store=artifact_store)
        try:
            proposal = pipeline.managed_ingest(
                raw_bytes, content_type, source_path.name, source_id, authored_markdown
            )
        except (ManagedIngestError, SourceRegistryError) as exc:
            raise CliError(str(exc), exit_code=1) from exc
        print(f"Staged proposal {proposal.id}")
        print(f"  status:         {proposal.status}")
        print(f"  source_id:      {proposal.provenance.source_id}")
        print(f"  content_type:    {proposal.provenance.content_type}")
        print(f"  converted_by:   {proposal.provenance.converted_by}")
        print(f"  source_hash:    {proposal.provenance.source_hash}")
        print(f"  affected_pages: {', '.join(proposal.affected_pages) or '(none)'}")
        print(f"  blocked:        {proposal.blocked}")
        print()
        print("Review with:")
        print(f"  lumio-wiki proposal inspect {args.path} {proposal.id}")
        print(f"  lumio-wiki proposal validate {args.path} {proposal.id}")
        if is_reviewable_proposal(proposal) and not proposal.blocked:
            print(f"  lumio-wiki publish {args.path} {proposal.id}")
        return 0

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
    if proposal.provenance.content_type:
        print(f"content_type:    {proposal.provenance.content_type}")
    # issue #149: managed host-Distiller provenance — the explicit source
    # identity the raw source was bound to, and the immutable content hash
    # over the ORIGINAL bytes. These distinguish raw-source provenance from
    # the authored Compiled Page content shown in the Diff below (AC4).
    if proposal.provenance.source_id:
        print(f"source_id:       {proposal.provenance.source_id}")
    if proposal.provenance.source_hash:
        print(f"source_hash:     {proposal.provenance.source_hash}")
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


def _cmd_relationship_stage(args: argparse.Namespace) -> int:
    """Stage one reviewable typed Relationship proposal (issue #151).

    Delegates to ``lumio_wiki.stage_relationship_proposal``: appends a typed
    Relationship to the source page's frontmatter and stages a compound
    revision through the ordinary Proposal Pipeline. A typed Relationship is a
    canonical, reviewed edge resolved by Canonical Page Title — distinct from
    ``cross-link --stage``, which only adds authored Markdown links that
    become Extracted References (ADR-0011). Never direct-writes; never infers
    a relationship type.
    """
    kb, _report = _load_kb(args.path)
    source_title = args.source
    target_title = args.target
    rel_type = args.type

    if not rel_type.strip():
        raise CliError(
            "relationship type must not be empty; preferred types are: "
            + ", ".join(sorted(PREFERRED_RELATIONSHIP_TYPES)),
            exit_code=1,
        )

    # Missing source page — actionable diagnostic (mirrors MaintenanceError).
    source_page = next((p for p in kb.pages if p.title == source_title), None)
    if source_page is None:
        raise CliError(f"no Compiled Page titled {source_title!r}", exit_code=1)

    # Unresolved target — actionable diagnostic.
    if not any(p.title == target_title for p in kb.pages):
        raise CliError(
            f"unresolved target: {target_title!r} is not a Canonical Page Title",
            exit_code=1,
        )

    # Duplicate edge — actionable diagnostic. The maintenance helper silently
    # no-ops a duplicate; surface it instead of staging a no-op proposal.
    if any(
        rel.target == target_title and rel.type == rel_type for rel in source_page.relationships
    ):
        raise CliError(
            f"relationship already exists: {source_title!r} -> {target_title!r} "
            f"(type {rel_type!r}); nothing to stage",
            exit_code=1,
        )

    ingest_dir = _resolve_ingest_dir(args, kb.root)
    store = IngestStore(ingest_dir)
    try:
        proposal = lumio_wiki.stage_relationship_proposal(
            args.path, source_title, target_title, rel_type, store=store
        )
    except MaintenanceError as exc:
        raise CliError(str(exc), exit_code=1) from exc

    print(f"Staged proposal {proposal.id}")
    print(f"  status:         {proposal.status}")
    print(f"  relationship:   {source_title!r} -> {target_title!r} (type {rel_type!r})")
    print(f"  affected_pages: {', '.join(proposal.affected_pages) or '(none)'}")
    print(f"  blocked:        {proposal.blocked}")
    if rel_type not in PREFERRED_RELATIONSHIP_TYPES:
        print(
            f"  note: type {rel_type!r} is not a preferred Relationship type; "
            f"preferred types are: {', '.join(sorted(PREFERRED_RELATIONSHIP_TYPES))}. "
            f"Staged as a warning-level generic edge."
        )
    print()
    print("Review with:")
    print(f"  lumio-wiki proposal inspect {args.path} {proposal.id}")
    print(f"  lumio-wiki proposal validate {args.path} {proposal.id}")
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


def _lance_storage_options_from_env() -> dict[str, str]:
    """Build LanceDB's own S3 ``storage_options`` from environment variables.

    LanceDB connects to its own URI with its own options (ADR-0013): Lumio
    never hands its obstore client to LanceDB. Mirrors the resolution order of
    :func:`_s3_config_from_env` (LUMIO_S3_* first — exported process value,
    then the project ``.env`` allowlist written by setup (issue #161) — then
    the generic ``AWS_*`` fallback). Credentials are never read from ``.env``.
    """
    project = load_project_config()
    options: dict[str, str] = {}
    region = (
        os.environ.get("LUMIO_S3_REGION")
        or project.get("LUMIO_S3_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if region:
        options["region"] = region
    endpoint = (
        os.environ.get("LUMIO_S3_ENDPOINT")
        or project.get("LUMIO_S3_ENDPOINT")
        or os.environ.get("AWS_ENDPOINT_URL_S3")
    )
    if endpoint:
        options["endpoint"] = endpoint
        if endpoint.startswith("http://"):
            options["allow_http"] = "true"
    key = os.environ.get("LUMIO_S3_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("LUMIO_S3_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key:
        options["access_key_id"] = key
    if secret:
        options["secret_access_key"] = secret
    return options


def _publication_index_builder(destination: str):
    """Build the remote-LanceDB publication index builder (issue #163).

    The builder (``lumio_lancedb.remote_publication_builder``) builds the
    lexical BM25 + page tables under the version's ``derived/lance/`` prefix
    and health-checks them before the publisher may activate. Semantic vectors
    are not built on this path (they need an embedder; wire one explicitly
    when that journey lands).
    """
    import importlib
    from urllib.parse import urlparse

    from lumio_wiki import retrieval_eval

    if not retrieval_eval.lancedb_available():
        raise CliError(
            "publish-s3 --retrieval lancedb needs lumio-lancedb; install with:  "
            "pip install lumio-lancedb"
        )
    parsed = urlparse(destination)
    if parsed.scheme != "s3":
        raise CliError(f"--retrieval lancedb requires an s3:// destination, got {destination!r}")
    lumio_lancedb = importlib.import_module("lumio_lancedb")
    return lumio_lancedb.remote_publication_builder(
        store_uri=f"s3://{parsed.netloc}",
        storage_options=_lance_storage_options_from_env() or None,
    )


def _cmd_publish_s3(args: argparse.Namespace) -> int:
    """Publish a local Knowledge Base as an immutable S3 Published Version.

    Thin, model-free orchestration: validate + write canonical content and the
    derived Discovery Graph under an immutable version prefix, then
    conditionally advance the active pointer. Credentials/region/endpoint come
    from the standard LUMIO_S3_* / AWS_* environment variables.
    """
    from lumio_wiki.s3_location import _require_obstore
    from lumio_wiki.s3_publish import S3PublicationConflict, publish_s3_version

    # A single object-store argument is the destination: the publication
    # source is always a local directory, so shift a URI out of the path slot
    # and resolve the source from the default KB path (issue #161).
    if _is_object_store_uri(args.path):
        if args.destination is not None:
            raise CliError(
                "publish-s3 needs a local Knowledge Base source directory; "
                "two object-store URIs were given"
            )
        args.destination = args.path
        args.path = _resolve_default_kb_path()
        if args.path is None:
            raise CliError(
                "no Knowledge Base path provided. Pass <kb>, export "
                "LUMIO_KB_PATH, or run 'lumio-wiki setup <kb>' to write .env."
            )

    root = Path(args.path)
    if not root.is_dir():
        raise CliError(f"Knowledge Base source is not a directory: {root}")
    try:
        _require_obstore()
    except KnowledgeBaseError as exc:
        raise CliError(str(exc)) from exc
    destination = args.destination
    if destination is None:
        # The destination default comes from the bounded .env allowlist with
        # exported-process precedence (issue #161, ADR-0019).
        destination = load_project_config().get(PUBLISH_TO_ENV_VAR)
        if destination is None:
            raise CliError(
                "no publish destination given. Pass <destination>, or record "
                "one with 'lumio-wiki setup <kb> --publish-to <s3-uri>' "
                "(written to .env as LUMIO_PUBLISH_TO)."
            )
    store, prefix = _build_publish_store(destination)
    index_builder = None
    if getattr(args, "retrieval", "zero-index") == "lancedb":
        index_builder = _publication_index_builder(destination)
    # Private Source Artifact retention (issue #164, ADR-0020): when a store
    # is configured, the pre-activation hook writes the private Source
    # Binding Manifest BEFORE the pointer advances; with retention REQUIRED
    # it blocks activation while a referenced non-synthetic source lacks a
    # verified artifact. Without a store, publication is unchanged.
    from lumio_wiki.artifact_store import RetentionRequiredError, activation_binding_hook

    retention_required = _artifact_retention_required()
    before_activation = None
    artifact_store = _artifact_store_from_env()
    if retention_required and artifact_store is None:
        # Fail closed (#164 review): required retention with no configured
        # store can never verify coverage, so publication is refused rather
        # than silently publishing ungated.
        raise CliError(
            "artifact retention is required (LUMIO_ARTIFACT_RETENTION=required) "
            "but no Source Artifact Store is configured (LUMIO_SOURCE_STORE); "
            "refusing to publish ungated — configure the store or set "
            "retention to disabled",
            exit_code=1,
        )
    if artifact_store is not None:
        from lumio_wiki.ingest import IngestStore

        registry = IngestStore(default_ingest_dir(root)).source_registry
        before_activation = activation_binding_hook(
            artifact_store=artifact_store,
            registry=registry,
            source_root=root,
            required=retention_required,
        )
    try:
        manifest = publish_s3_version(
            store,
            prefix,
            source_root=root,
            version=args.version,
            expected_pointer_version=args.expected_pointer_version,
            index_builder=index_builder,
            before_activation=before_activation,
        )
    except RetentionRequiredError as exc:
        raise CliError(
            f"publication blocked (artifact retention required): {exc}", exit_code=1
        ) from exc
    except S3PublicationConflict as exc:
        raise CliError(f"publication conflict (pointer not advanced): {exc}") from exc
    except KnowledgeBaseError as exc:
        raise CliError(f"publication failed: {exc}") from exc
    except Exception as exc:
        # Object-store transport/credential failures (unreachable endpoint,
        # IMDS probe) and a requested-but-failed LanceDB build/health check
        # (issue #163: publication was blocked before activation) both surface
        # as actionable errors, never a traceback.
        raise CliError(f"publication failed: {exc}") from exc
    print(f"Published {manifest.version}: {len(manifest.files)} canonical file(s)")
    print(f"  fingerprint: {manifest.fingerprint}")
    if index_builder is not None:
        print("  lance:       built and health-checked under derived/lance/")
    if artifact_store is not None:
        print("  artifacts:   private Source Binding Manifest stored pre-activation")
    print(f"  location:    {destination}@{manifest.version}")
    return 0


def _cmd_rollback_s3(args: argparse.Namespace) -> int:
    """CAS-activate an already complete immutable S3 Published Version."""
    from lumio_wiki.artifact_store import (
        RetentionRequiredError,
        verify_rollback_coverage,
    )
    from lumio_wiki.s3_publish import (
        S3PublicationConflict,
        list_cleanup_candidates,
        rollback_s3_version,
    )

    store, prefix = _build_publish_store(args.destination)
    # Rollback IS an activation (#164 review): under required retention the
    # historical version's private binding manifest must still prove every
    # referenced non-synthetic source has a verified artifact.
    if _artifact_retention_required():
        artifact_store = _artifact_store_from_env()
        if artifact_store is None:
            raise CliError(
                "artifact retention is required (LUMIO_ARTIFACT_RETENTION="
                "required) but no Source Artifact Store is configured "
                "(LUMIO_SOURCE_STORE); refusing to activate ungated",
                exit_code=1,
            )
        try:
            verify_rollback_coverage(
                artifact_store, args.version, required=True
            )
        except RetentionRequiredError as exc:
            raise CliError(
                f"rollback blocked (artifact retention required): {exc}", exit_code=1
            ) from exc
    try:
        manifest = rollback_s3_version(
            store,
            prefix,
            version=args.version,
            expected_pointer_version=args.expected_pointer_version,
        )
    except S3PublicationConflict as exc:
        raise CliError(f"rollback conflict (pointer not advanced): {exc}") from exc
    except KnowledgeBaseError as exc:
        candidates = list_cleanup_candidates(store, prefix)
        hint = (
            f" Inactive incomplete prefixes (cleanup candidates): "
            f"{', '.join(c.version for c in candidates)}."
            if candidates
            else ""
        )
        raise CliError(f"rollback failed: {exc}{hint}") from exc
    print(f"Rolled back to {manifest.version}: {len(manifest.files)} canonical file(s)")
    print(f"  fingerprint: {manifest.fingerprint}")
    print(f"  location:    {args.destination}@{manifest.version}")
    return 0


def _cmd_cleanup_s3(args: argparse.Namespace) -> int:
    """Report inactive incomplete S3 version prefixes (report-only)."""
    from lumio_wiki.s3_publish import list_cleanup_candidates

    store, prefix = _build_publish_store(args.destination)
    candidates = list_cleanup_candidates(store, prefix)
    if not candidates:
        print("No cleanup candidates: every inactive version prefix is complete.")
        return 0
    print(f"Cleanup candidates under {args.destination} (nothing is deleted):")
    for candidate in candidates:
        print(f"  {candidate.version}  ({candidate.object_count} object(s), no manifest)")
    print(
        "These prefixes are incomplete and can never be activated; remove them "
        "with your object-store tooling when convenient."
    )
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
        # Score a real embedding model when one is requested, mirroring
        # ``search --model``: an explicit ``--model``, or any provider env
        # (``LUMIO_PROVIDER_BASE_URL`` / ``LUMIO_PROVIDER_API_KEY``). The same
        # ``_resolve_embedder`` gives eval and search shared provider-first /
        # local-fallback behavior and error messages (issue #158). Without
        # either, keep the hermetic DeterministicHashEmbedder so the default
        # ``--semantic`` run stays offline and CI-stable.
        provider_configured = bool(
            os.environ.get("LUMIO_PROVIDER_BASE_URL") or os.environ.get("LUMIO_PROVIDER_API_KEY")
        )
        if args.model or provider_configured:
            embedder = _resolve_embedder(args.model)
        else:
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
        "documents": all(
            _detect_module(module) for module in ("liteparse", "markitdown", "anydoc")
        ),
        "llm": _detect_module("openai"),
        "s3": _detect_module("obstore"),
        "lancedb": _detect_module("lancedb"),
    }
    extra_hint = {
        "documents": "pip install 'lumio-wiki[documents]'",
        "llm": "pip install 'lumio-wiki[llm]'",
        "s3": "pip install 'lumio-wiki[s3]'",
        "lancedb": "pip install 'lumio-lancedb[s3]'",
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


def _skill_target_values(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, Path | None]:
    agent = getattr(args, "agent", None)
    scope = getattr(args, "scope", None)
    dest = getattr(args, "dest", None)
    if agent is not None and not isinstance(agent, str):
        raise CliError("skill agent must be a string")
    if scope is not None and not isinstance(scope, str):
        raise CliError("skill scope must be a string")
    if dest is not None and not isinstance(dest, Path):
        raise CliError("skill destination must be a path")
    return agent, scope, dest


def _print_skill_status(status) -> None:
    print(f"state:             {status.state}")
    print(f"target:            {status.target}")
    print(f"target_kind:       {status.target_kind}")
    print(f"packaged_version:  {status.packaged_version}")
    print(f"packaged_hash:     {status.packaged_hash}")
    print(f"installed_version: {status.installed_version or '(none)'}")
    print(f"installed_hash:    {status.installed_hash or '(none)'}")
    print(f"detail:            {status.detail}")


def _cmd_skill_install(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import SkillError, install_skill, skill_status

    agent, scope, dest = _skill_target_values(args)
    try:
        status = skill_status(agent, scope=scope, dest=dest, project_dir=Path.cwd())
        if status.state == "current" and not args.overwrite:
            print(
                "error: destination already contains the current skill; "
                "no installation was performed",
                file=sys.stderr,
            )
            _print_skill_status(status)
            return 1
        if status.state != "missing" and not args.overwrite:
            print(
                f"error: installed skill is {status.state}; run 'lumio-wiki skill "
                "update' for the same target or pass --overwrite explicitly",
                file=sys.stderr,
            )
            return 1
        target = install_skill(
            agent,
            scope=scope,
            dest=dest,
            project_dir=Path.cwd(),
            overwrite=args.overwrite,
        )
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Installed lumio-wiki Agent Skill:")
    print(f"  {target}")
    print("Restart the agent or start a new session to discover the installed skill.")
    return 0


def _cmd_skill_status(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import SkillError, skill_status

    agent, scope, dest = _skill_target_values(args)
    try:
        status = skill_status(agent, scope=scope, dest=dest, project_dir=Path.cwd())
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_skill_status(status)
    return 0 if status.state == "current" else 1


def _cmd_skill_update(args: argparse.Namespace) -> int:
    from lumio_wiki.skill import SkillError, skill_status, update_skill

    agent, scope, dest = _skill_target_values(args)
    try:
        before = skill_status(agent, scope=scope, dest=dest, project_dir=Path.cwd())
        target = update_skill(agent, scope=scope, dest=dest, project_dir=Path.cwd())
        after = skill_status(agent, scope=scope, dest=dest, project_dir=Path.cwd())
    except SkillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if before.state == "current":
        print(f"lumio-wiki Agent Skill is already current: {target}")
    else:
        print(f"Updated lumio-wiki Agent Skill ({before.state} -> {after.state}):")
        print(f"  {target}")
        print("Restart the agent or start a new session to discover the updated skill.")
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
# Authorized Source Artifact inspection (issue #165, ADR-0020).
#
# `source inspect|fetch|link` resolve ONE exact Source Version for a source
# id — the local registry's current version for a worktree, or the exact
# (source_id, content_hash) bound by a private Source Binding Manifest for an
# S3 Knowledge Base / an explicit --published-version — and operate on it
# through the public Source Artifact Store seam. There is no fetch-by-hash and
# no object-key interface; a missing binding NEVER substitutes the latest
# Source Version. Distinct actionable outcomes (access denied / absent
# binding / unavailable / corruption / historical-version mismatch / signing
# unsupported) surface as exit-1 errors whose messages never disclose
# credentials, private object keys, or secret-bearing URLs.
# ---------------------------------------------------------------------------


def _active_published_version(uri: str) -> str:
    """Resolve the active Published Version of an S3 Knowledge Base once.

    Reads only the activation pointer — not the whole Published Version —
    because inspect/fetch/link need the version identity, not page content.
    The pointer only ever advances after a complete upload (#163), so an
    observed version is complete by construction.
    """
    from lumio_wiki.s3_publish import observe_current_pointer

    store, prefix = _build_publish_store(uri)
    observation = observe_current_pointer(store, prefix)
    if observation.version is None:
        raise CliError(
            "this S3 Knowledge Base has no active Published Version yet",
            exit_code=1,
        )
    return observation.version


def _resolve_source_binding(args: argparse.Namespace):
    """Resolve ONE exact Source Version for inspect/fetch/link.

    Returns ``(binding, artifact_store)`` where ``artifact_store`` may be
    ``None`` (retention not configured). Raises :class:`CliError` with the
    distinct #165 outcomes via :class:`SourceInspectionError`.
    """
    version = getattr(args, "published_version", None)
    if version is None and _is_object_store_uri(str(args.path)):
        version = _active_published_version(str(args.path))
    if version is not None:
        # Validate BEFORE any binding lookup or error echo: an unsafe label
        # (traversal, secret-bearing text) is a usage error reported without
        # echoing the value (#165 review).
        try:
            validate_published_version(version)
        except ValueError as exc:
            raise CliError(str(exc)) from None
    artifact_store = _artifact_store_from_env()
    try:
        if version is not None:
            # An S3 Knowledge Base (active or --published-version history)
            # resolves through the private Source Binding Manifest.
            if artifact_store is None:
                raise CliError(
                    "no private Source Artifact Store is configured "
                    "(LUMIO_SOURCE_STORE); publication bindings are stored "
                    "there — record one with 'lumio-wiki setup "
                    "--source-store <s3-uri|path>'",
                    exit_code=1,
                )
            return resolve_manifest_binding(artifact_store, version, args.source_id), artifact_store
        # Local worktree behavior: resolve through private registry state.
        kb, _report = _load_kb(args.path)
        ingest_dir = _resolve_ingest_dir(args, kb.root)
        registry = IngestStore(ingest_dir).source_registry
        return resolve_registry_binding(registry, args.source_id), artifact_store
    except SourceInspectionError as exc:
        raise CliError(str(exc), exit_code=1) from exc


def _binding_availability(artifact_store, binding) -> str:
    """Live, digest-verified availability label for ``source inspect``.

    Reads the exact artifact through the store seam so the report reflects
    what an authorized fetch would return, not a stale registry flag. Access
    denial is a distinct outcome and fails the command rather than being
    reported as absence.
    """
    if artifact_store is None:
        return "not retained (no Source Artifact Store configured)"
    try:
        fetch_verified_artifact(artifact_store, binding)
    except SourceInspectionError as exc:
        if exc.outcome == OUTCOME_ACCESS_DENIED:
            raise CliError(str(exc), exit_code=1) from exc
        if exc.outcome == OUTCOME_CORRUPTION:
            return "corrupt (stored bytes failed verification)"
        return "not retained"
    return "retained (digest and size verified)"


def _cmd_source_inspect(args: argparse.Namespace) -> int:
    binding, artifact_store = _resolve_source_binding(args)
    availability = _binding_availability(artifact_store, binding)
    bound_to = (
        f"published version {binding.published_version} (Source Binding Manifest)"
        if binding.published_version is not None
        else "current registry version (local worktree)"
    )
    authorization = (
        "granted (private Source Artifact Store read verified)"
        if artifact_store is not None
        else "granted (private registry view)"
    )
    digest = binding.content_hash
    print(f"source_id:       {binding.source_id}")
    print(f"content_hash:    {digest[:12]} (sha256, abbreviated)")
    print(f"filename:        {binding.filename or '(none recorded)'}")
    print(f"media_type:      {binding.content_type or '(none recorded)'}")
    print(f"size:            {binding.size if binding.size is not None else '(unknown)'}")
    print(f"bound_to:        {bound_to}")
    print(f"availability:    {availability}")
    print(f"authorization:   {authorization}")
    return 0


def _cmd_source_fetch(args: argparse.Namespace) -> int:
    binding, artifact_store = _resolve_source_binding(args)
    if artifact_store is None:
        raise CliError(
            "no private Source Artifact Store is configured (LUMIO_SOURCE_STORE); "
            "fetch retrieves retained Source Artifacts only — record one with "
            "'lumio-wiki setup --source-store <s3-uri|path>'",
            exit_code=1,
        )
    try:
        raw = fetch_verified_artifact(artifact_store, binding)
    except SourceInspectionError as exc:
        raise CliError(str(exc), exit_code=1) from exc
    destination = safe_fetch_destination(Path(args.output), binding)
    try:
        destination.write_bytes(raw)
    except OSError as exc:
        raise CliError(
            f"could not write the fetched artifact to --output: {exc}",
            exit_code=1,
        ) from exc
    print(f"fetched: {destination} ({len(raw)} bytes, digest and size verified)")
    return 0


def _cmd_source_link(args: argparse.Namespace) -> int:
    from lumio_wiki.artifact_store import ArtifactAccessDenied, SigningUnavailable

    binding, artifact_store = _resolve_source_binding(args)
    if artifact_store is None:
        raise CliError(
            "no private Source Artifact Store is configured (LUMIO_SOURCE_STORE); "
            "signed links are issued by the S3 adapter — record a store with "
            "'lumio-wiki setup --source-store <s3-uri>'",
            exit_code=1,
        )
    try:
        duration = parse_expires(args.expires)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    if not artifact_store.supports_signing():
        raise CliError(
            "the configured Source Artifact Store does not support signed URLs "
            "(only the S3 adapter signs); use 'source fetch' for verified "
            "byte-exact retrieval",
            exit_code=1,
        )
    # Verify the exact bound artifact is present and digest-correct first, so
    # an issued link downloads the same digest (issue #165 AC) and an
    # unavailable/corrupt artifact is a distinct outcome, not a dead URL.
    try:
        fetch_verified_artifact(artifact_store, binding)
    except SourceInspectionError as exc:
        raise CliError(str(exc), exit_code=1) from exc
    try:
        url = artifact_store.signed_get_url(
            source_id=binding.source_id,
            content_hash=binding.content_hash,
            expires_in=duration,
        )
    except SigningUnavailable as exc:
        raise CliError(str(exc), exit_code=1) from exc
    except ArtifactAccessDenied as exc:
        raise CliError(str(exc), exit_code=1) from exc
    seconds = duration // timedelta(seconds=1)
    expiry_note = f"{args.expires or DEFAULT_LINK_EXPIRES} ({seconds}s)"
    print(url)
    print(f"expires:         {expiry_note}")
    print(
        "handling:        treat this URL as a bearer secret — never persist, "
        "log, or paste it into tracked files; it authorizes exactly this "
        "object and the GET method"
    )
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


def _add_skill_target_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    target = parser.add_mutually_exclusive_group(required=required)
    target.add_argument(
        "--scope",
        choices=["user", "project"],
        default=None,
        help=(
            "Shared Agent Skills scope: user installs under ~/.agents/skills; "
            "project installs under ./.agents/skills. Defaults to user when omitted."
        ),
    )
    target.add_argument(
        "--agent",
        choices=["pi", "hermes", "codex", "claude-code"],
        default=None,
        help="Compatibility target for a client-specific user skill directory.",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Override a selected agent's destination directory.",
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
            "Canonical project bootstrap (issue #161; ADR-0017/0019). "
            "Maintainer form: 'setup <local-kb> [--publish-to <s3-uri>]' "
            "creates or adopts a local Knowledge Base and records optional "
            "S3 publication, retrieval backend, and private source-store "
            "settings in .env. Reader form: 'setup --from <s3-uri>' "
            "configures a read-only project against an existing S3 Knowledge "
            "Base Location. After setup, subsequent lumio-wiki commands load "
            "LUMIO_KB_PATH from .env automatically, so no <kb> argument is "
            "needed. Setup never installs optional capabilities for you: it "
            "prints the exact command (pip install 'lumio-wiki[s3]' / "
            "pip install 'lumio-lancedb[s3]') and writes no credentials."
        ),
    )
    setup_parser.add_argument(
        "kb_path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Local Knowledge Base root directory (created if it does not "
            "exist) — the Maintainer form. Mutually exclusive with --from."
        ),
    )
    setup_parser.add_argument(
        "--from",
        dest="from_uri",
        default=None,
        metavar="S3-URI",
        help=(
            "Existing S3 Knowledge Base Location URI (e.g. s3://bucket/kb) — "
            "configures a read-only project; pathless reads resolve the "
            "active Published Version. Requires lumio-wiki[s3]."
        ),
    )
    setup_parser.add_argument(
        "--publish-to",
        dest="publish_to",
        default=None,
        metavar="S3-URI",
        help=(
            "S3 publication destination URI recorded as LUMIO_PUBLISH_TO "
            "(Maintainer form only; requires lumio-wiki[s3]). 'publish-s3' "
            "reads it when no destination argument is given."
        ),
    )
    setup_parser.add_argument(
        "--retrieval",
        choices=["zero-index", "lancedb"],
        default=None,
        help=(
            "Retrieval backend recorded as LUMIO_RETRIEVAL_BACKEND "
            "(zero-index is always available; lancedb requires "
            "lumio-lancedb[s3]). The retrieval mode (lexical/semantic/"
            "hybrid) stays a separate setting."
        ),
    )
    setup_parser.add_argument(
        "--source-store",
        dest="source_store",
        default=None,
        metavar="URI-OR-PATH",
        help=(
            "Private Source Artifact Store recorded as LUMIO_SOURCE_STORE "
            "(ADR-0020): an object-store URI (s3://bucket/prefix; requires "
            "lumio-wiki[s3]) or a local directory path (development). "
            "Refused when the project's .env is tracked by git."
        ),
    )
    setup_parser.add_argument(
        "--artifact-retention",
        dest="artifact_retention",
        default=None,
        choices=["required", "disabled"],
        help=(
            "Source Artifact retention policy recorded as "
            "LUMIO_ARTIFACT_RETENTION (issue #164, ADR-0020): 'required' "
            "blocks publication activation while a referenced non-synthetic "
            "source lacks a verified artifact; 'disabled' (the default) "
            "keeps hash-only behavior. 'required' needs --source-store."
        ),
    )
    setup_skill_target = setup_parser.add_mutually_exclusive_group()
    setup_skill_target.add_argument(
        "--agent",
        choices=["pi", "hermes", "codex", "claude-code"],
        default=None,
        help="Explicitly install the Agent Skill for this compatibility target.",
    )
    setup_skill_target.add_argument(
        "--skill-scope",
        choices=["user", "project"],
        default=None,
        help="Explicitly install the shared Agent Skill at user or project scope.",
    )
    setup_parser.add_argument(
        "--no-agents-md",
        action="store_true",
        help="Skip writing/updating the AGENTS.md section.",
    )
    setup_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace a stale or corrupt skill when an install target is requested.",
    )
    setup_parser.set_defaults(func=_cmd_setup)

    # init
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a lower-level Knowledge Base directory (no project wiring).",
        description=(
            "Create a new categorized Knowledge Base root with a seeded Control "
            "File. This is the lower-level KB-only operation; prefer 'setup' for "
            "a project's first run, which also writes .env and AGENTS.md."
        ),
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
        help="Retrieve citation-ready Evidence: lexical (default), semantic, or hybrid.",
        description=(
            "Retrieval over the Knowledge Base. Default mode is zero-index lexical "
            "(no index or model required). --mode semantic/hybrid add embedding-based "
            "retrieval and need lumio-lancedb + an embedder (lumio-lancedb[embeddings] "
            "or LUMIO_PROVIDER_*)."
        ),
    )
    _add_kb_argument(search_parser)
    search_parser.add_argument("query", type=str, help="Search query.")
    search_parser.add_argument("--limit", type=int, default=20, help="Max results (default: 20).")
    search_parser.add_argument(
        "--mode",
        choices=("lexical", "semantic", "hybrid"),
        default=None,
        help=(
            "Retrieval mode (default: lexical, or LUMIO_RETRIEVAL_MODE from env/.env). "
            "semantic/hybrid need lumio-lancedb + an embedder."
        ),
    )
    search_parser.add_argument(
        "--model",
        default=None,
        help="Embedding model: local sentence-transformers name or provider model id.",
    )
    search_parser.add_argument(
        "--index-dir",
        default=None,
        help="Derived LanceDB index dir for semantic/hybrid (default: <kb>/.lumio/lance).",
    )
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
            "LUMIO_PROVIDER_* env vars). Ignored in managed mode."
        ),
    )
    ingest_parser.add_argument(
        "--compiled-page",
        type=Path,
        default=None,
        help=(
            "Managed host-Distiller mode (issue #149): path to the host-agent-"
            "authored Compiled Page Markdown to bind to the original source. "
            "Required together with --source-id; when both are given the "
            "original raw source is registered under that stable identity and "
            "the authored page (which must declare the id in sources[].id) is "
            "staged as one proposal. No document converter runs, so PDF/DOCX/"
            "HTML sources do not need the [documents] extra."
        ),
    )
    ingest_parser.add_argument(
        "--source-id",
        type=str,
        default=None,
        help=(
            "Managed host-Distiller mode (issue #149): explicit, stable "
            "Knowledge Source identity for the original raw source (a lowercase "
            "ASCII label). Required together with --compiled-page."
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
        nargs="?",
        default=None,
        help=(
            "Object-store destination URI (e.g. s3://bucket/kb). When "
            "omitted, LUMIO_PUBLISH_TO from the environment or project .env "
            "(written by 'setup --publish-to') is used (issue #161)."
        ),
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
    publish_s3_parser.add_argument(
        "--retrieval",
        choices=["zero-index", "lancedb"],
        default="zero-index",
        help=(
            "Retrieval artifacts to publish (issue #163). zero-index (default) "
            "publishes canonical content + Discovery Graph only; lancedb also "
            "builds and health-checks a remote LanceDB index under the "
            "version's derived/lance/ prefix BEFORE activation — an index "
            "build or health failure blocks the pointer advance. Needs "
            "lumio-lancedb."
        ),
    )
    publish_s3_parser.set_defaults(func=_cmd_publish_s3)

    # rollback-s3 (issue #163, ADR-0019)
    rollback_s3_parser = subparsers.add_parser(
        "rollback-s3",
        help="CAS-activate an already complete S3 Published Version.",
        description=(
            "Conditionally activate a prior complete immutable S3 Published "
            "Version (compare-and-swap on the activation pointer). The target "
            "version is validated complete and never rebuilt or overwritten; a "
            "stale rollback fails closed. Requires lumio-wiki[s3]."
        ),
    )
    rollback_s3_parser.add_argument(
        "destination",
        type=str,
        help="Object-store Knowledge Base URI (e.g. s3://bucket/kb).",
    )
    rollback_s3_parser.add_argument(
        "--version",
        required=True,
        type=str,
        help="The complete immutable version to activate.",
    )
    rollback_s3_parser.add_argument(
        "--expected-pointer-version",
        default=None,
        type=str,
        help=(
            "Active version expected before rolling back (compare-and-swap "
            "guard); omit to CAS against the currently observed pointer."
        ),
    )
    rollback_s3_parser.set_defaults(func=_cmd_rollback_s3)

    # cleanup-s3 (issue #163, ADR-0019: report-only)
    cleanup_s3_parser = subparsers.add_parser(
        "cleanup-s3",
        help="Report inactive incomplete S3 version prefixes (cleanup candidates).",
        description=(
            "List version prefixes without a manifest — the residue of "
            "interrupted or failed publications that can never be activated. "
            "Report-only: nothing is deleted (issue #163)."
        ),
    )
    cleanup_s3_parser.add_argument(
        "destination",
        type=str,
        help="Object-store Knowledge Base URI (e.g. s3://bucket/kb).",
    )
    cleanup_s3_parser.set_defaults(func=_cmd_cleanup_s3)

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

    # relationship — stage typed Relationship proposals (issue #151).
    relationship_parser = subparsers.add_parser(
        "relationship",
        help="Stage typed Relationship proposals through the proposal pipeline.",
        description=(
            "Stage reviewable, typed Relationship proposals through the ordinary "
            "proposal pipeline (issue #151). A typed Relationship is a canonical, "
            "reviewed edge resolved by Canonical Page Title — distinct from "
            "`cross-link --stage`, which only adds authored Markdown links that "
            "become Extracted References (ADR-0011). Never direct-writes; never "
            "infers a relationship type."
        ),
    )
    relationship_sub = relationship_parser.add_subparsers(
        dest="relationship_command",
        required=True,
        metavar="<relationship command>",
    )
    relationship_stage = relationship_sub.add_parser(
        "stage",
        help="Stage one reviewable typed Relationship proposal.",
        description=(
            "Stage one reviewable typed Relationship proposal with an explicit "
            "source Canonical Page Title, target Canonical Page Title, and "
            "relationship type. Delegates to the public proposal pipeline; the "
            "Knowledge Base on disk is unchanged until `publish`. Missing "
            "source/target, an unresolved target, or a duplicate edge produce "
            "actionable diagnostics. A non-preferred type stages as a "
            "warning-level generic edge (preferred types: contradicts, "
            "derived-from, extends, implements, relates-to, replaces, uses)."
        ),
    )
    _add_kb_argument(relationship_stage)
    relationship_stage.add_argument(
        "source",
        type=str,
        help="Source Canonical Page Title (the page that owns the Relationship).",
    )
    relationship_stage.add_argument(
        "target",
        type=str,
        help="Target Canonical Page Title (the page the Relationship points at).",
    )
    relationship_stage.add_argument(
        "--type",
        required=True,
        type=str,
        help=(
            "Explicit Relationship type (e.g. relates-to, uses, extends, "
            "implements, contradicts, derived-from, replaces). Non-preferred "
            "types stage as warning-level generic edges; the type is never inferred."
        ),
    )
    _add_ingest_dir_argument(relationship_stage)
    relationship_stage.set_defaults(func=_cmd_relationship_stage)

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
            "Enable LanceDB semantic/hybrid stages. Without --model, uses a "
            "deterministic, offline hash embedder (no provider, no network); "
            "pass --model (or set LUMIO_PROVIDER_*) to score a real embedding "
            "model, mirroring `search --model`."
        ),
    )
    eval_parser.add_argument(
        "--synonym",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="Collapse a paraphrase to a shared token for the --semantic hash embedder.",
    )
    eval_parser.add_argument(
        "--model",
        default=None,
        help=(
            "Embedding model for --semantic: local sentence-transformers name or "
            "provider model id. Without --model (and no LUMIO_PROVIDER_*), "
            "--semantic uses the hermetic deterministic hash embedder."
        ),
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
        "Discovery Graph impact (issue #127). Read-only unless --stage is given; --stage "
        "repairs candidates as authored Markdown links (Extracted References in the "
        "Discovery Graph) — it never creates typed canonical Relationships. Use "
        "`lumio-wiki relationship stage` to promote a typed Relationship. Staging "
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
        help="Locate, install, inspect, or update the packaged Agent Skill.",
        description=(
            "Manage explicit cross-client, project, or compatibility-target Agent Skill "
            "copies. The installed wheel remains the canonical contract."
        ),
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
        help="Explicitly install the canonical skill bundle.",
        description=(
            "Install into shared user/project .agents/skills or a supported client's "
            "compatibility directory. Installation never happens as a package side effect."
        ),
    )
    _add_skill_target_arguments(skill_install, required=True)
    skill_install.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly replace an existing stale or corrupt destination.",
    )
    skill_install.set_defaults(func=_cmd_skill_install)

    skill_status_parser = skill_sub.add_parser(
        "status",
        help="Report missing, current, stale, or corrupt without modifying files.",
        description="Compare an installed copy and manifest with the current wheel bundle.",
    )
    _add_skill_target_arguments(skill_status_parser, required=False)
    skill_status_parser.set_defaults(func=_cmd_skill_status)

    skill_update = skill_sub.add_parser(
        "update",
        help="Explicitly refresh an installed skill from the current wheel.",
        description=(
            "Atomically refresh a stale or corrupt destination. Missing destinations must "
            "be installed first."
        ),
    )
    _add_skill_target_arguments(skill_update, required=False)
    skill_update.set_defaults(func=_cmd_skill_update)

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

    # Authorized Source Artifact inspection (issue #165, ADR-0020): the three
    # read-only operations resolve ONE exact Source Version behind a source
    # id — never fetch-by-hash, never an object key, never a silent fallback
    # to the latest version when a binding is requested but absent.
    source_inspect = source_sub.add_parser(
        "inspect",
        help="Inspect the exact Source Version bound to a source id.",
        description=(
            "Report secret-free inspection metadata for ONE exact Source "
            "Version: safe filename, media type, size, digest abbreviation, "
            "publication binding, verified artifact availability, and the "
            "authorization outcome. A local worktree resolves the registry's "
            "current version; an S3 Knowledge Base (or --published-version) "
            "resolves the private Source Binding Manifest of exactly one "
            "Published Version."
        ),
    )
    _add_kb_argument(source_inspect)
    _add_ingest_dir_argument(source_inspect)
    source_inspect.add_argument(
        "--source-id", required=True, help="Knowledge Source identity to inspect."
    )
    source_inspect.add_argument(
        "--published-version",
        default=None,
        metavar="VERSION",
        help=(
            "Resolve the exact binding of this Published Version instead of "
            "the local registry's current version (required semantics for "
            "historical inspection; never falls back to the latest version)."
        ),
    )
    source_inspect.set_defaults(func=_cmd_source_inspect)

    source_fetch = source_sub.add_parser(
        "fetch",
        help="Fetch the byte-exact Source Artifact bound to a source id.",
        description=(
            "Retrieve the exact original bytes for the resolved Source "
            "Version, re-verify digest and size, and write them to an "
            "explicit --output destination (a directory receives the safe "
            "filename). Raw artifacts stay private: they never become public "
            "Evidence, and content is not rendered or converted here — open "
            "the fetched file with an appropriate sandboxed tool."
        ),
    )
    _add_kb_argument(source_fetch)
    _add_ingest_dir_argument(source_fetch)
    source_fetch.add_argument(
        "--source-id", required=True, help="Knowledge Source identity to fetch."
    )
    source_fetch.add_argument(
        "--published-version",
        default=None,
        metavar="VERSION",
        help="Resolve the exact binding of this Published Version.",
    )
    source_fetch.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit destination file path (an existing directory receives the safe filename).",
    )
    source_fetch.set_defaults(func=_cmd_source_fetch)

    source_link = source_sub.add_parser(
        "link",
        help="Issue a short-lived signed GET URL for the exact bound artifact.",
        description=(
            "Explicitly issue a signed GET URL for ONE exact artifact when "
            "the Source Artifact Store supports signing (the S3 adapter "
            "does). Default expiry five minutes, maximum one hour. The URL "
            "is a temporary bearer secret: never persist or log it. Prefer "
            "verified 'source fetch' — a signed URL can leak through "
            "conversation history."
        ),
    )
    _add_kb_argument(source_link)
    _add_ingest_dir_argument(source_link)
    source_link.add_argument(
        "--source-id", required=True, help="Knowledge Source identity to link."
    )
    source_link.add_argument(
        "--published-version",
        default=None,
        metavar="VERSION",
        help="Resolve the exact binding of this Published Version.",
    )
    source_link.add_argument(
        "--expires",
        default=DEFAULT_LINK_EXPIRES,
        metavar="DURATION",
        help="Signed URL lifetime like 30s, 5m (default), or 1h; maximum one hour.",
    )
    source_link.set_defaults(func=_cmd_source_link)

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
