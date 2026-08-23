"""Cross-page QA validation report expansion (issue #86, ADR-0011).

The cross-page validation report is expanded with Maintainer QA issue kinds
that consume the canonical Relationship and Extracted Reference outcomes
already produced by the Discovery Graph (issue #107). The report surfaces
structural and content-health problems as ``ValidationIssue`` entries in the
existing ``ValidationReport`` shape — no parallel QA structure, no new link
parser, no LLM/embedding calls.

Acceptance mapping (issue #86):

* AC1 — the report surfaces orphan, broken-internal-link, stale,
  contradiction, and missing-frontmatter issues.
* AC2 — orphan findings disclose canonical-graph vs Discovery-Graph scope,
  distinguishing pages with no reviewed inbound Relationships from pages
  with no inbound topology at all.
* AC3 — broken/ambiguous/escaping/duplicate/unsupported link outcomes from
  the shared Extracted Reference resolver carry source page, severity, and
  enough location context to act.
* AC4 — already-authored, exactly-resolved internal links are NOT reported
  as broken and do not create proposals merely because they produce
  Extracted References.
* AC5 — checks are deterministic and model-free.
* AC6 — new issue kinds reuse the existing report shape and Discovery Graph
  outcomes; no parallel QA structure or link parser is introduced.

The primary tests exercise the PUBLIC ``validate(path)`` seam so they verify
the end-to-end report a Maintainer or CLI caller receives. A focused subset
exercises edge cases through the internal ``_cross_page_issues`` helper for
precision (self-relationships, scope-boundary orphans, generic diagnostic
forwarding).
"""

from __future__ import annotations

import re
from pathlib import Path

from lumio_wiki.knowledge_base import (
    ExtractionDiagnostic,
    _cross_page_issues,
    _KnowledgeIndex,
    validate,
)
from lumio_wiki.records import (
    CLAIM_STATUS_ACCEPTED,
    Claim,
    CompiledPage,
    Relationship,
    Source,
    ValidationIssue,
    ValidationReport,
)

# ---------------------------------------------------------------------------
# Page builders — in-memory (mirror test_discovery_graph.py conventions)
# and on-disk (for public-seam integration tests).
# ---------------------------------------------------------------------------


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _claims_from(
    source_title: str, relationships: list[Relationship] | None
) -> list[Claim]:
    """Convert title-level test edges to accepted entity-to-entity Claims."""
    return [
        Claim(
            id=f"claim:{_slug(source_title)}-{_slug(rel.target)}-{n}",
            predicate=rel.type,
            object=f"entity:{_slug(rel.target)}",
            status=CLAIM_STATUS_ACCEPTED,
        )
        for n, rel in enumerate(relationships or [])
    ]


def _page(
    title: str,
    *,
    body: str = "",
    aliases: list[str] | None = None,
    relationships: list[Relationship] | None = None,
    path: str | None = None,
    lifecycle: str = "approved",
    visibility: str = "public",
    tags: list[str] | None = None,
    sources: list[Source] | None = None,
    synthetic: bool = False,
) -> CompiledPage:
    return CompiledPage(
        path=path or f"{title.lower().replace(' ', '-')}.md",
        title=title,
        id=f"entity:{_slug(title)}",
        entity_types=["concept"],
        aliases=aliases or [],
        tags=tags or ["test"],
        summary=f"{title} summary",
        lifecycle=lifecycle,
        visibility=visibility,
        sources=sources if sources is not None else [
            Source(id=f"src-{title.lower()}", title=title)
        ],
        claims=_claims_from(title, relationships),
        synthetic=synthetic,
        body=body,
    )


def _page_md(
    title: str,
    *,
    body: str = "",
    lifecycle: str = "approved",
    visibility: str = "public",
    aliases: list[str] | None = None,
    claims: list[tuple[str, str, str]] | None = None,
    entity_id: str | None = None,
    synthetic: bool = False,
) -> str:
    """Render a single-page Markdown source for on-disk KB fixtures.

    ``claims`` carries ``(claim_id, predicate, object_entity)`` triples
    rendered as accepted, evidence-bearing Claims (ADR-0021). Pages carrying
    claims (or any entity key) must also declare ``entity_id`` and entity
    types and be validated against a version-2 Control File ontology.
    """
    alias_yaml = ""
    if aliases:
        alias_yaml = "aliases:\n" + "".join(f'  - "{a}"\n' for a in aliases)
    id_yaml = f'id: "{entity_id}"\n' if entity_id else ""
    types_yaml = "entity_types:\n  - concept\n" if entity_id else ""
    claims_yaml = ""
    if claims:
        claims_yaml = "claims:\n" + "".join(
            f"  - id: {cid}\n"
            f"    predicate: {predicate}\n"
            f'    object: "{object_}"\n'
            "    status: accepted\n"
            "    evidence:\n"
            f'      - section: "{title}"\n'
            for cid, predicate, object_ in claims
        )
    syn_yaml = f"synthetic: {str(synthetic).lower()}\n" if synthetic else ""
    return (
        "---\n"
        f"{id_yaml}"
        f'title: "{title}"\n'
        f"{types_yaml}"
        f"{alias_yaml}"
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        f'lifecycle: "{lifecycle}"\n'
        f'visibility: "{visibility}"\n'
        f'sources:\n  - id: "src-{title.lower()}"\n    title: "{title} Source"\n'
        f"{claims_yaml}{syn_yaml}"
        "---\n\n"
        f"# {title}\n\n{body}"
    )


def _write_kb(root: Path, pages: dict[str, str]) -> Path:
    """Write an on-disk KB: ``pages`` maps relative path → Markdown source."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, md in pages.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md)
    return root


def _qa_issues(pages: list[CompiledPage]) -> list[ValidationIssue]:
    """Run cross-page validation over an in-memory page set.

    Builds the Discovery Graph index (issue #107 outcomes) and runs the
    expanded cross-page validator — the same composition ``_load_and_validate``
    performs at load time. Used for edge-case tests that need precise control
    over page records without a filesystem round-trip.
    """
    index = _KnowledgeIndex(pages)
    return _cross_page_issues(pages, index)


def _by_field(issues: list[ValidationIssue], field: str) -> list[ValidationIssue]:
    return [i for i in issues if i.field == field]


# ---------------------------------------------------------------------------
# AC1 + AC6: report shape — every QA finding is a ValidationIssue in the
# existing report. No new types, no parallel structure.
# ---------------------------------------------------------------------------


def test_every_qa_finding_is_a_validation_issue_in_the_existing_shape():
    pages = [
        _page("Orphan", body="standalone.\n"),
        _page("Stale", lifecycle="deprecated"),
    ]
    issues = _qa_issues(pages)

    assert issues, "expected QA issues"
    for issue in issues:
        assert isinstance(issue, ValidationIssue)
        assert issue.file
        assert issue.field
        assert issue.message
        assert issue.severity in {"error", "warning"}


def test_report_wraps_qa_issues_in_existing_validation_report(tmp_path):
    # End-to-end public seam: a KB on disk with a deprecated page produces a
    # ValidationReport whose issues include the stale finding.
    root = _write_kb(tmp_path, {"stale.md": _page_md("Stale", lifecycle="deprecated")})
    report = validate(root)

    assert isinstance(report, ValidationReport)
    stale = [i for i in report.issues if i.field == "lifecycle" and "deprecated" in i.message]
    assert stale, [i.message for i in report.issues]


def test_no_new_types_introduced_beyond_validation_issue():
    import lumio_wiki

    public = set(lumio_wiki.__all__)
    assert "ValidationIssue" in public
    assert "ValidationReport" in public
    qa_names = {name for name in public if "qa" in name.lower()}
    assert qa_names == set(), qa_names


# ---------------------------------------------------------------------------
# AC1 + AC2: orphan detection with scope disclosure.
# ---------------------------------------------------------------------------


def test_orphan_discovery_scope_has_no_inbound_topology_at_all():
    gamma = _page("Gamma", body="Gamma stands alone.\n")
    alpha = _page("Alpha", body="Alpha is separate.\n")
    issues = _qa_issues([gamma, alpha])

    gamma_orphan = [
        i for i in issues
        if i.file == gamma.path and i.field == "topology" and "orphan" in i.message
    ]
    assert gamma_orphan, [i.message for i in issues if i.file == gamma.path]
    msg = gamma_orphan[0].message.lower()
    assert "discovery" in msg, gamma_orphan[0].message
    assert "no inbound topology" in msg, gamma_orphan[0].message
    assert gamma_orphan[0].severity == "warning"


def test_orphan_canonical_scope_has_body_links_but_no_reviewed_relationships():
    # Alpha links to Beta via a body link (Extracted Reference), but no page
    # has a reviewed typed Relationship targeting Beta. Beta is therefore an
    # orphan in the CANONICAL scope only.
    alpha = _page("Alpha", body="See [b](beta.md).\n")
    beta = _page("Beta", path="beta.md")
    issues = _qa_issues([alpha, beta])

    beta_orphan = [
        i for i in issues
        if i.file == beta.path and i.field == "topology" and "orphan" in i.message
    ]
    assert beta_orphan, [i.message for i in issues if i.file == beta.path]
    msg = beta_orphan[0].message.lower()
    assert "canonical" in msg, beta_orphan[0].message
    assert "no reviewed inbound relationship" in msg, beta_orphan[0].message
    assert "extracted reference" in msg, beta_orphan[0].message


def test_page_with_inbound_relationship_is_not_an_orphan():
    alpha = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="relates-to")],
    )
    beta = _page("Beta")
    issues = _qa_issues([alpha, beta])

    beta_orphan = [
        i for i in issues
        if i.file == beta.path and i.field == "topology" and "orphan" in i.message
    ]
    assert not beta_orphan, [i.message for i in issues if i.file == beta.path]


def test_self_relationship_does_not_mask_orphan():
    gamma = _page(
        "Gamma",
        relationships=[Relationship(target="Gamma", type="relates-to")],
    )
    issues = _qa_issues([gamma])

    orphan = [i for i in issues if i.file == gamma.path and i.field == "topology"]
    assert orphan, "self-relationship must not prevent orphan detection"


def test_orphan_scope_disclosure_through_public_validate_seam(tmp_path):
    # Integration: a KB with an isolated page and a body-linked page both
    # surface orphan findings through validate(), with correct scope disclosure.
    root = _write_kb(tmp_path, {
        "alpha.md": _page_md("Alpha", body="See [Beta](beta.md).\n"),
        "beta.md": _page_md("Beta"),
        "gamma.md": _page_md("Gamma", body="Standalone.\n"),
    })
    report = validate(root)
    topology = [i for i in report.issues if i.field == "topology"]
    # Gamma is a discovery-scope orphan; Beta is a canonical-scope orphan
    # (has inbound Extracted References via Alpha's body link).
    scopes = {i.file: i.message for i in topology}
    assert any("discovery" in m.lower() for m in scopes.values()), scopes
    assert any("canonical" in m.lower() for m in scopes.values()), scopes


# ---------------------------------------------------------------------------
# AC1 + AC3 + AC4: broken-internal-link from shared resolver diagnostics;
# exactly-resolved links stay clean.
# ---------------------------------------------------------------------------


def test_broken_internal_link_reported_from_resolver_diagnostic():
    alpha = _page("Alpha", body="[missing](no-such-page.md)\n")
    beta = _page("Beta")
    issues = _qa_issues([alpha, beta])

    broken = _by_field(issues, "internal-links")
    assert broken, [i.message for i in issues]
    issue = broken[0]
    assert issue.file == alpha.path
    assert issue.severity == "warning"
    msg = issue.message.lower()
    assert "broken" in msg
    assert "no-such-page.md" in issue.message
    assert "line" in msg


def test_ambiguous_internal_link_reported_from_resolver_diagnostic():
    alpha = _page("Alpha", body="[b](beta.md)\n")
    b1 = _page("Beta", path="beta.md")
    b2 = _page("Beta", path="beta-other.md")
    issues = _qa_issues([alpha, b1, b2])

    ambiguous = [
        i for i in _by_field(issues, "internal-links")
        if "ambiguous" in i.message.lower()
    ]
    assert ambiguous, [i.message for i in issues]
    assert ambiguous[0].file == alpha.path
    assert ambiguous[0].severity == "warning"


def test_broken_internal_link_forwarded_through_public_validate_seam(tmp_path):
    # Integration: a broken body link surfaces as a warning through validate().
    root = _write_kb(tmp_path, {
        "alpha.md": _page_md("Alpha", body="See [Missing](nope.md).\n"),
        "beta.md": _page_md("Beta"),
    })
    report = validate(root)
    broken = [i for i in report.issues if i.field == "internal-links"]
    assert broken, [i.message for i in report.issues]
    assert broken[0].severity == "warning"
    assert "nope.md" in broken[0].message


def test_broken_internal_link_handles_any_diagnostic_kind():
    # The report must forward ANY diagnostic kind the resolver emits, not just
    # "broken" and "ambiguous". If #107 or a later issue adds escaping,
    # duplicate, or unsupported diagnostics, they surface automatically with
    # source page, severity, and location context (AC3).
    alpha = _page("Alpha", body=".\n")
    index = _KnowledgeIndex([alpha])
    # Inject a synthetic diagnostic with an unusual kind to prove the report
    # is kind-agnostic: it forwards whatever the resolver produces.
    index.extraction_diagnostics = [
        ExtractionDiagnostic(
            source_path=alpha.path,
            line_start=1,
            target="weird.md",
            kind="unsupported",
            detail="unsupported link target: weird.md",
        )
    ]
    issues = _cross_page_issues([alpha], index)

    forwarded = _by_field(issues, "internal-links")
    assert forwarded, [i.message for i in issues]
    assert "unsupported" in forwarded[0].message.lower()
    assert "weird.md" in forwarded[0].message
    assert forwarded[0].severity == "warning"


def test_exactly_resolved_internal_link_is_not_reported_broken():
    # AC4: an authored link that resolves to exactly one page produces an
    # Extracted Reference — it must NOT appear as a broken-internal-link.
    alpha = _page("Alpha", body="See [b](beta.md).\n")
    beta = _page("Beta", path="beta.md")
    issues = _qa_issues([alpha, beta])

    assert _by_field(issues, "internal-links") == [], [i.message for i in issues]


def test_exactly_resolved_link_does_not_spawn_proposal():
    # AC4: resolved links produce Extracted References only — never proposals.
    alpha = _page("Alpha", body="[b](beta.md) and [[Gamma]].\n")
    beta = _page("Beta", path="beta.md")
    gamma = _page("Gamma")
    issues = _qa_issues([alpha, beta, gamma])

    assert _by_field(issues, "internal-links") == []


def test_external_and_escaping_links_produce_no_internal_link_findings():
    # The shared resolver (#107) intentionally silences external URLs and
    # escaping paths as expected, non-actionable exclusions. This test
    # verifies the QA report faithfully forwards only what the resolver
    # actually emits as diagnostics — it does NOT introduce a parallel link
    # parser (AC6). If a future resolver change surfaces these as
    # diagnostics, the report will forward them automatically.
    alpha = _page("Alpha", body="[ext](https://example.com) [up](../outside.md)\n")
    beta = _page("Beta")
    issues = _qa_issues([alpha, beta])

    assert _by_field(issues, "internal-links") == [], [i.message for i in issues]


# ---------------------------------------------------------------------------
# AC1: stale (deprecated lifecycle) surfacing.
# ---------------------------------------------------------------------------


def test_deprecated_lifecycle_surfaced_as_stale():
    gamma = _page("Gamma", lifecycle="deprecated")
    issues = _qa_issues([gamma])

    stale = [
        i for i in issues
        if i.file == gamma.path and i.field == "lifecycle" and "stale" in i.message.lower()
    ]
    assert stale, [i.message for i in issues]
    assert "deprecated" in stale[0].message.lower()
    assert stale[0].severity == "warning"


def test_approved_lifecycle_not_flagged_stale():
    alpha = _page("Alpha", lifecycle="approved")
    beta = _page("Beta", lifecycle="review")
    draft = _page("Draft", lifecycle="draft")
    issues = _qa_issues([alpha, beta, draft])

    stale = [i for i in issues if "stale" in i.message.lower()]
    assert stale == [], [i.message for i in issues]


def test_stale_surfaced_through_public_validate_seam(tmp_path):
    root = _write_kb(tmp_path, {
        "old.md": _page_md("Old Page", lifecycle="deprecated"),
    })
    report = validate(root)
    stale = [i for i in report.issues if i.field == "lifecycle" and "stale" in i.message.lower()]
    assert stale, [i.message for i in report.issues]


# ---------------------------------------------------------------------------
# AC1: contradiction surfacing (``contradicts`` relationship type).
# ---------------------------------------------------------------------------


def test_contradiction_relationship_surfaced():
    alpha = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="contradicts")],
    )
    beta = _page("Beta")
    issues = _qa_issues([alpha, beta])

    contradiction = [
        i for i in issues
        if i.file == alpha.path
        and i.field == "claims"
        and "contradiction" in i.message.lower()
    ]
    assert contradiction, [i.message for i in issues]
    assert "entity:beta" in contradiction[0].message
    assert contradiction[0].severity == "warning"


def test_non_contradiction_relationship_not_flagged():
    alpha = _page(
        "Alpha",
        relationships=[Relationship(target="Beta", type="uses")],
    )
    beta = _page("Beta")
    issues = _qa_issues([alpha, beta])

    contradiction = [i for i in issues if "contradiction" in i.message.lower()]
    assert contradiction == [], [i.message for i in issues]


def test_contradiction_surfaced_through_public_validate_seam(tmp_path):
    # ADR-0021: the contradiction is an accepted Claim (predicate
    # ``contradicts``) validated against the Control File ontology.
    root = _write_kb(tmp_path, {
        "lumio.yaml": (
            "version: 2\n"
            'mode: "categorized"\n'
            "categories:\n"
            "  - name: concepts\n"
            "ontology:\n"
            "  entity_types:\n"
            "    concept: {}\n"
            "  predicates:\n"
            "    contradicts:\n"
            "      subject_types:\n"
            "        - concept\n"
            "      object_types:\n"
            "        - concept\n"
        ),
        "alpha.md": _page_md(
            "Alpha",
            entity_id="entity:alpha",
            claims=[("claim:alpha-contradicts-beta", "contradicts", "entity:beta")],
        ),
        "beta.md": _page_md(
            "Beta", entity_id="entity:beta", body="# Beta\n"
        ),
    })
    report = validate(root)
    assert report.is_valid, [i.message for i in report.issues]
    contradiction = [
        i for i in report.issues
        if "contradiction" in i.message.lower()
    ]
    assert contradiction, [i.message for i in report.issues]


# ---------------------------------------------------------------------------
# AC1: missing-frontmatter is already surfaced by per-page validation.
# Verify it appears in the aggregated cross-page report end-to-end.
# ---------------------------------------------------------------------------


def test_missing_required_frontmatter_appears_in_report(tmp_path):
    page = tmp_path / "incomplete.md"
    page.write_text(
        "---\n"
        'title: "Incomplete"\n'
        'summary: "Missing fields."\n'
        "---\n\nBody.\n"
    )
    report = validate(tmp_path)

    fields = {i.field for i in report.issues}
    assert "lifecycle" in fields, [i.field for i in report.issues]
    assert "visibility" in fields
    assert "tags" in fields
    assert not report.is_valid


# ---------------------------------------------------------------------------
# AC5: determinism — repeated validation produces identical issues.
# ---------------------------------------------------------------------------


def test_qa_report_is_deterministic_across_runs():
    pages = [
        _page("Alpha", body="[b](beta.md) and [missing](nope.md)\n"),
        _page("Beta", path="beta.md", lifecycle="deprecated"),
        _page("Gamma", relationships=[Relationship(target="Delta", type="contradicts")]),
        _page("Delta"),
        _page("Orphan"),
    ]
    first = _qa_issues(pages)
    second = _qa_issues(pages)

    assert [(i.file, i.field, i.message, i.severity) for i in first] == [
        (i.file, i.field, i.message, i.severity) for i in second
    ]


# ---------------------------------------------------------------------------
# Spec surfacing: ValidationReport.__str__ shows warnings so the CLI
# ``validate`` command surfaces QA findings even when the KB is valid.
# ---------------------------------------------------------------------------


def test_report_str_surfaces_warnings_when_valid():
    report = ValidationReport(
        issues=[
            ValidationIssue(
                file="gamma.md",
                field="topology",
                severity="warning",
                message="orphan page (discovery scope): no inbound topology at all",
            )
        ]
    )
    text = str(report)
    assert "valid" in text.lower()
    assert "orphan" in text.lower()
    assert "WARNING" in text


def test_report_str_shows_only_valid_when_no_issues():
    report = ValidationReport(issues=[])
    assert str(report) == "Knowledge base is valid."


# ---------------------------------------------------------------------------
# AC6: existing cross-page checks still fire alongside the new QA kinds.
# ---------------------------------------------------------------------------


def test_existing_cross_page_checks_still_fire():
    a1 = _page("Dup")
    a2 = _page("Dup", path="dup-2.md")
    b1 = _page("Bee", aliases=["Shared Alias"])
    b2 = _page("Bee Two", path="bee-two.md", aliases=["Shared Alias"])
    issues = _qa_issues([a1, a2, b1, b2])

    dup = [i for i in issues if i.field == "title" and "duplicate" in i.message.lower()]
    dup_alias = [
        i for i in issues if i.field == "aliases" and "duplicate" in i.message.lower()
    ]
    assert dup, [i.message for i in issues]
    assert dup_alias, [i.message for i in issues]
    # The old "unresolved relationship target" cross-page check moved to
    # ontology validation: dangling Claim objects are blocking errors there
    # (covered in test_entity_claims.py).
