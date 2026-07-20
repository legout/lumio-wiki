"""Knowledge Base Control File and portable published artifacts (issue #77).

Tests exercise the PUBLIC Core SDK seam: the versioned root Control File
(``lumio.yaml``), the reserved Activity Log and Hot Index artifacts, the
Legacy Flat Mode migration warning, and publication/regeneration of the
reserved-artifact set. They never reach into private parsing helpers.

Acceptance mapping (issue #77):
- Control file is versioned, carries the seeded catalog and Hot Index pins,
  and travels with the KB.
- Category/pin controls are validated as KB-local content controls.
- Valid marked Navigation Indexes, Activity Logs, and Hot Indexes are excluded
  from loading/retrieval/fingerprinting; invalid collisions are diagnosed.
- Successful publication regenerates Navigation Indexes and the Hot Index, then
  appends one grep-friendly Activity Log entry after KB state succeeds.
- The Activity Log excludes Reader queries, failed/discarded proposals, and
  unpublished uploads (it is written only via the explicit publish seam).
- No-control-file Knowledge Bases load in Legacy Flat Mode with a non-blocking
  migration warning.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lumio.core import (
    ACTIVITY_LOG_ARTIFACT,
    ACTIVITY_LOG_BASENAME,
    CONTROL_FILE_BASENAME,
    HOT_INDEX_ARTIFACT,
    HOT_INDEX_BASENAME,
    KB_MODE_CATEGORIZED,
    SEED_CATEGORY_CATALOG,
    ContentCategory,
    ControlFileError,
    HotIndexPin,
    KnowledgeBaseControlFile,
    NavigationIndexCollisionError,
    SourceFingerprint,
    append_activity_log_entry,
    fingerprint_sources,
    generate_hot_index,
    load_control_file,
    load_knowledge_base,
    make_activity_log_entry,
    publish_hot_index,
    publish_reserved_artifacts,
    regenerate_reserved_artifacts,
    seeded_control_file,
    validate,
    validate_proposed_control_file,
    write_control_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _page_markdown(title: str, summary: str, *, path: str, visibility: str = "internal") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        'tags: ["test"]\n'
        f'summary: "{summary}"\n'
        'lifecycle: "approved"\n'
        f'visibility: "{visibility}"\n'
        'sources:\n'
        '  - id: "test-src"\n'
        '    title: "Test source"\n'
        "---\n\n"
        f"# {title}\n\nbody\n"
    )


def _write_page(root: Path, rel: str, title: str, summary: str, **kwargs) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_page_markdown(title, summary, path=rel, **kwargs))


def _copy_categorized_kb(tmp_path: Path) -> Path:
    base = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", base)
    return base


def _build_categorized_kb(tmp_path: Path, *, pins: list[str] | None = None) -> Path:
    """Build a writable categorized KB with a seeded control file and two pages."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "concepts/overview.md", "Lumio Overview", "Root-level overview")
    _write_page(root, "entities/acme.md", "Acme Corp", "A named organization")
    control = seeded_control_file()
    if pins is not None:
        control = KnowledgeBaseControlFile(
            version=control.version,
            categories=list(control.categories),
            hot_index=[HotIndexPin(title=t) for t in pins],
            mode=control.mode,
        )
    write_control_file(root, control)
    return root


# ---------------------------------------------------------------------------
# Criterion 1: versioned Control File carries the catalog and pins, travels
# with the KB.
# ---------------------------------------------------------------------------


def test_control_file_loads_with_catalog_and_pins():
    kb, report = load_knowledge_base(FIXTURES / "categorized_kb")
    assert report.is_valid
    assert kb.control is not None
    assert kb.control.version == 1
    assert kb.control.mode == KB_MODE_CATEGORIZED
    names = [c.name for c in kb.control.categories]
    assert names == [
        "concepts",
        "entities",
        "references",
        "procedures",
        "tables",
        "datasets",
        "synthesis",
    ]
    assert [p.title for p in kb.control.hot_index] == ["Lumio Overview", "Acme Corp"]


def test_seeded_control_file_has_seven_seed_categories():
    control = seeded_control_file()
    assert control.version == 1
    assert [c.name for c in control.categories] == [c.name for c in SEED_CATEGORY_CATALOG]
    assert control.hot_index == []
    assert control.mode == KB_MODE_CATEGORIZED


def test_write_control_file_round_trips_and_is_deterministic(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    control = KnowledgeBaseControlFile(
        version=1,
        categories=[
            ContentCategory(name="concepts", description="Core ideas."),
            ContentCategory(name="entities"),
        ],
        hot_index=[HotIndexPin(title="Overview"), HotIndexPin(title="Acme", note="Pinned")],
    )
    first = write_control_file(root, control)
    second = write_control_file(root, control)
    assert first == second
    assert first.read_text() == second.read_text(), "control file must be byte-stable"

    # It is valid YAML and round-trips through the loader.
    loaded, report = load_knowledge_base(root)
    assert loaded.control is not None
    assert [c.name for c in loaded.control.categories] == ["concepts", "entities"]
    assert loaded.control.categories[0].description == "Core ideas."
    assert loaded.control.categories[1].description is None
    assert [p.title for p in loaded.control.hot_index] == ["Overview", "Acme"]
    assert loaded.control.hot_index[1].note == "Pinned"


def test_load_control_file_parse_seam_returns_control_or_none(tmp_path):
    root = _build_categorized_kb(tmp_path)
    assert load_control_file(root) is not None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert load_control_file(empty) is None


def test_load_control_file_raises_on_malformed_file(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: not-an-int\nmode: categorized\ncategories: []\n"
    )
    with pytest.raises(ControlFileError):
        load_control_file(root)


def test_control_file_is_fingerprinted_as_canonical_content(tmp_path):
    root = _build_categorized_kb(tmp_path)
    fp = fingerprint_sources(root)
    assert isinstance(fp, SourceFingerprint)
    assert CONTROL_FILE_BASENAME in {s.path for s in fp.sources}


def test_control_file_change_changes_fingerprint(tmp_path):
    root = _build_categorized_kb(tmp_path)
    before = fingerprint_sources(root)
    # Edit the control file (canonical KB content).
    control = load_control_file(root)
    assert control is not None
    new_control = KnowledgeBaseControlFile(
        version=control.version,
        categories=list(control.categories),
        hot_index=list(control.hot_index) + [HotIndexPin(title="Lumio Overview")],
        mode=control.mode,
    )
    write_control_file(root, new_control)
    after = fingerprint_sources(root)
    assert before.digest != after.digest, "control file change must change the fingerprint"


# ---------------------------------------------------------------------------
# Criterion 2: Category/pin controls validated as KB-local content controls,
# not Compiled Pages or app-only settings.
# ---------------------------------------------------------------------------


def test_control_file_is_not_loaded_as_a_compiled_page(tmp_path):
    root = _build_categorized_kb(tmp_path)
    kb, _ = load_knowledge_base(root)
    paths = {p.path for p in kb.pages}
    assert CONTROL_FILE_BASENAME not in paths
    # The .yaml control file is never scanned as a Markdown Compiled Page.
    assert all(p.path.endswith(".md") for p in kb.pages)


def test_unsupported_control_file_version_is_blocking(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 99\nmode: categorized\ncategories:\n  - concepts\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(
        i for i in report.issues
        if i.file == CONTROL_FILE_BASENAME and i.field == "version"
    )
    assert "99" in issue.message


def test_absent_categories_falls_back_to_seed(tmp_path):
    """An absent ``categories`` declaration applies the seeded default
    catalog and validates clean (ADR-0009). The Control File carries the
    resolved seed catalog so downstream loading, validation, and Navigation
    Index generation treat the seed as first-class declared categories."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "concepts/overview.md", "Overview", "An overview")
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: "categorized"\n'
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]
    assert [c.name for c in kb.control.categories] == [
        c.name for c in SEED_CATEGORY_CATALOG
    ]


def test_empty_categories_falls_back_to_seed(tmp_path):
    """An explicit empty ``categories: []`` declaration applies the seeded
    default catalog and validates clean (ADR-0009)."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "concepts/overview.md", "Overview", "An overview")
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: "categorized"\ncategories: []\n'
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]
    assert [c.name for c in kb.control.categories] == [
        c.name for c in SEED_CATEGORY_CATALOG
    ]


def test_empty_bare_category_name_is_blocking(tmp_path):
    """An empty bare-string category (``- ""``) is rejected, not silently
    admitted as a nameless category (issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: categorized\ncategories:\n  - ""\n  - concepts\n'
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    assert any(
        i.field == "categories" and "non-empty" in i.message for i in report.issues
    )
    # The parse seam raises rather than returning a control record carrying "".
    with pytest.raises(ControlFileError):
        load_control_file(root)


def test_whitespace_bare_category_name_is_blocking(tmp_path):
    """A whitespace-only bare-string category is treated as empty (issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: categorized\ncategories:\n  - "   "\n'
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid


def test_unsupported_control_file_mode_is_blocking(tmp_path):
    """A Control File ``mode`` outside the supported set is a blocking error,
    not silently accepted (issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 1\nmode: bogus-mode\ncategories:\n  - concepts\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if i.field == "mode")
    assert "not supported" in issue.message
    assert "categorized" in issue.message
    with pytest.raises(ControlFileError):
        load_control_file(root)


def test_legacy_flat_mode_in_present_control_file_is_blocking(tmp_path):
    """``legacy-flat`` means there is no Control File; a present Control File
    that declares ``mode: legacy-flat`` is contradictory and must be rejected
    as a blocking error. Legacy Flat Mode is entered only by omitting the
    Control File entirely (ADR-0008, issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "An overview")
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 1\nmode: legacy-flat\ncategories:\n  - concepts\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if i.field == "mode")
    assert "legacy-flat" in issue.message
    assert "no control file" in issue.message
    # The parse seam raises rather than returning a control record.
    with pytest.raises(ControlFileError):
        load_control_file(root)


def test_duplicate_category_is_blocking(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 1\nmode: categorized\n"
        "categories:\n  - name: concepts\n  - name: concepts\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    assert any("duplicate content category: concepts" in i.message for i in report.issues)


def test_unresolved_hot_index_pin_is_blocking(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "concepts/overview.md", "Overview", "An overview")
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 1\nmode: categorized\ncategories:\n  - concepts\n"
        "hot_index:\n  - title: Ghost Page\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(
        i for i in report.issues if i.field == "hot_index" and "Ghost Page" in i.message
    )
    assert "unresolved Hot Index pin" in issue.message


def test_resolved_hot_index_pins_are_valid(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    _, report = load_knowledge_base(root)
    assert report.is_valid


def test_blank_bare_hot_index_pin_title_is_blocking():
    """A bare-string Hot Index pin with a blank/whitespace title is rejected
    structurally, not silently admitted as a nameless pin (issue #77)."""
    proposed = KnowledgeBaseControlFile(
        version=1,
        categories=[ContentCategory(name="concepts")],
        hot_index=[HotIndexPin(title="   ")],
        mode="categorized",
    )
    issues = validate_proposed_control_file(proposed, None)
    assert any(
        i.field == "hot_index" and "non-empty" in i.message for i in issues
    )


def test_whitespace_mapping_hot_index_pin_title_is_blocking(tmp_path):
    """A mapping Hot Index pin with a whitespace-only ``title`` is rejected as
    structurally invalid, matching the bare-string form (issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "An overview")
    (root / CONTROL_FILE_BASENAME).write_text(
        "version: 1\nmode: categorized\ncategories:\n  - concepts\n"
        "hot_index:\n  - title: \"   \"\n    note: \"pinned\"\n"
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    assert any(
        i.field == "hot_index" and "non-empty" in i.message for i in report.issues
    )
    # The parse seam raises rather than returning a control record with a blank pin.
    with pytest.raises(ControlFileError):
        load_control_file(root)


# ---------------------------------------------------------------------------
# Criterion 3: valid marked Activity Log and Hot Index are excluded from
# loading/retrieval/fingerprinting; invalid collisions are diagnosed.
# ---------------------------------------------------------------------------


def _marked_reserved(basename: str, artifact: str, body: str = "# Reserved\n") -> str:
    return f"---\nlumio:\n  artifact: {artifact}\n  version: 1\n---\n\n{body}\n"


def test_marked_activity_log_is_excluded_from_pages_and_fingerprint(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / ACTIVITY_LOG_BASENAME).write_text(
        _marked_reserved(ACTIVITY_LOG_BASENAME, ACTIVITY_LOG_ARTIFACT, "# Activity Log\n")
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    assert ACTIVITY_LOG_BASENAME not in {p.path for p in kb.pages}
    fp = fingerprint_sources(root)
    assert ACTIVITY_LOG_BASENAME not in {s.path for s in fp.sources}


def test_marked_hot_index_is_excluded_from_pages_and_fingerprint(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / HOT_INDEX_BASENAME).write_text(
        _marked_reserved(HOT_INDEX_BASENAME, HOT_INDEX_ARTIFACT, "# Hot Index\n")
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    assert HOT_INDEX_BASENAME not in {p.path for p in kb.pages}
    fp = fingerprint_sources(root)
    assert HOT_INDEX_BASENAME not in {s.path for s in fp.sources}


def test_unmarked_log_md_is_blocking_collision(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / ACTIVITY_LOG_BASENAME).write_text(_page_markdown("My Log", "Authored", path="log.md"))
    report = validate(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if Path(i.file).name.lower() == ACTIVITY_LOG_BASENAME)
    assert issue.severity == "error"
    assert "Activity Log" in issue.message


def test_unmarked_hot_md_is_blocking_collision(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / HOT_INDEX_BASENAME).write_text(_page_markdown("My Hot", "Authored", path="hot.md"))
    report = validate(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if Path(i.file).name.lower() == HOT_INDEX_BASENAME)
    assert issue.severity == "error"
    assert "Hot Index" in issue.message


def test_case_insensitive_hot_md_collision_is_blocking(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / "HOT.MD").write_text(_page_markdown("Upper Hot", "Authored", path="HOT.MD"))
    report = validate(root)
    assert not report.is_valid
    assert any(Path(i.file).name.lower() == HOT_INDEX_BASENAME for i in report.issues)


def test_malformed_hot_index_marker_is_diagnosed(tmp_path):
    root = _build_categorized_kb(tmp_path)
    (root / HOT_INDEX_BASENAME).write_text(
        "---\nlumio:\n  artifact: hot-index\n  version: 7\n---\n\n# Bad\n"
    )
    report = validate(root)
    assert not report.is_valid
    assert any(
        "version" in i.message and "Hot Index" in i.message
        for i in report.issues
        if Path(i.file).name.lower() == HOT_INDEX_BASENAME
    )


def test_wrong_artifact_on_reserved_basename_is_diagnosed(tmp_path):
    """A navigation-index marker on log.md is the wrong artifact for that basename."""
    root = _build_categorized_kb(tmp_path)
    (root / ACTIVITY_LOG_BASENAME).write_text(
        "---\nlumio:\n  artifact: navigation-index\n  version: 1\n---\n\n# Wrong\n"
    )
    report = validate(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if Path(i.file).name.lower() == ACTIVITY_LOG_BASENAME)
    assert "activity-log" in issue.message
    assert "navigation-index" in issue.message


def test_valid_hot_index_does_not_change_canonical_fingerprint(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    publish_hot_index(root)
    before = fingerprint_sources(root)

    # Edit only the generated hot index (reserved, excluded).
    (root / HOT_INDEX_BASENAME).write_text(
        _marked_reserved(HOT_INDEX_BASENAME, HOT_INDEX_ARTIFACT, "# Hot Index (edited)\n")
    )
    after = fingerprint_sources(root)
    assert before.digest == after.digest


# ---------------------------------------------------------------------------
# Criterion 4: publication regenerates Navigation Indexes and the Hot Index,
# and appends one grep-friendly Activity Log entry after KB state succeeds.
# ---------------------------------------------------------------------------


def test_generate_hot_index_renders_pinned_titles_in_pin_order(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Acme Corp", "Lumio Overview"])
    kb, _ = load_knowledge_base(root)
    hot = generate_hot_index(kb.control, kb.pages)
    assert hot is not None
    assert "artifact: hot-index" in hot
    # Pin order is preserved (Acme before Overview).
    assert hot.index("[Acme Corp]") < hot.index("[Lumio Overview]")
    assert "entities/acme.md" in hot
    assert "concepts/overview.md" in hot


def test_generate_hot_index_none_without_control_file(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "An overview")
    kb, _ = load_knowledge_base(root)
    assert kb.control is None
    assert generate_hot_index(kb.control, kb.pages) is None


def test_generate_hot_index_none_without_pins(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=[])
    kb, _ = load_knowledge_base(root)
    assert generate_hot_index(kb.control, kb.pages) is None


def test_publish_reserved_artifacts_generates_nav_indexes_and_hot_index(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview", "Acme Corp"])
    written = publish_reserved_artifacts(root)
    written_names = {p.name.lower() for p in written}
    assert "index.md" in written_names
    assert HOT_INDEX_BASENAME in written_names

    root_index = (root / "index.md").read_text()
    assert "Lumio Overview" in root_index
    assert "Acme Corp" in root_index

    hot = (root / HOT_INDEX_BASENAME).read_text()
    assert "artifact: hot-index" in hot
    assert "[Lumio Overview]" in hot
    assert "[Acme Corp]" in hot


def test_publish_reserved_artifacts_prunes_hot_index_when_pins_removed(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    publish_reserved_artifacts(root)
    assert (root / HOT_INDEX_BASENAME).exists()

    # Remove all pins; publication must prune the now-empty Hot Index.
    control = load_control_file(root)
    assert control is not None
    write_control_file(
        root,
        KnowledgeBaseControlFile(
            version=control.version,
            categories=list(control.categories),
            hot_index=[],
            mode=control.mode,
        ),
    )
    publish_reserved_artifacts(root)
    assert not (root / HOT_INDEX_BASENAME).exists()


def test_regenerate_reserved_artifacts_regenerates_hot_index_without_churn(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    publish_reserved_artifacts(root)
    hot_before = (root / HOT_INDEX_BASENAME).read_text()

    changed = regenerate_reserved_artifacts(root)
    hot_after = (root / HOT_INDEX_BASENAME).read_text()
    assert hot_before == hot_after, "fresh hot index must not be churned on regenerate"
    # Nothing was force-rewritten on a fresh tree.
    assert not changed


def test_publish_reserved_artifacts_blocked_by_reserved_collision(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    (root / HOT_INDEX_BASENAME).write_text(
        _page_markdown("Authored Hot", "collision", path="hot.md")
    )
    with pytest.raises(NavigationIndexCollisionError, match="Hot Index"):
        publish_reserved_artifacts(root)


def test_append_activity_log_creates_file_with_marker(tmp_path):
    root = _build_categorized_kb(tmp_path)
    entry = make_activity_log_entry(
        operation="publish",
        description="Published pages: concepts/overview.md",
        timestamp=datetime(2026, 7, 16, 17, 13, 25, tzinfo=UTC),
    )
    append_activity_log_entry(root, entry)
    log = (root / ACTIVITY_LOG_BASENAME).read_text()
    assert "artifact: activity-log" in log
    assert "# Activity Log" in log
    assert "2026-07-16T17:13:25Z publish: Published pages: concepts/overview.md" in log


def test_append_activity_log_appends_one_line_preserving_history(tmp_path):
    root = _build_categorized_kb(tmp_path)
    first = make_activity_log_entry(
        operation="publish",
        description="First publish",
        timestamp=datetime(2026, 7, 16, 17, 13, 25, tzinfo=UTC),
    )
    second = make_activity_log_entry(
        operation="publish",
        description="Second publish",
        timestamp=datetime(2026, 7, 17, 9, 0, 0, tzinfo=UTC),
    )
    append_activity_log_entry(root, first)
    append_activity_log_entry(root, second)
    log = (root / ACTIVITY_LOG_BASENAME).read_text()
    # Exactly one marker block (never regenerated) and both entries in order.
    assert log.count("artifact: activity-log") == 1
    lines = [
        line for line in log.splitlines()
        if line.startswith("2026-")
    ]
    assert lines == [
        "2026-07-16T17:13:25Z publish: First publish",
        "2026-07-17T09:00:00Z publish: Second publish",
    ]


def test_activity_log_entry_is_grep_friendly():
    entry = make_activity_log_entry(
        operation="publish",
        description="Published pages: a.md, b.md",
        timestamp=datetime(2026, 7, 16, 17, 13, 25, tzinfo=UTC),
    )
    line = f"{entry.timestamp} {entry.operation}: {entry.description}"
    # Greppable by date, by operation, and by content.
    assert line.startswith("2026-07-16")
    assert " publish: " in line
    assert "a.md" in line


def test_activity_log_collapses_newlines_in_description(tmp_path):
    root = _build_categorized_kb(tmp_path)
    entry = make_activity_log_entry(
        operation="publish",
        description="line one\nline two",
        timestamp=datetime(2026, 7, 16, 17, 13, 25, tzinfo=UTC),
    )
    append_activity_log_entry(root, entry)
    log_lines = (root / ACTIVITY_LOG_BASENAME).read_text().splitlines()
    entry_lines = [line for line in log_lines if line.startswith("2026-")]
    assert len(entry_lines) == 1, "a multi-line description must remain one log line"


# ---------------------------------------------------------------------------
# Criterion 5: the Activity Log excludes Reader queries, failed/discarded
# proposals, and unpublished uploads. It is written only via the explicit
# publish seam, never by retrieval, ingestion staging, or discard paths.
# ---------------------------------------------------------------------------


def test_activity_log_not_created_by_loading_or_retrieval(tmp_path):
    """Loading and fingerprinting a categorized KB never creates an Activity Log."""
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    load_knowledge_base(root)
    fingerprint_sources(root)
    assert not (root / ACTIVITY_LOG_BASENAME).exists()


def test_activity_log_not_created_when_regeneration_runs(tmp_path):
    """Regeneration (sync path) never appends an Activity Log entry."""
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    regenerate_reserved_artifacts(root)
    assert not (root / ACTIVITY_LOG_BASENAME).exists()


def test_activity_log_not_created_when_artifact_publish_runs(tmp_path):
    """Artifact regeneration on the publish path never appends an Activity Log.

    Only the explicit :func:`append_activity_log_entry` seam writes the log, and
    the app publish flow calls it once, only after the KB state is committed.
    """
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview"])
    publish_reserved_artifacts(root)
    assert not (root / ACTIVITY_LOG_BASENAME).exists()


# ---------------------------------------------------------------------------
# Criterion 6: no-control-file Knowledge Bases load in Legacy Flat Mode with a
# non-blocking migration warning.
# ---------------------------------------------------------------------------


def test_legacy_kb_without_control_file_loads_with_migration_warning():
    kb, report = load_knowledge_base(FIXTURES / "valid")
    assert kb.control is None
    assert report.is_valid, "legacy warning must be non-blocking"
    warning = next(
        (i for i in report.issues if i.file == CONTROL_FILE_BASENAME), None
    )
    assert warning is not None
    assert warning.severity == "warning"
    assert "Legacy Flat Mode" in warning.message


def test_legacy_kb_validates_non_blocking(tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "An overview", visibility="public")
    report = validate(root)
    assert report.is_valid
    assert any(
        i.severity == "warning" and "Legacy Flat Mode" in i.message for i in report.issues
    )


def test_legacy_kb_publishes_only_navigation_indexes(tmp_path):
    """A legacy KB publish regenerates Navigation Indexes but no Hot Index or Log."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "An overview", visibility="public")
    publish_reserved_artifacts(root)
    assert (root / "index.md").exists()
    assert not (root / HOT_INDEX_BASENAME).exists()
    assert not (root / ACTIVITY_LOG_BASENAME).exists()


# ---------------------------------------------------------------------------
# Round-trip: the published categorized KB tree is valid and portable.
# ---------------------------------------------------------------------------


def test_publish_then_validate_categorized_kb_is_portable(tmp_path):
    root = _build_categorized_kb(tmp_path, pins=["Lumio Overview", "Acme Corp"])
    publish_reserved_artifacts(root)
    append_activity_log_entry(
        root,
        make_activity_log_entry(operation="publish", description="initial publish"),
    )

    # The published tree re-validates cleanly with all reserved artifacts present.
    kb, report = load_knowledge_base(root)
    assert report.is_valid
    assert kb.control is not None
    paths = {p.path for p in kb.pages}
    assert "index.md" not in paths
    assert HOT_INDEX_BASENAME not in paths
    assert ACTIVITY_LOG_BASENAME not in paths
    assert (root / "index.md").exists()
    assert (root / HOT_INDEX_BASENAME).exists()
    assert (root / ACTIVITY_LOG_BASENAME).exists()


# ---------------------------------------------------------------------------
# Maintainer proposal/validation seam for the Control File (issue #77). A
# proposed Control File is validated against the current Compiled Pages without
# ever touching the live Knowledge Base.
# ---------------------------------------------------------------------------


def test_validate_proposed_control_file_reports_unresolved_pins(tmp_path):
    """A proposed Control File with an unresolved Hot Index pin is flagged
    against the current Compiled Pages (issue #77)."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "concepts/overview.md", "Overview", "An overview")
    kb, report = load_knowledge_base(root)
    assert report.is_valid

    proposed = KnowledgeBaseControlFile(
        version=1,
        categories=[ContentCategory(name="concepts")],
        hot_index=[HotIndexPin(title="Ghost Page")],
        mode="categorized",
    )
    issues = validate_proposed_control_file(proposed, kb.pages)
    assert any(
        getattr(i, "field", "") == "hot_index" and "Ghost Page" in getattr(i, "message", "")
        for i in issues
    )

    # A resolved pin validates cleanly.
    valid = KnowledgeBaseControlFile(
        version=1,
        categories=[ContentCategory(name="concepts")],
        hot_index=[HotIndexPin(title="Overview")],
        mode="categorized",
    )
    assert validate_proposed_control_file(valid, kb.pages) == []

    # The proposed Control File never touched the live Knowledge Base.
    assert (root / CONTROL_FILE_BASENAME).exists() is False


def test_validate_proposed_control_file_empty_catalog_falls_back_to_seed(tmp_path):
    """An empty proposed category catalog applies the seeded default and is
    valid (ADR-0009). A Maintainer proposing a Control File without an
    explicit catalog gets the seed, just like an absent declaration."""
    proposed = KnowledgeBaseControlFile(
        version=1,
        categories=[],
        hot_index=[],
        mode="categorized",
    )
    issues = validate_proposed_control_file(proposed, None)
    assert issues == [], [i.message for i in issues]


# ---------------------------------------------------------------------------
# Extensible Content Category catalog (ADR-0009, issue #84). A KB's Control
# File may declare additional slug-valid Content Categories beyond the seeded
# default; declared categories are first-class for loading, validation,
# Navigation Index generation, and retrieval. Categories are never auto-created
# from content — declaring, adding, or removing one is a reviewed Maintainer
# action via Control File change.
# ---------------------------------------------------------------------------


def _write_declared_catalog_kb(
    tmp_path: Path,
    catalog: str,
    *,
    pages: list[tuple[str, str, str]] | None = None,
) -> Path:
    """Build a categorized KB with an explicit declared catalog and pages.

    ``catalog`` is the raw YAML under ``categories:``; ``pages`` is a list of
    ``(relative_path, title, summary)`` tuples written as minimal valid
    Compiled Pages.
    """
    root = tmp_path / "kb"
    root.mkdir()
    for rel, title, summary in pages or []:
        _write_page(root, rel, title, summary)
    (root / CONTROL_FILE_BASENAME).write_text(
        f'version: 1\nmode: "categorized"\ncategories:\n{catalog}'
    )
    return root


def test_declared_non_seed_categories_load_and_validate_clean(tmp_path):
    """A Control File declaring categories beyond the seed loads and validates
    clean (ADR-0009). ``projects`` and ``journal`` are not in the seed but are
    first-class once declared."""
    root = _write_declared_catalog_kb(
        tmp_path,
        "  - projects\n  - journal\n  - concepts\n",
        pages=[
            ("projects/launch.md", "Launch Plan", "A project plan"),
            ("journal/2026-01.md", "January Notes", "Journal entries"),
            ("concepts/overview.md", "Overview", "An overview"),
        ],
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]
    assert [c.name for c in kb.control.categories] == [
        "projects",
        "journal",
        "concepts",
    ]


def test_page_in_declared_non_seed_category_validates(tmp_path):
    """A Compiled Page whose path-derived category is a declared non-seed
    category validates clean (ADR-0009)."""
    root = _write_declared_catalog_kb(
        tmp_path,
        "  - projects\n",
        pages=[("projects/launch.md", "Launch Plan", "A project plan")],
    )
    _, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]


def test_page_in_undeclared_category_fails_validation(tmp_path):
    """A Compiled Page whose path-derived category is not in the KB's declared
    catalog fails validation with an actionable message (ADR-0009)."""
    root = _write_declared_catalog_kb(
        tmp_path,
        "  - concepts\n",
        pages=[("projects/secret.md", "Secret Project", "Undeclared")],
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if i.field == "category")
    assert "projects" in issue.message
    assert "projects/secret.md" == issue.file
    assert "ADR-0009" in issue.message


def test_page_in_undeclared_category_fails_under_seed_default(tmp_path):
    """When the catalog falls back to seed, a page under a non-seed category
    still fails validation (ADR-0009). The seed is the effective catalog."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "projects/secret.md", "Secret Project", "Undeclared")
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: "categorized"\n'
    )
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    issue = next(i for i in report.issues if i.field == "category")
    assert "projects" in issue.message


def test_invalid_category_slugs_are_blocking(tmp_path):
    """Invalid category slugs are rejected with clear messages (ADR-0009).
    Slugs must be lowercase ASCII letters/digits/hyphens, leading letter,
    bounded length."""
    catalog = "  - Projects\n  - 1bad\n  - 'has space'\n  - concepts\n"
    root = _write_declared_catalog_kb(tmp_path, catalog)
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    messages = " | ".join(i.message for i in report.issues if i.field == "categories")
    assert "'Projects'" in messages
    assert "'1bad'" in messages
    assert "'has space'" in messages
    # ``concepts`` is valid and not flagged.
    assert "'concepts'" not in messages


def test_reserved_category_names_are_blocking(tmp_path):
    """Category names colliding with reserved basenames/markers are rejected
    (ADR-0007, ADR-0009): ``index``, ``hot``, ``log``, and the ``lumio``
    marker system."""
    catalog = "  - index\n  - hot\n  - log\n  - lumio\n  - concepts\n"
    root = _write_declared_catalog_kb(tmp_path, catalog)
    _, report = load_knowledge_base(root)
    assert not report.is_valid
    messages = " | ".join(i.message for i in report.issues if i.field == "categories")
    for reserved in ("index", "hot", "log", "lumio"):
        assert reserved in messages


def test_extensible_catalog_appears_in_navigation_indexes(tmp_path):
    """Declared categories render as first-class directories in Navigation
    Indexes, identical to seeded ones (ADR-0009). A page under a declared
    non-seed category produces a per-directory index just like a seed one."""
    from lumio.core import generate_navigation_indexes

    root = _write_declared_catalog_kb(
        tmp_path,
        "  - concepts\n  - projects\n",
        pages=[
            ("concepts/overview.md", "Overview", "An overview"),
            ("projects/launch.md", "Launch Plan", "A project plan"),
        ],
    )
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]
    indexes = generate_navigation_indexes(kb.pages)
    # Both category directories get a per-directory index alongside the root.
    assert "index.md" in indexes
    assert "concepts/index.md" in indexes
    assert "projects/index.md" in indexes
    assert "Launch Plan" in indexes["projects/index.md"]
    assert "Overview" in indexes["concepts/index.md"]
    # The root index catalogs both declared categories equally.
    assert "## concepts" in indexes["index.md"]
    assert "## projects" in indexes["index.md"]


def test_legacy_flat_mode_unaffected_by_extensible_catalog(tmp_path):
    """A Knowledge Base with no Control File (Legacy Flat Mode) is unaffected:
    root-level pages load valid and no category routing is enforced
    (ADR-0008, ADR-0009)."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(root, "overview.md", "Overview", "Root-level page")
    _write_page(root, "notes.md", "Notes", "Another root-level page")
    kb, report = load_knowledge_base(root)
    assert kb.control is None
    warnings = [i for i in report.issues if i.severity == "warning"]
    assert any("Legacy Flat Mode" in w.message for w in warnings), [
        w.message for w in warnings
    ]
    assert report.is_valid
    # No category-path validation fires in Legacy Flat Mode.
    assert not any(i.field == "category" for i in report.issues)


def test_categories_never_auto_created_from_content(tmp_path):
    """Categories are never inferred from page paths or frontmatter: a page
    under an undeclared directory does not add the directory to the catalog
    (ADR-0009 Maintainer gate). The Control File catalog is unchanged."""
    root = _write_declared_catalog_kb(
        tmp_path,
        "  - concepts\n",
        pages=[("projects/secret.md", "Secret Project", "Undeclared")],
    )
    kb, report = load_knowledge_base(root)
    assert not report.is_valid
    # The catalog still carries only ``concepts`` — never auto-extended.
    assert [c.name for c in kb.control.categories] == ["concepts"]


def test_load_control_file_parse_seam_validates_declared_catalog(tmp_path):
    """The parse seam (``load_control_file``) applies the same slug and
    reserved-name validation as the full loader (ADR-0009)."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: "categorized"\ncategories:\n  - Projects\n'
    )
    with pytest.raises(ControlFileError) as exc_info:
        load_control_file(root)
    assert "Projects" in str(exc_info.value)


def test_load_control_file_parse_seam_accepts_declared_non_seed(tmp_path):
    """The parse seam accepts a declared non-seed category (ADR-0009)."""
    root = tmp_path / "kb"
    root.mkdir()
    (root / CONTROL_FILE_BASENAME).write_text(
        'version: 1\nmode: "categorized"\ncategories:\n  - projects\n'
    )
    control = load_control_file(root)
    assert control is not None
    assert [c.name for c in control.categories] == ["projects"]
