"""Issue #104, AC3: the ``lumio-wiki[documents]`` ingestion capability verified
independently from the workspace.

The base wheel must reject PDF/DOCX sources with an actionable missing-extra
error (proven in ``test_wheel_isolation.py``). This module proves the positive
complement: installing ``lumio-wiki[documents]`` alone — without LanceDB,
PyArrow, Stario, Piccolo, OpenAI, or the full ``lumio`` application — lets the
host coding agent convert a real PDF into a reviewable Ingest Proposal through
the public CLI.

The capability install matrix certified by issue #104:

* ``lumio-wiki``           — model-free foundation (``test_wheel_isolation.py``).
* ``lumio-wiki[documents]`` — this module (LiteParse + MarkItDown).
* ``lumio-wiki[llm]``      — unattended OpenAI Distiller (``test_wheel_isolation_llm.py``).
* ``lumio-wiki[all]``      — documents + llm aggregate (``test_wheel_isolation_llm.py``).
* ``lumio-lancedb``        — enhanced retrieval adapter (lumio-lancedb-wheel workflow).

Building and installing into a fresh venv is expensive; this runs only under
``-m slow``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

ROOT = Path(__file__).parents[3]

# A minimal but valid PDF with a text layer (same fixture shape as the
# workspace-level documents test — small enough to extract quickly under
# LiteParse without rendering).
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


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _build_wheel(wheel_dir: Path) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "build", "--package", "lumio-wiki", "--wheel", "--out-dir", str(wheel_dir)]
    subprocess.run(cmd, cwd=str(ROOT), check=True, capture_output=True)
    wheels = list(wheel_dir.glob("lumio_wiki-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _create_isolated_venv(venv_dir: Path) -> Path:
    venv.create(venv_dir, with_pip=True, clear=True)
    return venv_dir / "bin" / "python"


def _pip(python: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), "-m", "pip", *args],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def documents_wheel_env(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once, install with [documents] into a fresh venv.

    The install intentionally relies on the wheel's own METADATA to pull
    ``msgpack`` and ``msgspec`` — installing them separately would mask a
    broken dependency declaration in the wheel.
    """
    if not _has_uv():
        pytest.skip("uv required to build the wheel for the [documents] extra test")

    wheel_dir = tmp_path_factory.mktemp("documents_wheels")
    wheel_path = _build_wheel(wheel_dir)

    venv_dir = tmp_path_factory.mktemp("documents_venv")
    python = _create_isolated_venv(venv_dir)
    install = _pip(python, "install", f"{wheel_path}[documents]")
    assert install.returncode == 0, f"[documents] install failed:\n{install.stderr}"

    return python


def test_documents_extra_installs_converters(documents_wheel_env: Path):
    """AC3: ``[documents]`` actually installs LiteParse + MarkItDown + AnyDoc."""
    python = documents_wheel_env
    result = subprocess.run(
        [str(python), "-c", "import liteparse, markitdown, anydoc; print('documents extras ok')"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"converters missing:\n{result.stderr}"
    assert "documents extras ok" in result.stdout


def test_documents_extra_does_not_pull_other_capabilities(documents_wheel_env: Path):
    """AC3: ``[documents]`` is scoped — it brings no retrieval/llm/web deps."""
    python = documents_wheel_env
    for forbidden in ("lancedb", "pyarrow", "openai", "stario", "piccolo", "lumio"):
        result = subprocess.run(
            [str(python), "-c", f"import {forbidden}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"{forbidden!r} leaked into lumio-wiki[documents] (ADR-0010 scope invariant)"
        )


def test_isolated_documents_extra_converts_pdf_into_proposal(
    documents_wheel_env: Path, tmp_path: Path
) -> None:
    """AC3: a real PDF → Ingest Proposal journey from the ``[documents]`` install.

    Drives the full ``ingest`` CLI path against a minimal PDF with a text layer
    and asserts the proposal carries the extracted page text. This is the
    positive complement to ``test_wheel_isolation.py``'s missing-extra test.
    """
    python = documents_wheel_env

    kb_root = tmp_path / "kb"
    init = subprocess.run(
        [str(python), "-m", "lumio_wiki.cli", "init", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, f"init failed:\n{init.stderr}"

    pdf_path = tmp_path / "source.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)

    ingest = subprocess.run(
        [
            str(python), "-m", "lumio_wiki.cli",
            "ingest", str(kb_root), str(pdf_path),
        ],
        capture_output=True,
        text=True,
    )
    assert ingest.returncode == 0, f"ingest failed:\n{ingest.stderr}"
    assert "proposal" in ingest.stdout.lower(), ingest.stdout

    # List staged proposals and inspect the most recent one.
    listing = subprocess.run(
        [str(python), "-m", "lumio_wiki.cli", "proposal", "list", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 0, f"proposal list failed:\n{listing.stderr}"

    proposals_dir = kb_root / ".lumio" / "ingest" / "proposals"
    assert proposals_dir.is_dir(), (
        f"proposal directory missing; stdout was:\n{ingest.stdout}"
    )
    proposals = sorted(proposals_dir.glob("*.json"))
    assert proposals, f"no proposal staged; stdout was:\n{ingest.stdout}"


    proposal_payload = json.loads(proposals[-1].read_text(encoding="utf-8"))
    # The PdfSourceProcessor extracts "Hello Lumio World" from the text layer.
    # Proposed pages carry the full Markdown (frontmatter + body) under
    # ``markdown``; ``body`` is the assembled page body once distilled.
    pages = proposal_payload.get("proposed_pages", [])
    joined = "\n".join((page.get("markdown") or "") + (page.get("body") or "") for page in pages)
    assert "Hello Lumio World" in joined, (
        f"PDF text was not extracted into the proposal; payload was:\n{joined}"
    )


def test_doctor_reports_documents_extra_installed(documents_wheel_env: Path):
    """AC3/AC6: ``doctor`` reports the documents capability as installed."""
    python = documents_wheel_env
    result = subprocess.run(
        [str(python), "-m", "lumio_wiki.cli", "doctor"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "extra[documents]:" in result.stdout
    # 'not installed' is what the base wheel prints; with the extra present we
    # expect an affirmative marker instead.
    assert "installed" in result.stdout.lower(), result.stdout
