"""Portable external compiled-Markdown import adapter tests."""

from pathlib import Path

import msgspec.yaml as yaml
from lumio_wiki import import_external_compiled_markdown, import_okf_profile1


def _write_vault(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _frontmatter(markdown: str) -> dict:
    _, _, raw = markdown.partition("---\n")
    document, body = raw.split("\n---\n", 1)
    del body
    value = yaml.decode(document)
    return value if isinstance(value, dict) else {}


def test_external_adapter_accepts_any_compiled_markdown_directory(tmp_path):
    vault = _write_vault(
        tmp_path / "arbitrary-vault-name",
        {
            "concepts/Widget.md": (
                "---\n"
                'title: "Widget"\n'
                'summary: "An imported widget."\n'
                "tags: [product]\n"
                "---\n\n# Widget\n\nImported body.\n"
            ),
        },
    )

    parsed = import_external_compiled_markdown(vault)

    assert [page.relative_path for page in parsed.proposed_pages] == [
        "concepts/Widget.md"
    ]
    assert parsed.proposed_pages[0].title == "Widget"
    assert "Imported body." in parsed.proposed_pages[0].markdown
    assert "obsidian" not in parsed.proposed_pages[0].markdown.lower()


def test_external_adapter_is_deterministic_for_same_tree(tmp_path):
    vault = _write_vault(
        tmp_path / "vault",
        {
            "page.md": (
                "---\n"
                'title: "Page"\n'
                "tags: [x]\n"
                "---\n\n# Page\n\nBody.\n"
            )
        },
    )

    first = import_external_compiled_markdown(vault)
    second = import_external_compiled_markdown(vault)

    assert first.bundle_identity == second.bundle_identity
    assert [page.markdown for page in first.proposed_pages] == [
        page.markdown for page in second.proposed_pages
    ]


def test_external_canonical_frontmatter_preserves_synthetic_and_defaults(tmp_path):
    vault = _write_vault(
        tmp_path / "vault",
        {
            "synthetic.md": (
                "---\n"
                'title: "Synthesis"\n'
                'summary: "A derived conclusion."\n'
                "tags: [synthesis]\n"
                "synthetic: true\n"
                "---\n\n# Synthesis\n\nDerived body.\n"
            ),
            "ordinary.md": (
                "---\n"
                'title: "Ordinary"\n'
                "tags: [note]\n"
                "---\n\n# Ordinary\n\nOrdinary body.\n"
            ),
        },
    )

    parsed = import_external_compiled_markdown(vault)
    by_title = {
        page.title: _frontmatter(page.markdown) for page in parsed.proposed_pages
    }

    assert by_title["Synthesis"]["summary"] == "A derived conclusion."
    assert by_title["Synthesis"]["synthetic"] is True
    assert by_title["Ordinary"]["lifecycle"] == "draft"
    assert by_title["Ordinary"]["visibility"] == "internal"
    assert by_title["Ordinary"]["synthetic"] is False
    assert by_title["Ordinary"]["sources"][0]["id"].startswith("okf-profile1:")
    assert "ordinary.md" in by_title["Ordinary"]["sources"][0]["id"]


def test_generic_okf_discloses_dropped_top_level_compiled_page_metadata(tmp_path):
    bundle = _write_vault(
        tmp_path / "generic-okf",
        {
            "page.md": (
                "---\n"
                'title: "Generic"\n'
                'summary: "External-only summary."\n'
                "tags: [generic]\n"
                "aliases: [Unsafe Alias]\n"
                "lifecycle: approved\n"
                "visibility: public\n"
                "synthetic: true\n"
                "sources:\n"
                '  - id: "declared"\n'
                '    title: "Declared Source"\n'
                "relationships:\n"
                '  - target: "Other"\n'
                '    type: "related-to"\n'
                "---\n\n# Generic\n\nBody.\n"
            )
        },
    )

    parsed = import_okf_profile1(bundle)
    data = _frontmatter(parsed.proposed_pages[0].markdown)

    assert "summary" not in data
    assert "aliases" not in data
    assert data["lifecycle"] == "draft"
    assert data["visibility"] == "internal"
    assert data["synthetic"] is False
    assert data["sources"] == [
        {
            "id": f"okf-profile1:{parsed.bundle_identity}:page.md",
            "title": "page.md",
        }
    ]
    assert "relationships" not in data

    dropped_external_messages = [
        diagnostic.message
        for diagnostic in parsed.diagnostics
        if diagnostic.path == "page.md"
        and diagnostic.kind == "external-key"
        and diagnostic.severity == "dropped"
    ]
    for field in {
        "summary",
        "aliases",
        "lifecycle",
        "visibility",
        "sources",
        "relationships",
        "synthetic",
    }:
        assert any(repr(field) in message for message in dropped_external_messages)


def test_external_adapter_preserves_metadata_after_imported_provenance(tmp_path):
    vault = _write_vault(
        tmp_path / "external",
        {
            "page.md": (
                "---\n"
                'title: "External"\n'
                'summary: "External summary."\n'
                "tags: [external]\n"
                "aliases: [External Alias]\n"
                "lifecycle: approved\n"
                "visibility: public\n"
                "synthetic: false\n"
                "sources:\n"
                '  - id: "canonical"\n'
                '    title: "Canonical Source"\n'
                '    url: "https://example.com/source"\n'
                "relationships:\n"
                '  - target: "Other"\n'
                '    type: "related-to"\n'
                "---\n\n# External\n\nBody.\n"
            ),
            "synthetic.md": (
                "---\n"
                'title: "Synthesis"\n'
                "tags: [synthesis]\n"
                "synthetic: true\n"
                "sources:\n"
                '  - id: "declared-input"\n'
                '    title: "Declared Input"\n'
                "---\n\n# Synthesis\n\nBody.\n"
            ),
        },
    )

    parsed = import_external_compiled_markdown(vault)
    by_title = {
        page.title: _frontmatter(page.markdown) for page in parsed.proposed_pages
    }

    external = by_title["External"]
    assert external["summary"] == "External summary."
    assert external["aliases"] == ["External Alias"]
    assert external["lifecycle"] == "approved"
    assert external["visibility"] == "public"
    assert external["synthetic"] is False
    assert external["sources"] == [
        {
            "id": f"okf-profile1:{parsed.bundle_identity}:page.md",
            "title": "page.md",
        },
        {
            "id": "canonical",
            "title": "Canonical Source",
            "url": "https://example.com/source",
        },
    ]
    assert "relationships" not in external
    assert by_title["Synthesis"]["sources"] == [
        {"id": "declared-input", "title": "Declared Input"}
    ]
    # Title-based relationship frontmatter has no canonical equivalent since
    # ADR-0021: it is dropped at canonicalization with an explicit disclosure
    # diagnostic (canonical edges are evidence-bearing Claims).
    assert any(
        diagnostic.kind == "external-key"
        and diagnostic.severity == "dropped"
        and "relationships" in diagnostic.message
        for diagnostic in parsed.diagnostics
    )
