"""Tests for preferred relationship type vocabulary and warning-level validation."""

from pathlib import Path

from lumio.core.knowledge_base import (
    PREFERRED_RELATIONSHIP_TYPES,
    ValidationIssue,
    load_knowledge_base,
    validate,
)


def _write_page(
    directory: Path,
    title: str,
    relationships: list[dict[str, str]] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rel_yaml = ""
    if relationships:
        rel_yaml = "relationships:\n" + "".join(
            f'  - target: "{rel["target"]}"\n    type: "{rel["type"]}"\n'
            for rel in relationships
        )
    content = f"""---
title: "{title}"
tags:
  - "test"
summary: "Test page for relationship validation."
lifecycle: "approved"
visibility: "public"
sources:
  - id: "test-source"
    title: "Test Source"
{rel_yaml}---

# {title}

Test page.
"""
    (directory / f"{title.lower().replace(' ', '_')}.md").write_text(content)



def test_preferred_relationship_types_constant():
    assert PREFERRED_RELATIONSHIP_TYPES == frozenset(
        {
            "relates-to",
            "uses",
            "extends",
            "implements",
            "contradicts",
            "derived-from",
            "replaces",
        }
    )



def test_preferred_types_validate_without_warnings(tmp_path: Path):
    kb_dir = tmp_path / "kb"
    _write_page(
        kb_dir,
        "Source Page",
        relationships=[{"target": "Target Page", "type": "relates-to"}],
    )
    _write_page(
        kb_dir,
        "Target Page",
        relationships=[{"target": "Source Page", "type": "uses"}],
    )

    report = validate(kb_dir)
    assert report.is_valid
    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    assert warnings == []



def test_unknown_relationship_type_produces_warning(tmp_path: Path):
    kb_dir = tmp_path / "kb"
    _write_page(
        kb_dir,
        "Source Page",
        relationships=[{"target": "Target Page", "type": "depends-on"}],
    )
    _write_page(kb_dir, "Target Page")

    report = validate(kb_dir)

    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.field == "relationships"
    assert "depends-on" in warning.message
    assert "preferred" in warning.message.lower()



def test_unknown_type_warning_does_not_make_invalid(tmp_path: Path):
    kb_dir = tmp_path / "kb"
    _write_page(
        kb_dir,
        "Source Page",
        relationships=[{"target": "Target Page", "type": "see-also"}],
    )
    _write_page(kb_dir, "Target Page")

    report = validate(kb_dir)

    assert report.is_valid
    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    assert len(warnings) == 1



def test_free_form_relationship_types_remain_loadable(tmp_path: Path):
    kb_dir = tmp_path / "kb"
    _write_page(
        kb_dir,
        "Source Page",
        relationships=[{"target": "Target Page", "type": "custom-edge"}],
    )
    _write_page(kb_dir, "Target Page")

    kb, report = load_knowledge_base(kb_dir)

    assert len(kb.pages) == 2
    source = next(page for page in kb.pages if page.title == "Source Page")
    assert len(source.relationships) == 1
    assert source.relationships[0].type == "custom-edge"
    assert source.relationships[0].target == "Target Page"

    warnings = [issue for issue in report.issues if issue.severity == "warning"]
    assert len(warnings) == 1
    assert "custom-edge" in warnings[0].message



def test_validation_issue_defaults_to_error_severity():
    issue = ValidationIssue(file="file.md", field="title", message="missing")
    assert issue.severity == "error"
