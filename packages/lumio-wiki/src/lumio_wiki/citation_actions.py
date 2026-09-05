"""Openable citation actions and the shared Reader route contract (issue #177).

The full ``lumio`` application renders a cited Compiled Page in the browser
at ``/kb/page/{title}`` (standalone Reading Room document). This module owns
that public route contract inside the Core SDK so the ``lumio-wiki`` CLI and
the application share ONE definition instead of the CLI hard-coding a guessed
``/kb/...`` path: the application consumes :data:`READER_PAGE_PATH_TEMPLATE`
through this module's helpers.

The second concern is the optional deployment Reader base URL
(``LUMIO_READER_BASE_URL``). A browser link is emitted ONLY when a valid
http(s) base URL is explicitly configured; it is validated, normalized, and
NEVER inferred from S3 object locations (an object-store URI is a storage
location, not a public document URL — issue #177, ADR-0013/0020).

The third concern is labelling. :class:`~lumio_wiki.records.CitationOpenActions`
derives the labelled open actions for a page or citation:

- ``open:``  the copyable ``lumio-wiki page "<title>"`` CLI action;
- ``web:``   the optional Reader browser URL;
- ``source-url:`` the authored external ``sources[].url`` (never signed,
  never an object-store key);
- ``source-artifact:`` the EXPLICIT private-Source action — the
  ``lumio-wiki source inspect`` command, never an automatically generated
  signed URL (ADR-0020).

These actions are purely additive rendering metadata: citation grounding
fields (:class:`~lumio_wiki.records.Citation`) are unchanged and
backward-compatible.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from lumio_wiki.records import CitationOpenActions, CompiledPage

#: The full application's standalone Compiled Page route template. Defined
#: here — in the Core SDK — and consumed by the application so the CLI never
#: hard-codes a guessed browser path (issue #177).
READER_PAGE_PATH_TEMPLATE: str = "/kb/page/{title}"

#: URL schemes a public Reader deployment may use. Deliberately excludes every
#: object-store scheme: an S3/GS/Azure URI is private storage, never a public
#: browser URL (issue #177, ADR-0013).
_READER_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Object-store URI schemes that must never surface as user-facing document
#: URLs (issue #177 AC): an authored ``sources[].url`` holding one is storage
#: provenance, not an openable link.
_OBJECT_STORE_URL_SCHEMES: frozenset[str] = frozenset(
    {"s3", "s3a", "gs", "gcs", "az", "abfs"}
)

#: Characters that make a shell word safe to interpolate bare. A subset of
#: ``shlex``-safe characters: no whitespace, no quoting, no expansion.
_SHELL_SAFE_ARG_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _shell_double_quoted(value: str) -> str:
    """Render a copyable, shell-safe double-quoted argument.

    Escapes the characters that remain active inside double quotes
    (backslash, double quote, dollar, backtick) so a hostile Canonical Page
    Title can never alter the printed command.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def _shell_arg(value: str) -> str:
    """Bare when the value is a safe shell word; quoted otherwise."""
    if _SHELL_SAFE_ARG_RE.fullmatch(value):
        return value
    return _shell_double_quoted(value)


def _external_document_url(url: str | None) -> str | None:
    """Pass through only user-facing document URLs (issue #177 AC).

    Object-store URIs are private storage locations — they are never
    presented as document URLs, so they are dropped from the labelled
    ``source-url:`` action entirely.
    """
    if url is None:
        return None
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme in _OBJECT_STORE_URL_SCHEMES:
        return None
    return url


class ReaderBaseURLError(ValueError):
    """An invalid ``LUMIO_READER_BASE_URL`` value (actionable, exit-2 style).

    Subclasses :class:`ValueError` so callers that treat configuration errors
    uniformly keep working.
    """


def normalize_reader_base_url(raw: str) -> str:
    """Validate and normalize a deployment Reader base URL.

    Accepts an explicit ``http``/``https`` origin, optionally with a
    deployment sub-path, and returns it without a trailing slash. Rejects —
    with an actionable :class:`ReaderBaseURLError` — empty values, missing
    schemes/hosts, non-browser schemes (notably every object-store URI),
    embedded credentials, query strings, fragments, and control characters.
    """
    value = (raw or "").strip()
    if not value:
        raise ReaderBaseURLError(
            "Reader base URL is empty; set LUMIO_READER_BASE_URL to an http(s) "
            "origin like https://lumio.example.com or clear it to disable "
            "browser links"
        )
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise ReaderBaseURLError(
            "Reader base URL contains whitespace or control characters; set "
            "LUMIO_READER_BASE_URL to a clean http(s) origin"
        )
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if parts.scheme.lower() not in _READER_URL_SCHEMES:
        raise ReaderBaseURLError(
            f"Reader base URL must start with http:// or https:// (got {value!r}); "
            "object-store URIs are private storage locations, never public "
            "browser URLs"
        )
    if not parts.netloc or not parts.hostname:
        raise ReaderBaseURLError(
            f"Reader base URL is missing a host (got {value!r})"
        )
    if parts.username or parts.password:
        raise ReaderBaseURLError(
            "Reader base URL must not embed credentials; credentials never "
            "belong in a public URL"
        )
    if parts.query or parts.fragment:
        raise ReaderBaseURLError(
            "Reader base URL must be a bare origin (optionally with a "
            "sub-path), without query string or fragment"
        )
    port: int | None
    try:
        port = parts.port  # Raises ValueError for a malformed/out-of-range port.
    except ValueError:
        port = -1
    if port is not None and not 1 <= port <= 65535:
        raise ReaderBaseURLError(
            f"Reader base URL has an invalid port (got {value!r})"
        )
    # urlsplit keeps the case of the scheme; normalize it for joining.
    normalized = (
        value
        if parts.scheme.islower()
        else parts.scheme.lower() + value[len(parts.scheme) :]
    )
    return normalized.rstrip("/")


def reader_page_path(page_title: str) -> str:
    """Build the application's standalone Compiled Page path for a title.

    The Canonical Page Title is percent-encoded as ONE path segment (``/`` is
    encoded too) so a title can never escape the route segment.
    """
    return READER_PAGE_PATH_TEMPLATE.format(title=quote(page_title, safe=""))


def reader_page_url(reader_base_url: str, page_title: str) -> str:
    """Join a validated Reader base URL with a page's browser path."""
    base = normalize_reader_base_url(reader_base_url)
    # urljoin with the normalized (trailing-slash-free) base guarantees the
    # page path is appended exactly once.
    return urljoin(base + "/", reader_page_path(page_title).lstrip("/"))


def page_open_command(page_title: str) -> str:
    """The copyable, shell-safe CLI action that opens a cited Compiled Page."""
    return f"lumio-wiki page {_shell_double_quoted(page_title)}"


def source_inspect_command(source_id: str) -> str:
    """The explicit private-Source action (ADR-0020).

    A private Source Artifact is opened through explicit, inspectable
    commands — never an implicitly emitted signed or public URL. The id is
    quoted only when it is not a safe bare shell word.
    """
    return f"lumio-wiki source inspect --source-id {_shell_arg(source_id)}"


def page_open_actions(
    page: CompiledPage,
    *,
    reader_base_url: str | None = None,
) -> CitationOpenActions:
    """Derive the open actions for a Compiled Page (search/index surfaces)."""
    return citation_open_actions(
        page_title=page.title,
        page_path=page.path,
        entity_id=page.id or None,
        reader_base_url=reader_base_url,
    )


def citation_open_actions(
    *,
    page_title: str,
    page_path: str,
    entity_id: str | None = None,
    source_id: str | None = None,
    source_url: str | None = None,
    reader_base_url: str | None = None,
) -> CitationOpenActions:
    """Derive the labelled open actions for one citation or page.

    Every action is optional and never invented: the browser link appears
    only when a validated Reader base URL is configured, and Source actions
    appear only when a Source id/URL is actually present on the page.
    An invalid Reader base URL raises :class:`ReaderBaseURLError` so
    misconfiguration is actionable rather than silently degrading to no
    links. An authored ``source_url`` that is an object-store URI is
    dropped: object keys are never presented as user-facing document
    URLs (issue #177 AC).
    """
    reader_url = (
        reader_page_url(reader_base_url, page_title) if reader_base_url else None
    )
    return CitationOpenActions(
        page_title=page_title,
        page_path=page_path,
        entity_id=entity_id,
        open_command=page_open_command(page_title) if page_title else "",
        reader_url=reader_url,
        source_id=source_id,
        source_url=_external_document_url(source_url),
        source_command=source_inspect_command(source_id) if source_id else "",
    )


def source_action_lines(
    *,
    source_id: str | None = None,
    source_url: str | None = None,
) -> list[str]:
    """Render ONLY the Source-provenance action lines for one Source.

    Used by surfaces that already printed the page-level open/web actions and
    now label each Source's own provenance distinctly (issue #177).
    """
    actions = CitationOpenActions(
        page_title="",
        page_path="",
        source_id=source_id,
        source_url=_external_document_url(source_url),
        source_command=source_inspect_command(source_id) if source_id else "",
    )
    return render_open_actions(actions)


#: Rendered line labels, aligned with the CLI's ``key:``-prefixed contract.
_OPEN_LABEL = "open:"
_WEB_LABEL = "web:"
_SOURCE_URL_LABEL = "source-url:"
_SOURCE_ARTIFACT_LABEL = "source-artifact:"
_LABEL_PAD = 17  # "source-artifact: " width, keeps value columns aligned


def _line(label: str, value: str) -> str:
    return f"{label:<{_LABEL_PAD - 1}} {value}".rstrip()


def render_open_actions(actions: CitationOpenActions) -> list[str]:
    """Render the labelled open-action lines for one citation.

    Absent actions are skipped entirely (concise human output); machine
    consumers read the same labels from :class:`CitationOpenActions`. The
    private Source Artifact action is ALWAYS the explicit inspect command —
    this renderer structurally cannot emit a signed/public artifact URL.
    """
    lines: list[str] = []
    if actions.open_command:
        lines.append(_line(_OPEN_LABEL, actions.open_command))
    if actions.reader_url:
        lines.append(_line(_WEB_LABEL, actions.reader_url))
    if actions.source_url:
        lines.append(_line(_SOURCE_URL_LABEL, actions.source_url))
    if actions.source_command:
        lines.append(
            _line(_SOURCE_ARTIFACT_LABEL, actions.source_command)
        )
    return lines
