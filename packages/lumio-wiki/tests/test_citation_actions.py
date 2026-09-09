"""Issue #177: openable citation actions and the shared Reader route contract.

``lumio_wiki.citation_actions`` owns the public contract for OPENING a cited
Compiled Page: the full app's standalone Compiled Page route (``/kb/page/…``),
the optional Reader base URL (``LUMIO_READER_BASE_URL``), and the labelled
open actions derived for a page or citation. The route contract is defined
HERE, in the Core SDK, and consumed by the full ``lumio`` application, so the
CLI can never hard-code a guessed ``/kb/...`` path that drifts from the app.

Safety rules under test (issue #177, ADR-0020):

- Browser links appear only when a valid Reader base URL is configured.
- The base URL is validated and normalized; it is never an object-store URI
  and is never derived from S3 object locations.
- External Source URLs and private Source actions are visibly distinct,
  labelled lines — a private Source action is always an explicit command
  (``source inspect``), never an implicitly emitted signed/public URL.
"""

from __future__ import annotations

import pytest
from lumio_wiki.citation_actions import (
    READER_PAGE_PATH_TEMPLATE,
    ReaderBaseURLError,
    citation_open_actions,
    normalize_reader_base_url,
    page_open_actions,
    page_open_command,
    reader_page_path,
    reader_page_url,
    render_open_actions,
    source_inspect_command,
)
from lumio_wiki.records import CitationOpenActions, CompiledPage, Source

# ---------------------------------------------------------------------------
# Reader route contract (shared with the full application)
# ---------------------------------------------------------------------------


def test_reader_page_path_template_matches_the_app_route() -> None:
    assert READER_PAGE_PATH_TEMPLATE == "/kb/page/{title}"


def test_reader_page_path_encodes_the_title_segment() -> None:
    assert reader_page_path("Architecture") == "/kb/page/Architecture"
    assert reader_page_path("Lumio Overview") == "/kb/page/Lumio%20Overview"
    # A slash inside a title must never escape the single path segment.
    assert reader_page_path("a/b") == "/kb/page/a%2Fb"
    assert reader_page_path("Überblick") == "/kb/page/%C3%9Cberblick"


def test_reader_page_url_joins_base_and_path_without_double_slash() -> None:
    assert (
        reader_page_url("https://lumio.example.com", "Lumio Overview")
        == "https://lumio.example.com/kb/page/Lumio%20Overview"
    )
    # A trailing slash on the base is normalized away before joining.
    assert (
        reader_page_url("https://lumio.example.com/", "Architecture")
        == "https://lumio.example.com/kb/page/Architecture"
    )
    # Deployment sub-paths are preserved.
    assert (
        reader_page_url("https://example.com/lumio", "Architecture")
        == "https://example.com/lumio/kb/page/Architecture"
    )


# ---------------------------------------------------------------------------
# Reader base URL validation and normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("https://lumio.example.com", "https://lumio.example.com"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("https://lumio.example.com/", "https://lumio.example.com"),
        ("https://lumio.example.com///", "https://lumio.example.com"),
        ("https://example.com/lumio/", "https://example.com/lumio"),
    ],
)
def test_normalize_reader_base_url_accepts_http_bases(raw: str, normalized: str) -> None:
    assert normalize_reader_base_url(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "lumio.example.com",  # no scheme
        "ftp://example.com",  # non-browser scheme
        # Object-store URIs are never public browser URLs (issue #177).
        "s3://bucket/kb",
        "gs://bucket/kb",
        "https://",  # no host
        "https://user:pass@example.com",  # credentials in URL
        "https://example.com/kb?x=1",  # query string
        "https://example.com/kb#frag",  # fragment
        "https://example.com/kb\x00",  # embedded NUL
        "not a url at all",
    ],
)
def test_normalize_reader_base_url_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ReaderBaseURLError):
        normalize_reader_base_url(raw)


def test_reader_base_url_error_is_a_value_error_for_caller_ergonomics() -> None:
    assert issubclass(ReaderBaseURLError, ValueError)


# ---------------------------------------------------------------------------
# Open commands
# ---------------------------------------------------------------------------


def test_page_open_command_is_copyable() -> None:
    assert page_open_command("Lumio Overview") == 'lumio-wiki page "Lumio Overview"'


def test_page_open_command_is_shell_safe_for_hostile_titles() -> None:
    """A Canonical Page Title can never alter the printed command (review)."""
    command = page_open_command('Evil" $(rm -rf /) `x` \\y')
    assert command == 'lumio-wiki page "Evil\\" \\$(rm -rf /) \\`x\\` \\\\y"'


def test_source_inspect_command_is_explicit_and_never_a_url() -> None:
    command = source_inspect_command("annual-report")
    assert command == "lumio-wiki source inspect --source-id annual-report"
    assert "://" not in command


def test_source_inspect_command_quotes_unsafe_ids() -> None:
    """A hostile source id is quoted instead of interpolated bare (review)."""
    command = source_inspect_command("x; rm -rf /")
    assert command == 'lumio-wiki source inspect --source-id "x; rm -rf /"'


def test_s3_open_commands_pin_the_resolved_published_version() -> None:
    """Copyable S3 actions must not follow a later ``current.json`` pointer."""
    location = "s3://public-bucket/team-kb"
    assert (
        page_open_command("Lumio Overview", location, "v2026-08-21")
        == 'lumio-wiki page "s3://public-bucket/team-kb" "Lumio Overview" '
        "--published-version v2026-08-21"
    )
    assert (
        source_inspect_command("lumio-overview", location, "v2026-08-21")
        == 'lumio-wiki source inspect "s3://public-bucket/team-kb" '
        "--source-id lumio-overview --published-version v2026-08-21"
    )
    # Local actions deliberately retain their historical, version-free form.
    assert page_open_command("Lumio Overview", "/tmp/kb") == (
        'lumio-wiki page "/tmp/kb" "Lumio Overview"'
    )


# ---------------------------------------------------------------------------
# CitationOpenActions derivation
# ---------------------------------------------------------------------------


def _page(**overrides: object) -> CompiledPage:
    fields: dict[str, object] = {
        "path": "overview.md",
        "title": "Lumio Overview",
        "id": "entity:lumio-overview",
        "sources": [Source(id="lumio-overview", title="Lumio landing page",
                           url="https://example.com/lumio")],
        "body": "Body.",
    }
    fields.update(overrides)
    return CompiledPage(**fields)  # type: ignore[arg-type]


def test_page_open_actions_carry_identity_and_command() -> None:
    actions = page_open_actions(_page())
    assert actions.page_title == "Lumio Overview"
    assert actions.page_path == "overview.md"
    assert actions.entity_id == "entity:lumio-overview"
    assert actions.open_command == 'lumio-wiki page "Lumio Overview"'
    # No Reader base URL configured: no browser link is emitted.
    assert actions.reader_url is None
    # Page-level actions do not invent Source provenance.
    assert actions.source_id is None
    assert actions.source_url is None
    assert actions.source_command == ""


def test_page_open_actions_include_reader_url_only_when_configured() -> None:
    actions = page_open_actions(_page(), reader_base_url="https://lumio.example.com/")
    assert actions.reader_url == "https://lumio.example.com/kb/page/Lumio%20Overview"
    assert page_open_actions(_page()).reader_url is None


def test_citation_open_actions_label_source_provenance() -> None:
    actions = citation_open_actions(
        page_title="Lumio Overview",
        page_path="overview.md",
        entity_id="entity:lumio-overview",
        source_id="lumio-overview",
        source_url="https://example.com/lumio",
        reader_base_url="https://lumio.example.com",
    )
    assert actions.source_id == "lumio-overview"
    assert actions.source_url == "https://example.com/lumio"
    # The private Source action is an explicit inspect command, never a URL.
    assert actions.source_command == "lumio-wiki source inspect --source-id lumio-overview"


def test_citation_open_actions_never_invent_source_fields() -> None:
    actions = citation_open_actions(page_title="T", page_path="t.md")
    assert actions.entity_id is None
    assert actions.reader_url is None
    assert actions.source_id is None
    assert actions.source_url is None
    assert actions.source_command == ""


def test_citation_open_actions_carry_the_resolved_s3_version() -> None:
    actions = citation_open_actions(
        page_title="Lumio Overview",
        page_path="overview.md",
        source_id="lumio-overview",
        kb_location="s3://public-bucket/team-kb",
        published_version="v1",
    )
    assert actions.published_version == "v1"
    assert actions.open_command.endswith("--published-version v1")
    assert actions.source_command.endswith("--published-version v1")


def test_citation_open_actions_with_invalid_base_url_raise_for_actionability() -> None:
    with pytest.raises(ReaderBaseURLError):
        citation_open_actions(
            page_title="T",
            page_path="t.md",
            reader_base_url="s3://bucket/kb",
        )


# ---------------------------------------------------------------------------
# Labelled rendering
# ---------------------------------------------------------------------------


def test_render_open_actions_emit_labelled_stable_lines() -> None:
    actions = citation_open_actions(
        page_title="Lumio Overview",
        page_path="overview.md",
        entity_id="entity:lumio-overview",
        source_id="lumio-overview",
        source_url="https://example.com/lumio",
        reader_base_url="https://lumio.example.com",
    )
    lines = render_open_actions(actions)
    assert lines[0] == 'open:            lumio-wiki page "Lumio Overview"'
    assert lines[1].startswith("web:  ")
    assert lines[1].endswith("/kb/page/Lumio%20Overview")
    assert lines[2].startswith("source-url:      ")
    assert lines[2].endswith("https://example.com/lumio")
    assert lines[3].startswith("source-artifact: ")
    assert (
        "lumio-wiki source inspect --source-id lumio-overview" in lines[3]
    )


def test_render_open_actions_skip_absent_actions() -> None:
    lines = render_open_actions(citation_open_actions(page_title="T", page_path="t.md"))
    assert lines == ['open:            lumio-wiki page "T"']


def test_rendered_source_lines_are_never_signed_or_object_urls() -> None:
    """The rendered contract never emits an implicit artifact URL.

    A private Source renders ONLY as the explicit ``source inspect`` command;
    an authored external Source URL is the sole URL that may appear for a
    Source, and it is labelled ``source-url:`` so it cannot be confused with
    a Compiled Page browser link (``web:``).
    """
    actions = citation_open_actions(
        page_title="T",
        page_path="t.md",
        source_id="s",
        source_url="https://example.com/report.pdf",
    )
    lines = render_open_actions(actions)
    assert not any("s3://" in line or "X-Amz" in line for line in lines)
    assert any(line.startswith("source-url:") for line in lines)
    assert any(line.startswith("source-artifact:") for line in lines)


def test_citation_open_actions_record_is_backward_compatible() -> None:
    """New public record defaults every action field so existing consumers
    constructing Citation / RetrievalResult keep working (issue #177 AC)."""
    actions = CitationOpenActions(page_title="T", page_path="t.md")
    assert actions.entity_id is None
    assert actions.open_command == ""
    assert actions.reader_url is None
    assert actions.source_id is None
    assert actions.source_url is None
    assert actions.source_command == ""
