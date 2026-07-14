"""In-memory PDF and DOCX byte generators for Conversation Source tests (#35).

These build minimal but valid documents that the real Lumio conversion
adapters (LiteParse for PDF, MarkItDown for DOCX) actually parse, so HTTP
tests exercise the true pipeline end-to-end without committing binary
fixtures. Page numbers are preserved by LiteParse; DOCX has no page layer.
"""

from __future__ import annotations

import io
import zipfile

__all__ = ["make_pdf", "make_docx", "DOCX_CONTENT_TYPE"]


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_pdf(pages_text: list[str]) -> bytes:
    """Build a minimal valid multi-page PDF, one text line per page.

    Cross-reference offsets are scanned from the assembled bytes so LiteParse's
    native parser resolves every page. Each page carries its real 1-indexed
    page number.
    """
    objects: dict[int, bytes] = {}
    page_count = len(pages_text)
    font_obj = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(page_count))
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()
    for i, txt in enumerate(pages_text):
        page_obj = 3 + 2 * i
        content_obj = 4 + 2 * i
        objects[page_obj] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_obj} 0 R /Resources << /Font << /F1 {font_obj} 0 R >> >> >>"
        ).encode()
        stream = f"BT /F1 12 Tf 72 700 Td ({txt}) Tj ET".encode()
        objects[content_obj] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    objects[font_obj] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    body = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(body)
        body += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"

    max_obj = max(objects)
    xref_pos = len(body)
    xref = bytearray(f"xref\n0 {max_obj + 1}\n".encode())
    xref += b"0000000000 65535 f \r\n"
    for num in range(1, max_obj + 1):
        xref += f"{offsets[num]:010d} 00000 n \r\n".encode()
    trailer = (
        f"trailer\n<< /Size {max_obj + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(body) + bytes(xref) + trailer


def make_docx(heading: str, body: str) -> bytes:
    """Build a minimal valid DOCX (Office Open XML) with one heading + body."""
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument'
        '.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    document = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:r><w:t>{heading}</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t>{body}</w:t></w:r></w:p>'
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()
