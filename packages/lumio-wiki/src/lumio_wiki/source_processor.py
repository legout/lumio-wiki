"""Source Processor: convert Knowledge Source bytes into normalized text and
stable sections (issue #95, AC1; canonical owner moved into ``lumio-wiki`` by
issue #97; document processors added by issue #100).

A Source Processor is the first stage of ingestion. It turns raw Knowledge
Source bytes into a :class:`NormalizedSource` — normalized text plus stable,
line-addressable sections — without depending on a model provider or a web
request. Text and Markdown are handled by UTF-8 decoding and require no
heavyweight converter (AC4); document formats are handled by three layered
converters (ADR-0018): :class:`PdfSourceProcessor` (PDF and images → LiteParse,
which supplies real page boundaries and OCR for scanned pages),
:class:`AnyDocSourceProcessor` (office formats → AnyDoc, which dominates
MarkItDown on coverage, fidelity, and speed), and
:class:`MarkItDownSourceProcessor` (HTML and the remaining broad document
formats). Each lazily imports its converter only when the ``[documents]``
extra is installed. A base install that requests a document converter raises
:class:`MissingDocumentExtraError` with the exact install command.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import re
import zipfile
from collections.abc import Callable
from functools import partial
from multiprocessing import get_context
from multiprocessing.queues import Queue
from pathlib import PurePath
from queue import Empty
from typing import Any, Protocol, runtime_checkable

import msgspec

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


class NormalizedSection(msgspec.Struct, frozen=True):
    """A stable, line-addressable section derived from normalized source text.

    ``page_number`` carries the real, converter-supplied page number for
    page-addressable sources (e.g. a PDF page). It is ``None`` for sources
    whose converter does not supply page boundaries, in which case the
    ``title`` is a stable section label instead. Page numbers are never
    invented (issue #100, AC5).
    """

    id: str
    title: str
    line_start: int
    line_end: int
    text: str
    page_number: int | None = None


class NormalizedSource(msgspec.Struct, frozen=True):
    """Normalized material produced by a Source Processor.

    ``text`` is the full normalized source a Distiller consumes (the exact
    string the historical converter produced). ``sections`` are stable,
    heading-based, line-addressable slices available to Distillers that want
    finer-grained provenance. ``converted_by`` records which processor produced
    the material so it can flow into ``SourceProvenance`` unchanged.
    """

    text: str
    sections: list[NormalizedSection]
    converted_by: str
    source_hash: str
    filename: str | None
    content_type: str | None


class SourceProcessorError(ValueError):
    """A Knowledge Source could not be processed into normalized material."""


class MissingDocumentExtraError(SourceProcessorError):
    """A document converter was requested but the ``[documents]`` extra is absent.

    Raised when a PDF, image, DOCX, or other document source is processed
    without LiteParse or MarkItDown installed (issue #100, AC4/PRD #93 user
    story 18). The message names the exact install command so the failure is
    actionable rather than a raw ``ImportError``.
    """


@runtime_checkable
class SourceProcessor(Protocol):
    """Convert Knowledge Source bytes into normalized text and stable sections."""

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource: ...


def _sections(filename: str | None, text: str, digest: str) -> list[NormalizedSection]:
    """Split normalized text into stable, heading-based, line-addressable sections."""
    lines = text.splitlines()
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(2), len(match.group(1))))
    label = PurePath(filename or "source").name
    if not headings:
        end = max(len(lines), 1)
        return [NormalizedSection(f"{digest[:16]}:section:1", label, 1, end, text.strip())]
    result: list[NormalizedSection] = []
    for position, (line_start, title, level) in enumerate(headings):
        line_end = len(lines)
        for next_start, _next_title, next_level in headings[position + 1 :]:
            if next_level <= level:
                line_end = next_start - 1
                break
        section_text = "\n".join(lines[line_start - 1 : line_end]).strip()
        if section_text:
            result.append(
                NormalizedSection(
                    f"{digest[:16]}:section:{position + 1}",
                    title,
                    line_start,
                    line_end,
                    section_text,
                )
            )
    return result or [
        NormalizedSection(
            f"{digest[:16]}:section:1", label, 1, max(len(lines), 1), text.strip()
        )
    ]


def _is_markdown(filename: str | None, content_type: str | None) -> bool:
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in {".md", ".markdown"}:
        return True
    return (content_type or "").split(";")[0].strip().lower() == "text/markdown"

# ---------------------------------------------------------------------------
# Document conversion infrastructure (issue #100)
# ---------------------------------------------------------------------------

# Bounded extracted text and section/page counts so one pathological source
# cannot exhaust memory (issue #100, AC5). Whole pages or sections are dropped
# once the bound is reached, keeping line offsets consistent with stored text.
MAX_EXTRACTED_CHARS = 200_000
MAX_SECTIONS = 200
# Conversion (LiteParse/MarkItDown) is synchronous and may hang on malformed
# or hostile inputs. A single bad source is abandoned after this many seconds.
CONVERSION_TIMEOUT = 20.0
# Maximum accepted source size before conversion is attempted.
MAX_SOURCE_BYTES = 25 * 1024 * 1024
# Bounded total *uncompressed* DOCX/Office Open XML archive size. Only
# ``word/document.xml`` is read, and the aggregate uncompressed size is capped
# so a zip-bomb cannot exhaust conversion memory.
MAX_DOCX_ARCHIVE_BYTES = 2_000_000

DOCUMENTS_EXTRA_HINT = "pip install 'lumio-wiki[documents]'"

# Passive magic-byte signatures: nothing is opened, rendered, or executed.
_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Content-type / extension routing (ADR-0001, layered by ADR-0018). PDFs and
# images → LiteParse (real page boundaries and OCR for scanned pages); office
# formats → AnyDoc (broad, fast, dependency-free office conversion); HTML and
# the remaining broad document formats → MarkItDown.
_LITEPARSE_CONTENT_TYPES = frozenset({"application/pdf"})
_LITEPARSE_EXTENSIONS = frozenset({".pdf"})
_IMAGE_CONTENT_TYPE_PREFIX = "image/"
_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
)
# AnyDoc office-format superset (issue #154, ADR-0018): Word, PowerPoint, Excel,
# OpenDocument, RTF, EPUB, and CSV, including the legacy binary (.doc/.ppt/.xls)
# and macro-enabled/container variants. AnyDoc dominates MarkItDown on these.
_ANYDOC_EXTENSIONS = frozenset(
    {
        ".doc", ".docx", ".docm",
        ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
        ".xls", ".xlsx", ".xlsm", ".xlsb",
        ".odt", ".ods", ".odp",
        ".rtf", ".epub", ".csv",
    }
)
# MarkItDown retains HTML and the broad formats AnyDoc does not cover.
_MARKITDOWN_EXTENSIONS = frozenset({".html", ".htm", ".xml", ".json", ".rst"})
_DOCUMENT_EXTENSIONS = (
    _LITEPARSE_EXTENSIONS | _IMAGE_EXTENSIONS | _MARKITDOWN_EXTENSIONS | _ANYDOC_EXTENSIONS
)


def _require_documents_extra(module: str) -> None:
    """Raise an actionable error if a document converter dependency is missing.

    Checked lazily (inside the converter function) so the module imports
    cleanly without LiteParse or MarkItDown installed. The message names the
    exact install command (issue #100, AC4; PRD #93 user story 18).
    """
    try:
        __import__(module)
    except ImportError as exc:
        raise MissingDocumentExtraError(
            f"the {module!r} document converter is not installed; "
            f"install the documents extra:\n  {DOCUMENTS_EXTRA_HINT}"
        ) from exc


def _require_pdf_bytes(content: bytes) -> None:
    """Verify the bytes carry a real PDF signature (passive byte check)."""
    if not content.startswith(_PDF_SIGNATURE):
        raise SourceProcessorError(
            "the file does not appear to be a valid PDF (missing %PDF signature); "
            "check that the content type matches the uploaded file"
        )


def _is_pdf_source(filename: str | None, content_type: str | None) -> bool:
    """Return whether the source is a PDF (not an image also routed to LiteParse)."""
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in _LITEPARSE_EXTENSIONS:
        return True
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in _LITEPARSE_CONTENT_TYPES


def _require_docx_bytes(content: bytes) -> None:
    """Verify the bytes are an Office Open XML (DOCX) archive (passive check).

    MarkItDown accepts many container shapes, so the archive itself must be a
    ZIP that carries ``word/document.xml``. The check never executes embedded
    macros or active content.
    """
    if content[:4] not in _ZIP_SIGNATURES:
        raise SourceProcessorError(
            "the file does not appear to be a valid DOCX (missing Office Open "
            "XML archive signature); check that the content type matches the "
            "uploaded file"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if sum(info.file_size for info in archive.infolist()) > MAX_DOCX_ARCHIVE_BYTES:
                raise SourceProcessorError(
                    "the DOCX expands beyond the safe conversion limit"
                )
    except zipfile.BadZipFile as exc:
        raise SourceProcessorError(
            "the file is not a valid DOCX archive (corrupted or truncated ZIP)"
        ) from exc
    if "word/document.xml" not in names:
        raise SourceProcessorError(
            "the archive is not a Word document (missing word/document.xml); "
            "it may be another Office format or a renamed ZIP"
        )


def _bound_text(text: str, *, limit: int = MAX_EXTRACTED_CHARS) -> str:
    """Truncate extracted text at a line boundary to bound memory.

    Cutting at a newline keeps downstream section line offsets valid.
    """
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    cut = truncated.rfind("\n")
    if cut > limit // 2:
        truncated = truncated[:cut]
    return truncated


def _conversion_worker(
    output: Queue, fn: Callable[..., Any], args: tuple[Any, ...]
) -> None:
    """Run a converter in a spawned process; put (success, result) on the queue."""
    try:
        output.put((True, fn(*args)))
    except Exception as exc:  # noqa: BLE001 — surface any converter failure
        output.put((False, f"{type(exc).__name__}: {exc}"))


def run_conversion(
    fn: Callable[..., Any], *args: Any, timeout: float = CONVERSION_TIMEOUT
) -> Any:
    """Run a synchronous converter in a spawned process with a bounded timeout.

    LiteParse and MarkItDown are synchronous and may hang on malformed or
    hostile inputs. The call runs in a spawned process and is abandoned after
    ``timeout`` seconds so a single bad source cannot stall execution. Python
    cannot safely hard-cancel a thread mid C-extension, so a process is used;
    an abandoned process is terminated. The converter must be a module-level
    callable so the spawn context can re-import it.
    """
    context = get_context("spawn")
    output: Queue = context.Queue(maxsize=1)
    process = context.Process(target=_conversion_worker, args=(output, fn, args))
    process.start()
    try:
        # Wait for the RESULT on the queue, not for process exit. A large
        # result blocks the child's queue feeder thread during shutdown; if
        # we join first, parent and child deadlock on the pipe buffer.
        try:
            success, result = output.get(timeout=timeout)
        except Empty:
            if process.is_alive():
                process.terminate()
                process.join()
            raise SourceProcessorError(
                f"conversion timed out after {int(timeout)} seconds; the "
                "source may be corrupted or too complex to convert"
            ) from None
        # Reap the child process (it has already queued its result).
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join()
        if not success:
            raise SourceProcessorError(f"conversion failed: {result}")
        return result
    finally:
        output.close()
        output.join_thread()


def _default_liteparse_converter(content: bytes, *, max_pages: int) -> Any:
    """Lazy LiteParse converter: returns a ParseResult with ``.pages``.

    Each page exposes ``page_num``, ``text``, and ``markdown``. LiteParse
    supplies real page boundaries and OCR for scanned PDFs/images. Uses
    ``importlib`` rather than a top-level ``import`` so the module source
    contains no forbidden import statement (ADR-0010 base-install invariant).
    """
    _require_documents_extra("liteparse")
    liteparse = importlib.import_module("liteparse")
    return liteparse.LiteParse(max_pages=max_pages).parse(content)


def _default_markitdown_converter(content: bytes, filename: str | None) -> str:
    """Lazy MarkItDown converter: returns Markdown text for DOCX/HTML/broad docs.

    Uses ``importlib`` rather than a top-level ``import`` so the module source
    contains no forbidden import statement (ADR-0010 base-install invariant).
    """
    _require_documents_extra("markitdown")
    markitdown = importlib.import_module("markitdown")
    ext = PurePath(filename).suffix if filename else None
    result = markitdown.MarkItDown().convert(io.BytesIO(content), file_extension=ext)
    return result.text_content


def _default_anydoc_converter(content: bytes, filename: str | None) -> str:
    """Lazy AnyDoc converter: returns GitHub-Flavored Markdown for office docs.

    AnyDoc (``firecrawl-anydoc``, imported as ``anydoc``) auto-detects the format
    from content for signature-bearing office formats; the signature-less CSV
    format is named explicitly so a ``.csv`` source converts deterministically
    (issue #154, ADR-0018). Uses ``importlib`` rather than a top-level
    ``import`` so the module source contains no forbidden import statement
    (ADR-0010 base-install invariant). AnyDoc enforces its own resource limits
    (``max_entry_bytes`` / ``max_xml_depth`` / ``max_expansion``) inside the
    converter, so a hostile source is rejected before it can exhaust memory.
    """
    _require_documents_extra("anydoc")
    anydoc = importlib.import_module("anydoc")
    ext = PurePath(filename).suffix.lower() if filename else ""
    fmt = "csv" if ext == ".csv" else None
    return anydoc.to_markdown_bytes(content, fmt)


def _pages_to_sections(
    pages: list[Any], *, digest: str, max_chars: int, max_sections: int
) -> tuple[str, list[NormalizedSection]]:
    """Flatten converter pages into bounded text and page-addressable sections.

    Each non-empty page becomes one section carrying its real, converter-
    supplied page number (never renumbered). Section line offsets index into
    the returned text so citations stay consistent. Building stops once either
    ``max_sections`` or ``max_chars`` is reached.
    """
    lines: list[str] = []
    sections: list[NormalizedSection] = []
    total_chars = 0
    for page in pages:
        text = (
            getattr(page, "markdown", "") or getattr(page, "text", "") or ""
        ).strip()
        if not text:
            continue
        if len(sections) >= max_sections or total_chars >= max_chars:
            break
        remaining = max_chars - total_chars
        text = text[:remaining]
        page_lines = text.splitlines() or [""]
        line_start = len(lines) + 1
        lines.extend(page_lines)
        line_end = len(lines)
        total_chars += len(text)
        page_number = getattr(page, "page_num", 0) or None
        section_number = len(sections) + 1
        sections.append(
            NormalizedSection(
                id=(
                    f"{digest[:16]}:page:{page_number}"
                    if page_number is not None
                    else f"{digest[:16]}:section:{section_number}"
                ),
                title=(
                    f"Page {page_number}"
                    if page_number is not None
                    else f"Section {section_number}"
                ),
                line_start=line_start,
                line_end=line_end,
                text=text,
                page_number=page_number,
            )
        )
    return "\n".join(lines), sections


class TextMarkdownSourceProcessor:
    """Dependency-free processor for UTF-8 text and Markdown Knowledge Sources.

    Never imports LiteParse, MarkItDown, or any model provider, so a text or
    Markdown Knowledge Source can reach a reviewable Ingest Proposal without
    those installed (issue #95, AC4). ``converted_by`` is ``"markdown"`` for
    Markdown sources and ``"text"`` otherwise.
    """

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        if not content:
            raise SourceProcessorError("Knowledge Source is empty")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceProcessorError(
                "text/markdown Knowledge Source must be valid UTF-8"
            ) from exc
        if not text.strip():
            raise SourceProcessorError("Knowledge Source is empty")
        digest = hashlib.sha256(content).hexdigest()
        return NormalizedSource(
            text=text,
            sections=_sections(filename, text, digest),
            converted_by="markdown" if _is_markdown(filename, content_type) else "text",
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


class DocumentSourceProcessor:
    """Adapter over an existing byte-to-text converter (LiteParse / MarkItDown).

    The converter callable is injected so this module stays free of heavyweight
    imports; the caller (the full application's source-processor routing)
    supplies the lazily-imported converter. ``converted_by`` records the
    converter name so it flows into ``SourceProvenance`` unchanged.
    """

    def __init__(
        self, converted_by: str, convert: Callable[[bytes, str | None], str]
    ) -> None:
        self._converted_by = converted_by
        self._convert = convert  # type: ignore[assignment]

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        text = self._convert(content, filename)
        digest = hashlib.sha256(content).hexdigest()
        return NormalizedSource(
            text=text,
            sections=_sections(filename, text, digest),
            converted_by=self._converted_by,
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


class PdfSourceProcessor:
    """Convert PDF and image sources through LiteParse into page-addressable sections.

    Routed through LiteParse (ADR-0001), which supplies real page boundaries
    and OCR for scanned PDFs and images. The PDF is never opened or rendered,
    only text-extracted, so embedded JavaScript actions, launch annotations,
    and macros cannot execute. Pages are preserved verbatim; each section
    carries the exact, converter-supplied page number. ``converted_by`` is
    ``"liteparse"``.

    The converter and runner are injectable for testing. When ``converter`` is
    ``None``, the default LiteParse converter is used (which raises
    :class:`MissingDocumentExtraError` if the ``[documents]`` extra is absent).
    """

    def __init__(
        self,
        converter: Callable[[bytes], Any] | None = None,
        *,
        max_pages: int = MAX_SECTIONS,
        max_chars: int = MAX_EXTRACTED_CHARS,
        timeout: float = CONVERSION_TIMEOUT,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._converter = converter
        self._max_pages = max_pages
        self._max_chars = max_chars
        self._timeout = timeout
        self._runner = runner

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        if not content:
            raise SourceProcessorError("Knowledge Source is empty")
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceProcessorError(
                "Knowledge Source exceeds the 25 MiB size limit"
            )
        # Validate the PDF signature only for actual PDF sources. Images
        # (PNG, JPEG, etc.) are also routed here because LiteParse handles
        # OCR, but they do not carry a %PDF- header.
        if _is_pdf_source(filename, content_type):
            _require_pdf_bytes(content)
        runner = self._runner or run_conversion
        if self._converter is not None:
            converter = self._converter
        else:
            # Check the extra before spawning so the error type survives
            # (spawn serializes exceptions as strings). partial (not a
            # lambda) so the spawn-based runner can pickle it.
            _require_documents_extra("liteparse")
            converter = partial(_default_liteparse_converter, max_pages=self._max_pages)
        try:
            result = runner(converter, content, timeout=self._timeout)
        except (SourceProcessorError, MissingDocumentExtraError):
            raise
        except Exception as exc:
            raise SourceProcessorError(
                f"the source could not be converted: {exc}"
            ) from exc
        pages = list(getattr(result, "pages", []) or [])
        digest = hashlib.sha256(content).hexdigest()
        text, sections = _pages_to_sections(
            pages,
            digest=digest,
            max_chars=self._max_chars,
            max_sections=self._max_pages,
        )
        if not sections:
            raise SourceProcessorError(
                "the PDF contained no extractable text; it may be empty, "
                "scanned without a text layer, or password-protected"
            )
        return NormalizedSource(
            text=text,
            sections=sections,
            converted_by="liteparse",
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


class MarkItDownSourceProcessor:
    """Convert DOCX/HTML/broad document sources through MarkItDown into sections.

    Routed through MarkItDown (ADR-0001), which produces a single Markdown
    blob without page boundaries. Stable heading-based sections are derived
    from that Markdown, so citations identify the original filename plus a
    stable section (never an invented page number). The document is text-
    extracted only; embedded VBA macros and active content never execute.
    ``converted_by`` is ``"markitdown"``.

    The converter and runner are injectable for testing. When ``converter`` is
    ``None``, the default MarkItDown converter is used (which raises
    :class:`MissingDocumentExtraError` if the ``[documents]`` extra is absent).
    """

    def __init__(
        self,
        converter: Callable[[bytes, str | None], str] | None = None,
        *,
        max_chars: int = MAX_EXTRACTED_CHARS,
        timeout: float = CONVERSION_TIMEOUT,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._converter = converter
        self._max_chars = max_chars
        self._timeout = timeout
        self._runner = runner

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        if not content:
            raise SourceProcessorError("Knowledge Source is empty")
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceProcessorError(
                "Knowledge Source exceeds the 25 MiB size limit"
            )
        suffix = PurePath(filename or "").suffix.lower()
        if suffix == ".docx":
            _require_docx_bytes(content)
        runner = self._runner or run_conversion
        if self._converter is not None:
            converter = self._converter
        else:
            # Check the extra before spawning so the error type survives.
            _require_documents_extra("markitdown")
            converter = _default_markitdown_converter
        try:
            raw_text = runner(converter, content, filename, timeout=self._timeout)
        except (SourceProcessorError, MissingDocumentExtraError):
            raise
        except Exception as exc:
            raise SourceProcessorError(
                f"the document could not be converted: {exc}"
            ) from exc
        text = _bound_text((raw_text or "").strip(), limit=self._max_chars)
        if not text:
            raise SourceProcessorError(
                "the document contained no extractable text; it may be empty"
            )
        digest = hashlib.sha256(content).hexdigest()
        sections = _sections(filename, text, digest)[:MAX_SECTIONS]
        return NormalizedSource(
            text=text,
            sections=sections,
            converted_by="markitdown",
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


class AnyDocSourceProcessor:
    """Convert office document sources through AnyDoc into heading-based sections.

    Routed through AnyDoc (``firecrawl-anydoc``; ADR-0018) for the office
    superset (Word/PowerPoint/Excel/OpenDocument/RTF/EPUB/CSV and their
    container variants). AnyDoc produces a single Markdown blob without page
    boundaries, so stable heading-based sections are derived from that Markdown
    — exactly like :class:`MarkItDownSourceProcessor` — and citations identify
    the original filename plus a stable section (never an invented page
    number). AnyDoc enforces resource limits inside the converter, so no
    external zip-bomb guard is needed. ``converted_by`` is ``"anydoc"``.

    Fallback is explicit and observable (issue #154): a failed conversion is
    NEVER silently discarded and NEVER silently rerouted to another converter
    (which would break deterministic ``converted_by`` provenance and require the
    general extractor-provider abstraction this issue forbids). A failure raises
    an observable :class:`SourceProcessorError` naming the cause; a missing
    ``[documents]`` extra raises :class:`MissingDocumentExtraError`.

    The converter and runner are injectable for testing. When ``converter`` is
    ``None``, the default AnyDoc converter is used.
    """

    def __init__(
        self,
        converter: Callable[[bytes, str | None], str] | None = None,
        *,
        max_chars: int = MAX_EXTRACTED_CHARS,
        timeout: float = CONVERSION_TIMEOUT,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self._converter = converter
        self._max_chars = max_chars
        self._timeout = timeout
        self._runner = runner

    def process(
        self, filename: str | None, content_type: str | None, content: bytes
    ) -> NormalizedSource:
        if not content:
            raise SourceProcessorError("Knowledge Source is empty")
        if len(content) > MAX_SOURCE_BYTES:
            raise SourceProcessorError(
                "Knowledge Source exceeds the 25 MiB size limit"
            )
        runner = self._runner or run_conversion
        if self._converter is not None:
            converter = self._converter
        else:
            # Check the extra before spawning so the error type survives
            # (spawn serializes exceptions as strings).
            _require_documents_extra("anydoc")
            converter = _default_anydoc_converter
        try:
            raw_text = runner(converter, content, filename, timeout=self._timeout)
        except (SourceProcessorError, MissingDocumentExtraError):
            raise
        except Exception as exc:
            raise SourceProcessorError(
                f"the document could not be converted: {exc}"
            ) from exc
        text = _bound_text((raw_text or "").strip(), limit=self._max_chars)
        if not text:
            raise SourceProcessorError(
                "the document contained no extractable text; it may be empty"
            )
        digest = hashlib.sha256(content).hexdigest()
        sections = _sections(filename, text, digest)[:MAX_SECTIONS]
        return NormalizedSource(
            text=text,
            sections=sections,
            converted_by="anydoc",
            source_hash=digest,
            filename=filename,
            content_type=content_type,
        )


def is_document_source(filename: str | None, content_type: str | None) -> bool:
    """Return whether a source requires a document converter (not text/markdown)."""
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in _DOCUMENT_EXTENSIONS:
        return True
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _LITEPARSE_CONTENT_TYPES or ct.startswith(_IMAGE_CONTENT_TYPE_PREFIX):
        return True
    return False


def select_document_processor(
    filename: str | None, content_type: str | None
) -> SourceProcessor | None:
    """Route a document source to LiteParse, AnyDoc, or MarkItDown.

    Returns ``None`` for text and Markdown sources (handled by
    :class:`TextMarkdownSourceProcessor`). For document sources, returns a
    :class:`PdfSourceProcessor` (PDF and images → LiteParse, which supplies real
    page boundaries and OCR), an :class:`AnyDocSourceProcessor` (office formats
    → AnyDoc), or a :class:`MarkItDownSourceProcessor` (HTML and the remaining
    broad document formats → MarkItDown). The routing layers AnyDoc on top of
    the historical converter routing (ADR-0001) per ADR-0018 so provenance is
    deterministic.
    """
    suffix = PurePath(filename or "").suffix.lower()
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _LITEPARSE_CONTENT_TYPES or ct.startswith(_IMAGE_CONTENT_TYPE_PREFIX):
        return PdfSourceProcessor()
    if suffix in _LITEPARSE_EXTENSIONS or suffix in _IMAGE_EXTENSIONS:
        return PdfSourceProcessor()
    if suffix in _ANYDOC_EXTENSIONS:
        return AnyDocSourceProcessor()
    if suffix in _MARKITDOWN_EXTENSIONS:
        return MarkItDownSourceProcessor()
    return None


def source_converter_name(filename: str | None, content_type: str | None) -> str:
    """Return the converter NAME a source routes to, WITHOUT invoking it.

    Managed host-Distiller ingest (issue #149) records the converter in private
    proposal provenance from routing alone: the host coding agent already
    converted the original Knowledge Source and authored the Compiled Page, so
    the ``[documents]`` extra is never required and no converter runs. The
    values mirror :func:`select_document_processor` +
    :class:`TextMarkdownSourceProcessor` routing so provenance is deterministic
    and identical to the ordinary ingest path (``"markdown"``/``"text"`` for
    text/Markdown, ``"liteparse"`` for PDF and images, ``"anydoc"`` for office
    formats, ``"markitdown"`` for HTML and remaining broad document formats).
    """
    document_processor = select_document_processor(filename, content_type)
    if isinstance(document_processor, PdfSourceProcessor):
        return "liteparse"
    if isinstance(document_processor, AnyDocSourceProcessor):
        return "anydoc"
    if isinstance(document_processor, MarkItDownSourceProcessor):
        return "markitdown"
    return "markdown" if _is_markdown(filename, content_type) else "text"


__all__ = [
    "CONVERSION_TIMEOUT",
    "DOCUMENTS_EXTRA_HINT",
    "AnyDocSourceProcessor",
    "DocumentSourceProcessor",
    "MarkItDownSourceProcessor",
    "MAX_EXTRACTED_CHARS",
    "MAX_SECTIONS",
    "MAX_SOURCE_BYTES",
    "MissingDocumentExtraError",
    "NormalizedSection",
    "NormalizedSource",
    "PdfSourceProcessor",
    "SourceProcessor",
    "SourceProcessorError",
    "TextMarkdownSourceProcessor",
    "is_document_source",
    "run_conversion",
    "select_document_processor",
    "source_converter_name",
]
