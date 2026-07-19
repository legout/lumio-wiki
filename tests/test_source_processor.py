"""Tests for the Source Processor interface (issue #95, AC1)."""

import hashlib

from lumio.source_processor import (
    DocumentSourceProcessor,
    NormalizedSource,
    SourceProcessor,
    SourceProcessorError,
    TextMarkdownSourceProcessor,
)


def test_text_markdown_processor_decodes_utf8_with_heading_sections():
    body = "# Title One\n\nIntro paragraph.\n\n## Section Two\n\nMore text.\n"
    raw = body.encode("utf-8")
    result = TextMarkdownSourceProcessor().process("note.md", "text/markdown", raw)

    assert isinstance(result, NormalizedSource)
    assert result.text == body
    assert result.converted_by == "markdown"
    assert result.source_hash == hashlib.sha256(raw).hexdigest()
    assert result.filename == "note.md"
    assert result.content_type == "text/markdown"
    # Two heading-based sections, line-addressable.
    assert [s.title for s in result.sections] == ["Title One", "Section Two"]
    assert result.sections[0].line_start == 1
    assert result.sections[1].line_start == 5


def test_text_processor_reports_text_converted_by_for_plain_text():
    result = TextMarkdownSourceProcessor().process(
        "readme.txt", "text/plain", b"just a flat body\n"
    )
    assert result.converted_by == "text"
    # No headings -> a single stable section spanning the whole source.
    assert len(result.sections) == 1
    assert result.sections[0].title == "readme.txt"


def test_text_markdown_processor_rejects_empty_and_non_utf8():
    import pytest

    with pytest.raises(SourceProcessorError):
        TextMarkdownSourceProcessor().process("empty.md", "text/markdown", b"")
    with pytest.raises(SourceProcessorError):
        TextMarkdownSourceProcessor().process("blank.txt", "text/plain", b"   \n")
    with pytest.raises(SourceProcessorError):
        TextMarkdownSourceProcessor().process("bad.md", "text/markdown", b"\xff\xfe\x00")


def test_document_processor_wraps_an_injected_converter_and_records_provenance():
    calls = []

    def fake_convert(raw_bytes: bytes, filename):
        calls.append((filename, raw_bytes[:3]))
        return "Converted text body"

    processor = DocumentSourceProcessor("markitdown", fake_convert)
    result = processor.process("doc.docx", "application/vnd.openxmlformats", b"ABCD")

    assert calls == [("doc.docx", b"ABC")]
    assert result.text == "Converted text body"
    assert result.converted_by == "markitdown"
    assert result.source_hash == hashlib.sha256(b"ABCD").hexdigest()
    assert len(result.sections) == 1  # no headings -> one section


def test_source_processor_is_a_runtime_protocol():
    # Any object with a compatible process() method satisfies the Protocol.
    assert isinstance(TextMarkdownSourceProcessor(), SourceProcessor)
    assert isinstance(DocumentSourceProcessor("x", lambda b, f: ""), SourceProcessor)
