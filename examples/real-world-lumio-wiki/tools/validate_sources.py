"""Smoke-test the example corpus through installed lumio-wiki Source Processors."""

import mimetypes
import sys
from pathlib import Path

from lumio_wiki.source_processor import (  # type: ignore[import-not-found]
    AnyDocSourceProcessor,
    PdfSourceProcessor,
    TextMarkdownSourceProcessor,
    select_document_processor,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"

# Markers must remain stable because they anchor downstream evaluation questions.
EXPECTED_MARKERS = {
    "company-overview.md": ("Atlas Heatworks", "BrightHome Access Fund"),
    "customer-support-policy.txt": ("CarePlus", "45-day cure period"),
    "product-catalog.html": ("Aster 12", "Norrby Climate Systems"),
    "installation-handbook.docx": ("Commissioning gate", "Atlas-certified installer"),
    "2025-impact-report.pdf": ("428", "1,840"),
    "aster-pricing-deck.pptx": ("Aster 12", "CarePlus"),
    "aster-careplus-matrix.xlsx": ("Aster 12", "CarePlus"),
}

# Routing expectations match issue #154 / ADR-0018:
# office superset → AnyDoc; PDFs and images → LiteParse (pdf);
# HTML → MarkItDown; txt/md → text passthrough.
EXPECTED_PROCESSOR = {
    "company-overview.md": (TextMarkdownSourceProcessor, "markdown"),
    "customer-support-policy.txt": (TextMarkdownSourceProcessor, "text"),
    "product-catalog.html": (None, "markitdown"),
    "installation-handbook.docx": (AnyDocSourceProcessor, "anydoc"),
    "2025-impact-report.pdf": (PdfSourceProcessor, "liteparse"),
    "aster-pricing-deck.pptx": (AnyDocSourceProcessor, "anydoc"),
    "aster-careplus-matrix.xlsx": (AnyDocSourceProcessor, "anydoc"),
}


def emit(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def main() -> None:
    failures: list[str] = []
    actual_names = {path.name for path in SOURCES.iterdir() if path.is_file()}
    expected_names = set(EXPECTED_MARKERS)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        unexpected = sorted(actual_names - expected_names)
        raise SystemExit(f"source set mismatch: missing={missing}, unexpected={unexpected}")

    for path in sorted(SOURCES.iterdir()):
        content_type = mimetypes.guess_type(path.name)[0]
        processor = select_document_processor(path.name, content_type)
        if processor is None:
            processor = TextMarkdownSourceProcessor()

        expected_cls, expected_converter = EXPECTED_PROCESSOR[path.name]
        if expected_cls is not None and not isinstance(processor, expected_cls):
            failures.append(
                f"{path.name}: expected {expected_cls.__name__}, got {type(processor).__name__}"
            )
            continue

        try:
            normalized = processor.process(path.name, content_type, path.read_bytes())
        except Exception as exc:  # Surface converter diagnostics in this smoke tool.
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue

        if normalized.converted_by != expected_converter:
            failures.append(
                f"{path.name}: expected converted_by={expected_converter!r}, "
                f"got {normalized.converted_by!r}"
            )

        normalized_text = str(normalized.text)
        missing_markers = [
            marker for marker in EXPECTED_MARKERS[path.name] if marker not in normalized_text
        ]
        if missing_markers:
            failures.append(f"{path.name}: missing extracted markers {missing_markers}")

        marker_status = "ok" if not missing_markers else f"missing {missing_markers}"
        emit(
            f"{path.name}: {type(processor).__name__}, "
            f"converted_by={normalized.converted_by}, sections={len(normalized.sections)}, "
            f"chars={len(normalized_text)}, markers={marker_status}"
        )

    if failures:
        raise SystemExit("source validation failed:\n" + "\n".join(failures))
    emit("All source formats converted successfully.")


if __name__ == "__main__":
    main()
