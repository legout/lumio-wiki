"""Relationship input contract tests (ADR-0021, issue #168).

The title-based canonical ``Relationship(target, type)`` frontmatter input is
gone: ``relationships:`` frontmatter is a blocking error, and canonical graph
edges are evidence-bearing Claims validated against the Control File
(``lumio.yaml`` version 2) ontology. Predicates are closed-world: an unknown
predicate is a blocking finding, not a warning.
"""

from pathlib import Path

from lumio_wiki.knowledge_base import ValidationIssue, load_knowledge_base, validate

_CONTROL_V2 = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    software-system: {}
    library: {}
  predicates:
    uses:
      subject_types:
        - software-system
      object_types:
        - library
        - software-system
"""


def _write_kb(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _flat_page(title: str, *, rel_yaml: str = "") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src"\n    title: "Source"\n'
        f"{rel_yaml}"
        "---\n\n"
        f"# {title}\n\nTest page.\n"
    )


def _entity_page(
    title: str, entity_id: str, entity_types: list[str], *, claims_yaml: str = ""
) -> str:
    types_yaml = "".join(f"  - {t}\n" for t in entity_types)
    claims = f"claims:\n{claims_yaml}" if claims_yaml else ""
    return (
        "---\n"
        f'id: "{entity_id}"\n'
        f'title: "{title}"\n'
        f"entity_types:\n{types_yaml}"
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        f'sources:\n  - id: "src-{entity_id}"\n    title: "{title} Source"\n'
        f"{claims}"
        "---\n\n"
        f"# {title}\n\n## Overview\n\nOverview paragraph.\n"
    )


def _claim_yaml(claim_id: str, predicate: str, *, object: str | None = None) -> str:
    object_yaml = f'    object: "{object}"\n' if object is not None else ""
    return (
        f"  - id: {claim_id}\n"
        f"    predicate: {predicate}\n"
        f"{object_yaml}"
        '    status: "accepted"\n'
        '    evidence:\n      - section: "Overview"\n'
    )


def test_relationships_frontmatter_input_is_blocked(tmp_path: Path):
    rel_yaml = 'relationships:\n  - target: "Target Page"\n    type: "relates-to"\n'
    kb_dir = _write_kb(
        tmp_path / "kb",
        {
            "source.md": _flat_page("Source Page", rel_yaml=rel_yaml),
            "target.md": _flat_page("Target Page"),
        },
    )

    report = validate(kb_dir)

    assert not report.is_valid
    errors = [
        issue
        for issue in report.issues
        if issue.severity == "error" and issue.field == "relationships"
    ]
    assert len(errors) == 1
    assert "ADR-0021" in errors[0].message
    assert "removed" in errors[0].message


def test_relationships_frontmatter_blocked_without_entries(tmp_path: Path):
    # Even an empty ``relationships: []`` block names the removed input contract.
    kb_dir = _write_kb(
        tmp_path / "kb", {"page.md": _flat_page("Page", rel_yaml="relationships: []\n")}
    )

    report = validate(kb_dir)

    assert any(issue.field == "relationships" for issue in report.issues)


def test_unknown_predicate_is_blocked(tmp_path: Path):
    files = {
        "lumio.yaml": _CONTROL_V2,
        "concepts/lumio.md": _entity_page(
            "Lumio",
            "entity:lumio",
            ["software-system"],
            claims_yaml=_claim_yaml("claim:lumio-x", "depends-on", object="entity:lancedb"),
        ),
        "concepts/lancedb.md": _entity_page("LanceDB", "entity:lancedb", ["library"]),
    }
    root = _write_kb(tmp_path, files)

    report = validate(root)

    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    assert any(
        "unknown predicate" in message and "depends-on" in message for message in errors
    ), errors
    assert not report.is_valid


def test_ontology_predicate_claim_is_valid(tmp_path: Path):
    files = {
        "lumio.yaml": _CONTROL_V2,
        "concepts/lumio.md": _entity_page(
            "Lumio",
            "entity:lumio",
            ["software-system"],
            claims_yaml=_claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb"),
        ),
        "concepts/lancedb.md": _entity_page("LanceDB", "entity:lancedb", ["library"]),
    }
    root = _write_kb(tmp_path, files)

    kb, report = load_knowledge_base(root)

    assert report.is_valid, [issue.message for issue in report.issues]
    lumio = next(page for page in kb.pages if page.title == "Lumio")
    assert len(lumio.claims) == 1
    assert lumio.claims[0].predicate == "uses"
    assert lumio.claims[0].object == "entity:lancedb"


def test_validation_issue_defaults_to_error_severity():
    issue = ValidationIssue(file="file.md", field="title", message="missing")
    assert issue.severity == "error"
