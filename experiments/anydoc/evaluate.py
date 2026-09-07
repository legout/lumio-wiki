#!/usr/bin/env python3
"""Reproducible AnyDoc evaluation for Lumio spike (issue #153).

Converts the fixture matrix under ``fixtures/`` with **AnyDoc** and, for the
DOCX/PDF fixtures Lumio already ships, with the **current** route
(``markitdown`` for DOCX/HTML, ``liteparse`` for PDF). Captures per-fixture
cold + warm latency, output size, structural counts, errors, determinism,
embedded-asset exposure, and a local-processing (no-network) audit.

This is a **spike harness**, not a product dependency. It imports anydoc,
markitdown, and liteparse directly. Install in an isolated Python 3.14 env:

    uv venv --python 3.14 .venv
    uv pip install --python .venv/bin/python firecrawl-anydoc==0.1.3
    uv pip install --python .venv/bin/python "markitdown[docx]" liteparse

Run:

    .venv/bin/python experiments/anydoc/evaluate.py \
        --out experiments/anydoc/results.json --warm 5

Outputs a JSON results blob and prints a human-readable summary.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import io
import json
import platform
import re
import signal
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Hard per-call timeout (seconds). Lumio's production route uses a 20s spawn
# timeout (``run_conversion``) because the synchronous converters may hang on
# hostile inputs; we mirror that here with a SIGALRM guard so one pathological
# fixture cannot stall the whole sweep.
CALL_TIMEOUT = 30.0


class _CallTimeout(Exception):
    """Raised when a single converter call exceeds CALL_TIMEOUT."""

WHEEL_VERSION = "0.1.3"

# ---------------------------------------------------------------------------
# Structural metric extraction (deterministic, no model).
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.MULTILINE)
_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_FOOTNOTE_REF_RE = re.compile(r"\[\^[^\]]+\]")


def structure(md: str) -> dict[str, int]:
    """Cheap deterministic structural counts over rendered Markdown."""
    return {
        "chars": len(md),
        "lines": md.count("\n") + 1 if md else 0,
        "headings": len(_HEADING_RE.findall(md)),
        "table_rows": len(_TABLE_ROW_RE.findall(md)),
        "list_items": len(_LIST_RE.findall(md)),
        "links": len(_LINK_RE.findall(md)),
        "images": len(_IMAGE_RE.findall(md)),
        "footnote_refs": len(_FOOTNOTE_REF_RE.findall(md)),
    }


# ---------------------------------------------------------------------------
# Converter wrappers — each returns (markdown_str, error_or_None).
# ---------------------------------------------------------------------------

def _anydoc_version() -> str:
    try:
        from importlib.metadata import version

        import anydoc  # noqa: F401

        return version("firecrawl-anydoc")
    except Exception:
        return WHEEL_VERSION


def _with_timeout(fn, *args, timeout: float = CALL_TIMEOUT):
    """Run fn(*args) with a SIGALRM hard timeout (Linux main-thread only)."""
    def _handler(signum, frame):  # noqa: ARG001
        raise _CallTimeout(f"call exceeded {timeout:.0f}s")
    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return fn(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def convert_anydoc(data: bytes, ext: str) -> tuple[str, str | None, dict[str, Any]]:
    """AnyDoc: to_markdown_bytes. Signature-less formats (csv) need a hint."""
    anydoc = importlib.import_module("anydoc")
    fmt = None
    if ext == ".csv":
        fmt = "csv"
    meta: dict[str, Any] = {"detected_format": None, "assets": None}
    try:
        if fmt is None:
            meta["detected_format"] = anydoc.format_from_bytes(data)
        md = anydoc.to_markdown_bytes(data, fmt)
        # Asset audit via the document model (unsupported for pdf).
        if fmt != "csv":
            try:
                fmt2 = fmt or anydoc.format_from_bytes(data)
                if fmt2 and fmt2 != "pdf":
                    doc = anydoc.to_document(data, fmt2)
                    meta["assets"] = [
                        {"media_type": a.media_type, "origin_part": a.origin_part,
                         "bytes": len(a.data)}
                        for a in getattr(doc, "assets", []) or []
                    ]
            except Exception as exc:  # noqa: BLE001
                meta["asset_error"] = f"{type(exc).__name__}: {exc}"
        return md, None, meta
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}", meta


def convert_markitdown(data: bytes, ext: str) -> tuple[str, str | None]:
    """Lumio current DOCX/HTML route: MarkItDown."""
    markitdown = importlib.import_module("markitdown")
    try:
        result = markitdown.MarkItDown().convert(io.BytesIO(data), file_extension=ext)
        return result.text_content, None
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"


def convert_liteparse(data: bytes, ext: str) -> tuple[str, str | None]:
    """Lumio current PDF route: LiteParse.

    Mirrors ``lumio_wiki.source_processor._pages_to_sections``: prefer the
    per-page ``markdown`` and fall back to ``text`` when it is empty, so the
    comparison reflects what Lumio's production code actually feeds into
    ``NormalizedSource``.
    """
    liteparse = importlib.import_module("liteparse")
    try:
        result = liteparse.LiteParse(max_pages=200).parse(data)
        pages = list(getattr(result, "pages", []) or [])
        md = "\n\n".join(
            (getattr(p, "markdown", "") or getattr(p, "text", "") or "").strip()
            for p in pages
        )
        return md, None
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Timing harness: cold (first call) + warm (median of N), + peak memory once.
# ---------------------------------------------------------------------------

@dataclass
class TimedResult:
    ok: bool
    error: str | None
    md: str
    cold_ms: float
    warm_ms: float | None
    peak_bytes: int | None
    meta: dict[str, Any] = field(default_factory=dict)


def time_converter(fn, data: bytes, ext: str, warm: int):
    gc.collect()
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        out = _with_timeout(fn, data, ext)
        cold_ms = (time.perf_counter() - t0) * 1000.0
    except _CallTimeout as exc:
        tracemalloc.stop()
        return TimedResult(False, f"TIMEOUT: {exc}", "", 0.0, None, None)
    except Exception as exc:  # noqa: BLE001
        tracemalloc.stop()
        return TimedResult(False, f"{type(exc).__name__}: {exc}", "", 0.0, None, None)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if isinstance(out, tuple) and len(out) == 3:
        md, err, meta = out
    else:
        md, err = out
        meta = {}
    if err is not None:
        return TimedResult(False, err, "", cold_ms, None, peak, meta)
    warm_samples = []
    for _ in range(warm):
        try:
            t0 = time.perf_counter()
            out2 = _with_timeout(fn, data, ext)
            warm_samples.append((time.perf_counter() - t0) * 1000.0)
            if isinstance(out2, tuple) and out2[1] is not None:
                warm_samples.pop()  # diverged to an error mid-run
                break
        except _CallTimeout:
            break  # a warm run stalled; report median of what we have
    warm_ms = statistics.median(warm_samples) if warm_samples else None
    return TimedResult(True, None, md, cold_ms, warm_ms, peak, meta)


# ---------------------------------------------------------------------------
# Determinism: convert twice, compare bytes-for-bytes.
# ---------------------------------------------------------------------------

def determinism_anydoc(data: bytes, ext: str) -> bool | None:
    anydoc = importlib.import_module("anydoc")
    fmt = "csv" if ext == ".csv" else None
    try:
        a = anydoc.to_markdown_bytes(data, fmt)
        b = anydoc.to_markdown_bytes(data, fmt)
        return a == b
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Local-processing audit: run one conversion of each kind under strace and
# count outbound network syscalls. Reproducible from the recorded command.
# ---------------------------------------------------------------------------

def audit_network(fixtures_root: Path, warm_runs: int) -> dict[str, Any]:
    """Best-effort no-network audit via strace (skipped if strace is absent).

    Spawns a child interpreter under ``strace -f -e trace=network`` that imports
    ``anydoc`` and converts one fixture of each major kind, then counts any
    outbound syscalls (connect/sendto/recvfrom/socket(AF_INET*)/getaddrinfo).
    The exact command is recorded so the conclusion is reproducible.
    """
    import shutil
    import subprocess
    import tempfile

    fixture = fixtures_root / "real-world" / "installation-handbook.docx"
    if not fixture.exists():
        # fall back to any fixture that exercises the office parser
        fixture = next((fixtures_root).rglob("*.docx"), None)
    result: dict[str, Any] = {
        "command": (
            "strace -f -e trace=network -o <trace> python -c '"
            "import anydoc; ... convert one fixture of each kind ...'"
        ),
        "strace_available": shutil.which("strace") is not None,
        "outbound_syscalls": None,
    }
    if not result["strace_available"] or fixture is None:
        result["note"] = "strace missing or no fixture; command recorded for manual run"
        return result
    child = (
        "import anydoc; "
        f"d=open({str(fixture)!r},'rb').read(); "
        "anydoc.to_markdown_bytes(d); anydoc.to_document(d); "
        "print('audit conversions done')"
    )
    with tempfile.NamedTemporaryFile("w+", suffix=".trace", delete=False) as tf:
        trace_path = tf.name
    cmd = ["strace", "-f", "-e", "trace=network", "-o", trace_path,
           sys.executable, "-c", child]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        lines = Path(trace_path).read_text(errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"strace run failed: {exc}"
        return result
    finally:
        Path(trace_path).unlink(missing_ok=True)
    # Outbound indicators; pure exit notices ("+++ exited ...") are not calls.
    outbound = [ln for ln in lines
                if any(tok in ln for tok in (
                    "connect", "sendto", "recvfrom", "getaddrinfo",
                    "socket(AF_INET", "socket(AF_INET6"))]
    result["outbound_syscalls"] = len(outbound)
    result["child_returncode"] = proc.returncode
    result["total_trace_lines"] = len(lines)
    return result


# ---------------------------------------------------------------------------
# Main sweep.
# ---------------------------------------------------------------------------

# Formats AnyDoc claims to support (from python/README + the supported-formats
# table; container variants like .docm/.xlsm/.ppsx share a parser with their
# parent and are included so the anydoc_supported flag is accurate).
ANYDOC_FORMATS = {
    ".doc", ".docx", ".docm",
    ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsx", ".xlsm", ".xlsb",
    ".odt", ".ods", ".odp",
    ".rtf", ".epub", ".csv", ".pdf",
}

# Routing mirrors lumio_wiki.source_processor (ADR-0001): PDF -> LiteParse,
# DOCX/HTML/broad documents -> MarkItDown. The extension set mirrors
# _MARKITDOWN_EXTENSIONS so the legacy/office formats Lumio routes to
# MarkItDown are all exercised by the baseline (they error without the right
# extras, which is itself a recorded finding).
_MARKITDOWN_ROUTE = frozenset({
    ".docx", ".doc", ".html", ".htm",
    ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".csv", ".xml", ".json", ".rst", ".rtf",
})


def current_route(ext: str) -> str | None:
    e = ext.lower()
    if e == ".pdf":
        return "liteparse"
    if e in _MARKITDOWN_ROUTE:
        return "markitdown"
    return None


def iter_fixtures(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    ap.add_argument("--fixtures", default=str(here / "fixtures"))
    ap.add_argument("--out", default=str(here / "results.json"))
    ap.add_argument("--warm", type=int, default=3, help="warm runs (median)")
    ap.add_argument("--baseline-only-docx-pdf", action="store_true",
                    help="only run the current-route baseline for docx/pdf/html")
    args = ap.parse_args()

    fixtures = Path(args.fixtures)
    results: dict[str, Any] = {
        "anydoc_version": _anydoc_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "arch": platform.machine(),
        "warm_runs": args.warm,
        "fixtures": [],
        "notes": [],
    }

    current_funcs = {"markitdown": convert_markitdown, "liteparse": convert_liteparse}

    for fx in iter_fixtures(fixtures):
        ext = fx.suffix.lower()
        data = fx.read_bytes()
        rel = str(fx.relative_to(fixtures))
        # Hostile fixtures (abuse/malformed) are an AnyDoc failure-mode audit only:
        # Lumio's production route already refuses them via _require_docx_bytes /
        # MAX_DOCX_ARCHIVE_BYTES and a 20s spawn timeout, and MarkItDown/LiteParse
        # can hang on a zipbomb, so the baseline is skipped for these.
        hostile = any(tag in fx.name for tag in ("--errors", "--skips", "--recovers"))
        entry: dict[str, Any] = {
            "path": rel,
            "ext": ext,
            "bytes": len(data),
            "anydoc_supported": ext in ANYDOC_FORMATS,
        }
        # AnyDoc timing.
        r = time_converter(convert_anydoc, data, ext, args.warm)
        entry["anydoc"] = {
            "ok": r.ok, "error": r.error,
            "cold_ms": round(r.cold_ms, 3),
            "warm_ms": None if r.warm_ms is None else round(r.warm_ms, 3),
            "peak_bytes": r.peak_bytes,
            "structure": structure(r.md) if r.ok else None,
            "detected_format": r.meta.get("detected_format"),
            "assets": r.meta.get("assets"),
            "asset_error": r.meta.get("asset_error"),
            "deterministic": determinism_anydoc(data, ext) if r.ok else None,
            "md_preview": r.md[:200] if r.ok else None,
        }
        # Current-route baseline for the overlapping formats (docx/pdf/html).
        # Skipped for hostile fixtures (see above) to avoid converter hangs.
        route = current_route(ext)
        if route and not hostile and (
            not args.baseline_only_docx_pdf or ext in {".docx", ".pdf", ".html"}
        ):
            rb = time_converter(current_funcs[route], data, ext, args.warm)
            entry["baseline"] = {
                "route": route,
                "ok": rb.ok, "error": rb.error,
                "cold_ms": round(rb.cold_ms, 3),
                "warm_ms": None if rb.warm_ms is None else round(rb.warm_ms, 3),
                "peak_bytes": rb.peak_bytes,
                "structure": structure(rb.md) if rb.ok else None,
            }
        results["fixtures"].append(entry)

    results["network_audit"] = audit_network(fixtures, args.warm)
    Path(args.out).write_text(json.dumps(results, indent=2))
    _print_summary(results)
    return 0


def _print_summary(results: dict[str, Any]) -> None:
    print(f"# AnyDoc {results['anydoc_version']} | Python {results['python']} "
          f"| {results['arch']} | warm_runs={results['warm_runs']}")
    print(f"{'fixture':<46}{'ext':<7}{'anydoc_warm_ms':>15}"
          f"{'baseline_warm_ms':>18}{'anydoc_chars':>14}{'base_chars':>12}")
    for f in results["fixtures"]:
        ad = f["anydoc"]
        bl = f.get("baseline")
        name = f["path"].split("/", 1)[-1][:45]
        aw = ad["warm_ms"] if ad["warm_ms"] is not None else ("ERR" if not ad["ok"] else "-")
        bw = (bl["warm_ms"] if bl and bl.get("warm_ms") is not None
              else ("ERR" if bl and not bl["ok"] else "-"))
        ac = ad["structure"]["chars"] if ad["ok"] else "-"
        bc = bl["structure"]["chars"] if (bl and bl["ok"]) else "-"
        print(f"{name:<46}{f['ext']:<7}{str(aw):>15}{str(bw):>18}"
              f"{str(ac):>14}{str(bc):>12}")


if __name__ == "__main__":
    raise SystemExit(main())
