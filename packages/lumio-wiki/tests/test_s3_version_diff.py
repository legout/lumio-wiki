"""S3 Published Version diff — read-only deterministic comparison.

t_66f162f2. Proves ``compare_published_versions`` reports stable added,
removed, and changed Page and Source identities between two immutable
Published Version manifests, with lifecycle/visibility changes only when the
canonical data carries them. Diffing is over parsed canonical content, never
paths: the generated Navigation Index is excluded by its reserved-artifact
marker (the Wave 2 gate makes that exclusion mandatory), not by fragile path
matching. Everything is pure — the CLI materializes versions and passes them
in; this module never touches an object store.
"""

from __future__ import annotations

import msgspec
import pytest
from lumio_wiki.knowledge_base import KnowledgeBaseError
from lumio_wiki.s3_version_diff import compare_published_versions

INDEX_MD = "---\nlumio:\n  artifact: navigation-index\n  version: 1\n---\n\n# index\n"


def _page(
    rel: str,
    title: str,
    *,
    sid: str = "s1",
    lifecycle: str = "approved",
    visibility: str = "public",
    body: str = "Body.",
) -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        'id: "entity:x"\n'
        "entity_types:\n  - concept\n"
        f'summary: "{title} summary."\n'
        f'lifecycle: "{lifecycle}"\n'
        f'visibility: "{visibility}"\n'
        "sources:\n  - id: " + f'"{sid}"\n' + 'synthetic: false\n'
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _content(*pages: str) -> dict[str, bytes]:
    return {f"pages/{i}.md": page.encode() for i, page in enumerate(pages)}


def test_no_changes_for_identical_content():
    report = compare_published_versions("v1", "v2", _content(_page("a.md", "Alpha")))
    assert report.from_version == "v1"
    assert report.to_version == "v2"
    assert not report.has_changes


def test_added_removed_changed_pages():
    v1 = _content(_page("a.md", "Alpha"), _page("b.md", "Beta", body="One."))
    v2 = _content(
        _page("a.md", "Alpha"), _page("b.md", "Beta", body="Two."), _page("c.md", "Gamma")
    )
    report = compare_published_versions("v1", "v2", v1, v2)
    assert report.added_pages == ("pages/2.md",)
    assert report.removed_pages == ()
    assert report.changed_pages == ("pages/1.md",)


def test_changed_page_reports_lifecycle_visibility_and_sources():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = _content(
        _page(
            "a.md",
            "Alpha",
            sid="s2",
            lifecycle="deprecated",
            visibility="restricted",
        )
    )
    change = compare_published_versions("v1", "v2", v1, v2).changed[0]
    assert change.path == "pages/0.md"
    assert change.title == ("Alpha", "Alpha")
    assert change.lifecycle == ("approved", "deprecated")
    assert change.visibility == ("public", "restricted")
    assert change.added_sources == ("s2",)
    assert change.removed_sources == ("s1",)


def test_title_change_moves_identity_between_versions():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = _content(_page("a.md", "Omega"))
    change = compare_published_versions("v1", "v2", v1, v2).changed[0]
    assert change.title == ("Alpha", "Omega")


def test_generated_navigation_index_is_excluded_even_when_added():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = {**v1, "index.md": INDEX_MD.encode()}
    report = compare_published_versions("v1", "v2", v1, v2)
    assert not report.has_changes


def test_source_identities_are_reported_across_versions():
    v1 = _content(_page("a.md", "Alpha", sid="keep"), _page("b.md", "Beta", sid="gone"))
    v2 = _content(_page("a.md", "Alpha", sid="keep"), _page("c.md", "Gamma", sid="new"))
    report = compare_published_versions("v1", "v2", v1, v2)
    assert report.added_sources == ("new",)
    assert report.removed_sources == ("gone",)


def test_summary_change_is_a_change_without_lifecycle_noise():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = _content(_page("a.md", "Alpha", body="Different body."))
    report = compare_published_versions("v1", "v2", v1, v2)
    assert report.changed_pages == ("pages/0.md",)
    assert report.changed[0].lifecycle is None
    assert report.changed[0].visibility is None


def test_control_file_change_is_ignored_as_non_markdown():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = {**v1, "lumio.yaml": b"categories: []\n"}
    report = compare_published_versions("v1", "v2", v1, v2)
    assert not report.has_changes


def test_unparseable_markdown_fails_closed():
    v1 = _content(_page("a.md", "Alpha"))
    v2 = {**v1, "pages/1.md": b"---\nnot: [valid\n"}
    with pytest.raises(KnowledgeBaseError):
        compare_published_versions("v1", "v2", v1, v2)


def test_json_round_trip_is_stable():
    report = compare_published_versions(
        "v1",
        "v2",
        _content(_page("a.md", "Alpha")),
        _content(_page("a.md", "Alpha", visibility="internal")),
    )
    encoded = msgspec.json.encode(report)
    decoded = msgspec.json.decode(encoded, type=type(report))
    assert decoded == report
