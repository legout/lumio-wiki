"""Entity, Claim, and ontology contracts (issue #168, ADR-0021).

Covers the canonical record and validation foundation: every Compiled Page
declares exactly one stable Entity ID and at least one controlled Entity
Type, owns zero or more evidence-bearing Claims validated against the
Control File ontology (``lumio.yaml`` version 2), and Entity redirects are
validated for target resolution and acyclicity. The title-based canonical
``Relationship`` input contract is gone: canonical graph edges are accepted
entity-to-entity Claims.

Acceptance mapping (issue #168):

* AC1 — a minimal categorized KB with two typed Entity pages and one
  accepted entity Claim loads successfully.
* AC2 — Literal Claims accept only the Predicate's configured literal kind.
* AC3 — duplicate/dangling IDs, unknown types/Predicates, domain/range
  violations, invalid lifecycle, merge cycles, and missing/out-of-range
  Claim Evidence are aggregated as blocking validation findings.
* AC4 — a Claim cannot provide both ``object`` and ``value``, or neither.
* AC5 — proposed/rejected assertions cannot appear in an active Compiled
  Page.
* AC6 — Extracted References remain distinguishable from Claims.
* AC7 — Fingerprints include canonical ontology, Entity, Claim,
  evidence-anchor, lifecycle, and redirect content.
* AC8 — the records are exported from ``lumio_wiki`` and serialize
  deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lumio_wiki.knowledge_base import (
    GRAPH_SCOPE_CANONICAL,
    KnowledgeBaseError,
    fingerprint_sources,
    load_knowledge_base,
    seeded_control_file,
    validate,
    write_control_file,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    PUBLISHED_CLAIM_STATUSES,
    ValidationReport,
)

# ---------------------------------------------------------------------------
# Fixture builders. Pages are rendered as line lists so evidence ``lines``
# anchors can be computed against the exact rendered file.
# ---------------------------------------------------------------------------

_CONTROL_V2 = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    software-system:
      description: "A software system."
    library: {}
  predicates:
    uses:
      subject_types:
        - software-system
      object_types:
        - library
        - software-system
    described-as:
      subject_types:
        - software-system
        - library
      literal_kind: string
  redirects:
    entity:old-lumio: entity:lumio
"""


def _entity_page(
    title: str,
    entity_id: str,
    entity_types: list[str],
    *,
    claims_yaml: str = "",
    body: str = "## Overview\n\nOverview paragraph.\n",
    lifecycle: str = "approved",
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
        f'lifecycle: "{lifecycle}"\n'
        'visibility: "public"\n'
        f'sources:\n  - id: "src-{entity_id}"\n    title: "{title} Source"\n'
        f"{claims}"
        "---\n\n"
        f"# {title}\n\n{body}"
    )


def _claim_yaml(
    claim_id: str,
    predicate: str,
    *,
    status: str = "accepted",
    object: str | None = None,
    value_yaml: str = "",
    evidence_yaml: str | None = None,
) -> str:
    object_yaml = f'    object: "{object}"\n' if object is not None else ""
    if evidence_yaml is None:
        evidence_yaml = '      - section: "Overview"\n'
    return (
        f"  - id: {claim_id}\n"
        f"    predicate: {predicate}\n"
        f"{object_yaml}{value_yaml}"
        f"    status: {status}\n"
        f"    evidence:\n{evidence_yaml}"
    )


def _write_kb(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _valid_kb_files() -> dict[str, str]:
    return {
        "lumio.yaml": _CONTROL_V2,
        "concepts/lumio.md": _entity_page(
            "Lumio",
            "entity:lumio",
            ["software-system"],
            claims_yaml=_claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb"),
        ),
        "concepts/lancedb.md": _entity_page("LanceDB", "entity:lancedb", ["library"]),
    }


def _errors(report: ValidationReport) -> list[str]:
    return [i.message for i in report.issues if i.severity == "error"]


# ---------------------------------------------------------------------------
# AC1: minimal categorized KB with entity claims loads.
# ---------------------------------------------------------------------------


def test_minimal_entity_kb_loads_successfully(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    kb, report = load_knowledge_base(root)

    assert report.is_valid, [i.message for i in report.issues]
    by_id = {page.id: page for page in kb.pages}
    assert set(by_id) == {"entity:lumio", "entity:lancedb"}
    claims = by_id["entity:lumio"].claims
    assert len(claims) == 1
    assert claims[0].id == "claim:lumio-uses-lancedb"
    assert claims[0].predicate == "uses"
    assert claims[0].object == "entity:lancedb"
    assert claims[0].status == CLAIM_STATUS_ACCEPTED
    assert claims[0].evidence[0].section == "Overview"


def test_legacy_flat_pages_without_entity_contract_still_load(tmp_path):
    # No control file: Legacy Flat Mode keeps title-based pages valid; the
    # Entity contract is optional there (ontology checks need a control file).
    root = _write_kb(
        tmp_path,
        {
            "overview.md": (
                "---\n"
                'title: "Overview"\n'
                'tags:\n  - "test"\n'
                'summary: "Overview."\n'
                'lifecycle: "approved"\n'
                'visibility: "public"\n'
                'sources:\n  - id: "src-overview"\n    title: "Overview Source"\n'
                "---\n\nOverview body.\n"
            )
        },
    )
    _, report = load_knowledge_base(root)

    assert report.is_valid, [i.message for i in report.issues]


def test_entity_projection_exposes_identity_records(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    kb, report = load_knowledge_base(root)
    assert report.is_valid

    entities = sorted(kb.entities(), key=lambda e: e.id)
    assert [(e.id, e.title) for e in entities] == [
        ("entity:lancedb", "LanceDB"),
        ("entity:lumio", "Lumio"),
    ]
    lumio = entities[1]
    assert lumio.entity_types == ["software-system"]
    assert lumio.path == "concepts/lumio.md"


def test_value_type_without_value_is_blocked(tmp_path):
    claim = _claim_yaml("claim:lumio-bad", "described-as", value_yaml="    value_type: string\n")
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("value_type without a value" in m for m in _errors(report)), _errors(report)


def test_literal_value_kind_must_match_declared_kind(tmp_path):
    # value is a YAML integer but declares value_type string.
    claim = _claim_yaml(
        "claim:lumio-tier",
        "described-as",
        value_yaml="    value: 3\n    value_type: string\n",
    )
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("literal value kind mismatch" in m and "number" in m for m in _errors(report)), (
        _errors(report)
    )


def test_empty_evidence_anchor_is_blocked(tmp_path):
    claim = _claim_yaml(
        "claim:lumio-uses-lancedb",
        "uses",
        object="entity:lancedb",
        evidence_yaml="      - {}\n",
    )
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any(
        "evidence anchor must identify a section or a bounded line range" in m
        for m in _errors(report)
    ), _errors(report)


def test_v2_kb_requires_entity_contract_on_every_page(tmp_path):
    # ADR-0021: every page in a version-2 Knowledge Base declares one stable
    # Entity ID and at least one controlled Entity Type.
    files = _valid_kb_files()
    files["concepts/plain.md"] = (
        "---\n"
        'title: "Plain"\n'
        'tags:\n  - "test"\n'
        'summary: "Plain."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src-plain"\n    title: "Plain Source"\n'
        "---\n\nPlain body.\n"
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    errors = _errors(report)
    assert any("missing required field: id" in m for m in errors), errors
    assert any("at least one entity type" in m for m in errors), errors


def test_claim_valid_time_metadata_is_parsed_and_validated(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            _claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb").replace(
                "    status: accepted\n", '    status: accepted\n    valid_from: "2026-01-15"\n'
            )
        ),
    )
    root = _write_kb(tmp_path, files)
    kb, report = load_knowledge_base(root)

    assert report.is_valid, [i.message for i in report.issues]
    claim = next(p for p in kb.pages if p.id == "entity:lumio").claims[0]
    assert claim.valid_from == "2026-01-15"
    assert claim.valid_to is None

    bad = _valid_kb_files()
    bad["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb", "uses", object="entity:lancedb"
        ).replace("    status: accepted\n", '    status: accepted\n    valid_to: "not-a-date"\n'),
    )
    root2 = _write_kb(tmp_path / "bad", bad)
    errors = _errors(validate(root2))
    assert any("valid_to 'not-a-date' is not an ISO 8601 date" in m for m in errors), errors


def test_control_file_version_1_is_unsupported(tmp_path):
    files = _valid_kb_files()
    files["lumio.yaml"] = files["lumio.yaml"].replace("version: 2", "version: 1")
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("version" in i.field and "not supported" in i.message for i in report.issues)


# ---------------------------------------------------------------------------
# AC2: literal claims accept only the configured literal kind.
# ---------------------------------------------------------------------------


def test_literal_claim_with_configured_kind_is_valid(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-tier",
            "described-as",
            value_yaml='    value: "embedded-first"\n    value_type: string\n',
        ),
    )
    root = _write_kb(tmp_path, files)
    _, report = load_knowledge_base(root)

    assert report.is_valid, [i.message for i in report.issues]


def test_literal_claim_with_mismatched_kind_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-tier",
            "described-as",
            value_yaml="    value: 3\n    value_type: number\n",
        ),
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any(
        "literal kind mismatch" in m and "string" in m and "number" in m for m in _errors(report)
    ), _errors(report)


def test_entity_object_claim_on_literal_predicate_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml("claim:lumio-bad", "described-as", object="entity:lancedb"),
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("does not accept an entity object" in m for m in _errors(report)), _errors(report)


def test_literal_claim_on_entity_predicate_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-bad",
            "uses",
            value_yaml='    value: "x"\n    value_type: string\n',
        ),
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("does not accept a literal object" in m for m in _errors(report)), _errors(report)


# ---------------------------------------------------------------------------
# AC3: aggregated blocking findings.
# ---------------------------------------------------------------------------


def _one_claim_kb(
    tmp_path, *, claim: str, lumio_types: list[str] | None = None
) -> ValidationReport:
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio", "entity:lumio", lumio_types or ["software-system"], claims_yaml=claim
    )
    root = _write_kb(tmp_path, files)
    return validate(root)


def test_duplicate_entity_id_is_blocked(tmp_path):
    root = _write_kb(
        tmp_path,
        _valid_kb_files()
        | {
            "concepts/lumio-clone.md": _entity_page(
                "Lumio Clone", "entity:lumio", ["software-system"]
            )
        },
    )
    report = validate(root)

    assert any("duplicate entity id: entity:lumio" in m for m in _errors(report)), _errors(report)


def test_duplicate_claim_id_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["concepts/lancedb.md"] = _entity_page(
        "LanceDB",
        "entity:lancedb",
        ["library"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb",
            "described-as",
            value_yaml='    value: "dup"\n    value_type: string\n',
        ),
    )
    report = validate(_write_kb(tmp_path, files))

    assert any("duplicate claim id: claim:lumio-uses-lancedb" in m for m in _errors(report)), (
        _errors(report)
    )


def test_dangling_claim_object_is_blocked(tmp_path):
    claim = _claim_yaml("claim:lumio-uses-ghost", "uses", object="entity:ghost")
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("dangling object entity: entity:ghost" in m for m in _errors(report)), _errors(
        report
    )


def test_unknown_entity_type_is_blocked(tmp_path):
    report = _one_claim_kb(
        tmp_path,
        claim=_claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb"),
        lumio_types=["not-a-type"],
    )

    assert any("unknown entity type: not-a-type" in m for m in _errors(report)), _errors(report)


def test_unknown_predicate_is_blocked(tmp_path):
    claim = _claim_yaml("claim:lumio-x", "runs", object="entity:lancedb")
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("unknown predicate: runs" in m for m in _errors(report)), _errors(report)


def test_subject_domain_violation_is_blocked(tmp_path):
    # library-typed subject using `uses` (domain: software-system) is invalid.
    files = _valid_kb_files()
    files["concepts/lancedb.md"] = _entity_page(
        "LanceDB",
        "entity:lancedb",
        ["library"],
        claims_yaml=_claim_yaml("claim:lancedb-uses-lumio", "uses", object="entity:lumio"),
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("domain" in m for m in _errors(report)), _errors(report)


def test_object_range_violation_is_blocked(tmp_path):
    # `uses` range is [library, software-system]; a library-typed object is valid,
    # so build a range violation with a predicate whose object_types exclude it.
    files = _valid_kb_files()
    files["lumio.yaml"] = _CONTROL_V2.replace(
        (
            "    uses:\n"
            "      subject_types:\n"
            "        - software-system\n"
            "      object_types:\n"
            "        - library\n"
            "        - software-system\n"
        ),
        (
            "    uses:\n"
            "      subject_types:\n"
            "        - software-system\n"
            "      object_types:\n"
            "        - library\n"
        ),
    )
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml("claim:lumio-uses-lumio2", "uses", object="entity:lumio2"),
    )
    files["concepts/lumio2.md"] = _entity_page("Lumio Two", "entity:lumio2", ["software-system"])
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("range" in m for m in _errors(report)), _errors(report)


def test_missing_evidence_is_blocked(tmp_path):
    claim = (
        "  - id: claim:lumio-uses-lancedb\n"
        "    predicate: uses\n"
        '    object: "entity:lancedb"\n'
        "    status: accepted\n"
    )
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("missing evidence" in m for m in _errors(report)), _errors(report)


def test_out_of_range_evidence_lines_are_blocked(tmp_path):
    # Body spans known lines; anchor beyond the last body line is invalid.
    body = "line a\nline b\n"
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb",
            "uses",
            object="entity:lancedb",
            evidence_yaml="      - lines: [3, 99]\n",
        ),
        body=body,
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("out of bounds" in m for m in _errors(report)), _errors(report)


def test_unknown_evidence_section_is_blocked(tmp_path):
    report = _one_claim_kb(
        tmp_path,
        claim=_claim_yaml(
            "claim:lumio-uses-lancedb",
            "uses",
            object="entity:lancedb",
            evidence_yaml='      - section: "No Such Section"\n',
        ),
    )

    assert any("unknown evidence section: 'No Such Section'" in m for m in _errors(report)), (
        _errors(report)
    )


# ---------------------------------------------------------------------------
# Claim parser diagnostics (Plan 02 P1): structural Claim frontmatter errors
# must surface in the public ValidationReport instead of disappearing into a
# valid report or crashing the load. One distinct defect per test.
# ---------------------------------------------------------------------------


def test_malformed_claims_keep_structural_diagnostics(tmp_path):
    # Three malformed shapes across three pages: a scalar claims container, a
    # non-mapping entry, and entries missing required fields. Each must keep
    # the report invalid and name file/field/message exactly once — none may
    # silently vanish — while valid Claims on untouched pages still load.
    files = _valid_kb_files() | {
        "concepts/extra-a.md": _entity_page(
            "Extra A",
            "entity:extra-a",
            ["software-system"],
            claims_yaml='  "not-a-list"\n',
        ),
        "concepts/extra-b.md": _entity_page(
            "Extra B",
            "entity:extra-b",
            ["software-system"],
            claims_yaml="  - 42\n",
        ),
        "concepts/extra-c.md": _entity_page(
            "Extra C",
            "entity:extra-c",
            ["software-system"],
            claims_yaml=(
                "  - predicate: uses\n"
                '    object: "entity:lancedb"\n'
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
                "  - id: claim:extra-c-no-predicate\n"
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
            ),
        ),
    }
    kb, report = load_knowledge_base(_write_kb(tmp_path, files))

    assert not report.is_valid, [i.message for i in report.issues]
    claim_issues = [i for i in report.issues if i.field == "claims"]
    by_file_and_message = {(i.file, i.message) for i in claim_issues}
    assert ("concepts/extra-a.md", "claims must be a list") in by_file_and_message, sorted(
        by_file_and_message
    )
    assert (
        "concepts/extra-b.md",
        "claims must be a list of mappings",
    ) in by_file_and_message, sorted(by_file_and_message)
    assert ("concepts/extra-c.md", "claim missing id") in by_file_and_message, sorted(
        by_file_and_message
    )
    assert ("concepts/extra-c.md", "claim missing predicate") in by_file_and_message, sorted(
        by_file_and_message
    )
    # The valid Claim on the untouched page still loads unchanged.
    lumio = next(page for page in kb.pages if page.id == "entity:lumio")
    assert [claim.id for claim in lumio.claims] == ["claim:lumio-uses-lancedb"]


def test_invalid_claim_confidence_is_an_aggregated_issue(tmp_path):
    # A nonnumeric confidence must become a ValidationIssue naming the claim,
    # not an uncaught conversion error; validation continues so unrelated
    # findings from the same load land in the same report.
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            "  - id: claim:lumio-uses-lancedb\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    confidence: high\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
            "  - id: claim:lumio-unknown-predicate\n"
            "    predicate: runs\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
        ),
    )
    report = validate(_write_kb(tmp_path, files))
    messages = _errors(report)

    assert any("claim:lumio-uses-lancedb" in m and "confidence" in m for m in messages), messages
    assert any("unknown predicate: runs" in m for m in messages), messages
    assert not report.is_valid


def test_container_literal_is_blocked(tmp_path):
    # Mapping/list literal values are invalid even when value_type is
    # declared; scalar string values keep working with their declared kind.
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            "  - id: claim:lumio-mapping-value\n"
            "    predicate: described-as\n"
            "    value:\n"
            "      nested: mapping\n"
            "    value_type: string\n"
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
            "  - id: claim:lumio-list-value\n"
            "    predicate: described-as\n"
            "    value:\n"
            "      - 1\n"
            "      - 2\n"
            "    value_type: string\n"
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
            "  - id: claim:lumio-scalar-value\n"
            "    predicate: described-as\n"
            '    value: "plain text"\n'
            "    value_type: string\n"
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
        ),
    )
    report = validate(_write_kb(tmp_path, files))
    messages = _errors(report)

    assert any("claim:lumio-mapping-value" in m and "value must be" in m for m in messages), (
        messages
    )
    assert any("claim:lumio-list-value" in m and "value must be" in m for m in messages), messages
    assert not any("claim:lumio-scalar-value" in m for m in messages), messages
    assert not report.is_valid


def test_zero_evidence_line_is_blocked(tmp_path):
    # Evidence line anchors are 1-based, nonzero, ordered, and within the
    # owning body: zero/negative/reversed/out-of-body anchors are invalid,
    # zero is never silently normalized to one, a section-only anchor (no
    # lines) stays legal, and a valid 1-based range remains valid.
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            "  - id: claim:lumio-zero-line\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - lines: [0, 2]\n"
            "  - id: claim:lumio-negative-line\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - lines: [-1, 2]\n"
            "  - id: claim:lumio-reversed-lines\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - lines: [2, 1]\n"
            "  - id: claim:lumio-beyond-body\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - lines: [1, 99]\n"
            "  - id: claim:lumio-section-anchor\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
            "  - id: claim:lumio-valid-lines\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - lines: [1, 4]\n"
        ),
        body="## Overview\n\nalpha line\nbeta line\n",
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)
    messages = _errors(report)

    bounds_errors = [m for m in messages if "out of bounds" in m]
    assert any("claim:lumio-zero-line" in m for m in bounds_errors), messages
    assert any("claim:lumio-negative-line" in m for m in bounds_errors), messages
    assert any("claim:lumio-reversed-lines" in m for m in bounds_errors), messages
    assert any("claim:lumio-beyond-body" in m for m in bounds_errors), messages
    assert not any("claim:lumio-section-anchor" in m for m in messages), messages
    assert not any("claim:lumio-valid-lines" in m for m in messages), messages
    assert not report.is_valid


def test_confidence_overflow_is_an_aggregated_issue(tmp_path):
    # A YAML integer too large for float conversion must become a
    # ValidationIssue naming the claim — never an uncaught OverflowError.
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            "  - id: claim:lumio-huge-confidence\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            f"    confidence: 1{'0' * 400}\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
        ),
    )
    report = validate(_write_kb(tmp_path, files))
    messages = _errors(report)

    assert any("claim:lumio-huge-confidence" in m and "confidence" in m for m in messages), messages
    assert not report.is_valid


def test_evidence_lines_on_empty_body_are_out_of_bounds(tmp_path):
    # A page whose body is empty anchors nothing: a [1, 1] line range must be
    # reported out of bounds (no max(line_count, 1) acceptance), while
    # section-only anchors and nonempty 1-based bounds keep their behavior.
    files = _valid_kb_files()
    files["concepts/empty.md"] = (
        "---\n"
        'id: "entity:empty"\n'
        'title: "Empty"\n'
        "entity_types:\n  - software-system\n"
        'tags:\n  - "test"\n'
        'summary: "Empty summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n  - id: "src-empty"\n    title: "Empty Source"\n'
        "claims:\n"
        "  - id: claim:empty-body-lines\n"
        "    predicate: uses\n"
        '    object: "entity:lancedb"\n'
        "    status: accepted\n"
        "    evidence:\n"
        "      - lines: [1, 1]\n"
        "---"
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)
    messages = _errors(report)

    assert any("claim:empty-body-lines" in m and "out of bounds" in m for m in messages), messages
    assert not report.is_valid


def test_non_string_claim_scalars_are_blocked(tmp_path):
    # Bare YAML dates and numeric scalars must not reach validation through
    # str() coercion: a numeric predicate, a non-string evidence section (even
    # one matching a rendered heading), and a date literal value (even with
    # value_type declared) are each diagnosed as structural issues.
    body = "## 2020-01-01\n\nCoercion bait heading.\n"
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=(
            "  - id: claim:lumio-numeric-predicate\n"
            "    predicate: 42\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
            "  - id: claim:lumio-date-section\n"
            "    predicate: uses\n"
            '    object: "entity:lancedb"\n'
            "    status: accepted\n"
            "    evidence:\n"
            "      - section: 2020-01-01\n"
            "  - id: claim:lumio-date-value\n"
            "    predicate: described-as\n"
            "    value: 2020-01-01\n"
            "    value_type: string\n"
            "    status: accepted\n"
            "    evidence:\n"
            '      - section: "Overview"\n'
        ),
        body=body,
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)
    messages = _errors(report)

    assert any(
        "claim:lumio-numeric-predicate" in m and "predicate must be a string" in m for m in messages
    ), messages
    assert any(
        "claim:lumio-date-section" in m and "evidence section must be a string" in m
        for m in messages
    ), messages
    assert any("claim:lumio-date-value" in m and "value must be" in m for m in messages), messages
    assert not report.is_valid


def test_redirect_cycle_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["lumio.yaml"] = _CONTROL_V2.replace(
        "  redirects:\n    entity:old-lumio: entity:lumio\n",
        "  redirects:\n    entity:old-a: entity:old-b\n    entity:old-b: entity:old-a\n",
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("redirect cycle" in m for m in _errors(report)), _errors(report)


def test_redirect_dangling_target_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["lumio.yaml"] = _CONTROL_V2.replace(
        "entity:old-lumio: entity:lumio", "entity:old-lumio: entity:ghost"
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("redirect target does not resolve" in m for m in _errors(report)), _errors(report)


def test_redirect_retiring_live_entity_is_blocked(tmp_path):
    files = _valid_kb_files()
    files["lumio.yaml"] = _CONTROL_V2.replace(
        "entity:old-lumio: entity:lumio", "entity:lancedb: entity:lumio"
    )
    root = _write_kb(tmp_path, files)
    report = validate(root)

    assert any("live entity" in m for m in _errors(report)), _errors(report)


def test_valid_redirect_chain_resolves(tmp_path):
    files = _valid_kb_files()
    files["lumio.yaml"] = _CONTROL_V2.replace(
        "  redirects:\n    entity:old-lumio: entity:lumio\n",
        (
            "  redirects:\n"
            "    entity:old-lumio: entity:mid-lumio\n"
            "    entity:mid-lumio: entity:lumio\n"
        ),
    )
    root = _write_kb(tmp_path, files)
    _, report = load_knowledge_base(root)

    assert report.is_valid, [i.message for i in report.issues]


# ---------------------------------------------------------------------------
# AC4: exactly one claim object.
# ---------------------------------------------------------------------------


def test_claim_with_both_object_and_value_is_blocked(tmp_path):
    claim = _claim_yaml(
        "claim:lumio-bad",
        "uses",
        object="entity:lancedb",
        value_yaml='    value: "x"\n    value_type: string\n',
    )
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("exactly one of object or value" in m for m in _errors(report)), _errors(report)


def test_claim_with_neither_object_nor_value_is_blocked(tmp_path):
    claim = _claim_yaml("claim:lumio-bad", "uses")
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("exactly one of object or value" in m for m in _errors(report)), _errors(report)


def test_value_without_value_type_is_blocked(tmp_path):
    claim = _claim_yaml("claim:lumio-bad", "described-as", value_yaml='    value: "x"\n')
    report = _one_claim_kb(tmp_path, claim=claim)

    assert any("value requires value_type" in m for m in _errors(report)), _errors(report)


# ---------------------------------------------------------------------------
# AC5: proposed/rejected assertions cannot appear in an active page.
# ---------------------------------------------------------------------------


def test_proposed_status_is_blocked_on_active_page(tmp_path):
    report = _one_claim_kb(
        tmp_path,
        claim=_claim_yaml(
            "claim:lumio-uses-lancedb", "uses", object="entity:lancedb", status="proposed"
        ),
    )

    assert any("proposed" in m and "Ingest Proposal" in m for m in _errors(report)), _errors(report)


def test_rejected_status_is_blocked_on_active_page(tmp_path):
    report = _one_claim_kb(
        tmp_path,
        claim=_claim_yaml(
            "claim:lumio-uses-lancedb", "uses", object="entity:lancedb", status="rejected"
        ),
    )

    assert any("rejected" in m and "Ingest Proposal" in m for m in _errors(report)), _errors(report)


def test_all_published_statuses_are_valid_on_active_page(tmp_path):
    assert PUBLISHED_CLAIM_STATUSES == {"accepted", "disputed", "superseded"}
    for status in sorted(PUBLISHED_CLAIM_STATUSES):
        report = _one_claim_kb(
            tmp_path / status,
            claim=_claim_yaml(
                "claim:lumio-uses-lancedb", "uses", object="entity:lancedb", status=status
            ),
        )
        assert report.is_valid, (status, [i.message for i in report.issues])


# ---------------------------------------------------------------------------
# Claim lifecycle drives the canonical graph (ADR-0021).
# ---------------------------------------------------------------------------


def test_only_accepted_entity_claims_enter_canonical_traversal(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    kb, report = load_knowledge_base(root)
    assert report.is_valid

    related = kb.related_pages("Lumio", scope=GRAPH_SCOPE_CANONICAL)
    assert related == ["LanceDB"]

    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb", "uses", object="entity:lancedb", status="disputed"
        ),
    )
    root2 = _write_kb(tmp_path / "disputed", files)
    kb2, report2 = load_knowledge_base(root2)
    assert report2.is_valid, [i.message for i in report2.issues]
    assert kb2.related_pages("Lumio", scope=GRAPH_SCOPE_CANONICAL) == []


def test_related_from_projects_accepted_claims_by_title(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    kb, _ = load_knowledge_base(root)

    edges = kb.related_from("Lumio")
    assert [(rel.target, rel.type) for rel in edges] == [("LanceDB", "uses")]


def test_literal_claims_never_create_graph_edges(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-tier",
            "described-as",
            value_yaml='    value: "embedded-first"\n    value_type: string\n',
        ),
    )
    root = _write_kb(tmp_path, files)
    kb, report = load_knowledge_base(root)
    assert report.is_valid

    assert kb.related_pages("Lumio", scope=GRAPH_SCOPE_CANONICAL) == []


# ---------------------------------------------------------------------------
# AC6: Extracted References remain distinguishable from Claims.
# ---------------------------------------------------------------------------


def test_extracted_references_still_parse_beside_claims(tmp_path):
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml("claim:lumio-uses-lancedb", "uses", object="entity:lancedb"),
        body="## Overview\n\nSee [the LanceDB page](lancedb.md) for storage.\n",
    )
    root = _write_kb(tmp_path, files)
    kb, report = load_knowledge_base(root)
    assert report.is_valid, [i.message for i in report.issues]

    refs = kb.extracted_references("Lumio")
    assert [(r.source_title, r.target_title, r.origin) for r in refs] == [
        ("Lumio", "LanceDB", "markdown-link")
    ]
    # A Claim is not an Extracted Reference and vice versa: the claim edge is
    # typed ("uses"); the extracted edge is untyped navigation.
    assert all(r.origin == "markdown-link" for r in refs)
    lumio_page = next(p for p in kb.pages if p.id == "entity:lumio")
    assert lumio_page.claims, "claim record still parsed"


# ---------------------------------------------------------------------------
# AC7: fingerprints cover ontology, entity, claim, evidence, lifecycle,
# and redirect content.
# ---------------------------------------------------------------------------


def _digest(root: Path) -> str:
    return fingerprint_sources(root).digest


def test_fingerprint_is_sensitive_to_ontology_content(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["lumio.yaml"] = files["lumio.yaml"].replace(
        "    library: {}\n", "    library: {}\n    database: {}\n"
    )
    _write_kb(root, files)
    assert _digest(root) != before


def test_fingerprint_is_sensitive_to_entity_identity(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["concepts/lancedb.md"] = _entity_page("LanceDB", "entity:lancedb-2", ["library"])
    _write_kb(root, files)
    assert _digest(root) != before


def test_fingerprint_is_sensitive_to_claim_content(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml("claim:lumio-uses-lancedb-2", "uses", object="entity:lancedb"),
    )
    _write_kb(root, files)
    assert _digest(root) != before


def test_fingerprint_is_sensitive_to_evidence_anchor(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb",
            "uses",
            object="entity:lancedb",
            evidence_yaml='      - section: "Overview"\n        lines: [4, 4]\n',
        ),
    )
    _write_kb(root, files)
    assert _digest(root) != before


def test_fingerprint_is_sensitive_to_claim_lifecycle(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["concepts/lumio.md"] = _entity_page(
        "Lumio",
        "entity:lumio",
        ["software-system"],
        claims_yaml=_claim_yaml(
            "claim:lumio-uses-lancedb", "uses", object="entity:lancedb", status="superseded"
        ),
    )
    _write_kb(root, files)
    assert _digest(root) != before


def test_fingerprint_is_sensitive_to_redirects(tmp_path):
    root = _write_kb(tmp_path, _valid_kb_files())
    before = _digest(root)
    files = _valid_kb_files()
    files["lumio.yaml"] = files["lumio.yaml"].replace(
        "entity:old-lumio: entity:lumio", "entity:old-lumio-x: entity:lumio"
    )
    _write_kb(root, files)
    assert _digest(root) != before


# ---------------------------------------------------------------------------
# AC8: exports + deterministic serialization.
# ---------------------------------------------------------------------------


def test_write_control_file_is_deterministic(tmp_path) -> None:
    control = seeded_control_file()
    assert control.version == 2
    first = write_control_file(tmp_path / "a", control).read_bytes()
    second = write_control_file(tmp_path / "b", control).read_bytes()
    assert first == second
    assert b"ontology:" in first


def test_seeded_control_file_round_trips(tmp_path) -> None:
    from lumio_wiki.knowledge_base import load_control_file

    control = seeded_control_file()
    write_control_file(tmp_path, control)
    loaded = load_control_file(tmp_path)
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.ontology is not None


def test_load_knowledge_base_missing_path_raises(tmp_path) -> None:
    with pytest.raises(KnowledgeBaseError):
        load_knowledge_base(tmp_path / "nope")
