"""Managed host-Distiller ingest (issue #149).

Exercises the ONE deep operation that binds an original raw Knowledge Source
(PDF/DOCX/HTML/Markdown/TXT) and a host-agent-authored Compiled Page into a
single staged proposal, preserving raw-source lineage that ordinary
``ingest <temp-markdown>`` loses. Covers the eight acceptance criteria in
issue #149: the single journey, per-format provenance + immutable Source
Version, private isolation, inspect distinction, identical-byte reuse,
changed-byte rejection, export/privacy, and the authored-page source-id
contract.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import lumio_wiki as lw
import pytest
from lumio_wiki.ingest import IngestStore, ManagedIngestError
from lumio_wiki.proposal_pipeline import ProposalPipeline
from lumio_wiki.source_registry import SourceRegistryError

ROOT = Path(__file__).parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "valid"

# (filename, content_type, expected converter) for AC2. No converter runs —
# managed ingest derives the converter NAME from routing, so the [documents]
# extra is never required and fake raw bytes suffice for every format.
FORMAT_PROVENANCE = [
    ("2025-impact-report.pdf", "application/pdf", "liteparse"),
    ("2025-impact-report.docx", None, "markitdown"),
    ("2025-impact-report.html", "text/html", "markitdown"),
    ("2025-impact-report.md", "text/markdown", "markdown"),
    ("2025-impact-report.txt", "text/plain", "text"),
]


def _kb(tmp_path: Path):
    root = tmp_path / "kb"
    shutil.copytree(FIXTURES, root)
    kb, report = lw.load_knowledge_base(root)
    assert report.is_valid, report
    return kb


def _authored_page(source_id: str, *, title: str = "Impact Report") -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "aliases: []\n"
        'tags:\n  - "report"\n'
        'summary: "Authored by the host agent from the original source."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        f'  - id: "{source_id}"\n'
        f'    title: "{source_id} source"\n'
        "relationships: []\n"
        "synthetic: false\n"
        "---\n\n"
        f"# {title}\n\n"
        "Body authored by the host coding agent from the original source.\n"
    )


def _pipeline(kb, tmp_path: Path):
    store = IngestStore(tmp_path / "ingest")
    return ProposalPipeline(kb, store=store), store


# ---------------------------------------------------------------------------
# AC1 — one journey binds the original source and authored page.
# ---------------------------------------------------------------------------


def test_managed_ingest_binds_source_and_authored_page_into_one_staged_proposal(
    tmp_path: Path,
):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 original impact report bytes"

    proposal = pipeline.managed_ingest(
        raw,
        "application/pdf",
        "2025-impact-report.pdf",
        "annual-impact-report",
        _authored_page("annual-impact-report"),
    )

    assert proposal.status == "staged"
    assert proposal.affected_pages == ["Impact Report"]
    # One staged proposal (not a registry proposal plus a separate page proposal).
    assert [p.id for p in pipeline.list()] == [proposal.id]
    # The authored Markdown — not distilled text — is the proposed page content.
    assert "Body authored by the host coding agent" in proposal.proposed_pages[0].markdown


# ---------------------------------------------------------------------------
# AC2 — per-format provenance + immutable Source Version identity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename, content_type, converter", FORMAT_PROVENANCE)
def test_managed_ingest_records_original_provenance_per_format(
    tmp_path: Path, filename, content_type, converter
):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"original raw bytes for " + filename.encode()

    proposal = pipeline.managed_ingest(
        raw, content_type, filename, "annual-impact-report", _authored_page("annual-impact-report")
    )

    provenance = proposal.provenance
    assert provenance.original_filename == filename
    assert provenance.content_type == content_type
    assert provenance.converted_by == converter
    assert provenance.source_id == "annual-impact-report"
    expected_hash = hashlib.sha256(raw).hexdigest()
    assert provenance.source_hash == expected_hash

    # The private Source identity is established with an immutable Source
    # Version whose content hash is over the ORIGINAL raw bytes (ADR-0014).
    source = store.source_registry.get("annual-impact-report")
    assert source.status == "active"
    assert source.source_id == "annual-impact-report"
    assert len(source.versions) == 1
    assert source.versions[0].content_hash == expected_hash
    assert source.versions[0].source_id == "annual-impact-report"


def test_managed_ingest_never_requires_the_documents_extra(tmp_path: Path):
    # No document converter runs (the host agent authored the page). The
    # processor is never invoked, so a PDF/DOCX/HTML source reaches a staged
    # proposal without LiteParse/MarkItDown installed.
    kb = _kb(tmp_path)
    pipeline, _store = _pipeline(kb, tmp_path)
    for filename, content_type, _ in FORMAT_PROVENANCE:
        if content_type is None:
            continue
        proposal = pipeline.managed_ingest(
            b"raw",
            content_type,
            filename,
            f"src-{filename}",
            _authored_page(f"src-{filename}", title=f"Page {filename}"),
        )
        assert proposal.status == "staged"


# ---------------------------------------------------------------------------
# AC5 — repeating with identical bytes reuses the Source Version.
# ---------------------------------------------------------------------------


def test_managed_ingest_repeats_with_identical_bytes_reusing_source_version(
    tmp_path: Path,
):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 identical bytes"

    first = pipeline.managed_ingest(
        raw, "application/pdf", "r.pdf", "annual-impact-report", _authored_page("annual-impact-report")
    )
    second = pipeline.managed_ingest(
        raw, "application/pdf", "r.pdf", "annual-impact-report", _authored_page("annual-impact-report")
    )

    source = store.source_registry.get("annual-impact-report")
    assert len(source.versions) == 1  # no duplicate Source Version
    # Each retry is a fresh reviewable proposal; the identity is reused.
    assert first.id != second.id
    assert {p.id for p in pipeline.list()} == {first.id, second.id}


# ---------------------------------------------------------------------------
# AC6 — changed bytes fail without mutation; retired blocks until reactivation.
# ---------------------------------------------------------------------------


def test_managed_ingest_changed_bytes_fail_without_mutation_directing_to_workflow(
    tmp_path: Path,
):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 v1"
    pipeline.managed_ingest(
        raw, "application/pdf", "r.pdf", "annual-impact-report", _authored_page("annual-impact-report")
    )
    source = store.source_registry.get("annual-impact-report")
    first_hash = source.versions[-1].content_hash

    with pytest.raises(SourceRegistryError, match="retire and reactivate"):
        pipeline.managed_ingest(
            b"%PDF-1.4 CHANGED",
            "application/pdf",
            "r.pdf",
            "annual-impact-report",
            _authored_page("annual-impact-report"),
        )

    # No registry or proposal mutation: the single immutable version is intact
    # and no extra proposal was staged.
    source = store.source_registry.get("annual-impact-report")
    assert [v.content_hash for v in source.versions] == [first_hash]
    assert len(pipeline.list()) == 1


def test_managed_ingest_retired_source_blocks_until_reactivation(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    pipeline.managed_ingest(
        b"v1", "text/plain", "r.txt", "annual-impact-report", _authored_page("annual-impact-report")
    )
    retirement = pipeline.retire_source("annual-impact-report")
    pipeline.publish(retirement.id)
    assert store.source_registry.get("annual-impact-report").status == "retired"

    with pytest.raises(SourceRegistryError, match="retired"):
        pipeline.managed_ingest(
            b"v1",
            "text/plain",
            "r.txt",
            "annual-impact-report",
            _authored_page("annual-impact-report"),
        )


# ---------------------------------------------------------------------------
# Authored-page source-id contract — a missing/mismatched id blocks staging.
# ---------------------------------------------------------------------------


def test_managed_ingest_missing_source_id_blocks_staging(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    page = _authored_page("some-other-id")  # does NOT cite the chosen id

    with pytest.raises(ManagedIngestError, match="sources\\[\\].id"):
        pipeline.managed_ingest(b"raw", "text/plain", "r.txt", "annual-impact-report", page)
    # Nothing was staged.
    assert pipeline.list() == []


def test_managed_ingest_compound_revision_may_retain_additional_sources(tmp_path: Path):
    # The chosen source_id only needs to appear once; compound revisions may
    # retain additional existing Sources on the same page.
    kb = _kb(tmp_path)
    pipeline, _store = _pipeline(kb, tmp_path)
    page = (
        "---\n"
        'title: "Compound Page"\n'
        "sources:\n"
        '  - id: "existing-source"\n'
        '    title: "existing"\n'
        '  - id: "annual-impact-report"\n'
        '    title: "new source"\n'
        "---\n\n# Compound Page\n\nBody.\n"
    )

    proposal = pipeline.managed_ingest(
        b"raw", "application/pdf", "r.pdf", "annual-impact-report", page
    )
    assert proposal.status == "staged"
    assert "existing-source" in proposal.proposed_pages[0].markdown
    assert "annual-impact-report" in proposal.proposed_pages[0].markdown


# ---------------------------------------------------------------------------
# AC3 — the raw source is isolated in private ingest state, never under the KB.
# ---------------------------------------------------------------------------


def test_managed_ingest_keeps_raw_source_in_private_state_not_under_kb_root(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 private raw bytes"

    pipeline.managed_ingest(
        raw, "application/pdf", "2025-impact-report.pdf", "annual-impact-report", _authored_page(
            "annual-impact-report"
        )
    )

    # The registry lives in the ingest store, OUTSIDE the Knowledge Base root,
    # and retains only identity/version hashes — not the raw bytes.
    assert (store.root / "source-registry").exists()
    assert (store.root / "source-registry").is_dir()
    assert not (kb.root / "source-registry").exists()
    # No raw bytes are written under the KB root (page-discovery risk).
    for path in kb.root.rglob("*"):
        if path.is_file():
            assert raw not in path.read_bytes(), f"raw bytes leaked into KB: {path}"


# ---------------------------------------------------------------------------
# AC4 — inspect distinguishes raw-source provenance from authored content.
# ---------------------------------------------------------------------------


def test_managed_ingest_inspect_distinguishes_provenance_from_authored_content(
    tmp_path: Path,
):
    kb = _kb(tmp_path)
    pipeline, _store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 original"
    proposal = pipeline.managed_ingest(
        raw, "application/pdf", "2025-impact-report.pdf", "annual-impact-report", _authored_page(
            "annual-impact-report"
        )
    )

    reviewed = pipeline.review(proposal.id)
    provenance = reviewed.provenance
    # Raw-source provenance: original filename, content type, converter, hash,
    # and the bound source identity.
    assert provenance.original_filename == "2025-impact-report.pdf"
    assert provenance.content_type == "application/pdf"
    assert provenance.converted_by == "liteparse"
    assert provenance.source_hash == hashlib.sha256(raw).hexdigest()
    assert provenance.source_id == "annual-impact-report"
    # Authored Compiled Page content: the proposed page Markdown body.
    body = reviewed.proposed_pages[0].markdown
    assert "Body authored by the host coding agent" in body
    # The private hash does not appear in the authored page content.
    assert provenance.source_hash not in body


# ---------------------------------------------------------------------------
# AC7 — no raw bytes, filesystem paths, registry state, or private metadata
# leak into the published KB, export, or canonical fingerprint.
# ---------------------------------------------------------------------------


def test_managed_ingest_publish_never_leaks_raw_bytes_or_registry_state(tmp_path: Path):
    kb = _kb(tmp_path)
    pipeline, store = _pipeline(kb, tmp_path)
    raw = b"%PDF-1.4 SUPERSECRETRAWBYTES"
    proposal = pipeline.managed_ingest(
        raw, "application/pdf", "2025-impact-report.pdf", "annual-impact-report", _authored_page(
            "annual-impact-report"
        )
    )

    published = pipeline.publish(proposal.id)
    assert published.status == "published"

    page_path = kb.root / "impact_report.md"
    assert page_path.exists()
    page = page_path.read_text(encoding="utf-8")
    # The PUBLIC source id is intentionally part of the page's sources[].
    assert "annual-impact-report" in page
    # The private raw bytes, content hash, original filename, and converter
    # never reach the published page.
    assert b"SUPERSECRETRAWBYTES" not in page_path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    assert content_hash not in page
    assert "2025-impact-report.pdf" not in page
    assert "liteparse" not in page

    # The canonical fingerprint and KB export are over authored content only;
    # the private registry state (outside the KB root) is never included.
    fingerprint = lw.fingerprint_sources(kb.root)
    bundle = lw.export_bundle(kb)
    for artifact in (bundle, getattr(fingerprint, "digest", ""), repr(fingerprint)):
        assert content_hash not in artifact
        assert "SUPERSECRETRAWBYTES" not in artifact
    # The registry is private state under the ingest store, not the KB.
    assert not (kb.root / "source-registry").exists()
