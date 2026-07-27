"""OKF Profile 1 consumer-tolerance conformance (issue #139).

The exchange boundary accepts foreign OKF bundles even where canonical Lumio
validation is deliberately stricter. Unknown type values and producer keys are
previewed then disclosed as lossy canonicalization; unresolved prose links stay
as prose with a warning. Only a recognized ``lumio.relationships`` value is a
canonical Relationship and can therefore block publication.
"""

from pathlib import Path

from lumio_wiki import export_okf_profile1, import_okf_profile1, load_knowledge_base


def _write_bundle(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, content in files.items():
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    return root


def test_profile1_tolerates_foreign_metadata_and_broken_prose_links(tmp_path):
    """Foreign metadata and missing prose links remain reviewable and disclosed.

    Profile 1 deliberately does not preserve opaque producer metadata through
    canonical publication: the import diagnostics disclose that loss, while the
    re-export preserves the body link as ordinary Markdown.
    """
    bundle = _write_bundle(
        tmp_path / "foreign",
        {
            "foreign.md": (
                "---\n"
                'title: "Foreign Page"\n'
                'type: "Vendor Blueprint"\n'
                "tags:\n"
                '  - "import"\n'
                "producer_extension:\n"
                '  opaque_flag: "preserve-or-disclose"\n'
                "---\n\n"
                "# Foreign Page\n\n"
                "See [future knowledge](missing.md).\n"
            )
        },
    )

    imported = import_okf_profile1(bundle)

    assert [page.relative_path for page in imported.proposed_pages] == ["foreign.md"]
    canonical = imported.proposed_pages[0].markdown
    assert 'title: "Foreign Page"' in canonical
    assert '  - "import"' in canonical
    assert "type:" not in canonical
    assert "producer_extension:" not in canonical

    diagnostics = {(diagnostic.kind, diagnostic.severity) for diagnostic in imported.diagnostics}
    expected_diagnostics = {
        ("type", "dropped"),
        ("external-key", "dropped"),
        ("broken-link", "warning"),
    }
    assert expected_diagnostics <= diagnostics
    assert all(
        "preserve-or-disclose" not in diagnostic.message for diagnostic in imported.diagnostics
    )

    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    (canonical_root / "foreign.md").write_text(
        imported.proposed_pages[0].markdown, encoding="utf-8"
    )
    knowledge_base, report = load_knowledge_base(canonical_root)
    assert report.is_valid, report

    exported = export_okf_profile1(knowledge_base.pages)
    document = exported.files["foreign.md"]
    assert 'type: "Lumio Compiled Page"' in document
    assert "producer_extension:" not in document
    assert "preserve-or-disclose" not in document
    assert "[future knowledge](missing.md)" in document
    assert exported.broken_body_links[0].source_path == "foreign.md"
