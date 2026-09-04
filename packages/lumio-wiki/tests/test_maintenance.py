"""Tests for the portable Maintainer workflows (ADR-0015).

Covers ``lumio_wiki.maintenance`` (lint, cross-link staging, Dream Cycle) and
the ``lumio-wiki lint|cross-link|dream`` CLI verbs. Everything is model-free
and proposal-first: lint/dream reflection is read-only, and staging produces
reviewable Ingest Proposals that never touch the Knowledge Base on disk.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import lumio_wiki as lw
import msgspec
import pytest
from lumio_wiki.artifact_store import (
    SourceBindingEntry,
    SourceBindingManifest,
    build_binding_manifest,
)
from lumio_wiki.cli import main
from lumio_wiki.ingest import IngestStore
from lumio_wiki.knowledge_base import KnowledgeBase
from lumio_wiki.publish import merge_compound_sources
from lumio_wiki.records import CompiledPage
from lumio_wiki.source_registry import SourceRegistry

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures"

#: Raw bytes for the private Knowledge Source used in Source Drift tests.
DRIFT_RAW = b"drift diagnostic source bytes (#196)"


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    """Copy the categorized fixture into a writable Knowledge Base root."""
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "categorized_kb", root)
    return root


@pytest.fixture
def kb_with_candidate(kb_root: Path) -> Path:
    """A categorized KB whose overview page mentions Acme Corp UNLINKED."""
    overview = kb_root / "concepts" / "overview.md"
    text = overview.read_text(encoding="utf-8")
    overview.write_text(text.rstrip() + "\n\nAcme Corp is a launch customer.\n", encoding="utf-8")
    return kb_root


@pytest.fixture
def kb_with_same_directory_candidate(kb_root: Path) -> Path:
    """A KB whose cross-link candidate resolves within the source directory."""
    overview = kb_root / "concepts" / "overview.md"
    overview_text = overview.read_text(encoding="utf-8")
    glossary = (
        overview_text
        .replace('title: "Lumio Overview"', 'title: "Glossary"')
        .replace('id: "entity:lumio-overview"', 'id: "entity:glossary"')
        .replace('claim:lumio-overview-uses-acme', 'claim:glossary-uses-acme')
        .replace('section: "Lumio Overview"', 'section: "Glossary"')
        .replace('# Lumio Overview', '# Glossary')
    )
    (kb_root / "concepts" / "glossary.md").write_text(glossary, encoding="utf-8")
    overview.write_text(
        overview_text.rstrip() + "\n\nThe Glossary explains the KB terminology.\n",
        encoding="utf-8",
    )
    return kb_root


@pytest.fixture
def real_world_kb(tmp_path: Path) -> Path:
    """Copy the committed Atlas Heatworks trial fixture into a writable KB."""
    root = tmp_path / "atlas-heatworks-kb"
    shutil.copytree(FIXTURES / "real_world_atlas_kb", root)
    return root


def _store(kb_root: Path) -> IngestStore:
    store_dir = kb_root / ".lumio" / "ingest"
    store_dir.mkdir(parents=True, exist_ok=True)
    return IngestStore(store_dir)


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def test_run_lint_reports_both_scopes(kb_root: Path):
    report = lw.run_lint(kb_root)
    assert report.is_valid
    assert report.page_count == 2
    assert report.canonical_structure.scope == "canonical"
    assert report.discovery_structure.scope == "discovery"
    assert "canonical" in report.scope_disclosure
    assert "discovery" in report.scope_disclosure


def test_run_lint_is_read_only(kb_root: Path):
    before = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    lw.run_lint(kb_root)
    after = {p: p.read_bytes() for p in kb_root.rglob("*.md")}
    assert before == after


# ---------------------------------------------------------------------------
# Cross-link repair helpers
# ---------------------------------------------------------------------------


def test_repair_mention_wraps_verbatim_text():
    md = "We use Beta for storage.\n"
    candidate = lw.find_link_candidates(
        [
            _compiled_page("Alpha", body="We use Beta for storage.\n", path="concepts/alpha.md"),
            _compiled_page("Beta", path="entities/beta.md"),
        ]
    )[0]
    repaired = lw.repair_mention(md, candidate)
    assert "[Beta](../entities/beta.md)" in repaired


def _compiled_page(title: str, *, body: str = "", path: str | None = None):
    from lumio_wiki.records import CompiledPage, Source

    return CompiledPage(
        path=path or f"{title.lower()}.md",
        title=title,
        aliases=[],
        tags=["test"],
        summary=f"{title} summary",
        lifecycle="approved",
        visibility="public",
        sources=[Source(id=f"src-{title.lower()}", title=title)],
        body=body,
        body_start_line=1,
    )


@pytest.mark.parametrize("editor", [lw.mark_compound_revision])
def test_frontmatter_edit_rejects_missing_closing_fence(editor):
    with pytest.raises(lw.MaintenanceError, match="missing closing fence"):
        if editor is lw.mark_compound_revision:
            editor("---\ntitle: P\n", category="concepts")
        else:
            editor("---\ntitle: P\n", "Target", "references")


def test_mark_compound_revision_restates_routing_fields():
    md = "---\ntitle: P\n---\n\nbody\n"
    marked = lw.mark_compound_revision(md, category="concepts", durability_rationale="kept")
    data, _body, _ = lw.parse_frontmatter(marked, Path("p.md"))
    assert data["compound_revision"] is True
    assert data["category"] == "concepts"
    assert data["durability_rationale"] == "kept"


def test_category_for_page_path(kb_root: Path):
    kb, _report = lw.load_knowledge_base(kb_root)
    assert lw.category_for_page_path(kb, "entities/acme.md") == "entities"
    assert lw.category_for_page_path(kb, "acme.md") is None
    assert lw.category_for_page_path(kb, "unknown/acme.md") is None


# ---------------------------------------------------------------------------
# Cross-link staging (compound revision through the Proposal Pipeline)
# ---------------------------------------------------------------------------


def test_stage_cross_link_proposal_is_publishable(kb_with_candidate: Path):
    """A staged repair validates clean against a categorized KB.

    Regression: compound revisions of pages with id-less sources were falsely
    blocked (merged sources came back empty), and categorized revisions were
    blocked for missing category / durability rationale routing fields.
    """
    kb_root = kb_with_candidate
    # Strip the source id so the page carries id-less provenance (valid).
    acme = kb_root / "entities" / "acme.md"
    acme.write_text(
        acme.read_text(encoding="utf-8").replace('  - id: "acme"\n', "  - "),
        encoding="utf-8",
    )
    kb, _report = lw.load_knowledge_base(kb_root)
    candidates = lw.find_link_candidates(kb.pages)
    assert len(candidates) == 1

    proposal = lw.stage_cross_link_proposal(kb_root, candidates[0], store=_store(kb_root))

    assert proposal.status == "staged"
    assert not proposal.blocked, [
        f"{i.file}: {i.field}: {i.message}"
        for i in proposal.validation_report.issues
        if i.severity == "error"
    ]
    # The Knowledge Base on disk is untouched (proposal-first).
    assert "[Acme Corp]" not in (kb_root / "concepts" / "overview.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dream Cycle
# ---------------------------------------------------------------------------


def test_run_dream_cycle_reflects_without_writing(kb_with_candidate: Path):
    before = {p: p.read_bytes() for p in kb_with_candidate.rglob("*.md")}
    report = lw.run_dream_cycle(kb_with_candidate)
    assert report.is_valid
    assert report.candidate_count == 1
    ranked = report.ranked_candidates[0]
    assert ranked.candidate.target_title == "Acme Corp"
    after = {p: p.read_bytes() for p in kb_with_candidate.rglob("*.md")}
    assert before == after


def test_cli_dream_with_staging_loads_knowledge_base_once(
    kb_with_candidate: Path, monkeypatch
):
    import lumio_wiki.cli as cli_module
    import lumio_wiki.maintenance as maintenance_module

    real_load = maintenance_module.load_knowledge_base
    load_count = 0

    def counting_load(path):
        nonlocal load_count
        load_count += 1
        return real_load(path)

    monkeypatch.setattr(cli_module, "load_knowledge_base", counting_load)
    monkeypatch.setattr(maintenance_module, "load_knowledge_base", counting_load)

    assert main(["dream", str(kb_with_candidate), "--stage", "--limit", "1"]) == 0
    assert load_count == 1


def test_stage_dream_repairs_stages_bounded_proposals(kb_with_candidate: Path):
    result = lw.stage_dream_repairs(kb_with_candidate, store=_store(kb_with_candidate), limit=1)
    assert len(result.staged) == 1
    assert not result.skipped
    assert result.staged[0].status == "staged"
    assert not result.staged[0].blocked


def test_stage_dream_repairs_rejects_bad_limit(kb_with_candidate: Path):
    with pytest.raises(lw.MaintenanceError):
        lw.stage_dream_repairs(kb_with_candidate, store=_store(kb_with_candidate), limit=0)


# ---------------------------------------------------------------------------
# Source Drift diagnostic (issue #196, ADR-0014 / ADR-0020)
#
# A read-only, model-free Dream Cycle diagnostic over two scopes: the current
# working copy (non-synthetic pages whose declared source is retired) and the
# active Published Version's private Source Binding Manifest (entries bound to
# a retired source or superseded by the registry's current hash). The
# manifest tier describes the Published Version only — it is never joined to
# the current worktree by title.
# ---------------------------------------------------------------------------


def _drift_registry(tmp_path: Path) -> tuple[SourceRegistry, str]:
    """A real registry with one active and one retired registered source.

    Retirement flows through the same reviewed seam the CLI uses:
    ``stage_retirement -> bind_pending -> apply_transition``.
    """
    registry = SourceRegistry(tmp_path / "source-registry")
    registry.register_source("acme-active", b"active source bytes")
    retired_version = registry.register_source("acme-retired", DRIFT_RAW)
    transition = registry.stage_retirement("acme-retired")
    registry.bind_pending(transition, "proposal-retire")
    registry.apply_transition("proposal-retire")
    return registry, retired_version.content_hash


def _drift_kb(pages: list[CompiledPage]) -> KnowledgeBase:
    """A minimal in-memory KnowledgeBase record for the pure diagnostic."""
    return KnowledgeBase(root=Path("/tmp/drift-kb"), pages=pages)


def _page_declaring(source_id: str, title: str = "Acme Corp") -> CompiledPage:
    from lumio_wiki.records import Source

    page = _compiled_page(title)
    return msgspec.structs.replace(
        page, sources=[Source(id=source_id, title=title)]
    )


def test_source_drift_working_copy_retired_source(tmp_path: Path):
    """A working-copy page declaring a retired registered source is reported."""
    registry, retired_hash = _drift_registry(tmp_path)
    page = _page_declaring("acme-retired")
    report = lw.run_source_drift_check(_drift_kb([page]), registry)
    assert report.registry_checked
    assert report.count == 1
    finding = report.findings[0]
    assert finding.scope == "working-copy"
    assert finding.kind == "retired-source"
    assert finding.page_title == "Acme Corp"
    assert finding.page_path == page.path
    assert finding.source_id == "acme-retired"
    assert finding.current_content_hash == retired_hash
    assert finding.bound_content_hash is None


def test_source_drift_working_copy_active_source_is_clean(tmp_path: Path):
    registry, _hash = _drift_registry(tmp_path)
    report = lw.run_source_drift_check(_drift_kb([_page_declaring("acme-active")]), registry)
    assert report.count == 0


def test_source_drift_unregistered_id_is_public_provenance(tmp_path: Path):
    """An unregistered ``sources[].id`` stays public provenance: no finding."""
    registry, _hash = _drift_registry(tmp_path)
    report = lw.run_source_drift_check(_drift_kb([_page_declaring("never-registered")]), registry)
    assert report.count == 0


def test_source_drift_skips_synthetic_working_copy_pages(tmp_path: Path):
    """Synthetic pages may omit provenance (ADR-0014): never drift-checked."""
    registry, _hash = _drift_registry(tmp_path)
    page = msgspec.structs.replace(_page_declaring("acme-retired"), synthetic=True)
    report = lw.run_source_drift_check(_drift_kb([page]), registry)
    assert report.count == 0


def test_source_drift_no_registry_is_not_checked(tmp_path: Path):
    """Without a private registry the working-copy tier is simply unchecked."""
    report = lw.run_source_drift_check(_drift_kb([_page_declaring("acme-retired")]), None)
    assert report.registry_checked is False
    assert report.count == 0
    assert "not checked" in report.manifest_status


def _drift_manifest(
    entries: list[SourceBindingEntry], version: str = "v2026"
) -> SourceBindingManifest:
    return SourceBindingManifest(
        published_version=version, fingerprint="fp", created_at="2026-09-03T00:00:00Z",
        entries=entries,
    )


def _manifest_entry(
    source_id: str,
    content_hash: str,
    *,
    page_title: str = "Annual Report",
    synthetic_page: bool = False,
) -> SourceBindingEntry:
    return SourceBindingEntry(
        page_title=page_title,
        source_id=source_id,
        content_hash=content_hash,
        synthetic_page=synthetic_page,
    )


def test_source_drift_manifest_superseded_evidence(tmp_path: Path):
    """A non-synthetic active entry whose bound hash is stale is reported."""
    registry, _hash = _drift_registry(tmp_path)
    manifest = _drift_manifest([_manifest_entry("acme-active", "ab" * 32)])
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.registry_checked
    assert report.count == 1
    finding = report.findings[0]
    assert finding.scope == "published-version"
    assert finding.kind == "superseded-evidence"
    assert finding.published_version == "v2026"
    assert finding.page_title == "Annual Report"
    assert finding.source_id == "acme-active"
    assert finding.bound_content_hash == "ab" * 32
    assert finding.current_content_hash  # the registry's current hash
    assert finding.page_path is None


def test_source_drift_manifest_retired_source(tmp_path: Path):
    """A manifest entry bound to a retired source is reported — and wins."""
    registry, retired_hash = _drift_registry(tmp_path)
    # Retired AND bound to a stale hash: retired-source takes precedence.
    manifest = _drift_manifest([_manifest_entry("acme-retired", "cd" * 32)])
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.count == 1
    finding = report.findings[0]
    assert finding.scope == "published-version"
    assert finding.kind == "retired-source"
    assert finding.source_id == "acme-retired"
    assert finding.published_version == "v2026"


def test_source_drift_manifest_matching_hash_is_clean(tmp_path: Path):
    registry, _hash = _drift_registry(tmp_path)
    current = registry.get("acme-active").versions[-1].content_hash
    manifest = _drift_manifest([_manifest_entry("acme-active", current)])
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.count == 0


def test_source_drift_manifest_synthetic_entry_skipped(tmp_path: Path):
    """Synthetic manifest entries are skipped, exactly as publish skips them."""
    registry, _hash = _drift_registry(tmp_path)
    manifest = _drift_manifest(
        [_manifest_entry("acme-retired", "ab" * 32, synthetic_page=True)]
    )
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.count == 0


def test_source_drift_manifest_entry_absent_from_worktree_still_reported(tmp_path: Path):
    """The manifest tier never joins to the worktree by title (ADR-0020)."""
    registry, retired_hash = _drift_registry(tmp_path)
    manifest = _drift_manifest(
        [_manifest_entry("acme-retired", retired_hash, page_title="Vanished Page")]
    )
    # The worktree has NO page titled "Vanished Page" — the finding survives.
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.count == 1
    assert report.findings[0].page_title == "Vanished Page"


def test_source_drift_manifest_unknown_source_is_incomplete_never_clean(tmp_path: Path):
    """An entry whose source id is absent from the registry is incomplete."""
    registry, _hash = _drift_registry(tmp_path)
    manifest = _drift_manifest([_manifest_entry("ghost-source", "ab" * 32)])
    report = lw.run_source_drift_check(_drift_kb([]), registry, manifest=manifest)
    assert report.count == 0
    assert report.manifest_status.startswith("incomplete")


def test_source_drift_incomplete_overrides_caller_checked_status(tmp_path: Path):
    """Computed facts win: 'incomplete' overrides a caller 'checked' status."""
    registry, _hash = _drift_registry(tmp_path)
    manifest = _drift_manifest([_manifest_entry("ghost-source", "ab" * 32)])
    report = lw.run_source_drift_check(
        _drift_kb([]),
        registry,
        manifest=manifest,
        manifest_status="checked (published version v1)",
    )
    assert report.manifest_status.startswith("incomplete")
    assert not report.manifest_status.startswith("checked")


def test_source_drift_no_registry_never_echoes_caller_checked_status(tmp_path: Path):
    """registry is None + a caller 'checked' status is never echoed clean."""
    report = lw.run_source_drift_check(
        _drift_kb([]),
        None,
        manifest_status="checked (published version v1)",
    )
    assert report.registry_checked is False
    assert report.manifest_status.startswith("not checked")


def test_source_drift_findings_sort_deterministically(tmp_path: Path):
    """Findings sort by scope, published version, title/path, source, kind."""
    registry, _hash = _drift_registry(tmp_path)
    page = _page_declaring("acme-retired")
    manifest = _drift_manifest(
        [
            _manifest_entry("acme-retired", "cd" * 32),
            _manifest_entry("acme-active", "ef" * 32),
        ]
    )
    report = lw.run_source_drift_check(_drift_kb([page]), registry, manifest=manifest)
    keys = [
        (f.scope, f.published_version or "", f.page_path or f.page_title, f.source_id, f.kind)
        for f in report.findings
    ]
    assert keys == sorted(keys)
    assert report.count == 3  # 1 working-copy + 2 published-version


def test_source_drift_dream_report_carries_drift(kb_root: Path, tmp_path: Path):
    """``run_dream_cycle`` composes the diagnostic when inputs are given."""
    registry, _hash = _drift_registry(tmp_path)
    report = lw.run_dream_cycle(
        kb_root,
        registry=registry,
        manifest=None,
        manifest_status="not checked: no Published Version",
    )
    assert report.drift.registry_checked
    assert report.drift.manifest_status.startswith("not checked")


# ---------------------------------------------------------------------------
# Source Drift CLI (issue #196): ``lumio-wiki dream`` resolves optional
# private inputs and reports the tier status without ever falsely reporting
# a clean manifest check.
# ---------------------------------------------------------------------------


def test_cli_dream_reports_retired_source_finding(
    kb_root: Path, tmp_path: Path, monkeypatch, capsys
):
    registry, _hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    # Declare the retired source on the overview page (id must be registered).
    overview = kb_root / "concepts" / "overview.md"
    overview.write_text(
        overview.read_text(encoding="utf-8").replace('id: "lumio-overview"', 'id: "acme-retired"'),
        encoding="utf-8",
    )
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0  # advisory: the exit rule is validation-only
    assert "source_drift_findings:  1" in out
    assert "retired-source" in out
    assert "concepts/overview.md" in out
    assert "registry" in out


def test_cli_dream_without_ingest_dir_does_not_create_one(
    kb_root: Path, monkeypatch, capsys
):
    ingest_dir = kb_root / ".lumio" / "ingest"
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    assert not ingest_dir.exists()
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert not ingest_dir.exists()  # read-only: the store was never created
    assert "source_drift_registry:  not checked" in out


def test_cli_dream_without_artifact_store_reports_registry_only_tier(
    kb_root: Path, tmp_path: Path, monkeypatch, capsys
):
    registry, _hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    monkeypatch.delenv("LUMIO_SOURCE_STORE", raising=False)
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source_drift_registry:  checked" in out
    assert "source_drift_manifest: not checked: no Source Artifact Store" in out


def test_cli_dream_manifest_tier_incomplete_without_active_version(
    kb_root: Path, tmp_path: Path, monkeypatch, capsys
):
    """A configured store with no active Published Version skips the tier."""
    registry, _hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(tmp_path / "store"))
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source_drift_manifest" in out
    assert "incomplete" in out or "not checked" in out


def test_cli_dream_reports_manifest_tier_from_active_version(
    kb_root: Path, tmp_path: Path, monkeypatch, capsys
):
    """A local KB with LUMIO_PUBLISH_TO pointing at an object store checks
    the active Published Version's binding manifest."""
    from lumio_wiki import cli
    from lumio_wiki.s3_publish import PointerObservation

    store_root = tmp_path / "store"
    store = lw.LocalDirectoryArtifactStore(store_root)
    registry, retired_hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    kb, _report = lw.load_knowledge_base(kb_root)
    manifest = build_binding_manifest(
        published_version="v2026",
        fingerprint="fp",
        registry=registry,
        pages=kb.pages,
        now="2026-09-03T00:00:00Z",
    )
    store.put_binding_manifest("v2026", msgspec.json.encode(manifest))
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    monkeypatch.setenv("LUMIO_PUBLISH_TO", "s3://bucket/kb")
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (object(), "kb-prefix"))
    monkeypatch.setattr(
        "lumio_wiki.s3_publish.observe_current_pointer",
        lambda store_arg, prefix: PointerObservation(version="v2026", e_tag="e"),
    )
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source_drift_manifest: checked (published version v2026)" in out


def test_cli_dream_store_failure_is_bounded_and_secret_free(
    kb_root: Path, tmp_path: Path, monkeypatch, capsys
):
    """An unreachable object store yields a bounded status, never a crash or
    a leaked raw object key / endpoint URL (review finding, #196)."""
    from lumio_wiki import cli
    from lumio_wiki.knowledge_base import KnowledgeBaseError

    store_root = tmp_path / "store"
    lw.LocalDirectoryArtifactStore(store_root)
    registry, _hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(store_root))
    monkeypatch.setenv("LUMIO_PUBLISH_TO", "s3://bucket/kb")
    monkeypatch.setattr(cli, "_build_publish_store", lambda uri: (object(), "kb-prefix"))
    monkeypatch.setattr(
        "lumio_wiki.s3_publish.observe_current_pointer",
        lambda store_arg, prefix: (_ for _ in ()).throw(
            KnowledgeBaseError(
                "could not read activation pointer 'private/prefix/current' at https://secret-endpoint"
            )
        ),
    )
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "source_drift_manifest: manifest check failed: unavailable" in out
    assert "secret-endpoint" not in out
    assert "current" not in out.split("source_drift_manifest")[1].splitlines()[0]


def test_cli_dream_missing_local_store_creates_nothing(kb_root, tmp_path, monkeypatch, capsys):
    """A nonexistent local store path is never created by a read-only dream."""
    registry, _hash = _drift_registry(tmp_path)
    ingest = IngestStore(kb_root / ".lumio" / "ingest")
    shutil.copytree(
        tmp_path / "source-registry", ingest.source_registry.root, dirs_exist_ok=True
    )
    missing = tmp_path / "missing-store"
    monkeypatch.setenv("LUMIO_SOURCE_STORE", str(missing))
    monkeypatch.delenv("LUMIO_PUBLISH_TO", raising=False)
    rc = main(["dream", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert not missing.exists()  # read-only: the store path was never created
    assert "source_drift_manifest" in out


def test_merge_compound_sources_preserves_idless_sources():
    existing = textwrap.dedent(
        """\
        ---
        title: "P"
        sources:
          - title: "Medium post"
            url: "https://example.com/post"
          - title: "Spec repo"
            url: "https://example.com/spec"
        ---

        old body
        """
    )
    proposed = textwrap.dedent(
        """\
        ---
        title: "P"
        sources:
          - title: "Medium post"
            url: "https://example.com/post"
        ---

        new body
        """
    )
    merged = merge_compound_sources(proposed, existing)
    data, body, _ = lw.parse_frontmatter(merged, Path("p.md"))
    sources = lw.as_sources(data.get("sources"))
    titles = [s.title for s in sources]
    assert titles == ["Medium post", "Spec repo"], (
        "id-less existing sources are preserved, not dropped"
    )
    assert "new body" in body


# ---------------------------------------------------------------------------
# CLI verbs
# ---------------------------------------------------------------------------


def test_cli_lint_reports_and_exits_zero(kb_root: Path, capsys):
    rc = main(["lint", str(kb_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "valid:               True" in out
    assert "scope_disclosure:" in out
    assert "structure_scope:     canonical" in out
    assert "structure_scope:     discovery" in out


def test_cli_lint_exit_one_on_invalid(tmp_path: Path, capsys):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES / "invalid", root)
    rc = main(["lint", str(root)])
    assert rc == 1


def test_cli_cross_link_lists_candidates(kb_with_candidate: Path, capsys):
    rc = main(["cross-link", str(kb_with_candidate)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "link_candidates:     1" in out
    assert "Lumio Overview -> Acme Corp" in out
    assert "--stage" in out


def test_cli_cross_link_stage(kb_with_candidate: Path, capsys):
    rc = main(["cross-link", str(kb_with_candidate), "--stage"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staged_proposals:    1" in out
    assert "[Acme Corp]" not in (kb_with_candidate / "concepts" / "overview.md").read_text(
        encoding="utf-8"
    )


def test_cli_dream_reflects(kb_with_candidate: Path, capsys):
    rc = main(["dream", str(kb_with_candidate)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Dream Cycle" in out
    assert "link_candidates:     1" in out


def test_cli_dream_stage(kb_with_candidate: Path, capsys):
    rc = main(["dream", str(kb_with_candidate), "--stage", "--limit", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staged_proposals:    1" in out


# ---------------------------------------------------------------------------
# relationship stage CLI (issue #151): typed Relationship proposals.
#
# A typed Relationship is a canonical, reviewed edge (canonical-graph scope).
# It is distinct from ``cross-link --stage``, which only adds authored
# Markdown links that become Extracted References (discovery-graph scope).
# ---------------------------------------------------------------------------


def _extract_proposal_id(output: str) -> str:
    """Pull the staged proposal id from a ``Staged proposal <id>`` line."""
    return output.split("Staged proposal")[1].split()[0]


def _extract_staged_id(output: str) -> str:
    """Pull the proposal id from a cross-link/dream ``staged: <id>`` line."""
    return output.split("staged:")[1].split()[0]


def test_cli_cross_link_stage_is_discovery_only(
    kb_with_same_directory_candidate: Path, capsys
):
    # AC#6: ``cross-link --stage`` repairs a candidate as an authored Markdown
    # link that becomes an Extracted Reference. It never creates a typed
    # canonical Relationship.
    rc = main(["cross-link", str(kb_with_same_directory_candidate), "--stage"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staged_proposals:    1" in out
    pid = _extract_staged_id(out)
    assert main(["publish", str(kb_with_same_directory_candidate), pid]) == 0
    capsys.readouterr()  # drain publish output
    assert main(["validate", str(kb_with_same_directory_candidate)]) == 0

    overview = (
        kb_with_same_directory_candidate / "concepts" / "overview.md"
    ).read_text(encoding="utf-8")
    assert "[Glossary](glossary.md)" in overview
    assert "relationships:" not in overview
    main(
        [
            "related",
            str(kb_with_same_directory_candidate),
            "Lumio Overview",
            "--scope",
            "canonical",
        ]
    )
    assert "Glossary" not in capsys.readouterr().out
    main(
        [
            "related",
            str(kb_with_same_directory_candidate),
            "Lumio Overview",
            "--scope",
            "discovery",
        ]
    )
    assert "Glossary" in capsys.readouterr().out


def test_canonical_vs_discovery_scope_distinction(kb_root: Path, capsys):
    """AC#8: an accepted Claim is a canonical edge; a same-directory body
    link is a discovery-only Extracted Reference. The two scopes differ."""
    # Author a same-directory sibling page (mirrors a valid fixture page) and
    # an authored same-directory body link, which resolves (no ``..``) to a
    # discovery-only Extracted Reference.
    overview_src = (kb_root / "concepts" / "overview.md").read_text(encoding="utf-8")
    glossary_src = (
        overview_src
        .replace('title: "Lumio Overview"', 'title: "Glossary"')
        .replace('id: "entity:lumio-overview"', 'id: "entity:glossary"')
        .replace('claim:lumio-overview-uses-acme', 'claim:glossary-uses-acme')
        .replace('section: "Lumio Overview"', 'section: "Glossary"')
        .replace('# Lumio Overview', '# Glossary')
    )
    (kb_root / "concepts" / "glossary.md").write_text(glossary_src, encoding="utf-8")
    overview = kb_root / "concepts" / "overview.md"
    overview.write_text(
        overview.read_text(encoding="utf-8").rstrip()
        + "\n\nSee the [Glossary](glossary.md) for terms.\n",
        encoding="utf-8",
    )
    assert main(["validate", str(kb_root)]) == 0

    main(["related", str(kb_root), "Lumio Overview", "--scope", "canonical"])
    canonical = capsys.readouterr().out
    main(["related", str(kb_root), "Lumio Overview", "--scope", "discovery"])
    discovery = capsys.readouterr().out

    # The fixture's accepted Claim (uses -> Acme Corp) is canonical.
    assert "Acme Corp" in canonical
    assert "Acme Corp" in discovery
    # The same-directory body link is a discovery-ONLY edge (no Claim).
    assert "Glossary" not in canonical
    assert "Glossary" in discovery
