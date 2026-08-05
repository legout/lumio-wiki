# AnyDoc fit for Lumio

## Tracking

- **Issue:** [#153 — Spike: evaluate AnyDoc as a local document-to-Markdown backend](https://github.com/legout/lumio/issues/153).
- **Status:** spike complete; no production dependency or routing change has been made.
- **Verdict:** **defer.** AnyDoc is a high-quality, fast, dependency-free converter that
  strictly dominates MarkItDown on office formats and would widen Lumio's format coverage
  while shrinking the install. Two production capabilities Lumio already has are missing,
  and the project is at `0.1.x` with no maturity track record, so full adoption of the
  `[documents]` extra is deferred to a separate, reviewed routing decision (see
  [Recommendation](#recommendation)).
- **Spike artifacts:** [`experiments/anydoc/`](../../experiments/anydoc/) — reproducible
  evaluation script, fixture matrix, and committed `results.json` (which also
  records the no-network audit). No `lumio-wiki` or production dependency was touched.

Primary sources:

- [firecrawl/anydoc](https://github.com/firecrawl/anydoc) (Rust crate)
- [python/README.md](https://github.com/firecrawl/anydoc/blob/main/python/README.md) (`firecrawl-anydoc`, imported as `anydoc`)
- [bench/README.md](https://github.com/firecrawl/anydoc/blob/main/bench/README.md) (upstream benchmark; corpus not redistributable)
- [`firecrawl-anydoc` on PyPI](https://pypi.org/project/firecrawl-anydoc/)

## Verdict

AnyDoc is a much better office-document converter than MarkItDown and a much faster
text-PDF extractor than LiteParse, with a smaller footprint, zero Python dependencies,
and built-in resource limits that mirror Lumio's own defensive posture. It should not,
on its own, become the sole `[documents]` backend, because it cannot replace two
capabilities the current route already provides:

1. **Scanned-PDF OCR.** AnyDoc's PDF path (`pdf-inspector`) is text-extraction only; it
   cannot read image-only / scanned pages. LiteParse supplies OCR today, and Lumio's
   `PdfSourceProcessor` depends on it.
2. **HTML.** AnyDoc does not accept HTML at all (format detection returns `None`). Lumio
   routes HTML through MarkItDown, and ships an HTML fixture (`product-catalog.html`).

The practical recommendation is:

1. **defer** replacing the `[documents]` extra wholesale;
2. treat AnyDoc as the strongest candidate for a **layered** office-format route
   (`doc/docx/ppt/pptx/xls/xlsx/odt/ods/odp/rtf/epub/csv`), keeping LiteParse for
   scanned-PDF OCR and MarkItDown (or another converter) for HTML;
3. revisit as `adopt` once the maintainer ships OCR or once Lumio makes an explicit
   layered-routing decision (see [Recommendation](#recommendation)).

The evidence below supports each point with numbers measured in this spike, not the
upstream benchmark.

## What AnyDoc is

AnyDoc is an MIT-licensed Rust converter with Python bindings distributed as
`firecrawl-anydoc` (imported as `anydoc`). It converts Word, PowerPoint, Excel,
OpenDocument, RTF, EPUB, CSV, and text-based PDF to GitHub-Flavored Markdown through a
single shared document model and one Markdown serializer. The Python API is small:

```python
import anydoc
markdown = anydoc.to_markdown_bytes(data)          # bytes -> Markdown (format auto-detected)
markdown = anydoc.to_markdown_bytes(data, "csv")   # signature-less formats named explicitly
document = anydoc.to_document(data)                # document model + embedded assets (not PDF)
```

`to_markdown_bytes` returns a single Markdown string; `to_document` additionally exposes
the structured block/inline/table/note model and embedded `assets` (each with a media
type, origin part, and raw bytes). PDF has no document-model form — it goes straight to
Markdown. The package releases the GIL during conversion and ships type stubs
(`py.typed`).

## Spike methodology

The spike ran AnyDoc **0.1.3** against a 37-file fixture matrix under
[`experiments/anydoc/fixtures/`](../../experiments/anydoc/fixtures/):

- **Real-world Lumio sources** (DOCX/PDF/HTML) from
  [`examples/real-world-lumio-wiki/sources/`](../../examples/real-world-lumio-wiki/sources/),
  for the direct DOCX/PDF comparison with the current route.
- A **representative, redistributable subset of AnyDoc's own MIT-licensed `tests/fixtures/`**
  corpus — one or more files per claimed format (including legacy `.doc`, `.ppt`, `.xls`),
  plus the full `abuse/` set (zipbomb, imagebomb, hugerepeat, hugespan, deepxml) and
  `malformed/` set (encrypted ODT, truncated, empty, corrupt-styles, mismatched,
  unbalanced) for the failure-mode audit. The fixture filenames carry an `--errors` /
  `--skips` / `--recovers` suffix that documents each file's expected behaviour.
- Two **generated fixtures** for behaviour the upstream corpus does not cover: an
  **encrypted PDF** (`encrypted--errors.pdf`, built with `pikepdf`, user/owner password
  `lumio`) and a **mislabeled file** (`mislabeled-docx-as-pdf.pdf` — a DOCX whose bytes
  are intact but whose extension says `.pdf`) to test content-based format detection.

For every fixture the harness converts with AnyDoc and (for DOCX/HTML via MarkItDown and
PDF via LiteParse — Lumio's actual `select_document_processor` routing from
`lumio_wiki.source_processor`) with the current route, recording:

- **cold** latency (first call) and **warm** latency (median of 3 subsequent calls),
  matching the upstream "one warm conversion" convention;
- peak Python-traced memory for the cold call;
- deterministic structural counts over the rendered Markdown (headings, table rows, list
  items, links, images, footnote refs, chars);
- a two-run **determinism** check (byte-identical output);
- embedded-**asset** exposure via `to_document`;
- per-call **error type and message** for hostile/malformed inputs.

Each converter call is wrapped in a 30 s SIGALRM timeout (mirroring Lumio's 20 s
`run_conversion` spawn timeout), and the baseline is skipped for the hostile set because
Lumio already refuses those before conversion (`_require_docx_bytes`,
`MAX_DOCX_ARCHIVE_BYTES`) and MarkItDown/LiteParse hang on a zipbomb. Environment:
**Python 3.14.0, Linux aarch64, glibc 2.39, `warm_runs=3`**. Full numbers are in
[`experiments/anydoc/results.json`](../../experiments/anydoc/results.json).

## Evidence

### 1. Install, footprint, and Python-3.14 / platform compatibility

`firecrawl-anydoc==0.1.3` installs in an isolated Python 3.14 environment with **zero
Python dependencies** (resolved: 1 package) and an on-disk footprint of **7.1 MiB** (a
single `cp310-abi3` native extension). The `abi3` tag means the same wheel serves
CPython 3.10 through 3.14+. PyPI carries wheels for every Lumio-relevant platform:

| wheel | platform |
| --- | --- |
| `cp310-abi3-manylinux_2_17_x86_64` | Linux x86_64 (glibc) |
| `cp310-abi3-manylinux_2_17_aarch64` | Linux aarch64 (glibc) — *this spike* |
| `cp310-abi3-musllinux_1_2_x86_64` / `_aarch64` | Linux (musl / Alpine) |
| `cp310-abi3-macosx_10_12_x86_64` | macOS Intel |
| `cp310-abi3-macosx_11_0_arm64` | macOS Apple Silicon |
| `cp310-abi3-win_amd64` | Windows x86_64 |
| `firecrawl-anydoc-0.1.3.tar.gz` | sdist (source build fallback) |

The install weight is the headline contrast with the current route. All three converters
installed cleanly on Python 3.14 / aarch64, but their footprints differ by an order of
magnitude:

| converter | runtime deps | installed size | notable baggage |
| --- | --- | --- | --- |
| **anydoc** | **0** | **7.1 MiB** | none |
| liteparse | 0 | 31 MiB | bundled OCR models |
| markitdown[docx] | ~16 | **~94 MiB** | numpy 27 MiB, **onnxruntime 46 MiB** (ML runtime), lxml 12 MiB, magika |

MarkItDown pulls in an ML inference runtime (`onnxruntime`) plus `numpy` and `magika`
(an ML file-type detector). AnyDoc and LiteParse are dependency-free native wheels.
**Lumio's current `[documents]` extra is `markitdown[docx]`** — the `[docx]` extra alone
covers only DOCX; the evaluation confirms PPTX and XLSX raise
`MissingDependencyException` (they need `[pptx]`/`[xlsx]`/`[all]`), and ODT is
unsupported outright by MarkItDown (see [Format coverage](#4-format-coverage)).

### 2. Performance

Warm-median conversion latency on this environment (Python 3.14, aarch64, `warm_runs=3`):

| converter | fixtures converted | median warm | max warm |
| --- | --- | --- | --- |
| **anydoc** | 24 (all office + text PDF) | **0.75 ms** | 23.8 ms |
| markitdown | 10 (docx/rtf/csv it supports) | 70.0 ms | 383.3 ms |
| liteparse | 2 (PDF) | 2 636.6 ms | 3 457.7 ms |

Representative direct comparisons (warm, ms):

| fixture | anydoc | current route | speedup |
| --- | --- | --- | --- |
| `real-world/installation-handbook.docx` | 23.8 ms | markitdown 383.3 ms | ~16× |
| `text.docx` | 3.9 ms | markitdown 89.4 ms | ~23× |
| `real-world/2025-impact-report.pdf` (text) | 2.0 ms | liteparse 3 457.7 ms | ~1 700× |
| `text.pdf` (text) | 6.0 ms | liteparse 1 815.6 ms | ~300× |
| `handmade-merge.rtf` | 0.18 ms | markitdown 62.4 ms | ~345× |

LiteParse's PDF latency is dominated by OCR, which it runs even when the page has a text
layer. AnyDoc is two to three orders of magnitude faster on text-based PDF and about two
orders of magnitude faster than MarkItDown on DOCX (markitdown median / anydoc median ≈
70.0 / 0.75 ≈ 94×). Upstream claims a 4.7 ms median on a Ryzen 9; this spike measured a
0.75 ms median on aarch64, so the "single-digit-millisecond" claim is confirmed
(conservatively) rather than relying on it.

### 3. Quality — concrete output differences

Quality was compared on real-world and shared fixtures by structural counts plus manual
preview, not aggregate scores.

- **`installation-handbook.docx` (real-world):** nearly identical content (AnyDoc 2 862
  chars / 8 headings / 7 table rows / 16 list items; MarkItDown 2 831 / 7 / 7 / 16).
  AnyDoc recovered one additional heading. Quality comparable; AnyDoc ~16× faster.
- **`2025-impact-report.pdf` (real-world, text-based):** AnyDoc preserves document
  **structure** as Markdown (2 120 chars, **7 headings, 8 table rows**); LiteParse
  returns **flat text** (2 255 chars, **0 headings, 0 table rows**). For Lumio's
  heading-based `_sections()` this is a material difference — AnyDoc yields real
  line-addressable sections, LiteParse yields one undifferentiated block.
- **`text.docx`:** AnyDoc preserves **footnotes** (4 refs); MarkItDown drops footnotes
  but emits image markers (1) and more links (6 vs 3). Different strengths.
- **`text.rtf`:** AnyDoc produces clean structured Markdown (1 324 chars, 7 headings, 5
  table rows, 4 footnotes); MarkItDown bloats to **20 136 chars with 0 headings** — it
  dumps near-raw content with no structure. Large quality gap on RTF.
- **`handmade-tables.docx`:** AnyDoc preserves more table grid rows (4 vs MarkItDown's 3).
- **`mislabeled-docx-as-pdf.pdf`:** a DOCX renamed `.pdf`. AnyDoc's content-based
  detection reads it as `docx` and converts correctly (1 319 chars — identical to the
  same file under its real name); the LiteParse baseline cannot (it sees non-PDF bytes
  and errors). Mislabeled files convert correctly, as the upstream README claims.

AnyDoc's single shared serializer means headings, tables, lists, and footnotes render
consistently across `docx`, `doc`, `odt`, `rtf`, etc.; MarkItDown's per-format backends
vary in fidelity (and several simply error — see below).

### 4. Format coverage

| format | anydoc | liteparse | markitdown (`[docx]` only, as Lumio ships) |
| --- | :---: | :---: | :---: |
| docx / docm | ✅ | — | ✅ |
| doc / ppt / xls (legacy binary) | ✅ | — | ❌ (`UnsupportedFormatException`) |
| pptx / ppt / pps… | ✅ | — | ❌ (`MissingDependencyException`) |
| xlsx / xls | ✅ | — | ❌ (`MissingDependencyException` / `FileConversionException`) |
| odt / ods / odp | ✅ | — | ❌ (unsupported) |
| rtf | ✅ | — | ⚠️ (works, poor quality) |
| epub | ✅ | — | ⚠️ (not tested here; upstream anydoc slightly trails) |
| csv | ✅ | — | ✅ |
| **pdf (text)** | ✅ | ✅ (slow) | — |
| **pdf (scanned/image)** | ❌ **(no OCR)** | ✅ **(OCR)** | — |
| **pdf (encrypted)** | ❌ **(clear `ConvertError`)** | — | — |
| **html** | ❌ **(unsupported)** | — | ✅ |
| images (png/jpg…) | ❌ | ✅ (OCR) | — |

Two coverage facts drive the verdict:

- AnyDoc covers **11 office container formats** that Lumio's *actually-installed*
  `[documents]` extra cannot read today (PPTX/XLSX/ODT/ODS/ODP/DOC/PPT/XLS error or are
  unsupported with `markitdown[docx]`, including the legacy binary `.doc`/`.ppt`/`.xls`
  which MarkItDown rejects outright). It would broaden Lumio's effective format
  support dramatically with zero extra installs.
- AnyDoc does **not** cover **HTML** and **cannot OCR scanned PDFs / images**. Both are
  live Lumio routes (HTML → MarkItDown; PDF/images → LiteParse OCR).

### 5. Failure-mode and malformed-input audit

Hostile fixtures (AnyDoc only; baseline skipped — see methodology). Every `--errors`
fixture fails **fast** with a specific, actionable `ConvertError` naming the exact
resource limit; every `--skips` / `--recovers` fixture degrades gracefully:

| fixture | AnyDoc behaviour |
| --- | --- |
| `zipbomb--errors.docx` | `ConvertError: resource limit exceeded (max_entry_bytes): word/document.xml declares 201 326 759 decompressed bytes` |
| `imagebomb--errors.docx` | `ConvertError: resource limit exceeded (max_entry_bytes): word/media/image1.png declares 201 326 592 decompressed bytes` |
| `deepxml--errors.docx` | `ConvertError: resource limit exceeded (max_xml_depth): element nesting exceeds 256` |
| `hugerepeat` / `hugespan--errors.ods` | `ConvertError: resource limit exceeded (max_expansion): table repeat expansion exceeds the content budget` |
| `encrypted--errors.odt` | `ConvertError: document is encrypted` |
| `encrypted--errors.pdf` (generated) | `ConvertError: document is encrypted` |
| `empty` / `truncated--errors.docx` | `ConvertError: unsupported input: unrecognized file content: name the format explicitly` |
| `corrupt-styles--skips.docx` | **recovers** (1 257 chars) |
| `mismatched--recovers.docx` | **recovers** (16 chars) |
| `unbalanced--recovers.rtf` | **recovers** (347 chars) |

This is notable: AnyDoc ships built-in resource limits (`max_entry_bytes`, `max_xml_depth`
= 256, `max_expansion`) that are the same class of defence Lumio implements by hand in
`source_processor.py` (`MAX_DOCX_ARCHIVE_BYTES = 2 000 000`, `MAX_EXTRACTED_CHARS`,
`MAX_SOURCE_BYTES`). AnyDoc enforces them **inside** the converter, so a hostile source
is rejected before it can exhaust memory — stronger than Lumio's current MarkItDown path,
where the bounds live in the surrounding adapter and MarkItDown itself has no zip-bomb
protection (the evaluation confirmed MarkItDown hangs on a zipbomb within the SIGALRM
budget). Error messages are specific and actionable rather than raw tracebacks.

### 6. Local-processing and privacy audit

`evaluate.py` runs one conversion of each major kind (DOCX `to_markdown_bytes` +
`to_document`, the generated encrypted/mislabeled fixtures) under
`strace -f -e trace=network` in a child interpreter and records the result in
`results.json` under `network_audit`. On this environment it reported
**`outbound_syscalls: 0`** (no `socket`/`connect`/`sendto`/DNS) with the child
exiting 0 — i.e. conversion is fully local. The exact command is recorded in
`results.json` so the conclusion is reproducible (`strace` is required; the
audit is skipped with a note if it is absent).

AnyDoc has zero Python dependencies, bundles `pdf-inspector` for text PDF, and does not
download models or contact any provider. (The hosted Firecrawl Parse OCR service is a
separate product; the local wheel never calls it.) This satisfies the spike's
"conversion performs no network access" criterion.

### 7. Embedded-asset handling and provenance

`to_markdown_bytes` renders embedded images as alt text only — **no bytes leak into the
Markdown**. `to_document` exposes assets with their raw bytes plus provenance
(`media_type` + `origin_part`, e.g. `word/media/image1.png`), and also surfaces embedded
OLE objects (e.g. `application/vnd.ms-ole-object` at `word/embeddings/oleObject1.bin`).
For Lumio this maps cleanly onto the existing contract:

- Lumio's `NormalizedSource` is **text-only**; using `to_markdown_bytes` keeps image
  bytes out of it entirely (private asset bytes never enter a Compiled Page or a
  Reader-visible artifact).
- `to_document` is the right call **only** for a Maintainer-private inspection path; its
  `origin_part`/`data` fields are equivalent to Lumio's private provenance and must stay
  out of public export, exactly like the Knowledge Source Registry today.

### 8. Determinism

Two consecutive `to_markdown_bytes` calls produced byte-identical output for all 26
successfully-converted fixtures (0 non-deterministic). Output is stable across repeated
runs — a prerequisite for stable Source fingerprints and incremental updates.

## Lumio architectural fit

AnyDoc fits Lumio's `SourceProcessor` seam without restructuring it. The existing
`DocumentSourceProcessor` is already "an adapter over an existing byte-to-text converter
… the converter callable is injected … `converted_by` records the converter name so it
flows into `SourceProvenance` unchanged" — signature `convert: Callable[[bytes,
str | None], str]`. AnyDoc slots in directly:

```python
# A hypothetical anydoc adapter — NOT implemented in this spike.
def _anydoc_convert(content: bytes, filename: str | None) -> str:
    fmt = _fmt_from_extension(filename)   # csv needs an explicit hint
    return anydoc.to_markdown_bytes(content, fmt)
DocumentSourceProcessor(converted_by="anydoc", convert=_anydoc_convert)
```

The remaining Lumio invariants are preserved by construction:

- **`NormalizedSource` / Source fingerprint / Source Versions:** Lumio computes the
  `source_hash` from the raw bytes; AnyDoc never touches identity. `converted_by="anydoc"`
  flows into `SourceProvenance` like `"liteparse"` / `"markitdown"` today.
- **Knowledge Source Registry / proposal-first / visibility / citation:** all sit
  downstream of the converter and are unaffected. Converter metadata and any private
  asset bytes stay out of Compiled Pages when using `to_markdown_bytes`.
- **Routing (`select_document_processor`, ADR-0001):** AnyDoc is a drop-in for the
  MarkItDown-routed office set. It is **not** a drop-in for the LiteParse-routed set
  (PDF/images), where it loses page boundaries and OCR.

The one nuance is PDF **page boundaries**. LiteParse supplies real, per-page numbers that
become page-addressable sections (`page_number` on `NormalizedSection`); AnyDoc's PDF
output is a single Markdown blob with no page info (like MarkItDown's DOCX path). Lumio's
`_sections()` would derive heading-based sections instead, losing page-level citation for
PDF — a regression for page-addressable PDF sources that LiteParse currently supports.

## Gaps that block full adoption

1. **No scanned-PDF / image OCR.** AnyDoc cannot read image-only pages. Lumio's
   `PdfSourceProcessor` relies on LiteParse's OCR for scanned PDFs (and routes images to
   LiteParse for the same reason). Replacing LiteParse entirely would silently break
   scanned-document ingestion.
2. **No HTML.** Format detection returns `None` for HTML; there is no HTML parser. Lumio
   routes `.html`/`.htm` to MarkItDown and ships HTML fixtures.
3. **No PDF page boundaries.** Text-PDF conversion loses per-page numbering (see above).
4. **Maintenance maturity.** PyPI carries only `0.1.1`, `0.1.2`, `0.1.3`; the project is
   new, from a single organisation, with no long release history. A production
   dependency needs more than a 0.1.x track record.

## Recommendation

**Defer.** The spike demonstrates AnyDoc is a strictly better office-format converter
than MarkItDown — broader coverage, far higher fidelity (especially RTF, tables,
footnotes), 15–450× faster, ~13× smaller install, zero dependencies, deterministic, fully
local, and with built-in resource limits that match Lumio's defensive posture. On those
axes alone it would be an `adopt`.

Full replacement of the `[documents]` extra is **deferred** because AnyDoc does not cover
two capabilities the current route already provides — **scanned-PDF OCR** (LiteParse) and
**HTML** (MarkItDown) — and loses PDF page boundaries, and because the project is at
`0.1.x`. The honest path is a **layered** routing decision, not a wholesale swap:

- AnyDoc as the primary converter for the office superset
  (`doc/docx/ppt/pptx/xls/xlsx/odt/ods/odp/rtf/epub/csv`), where it dominates MarkItDown;
- LiteParse retained for scanned-PDF OCR (and images);
- MarkItDown (or another converter) retained for HTML;
- PDF routing decided explicitly: text-PDF could move to AnyDoc for speed and structure,
  but only if the loss of page boundaries is accepted, and scanned-PDF must stay on
  LiteParse.

Because the verdict is **defer** (not `adopt`), no follow-up integration issue is opened
in this spike, per the issue's deliverable rules. The decision flips to `adopt` when
**any** of these holds:

- the maintainer adds OCR (or an OCR hook) and/or HTML, removing the coverage gaps; or
- Lumio makes the layered-routing decision above in a reviewed ADR and accepts the
  0.1.x maturity risk (or pins a version with a vendored fallback); or
- a second independent converter appears so the seam is no longer a single-vendor bet.

When that happens, the work is small and self-contained: a `_anydoc_convert` adapter behind
`DocumentSourceProcessor`, a `converted_by="anydoc"` provenance value, and routing entries
in `select_document_processor` / `source_converter_name` — all behind a `[documents]`
extra change, with no Distiller, Registry, proposal, visibility, or citation impact.

## Conclusion

> AnyDoc converts office documents better, faster, and lighter than MarkItDown — but it
> cannot OCR scanned PDFs or read HTML, and it is 0.1.x. Defer full adoption; pursue it
> as a layered office-format route once OCR/HTML coverage or an explicit routing decision
> closes the gaps.

The spike changed no production dependency or routing. All evidence is reproducible from
[`experiments/anydoc/`](../../experiments/anydoc/).
