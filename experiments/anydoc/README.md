# AnyDoc evaluation harness

Experiment-only harness for **GitHub issue #153** — *Spike: evaluate AnyDoc as a
local document-to-Markdown backend*. It implements the evaluation recommended by
[`docs/research/anydoc-fit.md`](../../docs/research/anydoc-fit.md).

> **This is an experiment, not a product integration.** Nothing here is a
> `lumio-wiki` dependency. `evaluate.py` imports `anydoc`, `markitdown`, and
> `liteparse` directly in an isolated virtualenv. Issue #153 explicitly forbids
> changing the `[documents]` extra or `SourceProcessor` routing during the spike;
> none of that is touched here.

## Pinned converter

- **AnyDoc:** `firecrawl-anydoc==0.1.3` (imported as `anydoc`). MIT.
- Baselines (Lumio's current route): `markitdown[docx]==0.1.7`,
  `liteparse==2.11.0`.
- Environment of record: **Python 3.14.0, Linux aarch64, glibc 2.39.**

## Fixture matrix

`fixtures/` holds 37 files across two provenances, plus two generated fixtures:

| subdir | source | licensing | purpose |
| --- | --- | --- | --- |
| `fixtures/real-world/` | Lumio's own [`examples/real-world-lumio-wiki/sources/`](../../examples/real-world-lumio-wiki/sources/) | Lumio's | direct DOCX/PDF/HTML comparison with the current route |
| `fixtures/anydoc-fixtures/` | representative subset of `firecrawl/anydoc` `tests/fixtures/` | **MIT** (upstream) | one+ per claimed format, plus the `abuse/` and `malformed/` sets |
| `fixtures/anydoc-fixtures/encrypted--errors.pdf` | generated with `pikepdf` (password `lumio`) | generated | encrypted-PDF behaviour (spec requires explicit encrypted-PDF result) |
| `fixtures/anydoc-fixtures/mislabeled-docx-as-pdf.pdf` | a DOCX renamed `.pdf` | generated | mislabeled-input / content-based format-detection audit |

The `anydoc-fixtures/` filenames retain AnyDoc's `--errors` / `--skips` /
`--recovers` suffixes, which document each hostile/malformed file's expected
behaviour and drive the [failure-mode audit](../../docs/research/anydoc-fit.md#5-failure-mode-and-malformed-input-audit).

Formats NOT exercised by a fixture are explicitly disclosed: **PPTM, DOCM, PPS,
PPSM, XLSB, XLB** (container variants that share a parser with the tested
`.pptx`/`.docx`/`.xlsx`) and **scanned/image-only PDF** (no redistributable
scanned fixture; OCR behaviour is documented from the upstream README, not
measured).

## Reproducible run

```bash
# 1. Isolated Python 3.14 environment with all three converters.
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python firecrawl-anydoc==0.1.3
uv pip install --python .venv/bin/python "markitdown[docx]" liteparse

# 2. Run the evaluation (warm median of 3 runs; per-call 30s timeout).
.venv/bin/python experiments/anydoc/evaluate.py \
    --fixtures experiments/anydoc/fixtures \
    --out experiments/anydoc/results.json \
    --warm 3
```

This writes `results.json` (per-fixture cold/warm latency, peak memory,
structural counts, determinism, asset exposure, error type/message, and a
`network_audit` block recording the no-network strace result) and prints a
one-line-per-fixture summary. To regenerate only the headline numbers in the
fit document, read the committed `results.json` (aarch64, Python 3.14).

### Options

- `--warm N` — warm runs whose median is reported (default 3; upstream uses 1).
- `--baseline-only-docx-pdf` — restrict the current-route baseline to DOCX/PDF/HTML.
- `--out` / `--fixtures` — output JSON and fixture root (default to this directory).

### Regenerating the generated fixtures

The two generated fixtures are committed, but they can be recreated deterministically:

```bash
uv pip install --python .venv/bin/python pikepdf
.venv/bin/python -c "
import pikepdf, shutil
from pathlib import Path
fx = Path('experiments/anydoc/fixtures/anydoc-fixtures')
with pikepdf.open(fx / 'text.pdf') as pdf:
    pdf.save(fx / 'encrypted--errors.pdf',
             encryption=pikepdf.Encryption(user='lumio', owner='lumio', R=4,
                                           allow=pikepdf.Permissions(extract=False)))
shutil.copyfile(fx / 'text.docx', fx / 'mislabeled-docx-as-pdf.pdf')
"
```

## What it measures

For each fixture, against AnyDoc and (for DOCX/HTML → MarkItDown, PDF → LiteParse,
matching `lumio_wiki.source_processor.select_document_processor`) against the current
route:

- **cold** latency (first call) and **warm** latency (median of `--warm`);
- peak traced memory on the cold call;
- structural counts over rendered Markdown (headings, table rows, list items, links,
  images, footnote refs, chars);
- a two-run **determinism** check;
- embedded-**asset** exposure via `to_document`;
- per-call **error type and message**.

Each converter call is guarded by a 30 s SIGALRM timeout (Lumio's production route uses
a 20 s spawn timeout for the same reason). The baseline is skipped for the
`--errors/--skips/--recovers` hostile set: Lumio already refuses those before conversion
(`_require_docx_bytes`, `MAX_DOCX_ARCHIVE_BYTES`) and MarkItDown/LiteParse hang on a
zipbomb.

## Methodology limits (disclosed)

- **No LLM judge.** Quality is compared by deterministic structural counts and manual
  preview, not an LLM-scored benchmark (the spike's scope is local fit, not a
  head-to-head quality corpus; AnyDoc's own upstream benchmark carries the LLM-judge
  numbers and is cited in the fit document).
- **Single host.** One aarch64/Linux host, not the upstream Ryzen 9. Speed is reported
  as measured; the fit document does not generalise absolute ms across hosts, only
  relative order-of-magnitude.
- **No scanned-PDF fixture.** OCR behaviour is documented from the upstream README
  (text-extraction only; scanned pages are not read locally), not measured. Encrypted-PDF
  behaviour IS measured (`encrypted--errors.pdf`, generated).
- **Network audit needs `strace`.** The no-network audit runs one conversion of each
  kind under `strace -f -e trace=network` and records `outbound_syscalls` in
  `results.json`; it is skipped with a note if `strace` is not on PATH.
- **Commitment to non-integration.** `evaluate.py` is standard-library + the three
  converters only; it never imports `lumio_wiki` and changes no production code.
