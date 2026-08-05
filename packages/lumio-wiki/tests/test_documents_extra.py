"""Issue #100: the ``lumio-wiki[documents]`` ingestion capability.

Tests the document Source Processor implementations (PdfSourceProcessor,
AnyDocSourceProcessor, and MarkItDownSourceProcessor), the routing logic,
bounded extraction, stable page/section numbers, timeout behavior, actionable
malformed/encrypted-input errors, and the full document → distill → propose
journey through the same Pipeline as text/Markdown sources.

These tests run in the workspace environment where LiteParse and MarkItDown
are installed (they are workspace-level dependencies of the full ``lumio``
application). The authoritative proof that the base wheel works WITHOUT
these converters — producing the actionable missing-extra error — lives in
``test_wheel_isolation.py`` (issue #100, AC6).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from lumio_wiki.source_processor import (
    DOCUMENTS_EXTRA_HINT,
    AnyDocSourceProcessor,
    MarkItDownSourceProcessor,
    MissingDocumentExtraError,
    NormalizedSection,
    NormalizedSource,
    PdfSourceProcessor,
    SourceProcessorError,
    is_document_source,
    run_conversion,
    select_document_processor,
    source_converter_name,
)

# A minimal but valid PDF with a text layer. LiteParse extracts page text
# and page boundaries from this without rendering.
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n"
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
    b" /Contents 4 0 R"
    b" /Resources << /Font << /F1 5 0 R >> >> >>\n"
    b"endobj\n"
    b"4 0 obj\n<< /Length 55 >>\nstream\n"
    b"BT /F1 24 Tf 100 700 Td (Hello Lumio World) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \r\n"
    b"0000000009 00000 n \r\n"
    b"0000000056 00000 n \r\n"
    b"0000000103 00000 n \r\n"
    b"0000000192 00000 n \r\n"
    b"0000000280 00000 n \r\n"
    b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n346\n%%EOF\n"
)


def _slow_converter(content: bytes):
    """Module-level slow converter for timeout testing (must be picklable for spawn)."""
    import time

    time.sleep(5)
    return "should never get here"


def _make_docx(text: str = "Hello from DOCX") -> bytes:
    """Build a minimal but valid Office Open XML (DOCX) archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>"
                f"{text}"
                "</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )
        zf.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="text/xml"/>'
                "</Types>"
            ),
        )
    return buf.getvalue()


def _sync_runner(fn, *args, **kwargs):
    """Run a converter synchronously, bypassing the spawn-based runner."""
    return fn(*args)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_pdf_suffix_routes_to_liteparse(self):
        proc = select_document_processor("report.pdf", None)
        assert isinstance(proc, PdfSourceProcessor)

    def test_pdf_content_type_routes_to_liteparse(self):
        proc = select_document_processor(None, "application/pdf")
        assert isinstance(proc, PdfSourceProcessor)

    def test_image_suffixes_route_to_liteparse(self):
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff"):
            proc = select_document_processor(f"scan{ext}", None)
            assert isinstance(proc, PdfSourceProcessor), f"{ext} should route to LiteParse"

    def test_image_content_type_routes_to_liteparse(self):
        proc = select_document_processor(None, "image/png")
        assert isinstance(proc, PdfSourceProcessor)

    def test_docx_routes_to_anydoc(self):
        proc = select_document_processor("doc.docx", None)
        assert isinstance(proc, AnyDocSourceProcessor)

    def test_office_formats_route_to_anydoc(self):
        """The full office superset routes to AnyDoc (issue #154, ADR-0018)."""
        office_exts = (
            ".doc", ".docx", ".docm",
            ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
            ".xls", ".xlsx", ".xlsm", ".xlsb",
            ".odt", ".ods", ".odp",
            ".rtf", ".epub", ".csv",
        )
        for ext in office_exts:
            proc = select_document_processor(f"file{ext}", None)
            assert isinstance(proc, AnyDocSourceProcessor), (
                f"{ext} should route to AnyDoc"
            )

    def test_html_routes_to_markitdown(self):
        proc = select_document_processor("page.html", None)
        assert isinstance(proc, MarkItDownSourceProcessor)

    def test_broad_formats_not_covered_by_anydoc_route_to_markitdown(self):
        """HTML and the broad formats AnyDoc does not cover stay on MarkItDown."""
        for ext in (".html", ".htm", ".xml", ".json", ".rst"):
            proc = select_document_processor(f"file{ext}", None)
            assert isinstance(proc, MarkItDownSourceProcessor), (
                f"{ext} should route to MarkItDown"
            )

    def test_text_and_markdown_return_none(self):
        assert select_document_processor("note.md", None) is None
        assert select_document_processor("note.txt", None) is None
        assert select_document_processor(None, "text/markdown") is None
        assert select_document_processor(None, "text/plain") is None

    def test_unknown_suffix_returns_none(self):
        assert select_document_processor("file.xyz", None) is None

    def test_is_document_source_matches_routing(self):
        assert is_document_source("doc.pdf", None) is True
        assert is_document_source("doc.docx", None) is True
        assert is_document_source("image.png", None) is True
        assert is_document_source(None, "application/pdf") is True
        assert is_document_source(None, "image/jpeg") is True
        assert is_document_source("note.md", None) is False
        assert is_document_source("note.txt", None) is False
        assert is_document_source(None, "text/plain") is False

    def test_source_converter_name_matches_routing(self):
        """The converter NAME from routing alone mirrors the processor routing
        (issue #154): office → anydoc, PDF/images → liteparse, HTML → markitdown."""
        assert source_converter_name("doc.docx", None) == "anydoc"
        assert source_converter_name("sheet.xlsx", None) == "anydoc"
        assert source_converter_name("data.csv", None) == "anydoc"
        assert source_converter_name("book.epub", None) == "anydoc"
        assert source_converter_name("report.pdf", None) == "liteparse"
        assert source_converter_name("scan.png", None) == "liteparse"
        assert source_converter_name("page.html", None) == "markitdown"
        assert source_converter_name("data.json", None) == "markitdown"
        assert source_converter_name("note.md", None) == "markdown"
        assert source_converter_name("note.txt", None) == "text"


# ---------------------------------------------------------------------------
# PdfSourceProcessor
# ---------------------------------------------------------------------------


class TestPdfSourceProcessor:
    def test_processes_pdf_into_page_addressable_sections(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        result = proc.process("test.pdf", "application/pdf", _MINIMAL_PDF)

        assert isinstance(result, NormalizedSource)
        assert result.converted_by == "liteparse"
        assert "Hello Lumio World" in result.text
        assert len(result.sections) >= 1
        section = result.sections[0]
        assert section.page_number is not None
        assert section.id.endswith(f":page:{section.page_number}")
        assert section.title == f"Page {section.page_number}"

    def test_source_hash_is_sha256_of_raw_bytes(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        result = proc.process("test.pdf", "application/pdf", _MINIMAL_PDF)
        assert result.source_hash == hashlib.sha256(_MINIMAL_PDF).hexdigest()

    def test_filename_and_content_type_preserved(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        result = proc.process("report.pdf", "application/pdf", _MINIMAL_PDF)
        assert result.filename == "report.pdf"
        assert result.content_type == "application/pdf"

    def test_section_line_offsets_are_consistent(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        result = proc.process("test.pdf", "application/pdf", _MINIMAL_PDF)
        lines = result.text.splitlines()
        for section in result.sections:
            assert 1 <= section.line_start <= section.line_end <= max(len(lines), 1)
            segment = "\n".join(lines[section.line_start - 1 : section.line_end]).strip()
            assert segment == section.text.strip()

    def test_empty_content_raises(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="empty"):
            proc.process("empty.pdf", None, b"")

    def test_invalid_pdf_signature_raises_actionable_error(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="%PDF signature"):
            proc.process("fake.pdf", None, b"not a pdf at all")

    def test_converter_failure_wrapped_as_source_processor_error(self):
        def bad_converter(content):
            raise RuntimeError("boom")

        proc = PdfSourceProcessor(converter=bad_converter, runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="could not be converted"):
            proc.process("test.pdf", "application/pdf", _MINIMAL_PDF)

    def test_no_extractable_text_raises(self):
        """A converter that returns empty pages raises an actionable error."""

        class EmptyPage:
            page_num = 1
            text = ""
            markdown = ""

        class EmptyResult:
            pages = [EmptyPage()]

        proc = PdfSourceProcessor(
            converter=lambda content: EmptyResult(), runner=_sync_runner
        )
        with pytest.raises(SourceProcessorError, match="no extractable text"):
            proc.process("blank.pdf", "application/pdf", _MINIMAL_PDF)

    def test_oversized_source_raises(self):
        proc = PdfSourceProcessor(runner=_sync_runner)
        huge = b"%PDF-" + b"x" * (26 * 1024 * 1024)
        with pytest.raises(SourceProcessorError, match="size limit"):
            proc.process("huge.pdf", None, huge)


# ---------------------------------------------------------------------------
# MarkItDownSourceProcessor
# ---------------------------------------------------------------------------


class TestMarkItDownSourceProcessor:
    def test_processes_docx_into_heading_sections(self):
        docx = _make_docx("Hello from DOCX")
        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        result = proc.process("test.docx", None, docx)

        assert isinstance(result, NormalizedSource)
        assert result.converted_by == "markitdown"
        assert "Hello from DOCX" in result.text
        assert len(result.sections) >= 1
        # MarkItDown produces a single blob — sections are heading-based,
        # so page_number is None (never invented).
        for section in result.sections:
            assert section.page_number is None

    def test_source_hash_is_sha256_of_raw_bytes(self):
        docx = _make_docx()
        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        result = proc.process("test.docx", None, docx)
        assert result.source_hash == hashlib.sha256(docx).hexdigest()

    def test_empty_content_raises(self):
        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="empty"):
            proc.process("empty.docx", None, b"")

    def test_invalid_docx_archive_raises_actionable_error(self):
        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="Office Open XML"):
            proc.process("fake.docx", None, b"not a zip at all")

    def test_docx_missing_word_document_xml_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("foo.txt", "bar")
        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="word/document.xml"):
            proc.process("bad.docx", None, buf.getvalue())

    def test_converter_failure_wrapped_as_source_processor_error(self):
        def bad_converter(content, filename):
            raise RuntimeError("boom")

        proc = MarkItDownSourceProcessor(converter=bad_converter, runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="could not be converted"):
            proc.process("test.docx", None, _make_docx())

    def test_no_extractable_text_raises(self):
        def empty_converter(content, filename):
            return ""

        proc = MarkItDownSourceProcessor(converter=empty_converter, runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="no extractable text"):
            proc.process("blank.docx", None, _make_docx())


# ---------------------------------------------------------------------------
# AnyDocSourceProcessor (issue #154, ADR-0018)
# ---------------------------------------------------------------------------


class TestAnyDocSourceProcessor:
    def test_processes_office_doc_into_heading_sections(self):
        def fake_convert(content, filename):
            return "# Heading\n\nBody text.\n\n## Sub\n\nMore.\n"

        proc = AnyDocSourceProcessor(converter=fake_convert, runner=_sync_runner)
        result = proc.process("report.docx", None, b"PK-office-bytes")

        assert isinstance(result, NormalizedSource)
        assert result.converted_by == "anydoc"
        assert "Body text." in result.text
        assert len(result.sections) >= 1
        # AnyDoc produces a single blob — sections are heading-based, so
        # page_number is None (never invented).
        for section in result.sections:
            assert section.page_number is None

    def test_source_hash_is_sha256_of_raw_bytes(self):
        proc = AnyDocSourceProcessor(converter=lambda c, f: "# T\n\nbody\n", runner=_sync_runner)
        raw = b"office-bytes"
        result = proc.process("doc.xlsx", None, raw)
        assert result.source_hash == hashlib.sha256(raw).hexdigest()

    def test_filename_content_type_and_converted_by_preserved(self):
        proc = AnyDocSourceProcessor(converter=lambda c, f: "# T\n\nbody\n", runner=_sync_runner)
        result = proc.process("slides.pptx", "application/vnd.ms-powerpoint", b"bytes")
        assert result.filename == "slides.pptx"
        assert result.content_type == "application/vnd.ms-powerpoint"
        assert result.converted_by == "anydoc"

    def test_empty_content_raises(self):
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="empty"):
            proc.process("empty.docx", None, b"")

    def test_oversized_source_raises(self):
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        huge = b"x" * (26 * 1024 * 1024)
        with pytest.raises(SourceProcessorError, match="size limit"):
            proc.process("huge.pptx", None, huge)

    def test_failed_conversion_is_observable_never_silent(self):
        """A failed AnyDoc conversion surfaces an error; it is NEVER silently
        discarded and NEVER silently rerouted to another converter (issue #154
        fallback / ADR-0018)."""

        def bad_converter(content, filename):
            raise RuntimeError("ConvertError: resource limit exceeded")

        proc = AnyDocSourceProcessor(converter=bad_converter, runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="could not be converted"):
            proc.process("bomb.docx", None, b"bytes")

    def test_no_extractable_text_raises(self):
        def empty_converter(content, filename):
            return ""

        proc = AnyDocSourceProcessor(converter=empty_converter, runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="no extractable text"):
            proc.process("blank.odt", None, b"bytes")

    def test_caps_max_chars(self):
        long_text = "A" * 500_000
        proc = AnyDocSourceProcessor(
            converter=lambda content, fn: long_text,
            runner=_sync_runner,
            max_chars=1000,
        )
        result = proc.process("big.csv", None, b"bytes")
        assert len(result.text) <= 1000

    def test_missing_extra_raises_actionable_error(self, monkeypatch):
        """A base install requesting AnyDoc raises the exact install command."""
        real_import = __import__

        def block_anydoc(name, *args, **kwargs):
            if name == "anydoc":
                raise ImportError("simulated: anydoc not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", block_anydoc)
        import sys

        monkeypatch.delitem(sys.modules, "anydoc", raising=False)

        proc = AnyDocSourceProcessor(runner=_sync_runner)
        with pytest.raises(MissingDocumentExtraError) as exc_info:
            proc.process("doc.docx", None, b"bytes")
        assert "lumio-wiki[documents]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AnyDocSourceProcessor against the REAL AnyDoc converter (issue #154, AC5)
# ---------------------------------------------------------------------------


class TestAnyDocRealConverter:
    """Exercise the real AnyDoc adapter (no fake converter) for a supported
    office format, deterministic output, the explicit CSV format hint, and an
    observable malformed-input error. These run where the ``[documents]`` extra
    is installed (the workspace). The full hostile/mislabeled fixture matrix is
    validated by the #153 spike harness (``experiments/anydoc/``)."""

    def test_real_docx_converts_with_anydoc_provenance(self):
        docx = _make_docx("Real AnyDoc body")
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        result = proc.process("real.docx", None, docx)

        assert result.converted_by == "anydoc"
        assert "Real AnyDoc body" in result.text
        assert result.source_hash == hashlib.sha256(docx).hexdigest()
        # Office formats have no page boundaries: heading-based sections, no
        # invented page numbers.
        for section in result.sections:
            assert section.page_number is None

    def test_real_output_is_deterministic(self):
        docx = _make_docx("Determinism body")
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        first = proc.process("det.docx", None, docx)
        second = proc.process("det.docx", None, docx)
        assert first.text == second.text
        assert first.source_hash == second.source_hash

    def test_real_csv_uses_explicit_format_hint(self):
        # CSV has no magic-byte signature, so the adapter names the format.
        csv_bytes = b"name,score\nalpha,1\nbeta,2\n"
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        result = proc.process("data.csv", None, csv_bytes)
        assert result.converted_by == "anydoc"
        assert "alpha" in result.text

    def test_real_malformed_input_raises_observable_error(self):
        # Unrecognizable bytes -> AnyDoc ConvertError, surfaced as an observable
        # SourceProcessorError (never silently discarded or rerouted).
        proc = AnyDocSourceProcessor(runner=_sync_runner)
        with pytest.raises(SourceProcessorError, match="could not be converted"):
            proc.process("bad.docx", None, b"this is not a real office document at all")


# ---------------------------------------------------------------------------
# Bounded extraction
# ---------------------------------------------------------------------------


class TestBoundedExtraction:
    def test_pdf_caps_max_sections(self):
        """Beyond max_pages, extra pages are dropped but offsets stay valid."""

        class FakePage:
            def __init__(self, n):
                self.page_num = n
                self.text = f"Page {n} content"
                self.markdown = ""

        class FakeResult:
            pages = [FakePage(i) for i in range(1, 10)]

        proc = PdfSourceProcessor(
            converter=lambda content: FakeResult(),
            runner=_sync_runner,
            max_pages=3,
        )
        result = proc.process("multi.pdf", "application/pdf", _MINIMAL_PDF)
        assert len(result.sections) <= 3
        # Line offsets still valid against the bounded text.
        lines = result.text.splitlines()
        for section in result.sections:
            assert section.line_start >= 1
            assert section.line_end <= max(len(lines), 1)

    def test_markitdown_caps_max_chars(self):
        long_text = "A" * 500_000
        proc = MarkItDownSourceProcessor(
            converter=lambda content, fn: long_text,
            runner=_sync_runner,
            max_chars=1000,
        )
        result = proc.process("big.docx", None, _make_docx())
        assert len(result.text) <= 1000


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_raises_actionable_error(self):
        """A converter that exceeds the timeout is abandoned."""
        proc = PdfSourceProcessor(
            converter=_slow_converter, runner=run_conversion, timeout=0.5
        )
        with pytest.raises(SourceProcessorError, match="timed out"):
            proc.process("slow.pdf", "application/pdf", _MINIMAL_PDF)

# ---------------------------------------------------------------------------
# Missing-extra error (AC4)
# ---------------------------------------------------------------------------


class TestMissingExtraError:
    def test_missing_extra_error_is_source_processor_error_subclass(self):
        assert issubclass(MissingDocumentExtraError, SourceProcessorError)

    def test_documents_extra_hint_names_install_command(self):
        assert "lumio-wiki[documents]" in DOCUMENTS_EXTRA_HINT

    def test_pdf_processor_raises_missing_extra_when_liteparse_unavailable(self, monkeypatch):
        """When liteparse cannot be imported, the error is actionable."""

        real_import = __import__

        def block_liteparse(name, *args, **kwargs):
            if name == "liteparse":
                raise ImportError("simulated: liteparse not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", block_liteparse)
        # Purge any cached import so the lazy import path fires.
        import sys

        monkeypatch.delitem(sys.modules, "liteparse", raising=False)

        proc = PdfSourceProcessor(runner=_sync_runner)
        with pytest.raises(MissingDocumentExtraError) as exc_info:
            proc.process("test.pdf", "application/pdf", _MINIMAL_PDF)
        assert "lumio-wiki[documents]" in str(exc_info.value)

    def test_markitdown_processor_raises_missing_extra_when_markitdown_unavailable(
        self, monkeypatch
    ):
        real_import = __import__

        def block_markitdown(name, *args, **kwargs):
            if name == "markitdown":
                raise ImportError("simulated: markitdown not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", block_markitdown)
        import sys

        monkeypatch.delitem(sys.modules, "markitdown", raising=False)

        proc = MarkItDownSourceProcessor(runner=_sync_runner)
        with pytest.raises(MissingDocumentExtraError) as exc_info:
            proc.process("test.docx", None, _make_docx())
        assert "lumio-wiki[documents]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Full journey: document → distill → propose (AC3)
# ---------------------------------------------------------------------------


class TestDocumentJourney:
    def test_pdf_flows_through_same_pipeline_as_markdown(self, tmp_path: Path):
        """A PDF source produces a reviewable Ingest Proposal through the same
        Distiller + Proposal Pipeline as text/Markdown (issue #100, AC3)."""
        import shutil

        import lumio_wiki as lw

        # Use the repo's valid fixture for a KB root.
        root = Path(__file__).parents[3] / "tests" / "fixtures" / "valid"
        kb_root = tmp_path / "kb"
        shutil.copytree(root, kb_root)
        kb, _report = lw.load_knowledge_base(kb_root)

        from lumio_wiki.ingest import IngestStore

        ingest_dir = tmp_path / "ingest"
        store = IngestStore(ingest_dir)

        proposal = lw.create_proposal_without_provider(
            _MINIMAL_PDF,
            "application/pdf",
            "report.pdf",
            kb,
            store=store,
        )

        assert proposal.provenance.converted_by == "liteparse"
        assert proposal.status == "staged"
        assert len(proposal.proposed_pages) >= 1
        assert "Hello Lumio World" in proposal.proposed_pages[0].markdown

    def test_docx_flows_through_same_pipeline_as_markdown(self, tmp_path: Path):
        import shutil

        import lumio_wiki as lw

        root = Path(__file__).parents[3] / "tests" / "fixtures" / "valid"
        kb_root = tmp_path / "kb"
        shutil.copytree(root, kb_root)
        kb, _report = lw.load_knowledge_base(kb_root)

        proposal = lw.create_proposal_without_provider(
            _make_docx("Markdown content from DOCX"),
            None,
            "doc.docx",
            kb,
        )

        assert proposal.provenance.converted_by == "anydoc"
        assert len(proposal.proposed_pages) >= 1


# ---------------------------------------------------------------------------
# NormalizedSection page_number field
# ---------------------------------------------------------------------------


class TestNormalizedSectionPageNumber:
    def test_page_number_defaults_to_none(self):
        section = NormalizedSection(
            id="hash:section:1", title="Intro", line_start=1, line_end=5, text="..."
        )
        assert section.page_number is None

    def test_page_number_can_be_set(self):
        section = NormalizedSection(
            id="hash:page:3", title="Page 3", line_start=10, line_end=20, text="...", page_number=3
        )
        assert section.page_number == 3
