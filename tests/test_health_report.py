"""Deterministic health report for the Knowledge Base."""

from pathlib import Path
from shutil import copytree

from lumio_wiki import KnowledgeBase, load_knowledge_base
from lumio_wiki.records import HealthReport

VALID_FIXTURES = Path(__file__).parent / "fixtures" / "valid"


def _write_page(
    root: Path,
    name: str,
    title: str,
    tags: list[str],
    summary: str | None,
    lifecycle: str | None,
    visibility: str | None,
    sources: list[dict[str, str]],
    aliases: list[str] | None = None,
    relationships: list[dict[str, str]] | None = None,
    body: str = "# Page\n\nContent.\n",
) -> None:
    lines = ["---"]
    lines.append(f'title: "{title}"')
    if aliases is not None:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f'  - "{alias}"')
    lines.append("tags:")
    for tag in tags:
        lines.append(f'  - "{tag}"')
    if summary is not None:
        lines.append(f'summary: "{summary}"')
    if lifecycle is not None:
        lines.append(f'lifecycle: "{lifecycle}"')
    if visibility is not None:
        lines.append(f'visibility: "{visibility}"')
    if sources:
        lines.append("sources:")
        for source in sources:
            lines.append(f'  - id: "{source["id"]}"')
            lines.append(f'    title: "{source["title"]}"')
    if relationships:
        lines.append("relationships:")
        for rel in relationships:
            lines.append(f'  - target: "{rel["target"]}"')
            lines.append(f'    type: "{rel["type"]}"')
    lines.append("---")
    lines.append("")
    lines.append(body)
    (root / name).write_text("\n".join(lines))


def test_healthy_kb_report(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    copytree(VALID_FIXTURES, root)
    kb, _ = load_knowledge_base(root)
    indexed = kb.build_index(tmp_path / "index")

    report = indexed.health_report()

    assert isinstance(report, HealthReport)
    assert report.is_healthy is True
    assert report.stale_index is False
    assert report.missing_summaries == []
    assert report.broken_relationships == []
    assert report.duplicate_aliases == []
    assert report.invalid_fields == []
    assert report.unknown_relationship_types == []


def test_missing_summaries_reported(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "page.md",
        title="Page",
        aliases=["Page"],
        tags=["page"],
        summary=None,
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "page-source", "title": "Page Source"}],
    )

    kb, _ = load_knowledge_base(root)
    report = kb.health_report()

    assert report.missing_summaries == ["page.md"]
    assert report.is_healthy is False


def test_broken_relationships_reported(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "a.md",
        title="A",
        aliases=["A"],
        tags=["a"],
        summary="Page A",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "a-source", "title": "A Source"}],
        relationships=[{"target": "Missing Page", "type": "relates-to"}],
    )
    _write_page(
        root,
        "b.md",
        title="B",
        aliases=["B"],
        tags=["b"],
        summary="Page B",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "b-source", "title": "B Source"}],
    )

    kb, _ = load_knowledge_base(root)
    report = kb.health_report()

    assert report.broken_relationships == ["a.md"]
    assert report.is_healthy is False


def test_stale_index_reported(tmp_path: Path) -> None:
    tmp_root = tmp_path / "kb"
    copytree(VALID_FIXTURES, tmp_root)
    index_dir = tmp_path / "index"

    kb, _ = load_knowledge_base(tmp_root)
    indexed = kb.build_index(index_dir)
    assert indexed.health_report().stale_index is False
    assert indexed.health_report().is_healthy is True

    page = tmp_root / "overview.md"
    page.write_text(page.read_text() + "\n<!-- changed -->\n")

    reloaded, _ = load_knowledge_base(tmp_root)
    stale_kb = KnowledgeBase(
        root=reloaded.root, pages=reloaded.pages, index_dir=index_dir
    )
    report = stale_kb.health_report()

    assert report.stale_index is True
    assert report.is_healthy is False


def test_unknown_relationship_types_reported(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "a.md",
        title="A",
        aliases=["A"],
        tags=["a"],
        summary="Page A",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "a-source", "title": "A Source"}],
        relationships=[{"target": "B", "type": "custom-relationship"}],
    )
    _write_page(
        root,
        "b.md",
        title="B",
        aliases=["B"],
        tags=["b"],
        summary="Page B",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "b-source", "title": "B Source"}],
    )

    kb, _ = load_knowledge_base(root)
    report = kb.health_report()

    assert report.unknown_relationship_types == ["a.md"]
    assert report.is_healthy is False


def test_duplicate_aliases_reported(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "a.md",
        title="A",
        aliases=["Shared"],
        tags=["a"],
        summary="Page A",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "a-source", "title": "A Source"}],
    )
    _write_page(
        root,
        "b.md",
        title="B",
        aliases=["Shared"],
        tags=["b"],
        summary="Page B",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "b-source", "title": "B Source"}],
    )

    kb, _ = load_knowledge_base(root)
    report = kb.health_report()

    assert report.duplicate_aliases == ["Shared"]
    assert report.is_healthy is False


def test_invalid_fields_reported(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "page.md",
        title="Page",
        aliases=["Page"],
        tags=["page"],
        summary="A page.",
        lifecycle=None,
        visibility="public",
        sources=[{"id": "page-source", "title": "Page Source"}],
    )

    kb, _ = load_knowledge_base(root)
    report = kb.health_report()

    assert report.invalid_fields == ["page.md"]
    assert report.is_healthy is False


def test_health_report_is_deterministic_and_zero_llm(tmp_path: Path) -> None:
    """health_report is deterministic and has no LLM/model dependencies."""
    root = tmp_path / "kb"
    root.mkdir()
    _write_page(
        root,
        "a.md",
        title="A",
        aliases=["Shared"],
        tags=["a"],
        summary="Page A",
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "a-source", "title": "A Source"}],
        relationships=[{"target": "Missing", "type": "custom-rel"}],
    )
    _write_page(
        root,
        "b.md",
        title="B",
        aliases=["Shared"],
        tags=["b"],
        summary=None,
        lifecycle="approved",
        visibility="public",
        sources=[{"id": "b-source", "title": "B Source"}],
    )

    kb, _ = load_knowledge_base(root)
    first = kb.health_report()
    second = kb.health_report()

    assert first == second
    assert first.missing_summaries == ["b.md"]
    assert first.broken_relationships == ["a.md"]
    assert first.duplicate_aliases == ["Shared"]
    assert first.invalid_fields == []
    assert first.unknown_relationship_types == ["a.md"]
    assert first.is_healthy is False
