---
status: accepted
related: ADR-0001
---

# ADR-0018: Layered Document Conversion — AnyDoc for Office Formats

## Context

Document conversion in Lumio lives behind the `SourceProcessor` seam
(ADR-0001): raw Knowledge Source bytes are turned into a `NormalizedSource`
by a converter, and the converter name flows into `SourceProvenance` as
`converted_by`. Before this decision the layered routing was:

- **PDF and images** → LiteParse (real page boundaries + OCR for scanned pages);
- **everything else** (DOCX, HTML, office and broad document formats) →
  MarkItDown.

Issue #153 evaluated [Firecrawl AnyDoc](https://github.com/firecrawl/anydoc)
(`firecrawl-anydoc`, imported as `anydoc`) as a local, model-free converter
and produced [`docs/research/anydoc-fit.md`](../research/anydoc-fit.md). The
spike's measured findings, not upstream marketing, drive this decision:

- AnyDoc **strictly dominates MarkItDown on office formats** — broader coverage
  (it converts the 11 office containers Lumio's `markitdown[docx]` extra cannot
  read: `.doc`/`.ppt`/`.xls` legacy binaries, `.pptx`/`.pptm`/`.ppsx`, `.xlsx`/
  `.xlsb`, `.odt`/`.ods`/`.odp`, `.rtf`, `.epub`, `.csv`), higher fidelity
  (especially RTF, tables, footnotes), and far higher speed (~15–450× faster;
  warm median 0.75 ms vs MarkItDown's 70 ms).
- It is **~13× smaller** (7.1 MiB, zero Python dependencies) than
  `markitdown[docx]` (~94 MiB, pulls in `onnxruntime` + `numpy` + `magika`).
- It is **deterministic** (byte-identical output across repeated runs — a
  prerequisite for stable Source fingerprints).
- It is **fully local** (the spike's `strace` audit recorded zero outbound
  network syscalls).
- It enforces **built-in resource limits** (`max_entry_bytes`, `max_xml_depth`,
  `max_expansion`) inside the converter, so hostile inputs are rejected before
  they can exhaust memory — the same class of defence Lumio hand-rolls around
  MarkItDown.

But the spike also found three hard gaps that block wholesale replacement:

1. **No scanned-PDF / image OCR.** AnyDoc's PDF path is text-extraction only;
   it cannot read image-only pages. LiteParse supplies OCR and the
   `PdfSourceProcessor` depends on it.
2. **No HTML.** AnyDoc's format detection returns `None` for HTML. Lumio routes
   HTML through MarkItDown.
3. **No PDF page boundaries.** AnyDoc's PDF output is a single Markdown blob
   with no per-page numbering, losing the page-addressable citations LiteParse
   provides today.

The #153 verdict was **defer wholesale replacement** and pursue a layered
office-format route. Issue #154 is that follow-up: a reviewed, layered routing
decision plus implementation, explicitly *not* a blind `[documents]`-extra swap.
Its preconditions required accepting AnyDoc's `0.1.x` maturity, deciding the
text-PDF page-boundary trade-off, and recording the decision here before
production wiring.

## Decision

Adopt a **three-way layered document-conversion routing** (issue #154), keeping
the base `lumio-wiki` package free of any AnyDoc dependency:

- **Office formats → AnyDoc** (`firecrawl-anydoc==0.1.3`). A new
  `AnyDocSourceProcessor` (adapter behind the existing `SourceProcessor` seam)
  handles the office superset:
  `.doc`/`.docx`/`.docm`, `.ppt`/`.pps`/`.pot`/`.pptx`/`.pptm`/`.ppsx`/`.ppsm`,
  `.xls`/`.xlsx`/`.xlsm`/`.xlsb`, `.odt`/`.ods`/`.odp`, `.rtf`, `.epub`, `.csv`.
  `converted_by` is `"anydoc"`. AnyDoc auto-detects format from content; the
  signature-less CSV format is named explicitly. Stable heading-based sections
  are derived from AnyDoc's Markdown output exactly as for MarkItDown (office
  formats have no page boundaries, so no page-number citations are invented).
- **PDF and images → LiteParse** (unchanged). Scanned-PDF OCR, image OCR, and
  text-PDF **all stay on LiteParse**, preserving real page boundaries.
- **HTML → MarkItDown** (unchanged), together with the broad formats AnyDoc
  does not cover (`.xml`, `.json`, `.rst`).

### Text-PDF routing is deferred, not changed

Text PDFs remain on LiteParse. Moving text PDFs to AnyDoc would trade away
page-addressable citations (a real regression LiteParse supports today) for
speed and structure. Per issue #154 — "Do not change this route until that
trade-off is accepted and tested" — that trade-off is **not** accepted here.
A future, separately reviewed decision may adopt a content-sensitive split
(text-PDF → AnyDoc, scanned-PDF → LiteParse) once the citation regression is
accepted and covered by tests.

### Fallback is explicit and observable

A failed AnyDoc conversion is **never silently discarded** and **never
silently rerouted** to another converter. Silent rerouting would break
deterministic `converted_by` provenance (the same source could record
`"anydoc"` one run and `"markitdown"` the next) and would require the general
extractor-provider abstraction that issue #154 explicitly forbids. Instead,
failure raises an observable `SourceProcessorError` naming the cause, and a
missing converter raises `MissingDocumentExtraError` naming the exact install
command. This is observable and covered by tests. Because LiteParse and
MarkItDown are **retained** (not removed), rollback is a routing-edit away and
needs no new dependency.

### Provenance and boundary invariants are preserved by construction

`NormalizedSource` is text-only and the `source_hash` is computed from the raw
bytes, so AnyDoc never touches Source identity. `converted_by="anydoc"` flows
into `SourceProvenance` exactly like `"liteparse"`/`"markitdown"`. Using
`anydoc.to_markdown_bytes` keeps embedded image/asset bytes out of the
Markdown, so converter internals and local paths never leak into Compiled
Pages or Reader retrieval. Distiller, RetrievalAdapter, Knowledge Source
Registry, proposal, visibility, lifecycle, and citation semantics are
unchanged.

## Consequences

- The `[documents]` extra installs `firecrawl-anydoc==0.1.3` alongside
  `liteparse` and `markitdown`; the base `lumio-wiki` package has no AnyDoc
  import or dependency (the converter is imported lazily, ADR-0010).
- Office-format coverage expands from "DOCX-only that installs" to the full
  office superset, with higher fidelity and ~two orders of magnitude speed,
  without changing public Compiled Page or Reader contracts.
- AnyDoc is pinned to `0.1.3` (the exact version the #153 spike measured). The
  `0.1.x` maturity risk is accepted for an **additive** layer only: LiteParse
  and MarkItDown remain installed, so an AnyDoc regression is contained and
  rollback does not strand a format.
- The `0.1.x` single-vendor risk is revisited (per #153) if the maintainer adds
  OCR/HTML, a second converter appears, or the seam needs to stop being a
  single-vendor bet.
- Text-PDF citations keep real page numbers; no page-boundary regression is
  shipped.

## Rejected alternatives

- **Wholesale `[documents]` swap (AnyDoc everywhere).** Rejected by #153: it
  would silently break scanned-PDF/image OCR and HTML ingestion, and lose
  text-PDF page boundaries.
- **Silent fallback to MarkItDown on AnyDoc failure.** Rejected: it breaks
  deterministic `converted_by` provenance and would require the general
  extractor-provider abstraction issue #154 forbids.
- **General extractor-provider abstraction for one adapter.** Rejected per the
  #154 guardrail: AnyDoc slots into the existing `SourceProcessor` seam; no new
  abstraction is added for a single adapter.
- **Removing LiteParse or MarkItDown now.** Rejected: replacement coverage is
  not proven (no OCR, no HTML) and removal needs separate approval.
