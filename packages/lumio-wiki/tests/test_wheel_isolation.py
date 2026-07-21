"""Issue #98, AC6: CLI and skill behavior verified from an isolated built-wheel
installation.

Builds the ``lumio-wiki`` wheel, installs it into a fresh virtual environment
that has NONE of the heavyweight optional dependencies (LanceDB, PyArrow,
Stario, Piccolo, OpenAI, LiteParse, MarkItDown) nor the full ``lumio``
application, then asserts:

1. The ``lumio-wiki`` console script exists and ``--help`` succeeds.
2. The packaged Agent Skill (``SKILL.md``) and coding-agent protocol
   (``PROTOCOL.md``) are present inside the installed wheel.
3. A smoke command sequence (``init`` into a temp dir, then ``health``)
   succeeds without the web app, LanceDB, or OpenAI.
4. ``skill path`` resolves the packaged ``SKILL.md`` deterministically.

This is the hermetic, deterministic isolated-wheel proof ADR-0010 requires.
The in-process CLI tests in ``test_cli.py`` are fast regression coverage;
this file is the authoritative release-blocking check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from lumio_wiki import GRAPH_ARTIFACT_FILENAME
from lumio_wiki.cli import default_index_dir

# Building and installing into a fresh venv is expensive; skip under the
# standard fast suite unless explicitly requested or running from the repo
# root (where the workspace is available).
pytestmark = pytest.mark.slow


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _build_wheel(wheel_dir: Path) -> Path:
    """Build the lumio-wiki wheel into ``wheel_dir`` and return its path."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    # Build from the workspace root so uv resolves the workspace member.
    workspace_root = Path(__file__).parents[3]
    if _has_uv():
        cmd = ["uv", "build", "--package", "lumio-wiki", "--wheel", "--out-dir", str(wheel_dir)]
    else:
        # Fallback: build directly from the package directory.
        pkg = workspace_root / "packages" / "lumio-wiki"
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(pkg)]
    subprocess.run(cmd, cwd=str(workspace_root), check=True, capture_output=True)
    wheels = list(wheel_dir.glob("lumio_wiki-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _create_isolated_venv(venv_dir: Path) -> tuple[Path, Path]:
    """Create a fresh venv and return (python, pip)."""
    venv.create(venv_dir, with_pip=True, clear=True)
    if os.name == "nt":  # pragma: no cover
        python = venv_dir / "Scripts" / "python.exe"
    else:
        python = venv_dir / "bin" / "python"
    return python, python


def _assert_heavyweight_not_importable(python: Path) -> None:
    """Assert none of the forbidden optional deps are importable in the venv."""
    forbidden = (
        "lancedb", "pyarrow", "stario", "piccolo",
        "openai", "liteparse", "markitdown", "lumio",
    )
    for module in forbidden:
        result = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"{module!r} must NOT be importable in the isolated lumio-wiki venv "
            f"(ADR-0010 base-install invariant)"
        )


@pytest.fixture(scope="module")
def isolated_wheel_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Build the wheel once, install into a fresh venv, and return the paths."""
    wheel_dir = tmp_path_factory.mktemp("wheels")
    venv_dir = tmp_path_factory.mktemp("venv")

    wheel_path = _build_wheel(wheel_dir)
    python = _create_isolated_venv(venv_dir)[0]

    # Install ONLY lumio-wiki (no optional extras, no lumio app).
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
        check=True,
        capture_output=True,
    )
    # Install runtime dependencies (msgpack, msgspec) from PyPI.
    subprocess.run(
        [str(python), "-m", "pip", "install", "msgpack>=1.0", "msgspec[yaml]>=0.21.1"],
        check=True,
        capture_output=True,
    )

    return {
        "python": python,
        "wheel": wheel_path,
        "venv": venv_dir,
    }


def test_console_script_exists_and_runs_help(isolated_wheel_env: dict):
    """AC1 + AC6: the wheel installs an unambiguous ``lumio-wiki`` console script."""
    python = isolated_wheel_env["python"]
    # The console script entry point should be installed.
    bin_dir = python.parent
    if os.name == "nt":  # pragma: no cover
        script = bin_dir / "lumio-wiki.exe"
    else:
        script = bin_dir / "lumio-wiki"
    assert script.is_file(), f"lumio-wiki console script not found at {script}"

    # ``lumio-wiki --help`` must succeed.
    result = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"--help failed:\n{result.stderr}"
    assert "lumio-wiki" in result.stdout
    assert "init" in result.stdout
    assert "validate" in result.stdout
    assert "ingest" in result.stdout


def test_console_script_reports_version(isolated_wheel_env: dict):
    """The console script reports the installed package version."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    # The version comes from lumio_wiki.__version__.
    version_output = result.stdout.strip()
    assert version_output.startswith("lumio-wiki "), version_output


def test_wheel_contains_skill_and_protocol(isolated_wheel_env: dict):
    """AC3: the wheel ships the Agent Skill and protocol as package data."""
    wheel_path: Path = isolated_wheel_env["wheel"]
    import zipfile

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
    assert "lumio_wiki/data/skill/SKILL.md" in names, (
        f"SKILL.md not found in wheel; contents: {names}"
    )
    assert "lumio_wiki/data/protocol/PROTOCOL.md" in names, (
        f"PROTOCOL.md not found in wheel; contents: {names}"
    )
    # The CLI module must also be present.
    assert "lumio_wiki/cli.py" in names
    assert "lumio_wiki/skill.py" in names


def test_skill_path_resolves_from_isolated_install(isolated_wheel_env: dict):
    """AC4: the CLI deterministically locates the packaged skill from the built wheel."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "skill", "path"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"skill path failed:\n{result.stderr}"
    skill_path = Path(result.stdout.strip())
    assert skill_path.is_file(), f"skill file not found at {skill_path}"
    assert skill_path.name == "SKILL.md"
    # It must live inside the installed lumio_wiki package.
    assert "lumio_wiki" in str(skill_path)


def test_protocol_path_resolves_from_isolated_install(isolated_wheel_env: dict):
    """AC4: the CLI deterministically locates the packaged protocol."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "skill", "protocol"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"skill protocol failed:\n{result.stderr}"
    protocol_path = Path(result.stdout.strip())
    assert protocol_path.is_file(), f"protocol file not found at {protocol_path}"
    assert protocol_path.name == "PROTOCOL.md"


def test_init_then_health_succeeds_without_optionals(isolated_wheel_env: dict, tmp_path: Path):
    """AC6: a smoke init → health cycle runs without LanceDB / web / OpenAI."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    kb_root = tmp_path / "kb"

    # init
    result = subprocess.run(
        [str(script), "init", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"init failed:\n{result.stderr}"
    assert (kb_root / "lumio.yaml").is_file()

    # health on the freshly-initialized (empty) KB.
    result = subprocess.run(
        [str(script), "health", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"health failed:\n{result.stderr}"
    assert "pages:" in result.stdout
    assert "valid:" in result.stdout


def test_doctor_runs_from_isolated_install(isolated_wheel_env: dict):
    """``doctor`` reports the install shape from the isolated wheel."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "doctor"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"doctor failed:\n{result.stderr}"
    assert "lumio-wiki" in result.stdout
    assert "extra[documents]:" in result.stdout
    assert "extra[llm]:" in result.stdout
    # In the isolated install, documents/llm/lancedb must be absent.
    assert "not installed" in result.stdout


def test_isolated_install_has_no_heavyweight_dependencies(isolated_wheel_env: dict):
    """ADR-0010 invariant: the base wheel does not pull in forbidden deps."""
    _assert_heavyweight_not_importable(isolated_wheel_env["python"])


def test_isolated_init_ingest_publish_journey(isolated_wheel_env: dict, tmp_path: Path):
    """AC2 + AC5 + AC6: init → ingest → publish through the isolated CLI.

    The host coding agent is the default Distiller; no OpenAI client required.
    """
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    kb_root = tmp_path / "kb"

    # init
    subprocess.run([str(script), "init", str(kb_root)], check=True, capture_output=True)

    # Author a page as the host agent would.
    source = tmp_path / "page.md"
    source.write_text(
        "---\n"
        'title: "Wheel Page"\n'
        "aliases: []\n"
        "tags:\n"
        '  - "wheel"\n'
        'summary: "Authored from the isolated wheel."\n'
        'category: "concepts"\n'
        'type: "concept"\n'
        'durability_rationale: "Durable concept definition for the wheel test."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        "sources:\n"
        '  - id: "wheel"\n'
        '    title: "Wheel test"\n'
        "relationships: []\n"
        "synthetic: false\n"
        "---\n\n"
        "# Wheel Page\n\n"
        "Body from the isolated install.\n",
        encoding="utf-8",
    )
    # ingest
    result = subprocess.run(
        [str(script), "ingest", str(kb_root), str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"ingest failed:\n{result.stderr}"
    assert "Staged proposal" in result.stdout

    # Extract the proposal id from the ingest store.
    proposals_dir = kb_root / ".lumio" / "ingest" / "proposals"
    proposal_files = list(proposals_dir.glob("*.json"))
    assert len(proposal_files) == 1
    proposal_id = proposal_files[0].stem

    result = subprocess.run(
        [str(script), "proposal", "validate", str(kb_root), proposal_id],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"proposal validate failed:\n{result.stderr}"

    # publish
    result = subprocess.run(
        [str(script), "publish", str(kb_root), proposal_id],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"publish failed:\n{result.stderr}"
    assert "Published" in result.stdout

    # The page was written (categorized KB routes to <category>/<slug>.md).
    assert (kb_root / "concepts" / "wheel_page.md").is_file()

    # health still succeeds and the KB is valid.
    result = subprocess.run(
        [str(script), "health", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "pages:               1" in result.stdout


def test_isolated_search_page_related_paths(isolated_wheel_env: dict, tmp_path: Path):
    """AC2 + AC6: search, page, related, and paths work from the isolated wheel."""
    import shutil

    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")

    # Copy the valid fixture so we have a populated KB.
    fixtures = Path(__file__).parents[3] / "tests" / "fixtures" / "valid"
    kb_root = tmp_path / "kb"
    shutil.copytree(fixtures, kb_root)

    # search
    result = subprocess.run(
        [str(script), "search", str(kb_root), "LanceDB"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"search failed:\n{result.stderr}"
    assert "Architecture" in result.stdout or "Technology" in result.stdout

    # page (read by Canonical Page Title)
    result = subprocess.run(
        [str(script), "page", str(kb_root), "Architecture"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"page failed:\n{result.stderr}"
    assert "# Architecture" in result.stdout
    assert "modular monolith" in result.stdout

    # related
    result = subprocess.run(
        [str(script), "related", str(kb_root), "Lumio Overview"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"related failed:\n{result.stderr}"
    assert "Architecture" in result.stdout

    # paths (shortest path)
    result = subprocess.run(
        [str(script), "paths", str(kb_root), "Lumio Overview", "Technology Stack"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"paths failed:\n{result.stderr}"
    assert "->" in result.stdout


def test_isolated_proposal_inspect_and_discard(isolated_wheel_env: dict, tmp_path: Path):
    """AC2 + AC6: proposal inspect and discard work from the isolated wheel."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    kb_root = tmp_path / "kb"

    subprocess.run([str(script), "init", str(kb_root)], check=True, capture_output=True)

    source = tmp_path / "page.md"
    source.write_text(
        "---\n"
        'title: "Inspect Page"\n'
        "aliases: []\n"
        'tags: ["inspect"]\n'
        'summary: "Test page."\n'
        'category: "concepts"\n'
        'type: "concept"\n'
        'durability_rationale: "Durable."\n'
        'lifecycle: "draft"\n'
        'visibility: "internal"\n'
        'sources: [{id: "test", title: "Test"}]\n'
        "relationships: []\n"
        "synthetic: false\n"
        "---\n\n# Inspect Page\n\nBody.\n",
        encoding="utf-8",
    )
    subprocess.run(
        [str(script), "ingest", str(kb_root), str(source)],
        check=True,
        capture_output=True,
    )

    proposals_dir = kb_root / ".lumio" / "ingest" / "proposals"
    proposal_id = next(proposals_dir.glob("*.json")).stem

    # proposal list
    result = subprocess.run(
        [str(script), "proposal", "list", str(kb_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert proposal_id in result.stdout

    # proposal inspect
    result = subprocess.run(
        [str(script), "proposal", "inspect", str(kb_root), proposal_id],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Inspect Page" in result.stdout
    assert "Diff:" in result.stdout

    # discard
    result = subprocess.run(
        [str(script), "discard", str(kb_root), proposal_id],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Discarded" in result.stdout


def test_isolated_skill_install(isolated_wheel_env: dict, tmp_path: Path):
    """AC4 + AC6: skill install works from the isolated wheel."""
    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    dest = tmp_path / "agent-skills" / "lumio-wiki"

    result = subprocess.run(
        [str(script), "skill", "install", "--agent", "claude-code", "--dest", str(dest)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"skill install failed:\n{result.stderr}"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "PROTOCOL.md").is_file()
    # The installed skill content must match the packaged source.
    from lumio_wiki.skill import resolve_skill_path

    assert (dest / "SKILL.md").read_text() == resolve_skill_path().read_text()



# ---------------------------------------------------------------------------
# Issue #100: [documents] extra isolation (AC4 + AC6)
# ---------------------------------------------------------------------------


def test_base_install_rejects_pdf_with_actionable_missing_extra_error(
    isolated_wheel_env: dict, tmp_path: Path
):
    """AC4 + AC6: a base install (no documents extra) produces an actionable
    error naming ``pip install 'lumio-wiki[documents]'`` when a PDF is
    ingested, rather than a raw ImportError (PRD #93 user story 18)."""
    python = isolated_wheel_env["python"]

    # Verify liteparse/markitdown are genuinely absent in the isolated venv.
    for module in ("liteparse", "markitdown"):
        check = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0, f"{module} should NOT be installed in base wheel"

    # Init a KB so the ingest command can load it.
    kb_root = tmp_path / "kb"
    init_cmd = (
        "from lumio_wiki.cli import main; import sys; "
        f'sys.exit(main(["init", "{kb_root}"]))'
    )
    subprocess.run(
        [str(python), "-c", init_cmd],
        capture_output=True,
        text=True,
        check=True,
    )

    # Write a minimal PDF and attempt to ingest it.
    pdf_file = tmp_path / "source.pdf"
    pdf_file.write_bytes(
        b"%PDF-1.4\n1 0 obj<<>>endobj\nxref\n0 1\n"
        b"0000000000 65535 f \ntrailer<<>>\nstartxref\n0\n%%EOF"
    )
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "ingest", str(kb_root), str(pdf_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "base install must NOT silently process a PDF"
    combined = result.stdout + result.stderr
    assert "lumio-wiki[documents]" in combined, (
        f"missing-extra error must name the exact install command; got:\n{combined}"
    )


def test_base_install_rejects_docx_with_actionable_missing_extra_error(
    isolated_wheel_env: dict, tmp_path: Path
):
    """AC4 + AC6: a base install produces the actionable error for DOCX too."""
    python = isolated_wheel_env["python"]
    kb_root = tmp_path / "kb"
    init_cmd = (
        "from lumio_wiki.cli import main; import sys; "
        f'sys.exit(main(["init", "{kb_root}"]))'
    )
    subprocess.run(
        [str(python), "-c", init_cmd],
        capture_output=True,
        text=True,
        check=True,
    )

    docx_file = tmp_path / "source.docx"
    # Build a valid DOCX archive structure so the signature check passes
    # and the converter (MarkItDown) is reached, triggering the missing-extra
    # error rather than a format-validation error.
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>',
        )
    docx_file.write_bytes(buf.getvalue())
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "ingest", str(kb_root), str(docx_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "lumio-wiki[documents]" in combined


# ---------------------------------------------------------------------------
# Issue #110, AC6: the isolated wheel demonstrates the COMPLETE zero-index
# retrieval ladder (Hot Index, Navigation Indexes, deterministic search,
# focused page read, related-page lookup, bounded paths) without LanceDB, a
# model provider, the web application, or internal repository imports — plus
# graceful graph recovery (AC4).
# ---------------------------------------------------------------------------


def test_isolated_retrieval_ladder(isolated_wheel_env: dict, tmp_path: Path):
    """AC2 + AC4 + AC6: the full retrieval ladder runs from the isolated wheel,
    and a corrupt graph artifact never blocks zero-index operation."""
    python = isolated_wheel_env["python"]
    script = python.parent / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    fixtures = Path(__file__).parents[3] / "tests" / "fixtures"

    def run(*args: str) -> str:
        result = subprocess.run([str(script), *args], capture_output=True, text=True)
        assert result.returncode == 0, f"{' '.join(args)} failed:\n{result.stderr}"
        return result.stdout

    # Ladder 0 — Hot Index (curated pins). categorized_kb has hot_index pins.
    cat_kb = tmp_path / "cat"
    shutil.copytree(fixtures / "categorized_kb", cat_kb)
    hot = run("hot", str(cat_kb))
    assert "Hot Index" in hot
    assert "Lumio Overview" in hot and "Acme Corp" in hot

    # Graph steps use the valid fixture (known canonical graph:
    # Lumio Overview -> Architecture -> Technology Stack).
    kb = tmp_path / "kb"
    shutil.copytree(fixtures / "valid", kb)

    # Ladder 1 — Navigation Index (generated catalog).
    nav = run("index", str(kb))
    assert "Lumio Overview" in nav and "Architecture" in nav and "Technology Stack" in nav

    # Ladder 2 — deterministic zero-index search.
    search = run("search", str(kb), "LanceDB")
    assert "Architecture" in search or "Technology" in search

    # Ladder 3 — focused page read.
    page = run("page", str(kb), "Architecture")
    assert "# Architecture" in page

    # Ladder 4 — related-page lookup, discovery scope, truthful trace.
    related = run(
        "related",
        str(kb),
        "Lumio Overview",
        "--scope",
        "discovery",
        "--direction",
        "both",
        "--depth",
        "2",
        "--trace",
    )
    assert "Architecture" in related
    assert "# trace:" in related
    assert "scope=discovery" in related and "direction=both" in related

    # Ladder 5 — bounded paths with truthful trace.
    paths = run(
        "paths",
        str(kb),
        "Lumio Overview",
        "Technology Stack",
        "--scope",
        "canonical",
        "--max-depth",
        "3",
        "--trace",
    )
    assert "Lumio Overview -> Architecture -> Technology Stack" in paths
    assert "# trace:" in paths and "found=true" in paths and "hops=2" in paths

    index_dir = default_index_dir(kb)
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / GRAPH_ARTIFACT_FILENAME).write_bytes(b"corrupt garbage")
    run("search", str(kb), "LanceDB")  # still works
    run("related", str(kb), "Lumio Overview")  # still works
    h_before = run("health", str(kb))
    assert "graph_fresh:" in h_before and "False" in h_before
    assert "graph_recovery:" in h_before
    run("health", str(kb), "--rebuild")
    h_after = run("health", str(kb))
    assert "graph_fresh:" in h_after and "True" in h_after
    assert "graph_recovery:" not in h_after

    # AC6 invariant re-checked from the same isolated install: none of the
    # heavyweight deps sneaked in while exercising the whole ladder.
    _assert_heavyweight_not_importable(python)