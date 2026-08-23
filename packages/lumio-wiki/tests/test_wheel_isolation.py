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

import hashlib
import json
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


# A categorized KB whose canonical graph is the chain
# Lumio Overview -> Architecture -> Technology Stack (accepted entity Claims;
# the title-based relationship frontmatter input is gone, ADR-0021).
_GRAPH_CONTROL = """\
version: 2
mode: "categorized"
categories:
  - name: concepts
ontology:
  entity_types:
    concept: {}
  predicates:
    uses:
      subject_types: [concept]
      object_types: [concept]
"""


def _graph_page(title: str, *, body: str, claims_yaml: str = "") -> str:
    slug = title.lower().replace(" ", "-")
    claims = f"claims:\n{claims_yaml}" if claims_yaml else ""
    return (
        "---\n"
        f'id: "entity:{slug}"\n'
        f'title: "{title}"\n'
        "entity_types:\n"
        "  - concept\n"
        'tags:\n  - "test"\n'
        f'summary: "{title} summary."\n'
        'lifecycle: "approved"\n'
        'visibility: "public"\n'
        'sources:\n'
        f'  - id: "src-{slug}"\n'
        f'    title: "{title} Source"\n'
        f"{claims}"
        "---\n\n"
        f"# {title}\n\n## Overview\n\n{body}\n"
    )


def _write_graph_kb(root: Path) -> Path:
    """Write a categorized KB with the known canonical chain into ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lumio.yaml").write_text(_GRAPH_CONTROL, encoding="utf-8")
    pages = {
        "concepts/overview.md": _graph_page(
            "Lumio Overview",
            body="Lumio is a deployable chat platform for trusted knowledge and data.",
            claims_yaml=(
                "  - id: claim:lumio-overview-uses-architecture\n"
                "    predicate: uses\n"
                '    object: "entity:architecture"\n'
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
            ),
        ),
        "concepts/architecture.md": _graph_page(
            "Architecture",
            body=(
                "Lumio is built as a modular monolith with a framework-independent "
                "Core SDK. Lumio uses LanceDB for the derived lexical index."
            ),
            claims_yaml=(
                "  - id: claim:architecture-uses-technology-stack\n"
                "    predicate: uses\n"
                '    object: "entity:technology-stack"\n'
                "    status: accepted\n"
                "    evidence:\n"
                '      - section: "Overview"\n'
            ),
        ),
        "concepts/technology.md": _graph_page(
            "Technology Stack",
            body="LanceDB provides the embedded vector index.",
        ),
    }
    for rel, text in pages.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _build_wheel(wheel_dir: Path) -> Path:
    """Build the lumio-wiki wheel into ``wheel_dir`` and return its path."""
    return _build_workspace_wheel(wheel_dir, "lumio-wiki", "lumio_wiki-*.whl")


def _build_workspace_wheel(wheel_dir: Path, package: str, wheel_glob: str) -> Path:
    """Build one workspace package's wheel and return its path (shared
    build/fallback shape for lumio-wiki and lumio-lancedb)."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    # Build from the workspace root so uv resolves the workspace member.
    workspace_root = Path(__file__).parents[3]
    if _has_uv():
        cmd = ["uv", "build", "--package", package, "--wheel", "--out-dir", str(wheel_dir)]
    else:
        # Fallback: build directly from the package directory.
        pkg = workspace_root / "packages" / package
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(pkg)]
    subprocess.run(cmd, cwd=str(workspace_root), check=True, capture_output=True)
    wheels = list(wheel_dir.glob(wheel_glob))
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
        "lancedb",
        "pyarrow",
        "stario",
        "piccolo",
        "openai",
        "liteparse",
        "markitdown",
        "anydoc",
        "lumio",
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

    skill_name = "lumio_wiki/data/skill/SKILL.md"
    protocol_name = "lumio_wiki/data/skill/PROTOCOL.md"
    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
        skill_text = zf.read(skill_name).decode("utf-8")
        protocol_text = zf.read(protocol_name).decode("utf-8")
    assert skill_name in names, f"SKILL.md not found in wheel; contents: {names}"
    assert protocol_name in names, f"PROTOCOL.md not found in wheel skill bundle; contents: {names}"

    import msgspec

    _opening, frontmatter, _body = skill_text.split("---", 2)
    metadata = msgspec.yaml.decode(frontmatter.encode("utf-8"))
    assert metadata["name"] == "lumio-wiki"
    assert metadata["metadata"]["distribution"] == "lumio-wiki"
    assert "(PROTOCOL.md)" in skill_text
    assert protocol_text.startswith("# Lumio Wiki Coding-Agent Protocol")
    # The CLI module must also be present.
    assert "lumio_wiki/cli.py" in names
    assert "lumio_wiki/skill.py" in names


def test_built_wheel_skill_protects_canonical_first_run_and_relationship_parity(
    isolated_wheel_env: dict,
):
    """Issue #148 / ADR-0017: the first-run and Relationship parity facts are
    validated against the BUILT-WHEEL copy of the skill and protocol, not only
    the editable source tree. A stale or mismatched wheel cannot ship: the
    installed-wheel surface is the drift-protection point ADR-0017 names."""
    import zipfile

    wheel_path: Path = isolated_wheel_env["wheel"]
    with zipfile.ZipFile(wheel_path) as zf:
        skill_text = zf.read("lumio_wiki/data/skill/SKILL.md").decode("utf-8")
        protocol_text = zf.read("lumio_wiki/data/skill/PROTOCOL.md").decode("utf-8")
    for name, text in (("SKILL.md", skill_text), ("PROTOCOL.md", protocol_text)):
        flat = " ".join(text.lower().split())
        # setup is the canonical first run; init is the lower-level KB-only op.
        assert "lumio-wiki setup" in flat, f"{name}: must name lumio-wiki setup"
        assert "lower-level" in flat, f"{name}: must document init as lower-level"
        # Extracted References stay distinct from typed canonical edges; since
        # ADR-0021 canonical edges are accepted, evidence-bearing Claims.
        assert "extracted reference" in flat, f"{name}: must name Extracted References"
        assert "evidence-bearing" in flat, f"{name}: must document evidence-bearing Claims"
        # The title-based relationship frontmatter input and its staging
        # command are gone; the shipped skill must not document them.
        assert "relationship stage" not in flat, (
            f"{name}: must not document the removed relationship stage command"
        )
    # SKILL.md additionally documents the cross-link command and the typed
    # traversal surface.
    skill_flat = " ".join(skill_text.lower().split())
    assert "cross-link" in skill_flat, "SKILL.md: must document cross-link"
    assert "typed" in skill_flat, "SKILL.md: must name typed Relationships"


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

    python = isolated_wheel_env["python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")

    # A populated KB with the canonical chain
    # Lumio Overview -> Architecture -> Technology Stack.
    kb_root = _write_graph_kb(tmp_path / "kb")

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
    assert (dest / ".lumio-skill-manifest.json").is_file()
    # The installed skill content must match the packaged source.
    from lumio_wiki.skill import resolve_skill_path

    assert (dest / "SKILL.md").read_text() == resolve_skill_path().read_text()

    status = subprocess.run(
        [
            str(script),
            "skill",
            "status",
            "--agent",
            "claude-code",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr
    assert "state:             current" in status.stdout

    manifest_path = dest / ".lumio-skill-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["distribution_version"] = "0.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    stale = subprocess.run(
        [
            str(script),
            "skill",
            "status",
            "--agent",
            "claude-code",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1
    assert "state:             stale" in stale.stdout
    stale_update = subprocess.run(
        [
            str(script),
            "skill",
            "update",
            "--agent",
            "claude-code",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    assert stale_update.returncode == 0, stale_update.stderr
    assert "stale -> current" in stale_update.stdout

    (dest / "SKILL.md").write_text("corrupt", encoding="utf-8")
    corrupt = subprocess.run(
        [
            str(script),
            "skill",
            "status",
            "--agent",
            "claude-code",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    assert corrupt.returncode == 1
    assert "state:             corrupt" in corrupt.stdout

    updated = subprocess.run(
        [
            str(script),
            "skill",
            "update",
            "--agent",
            "claude-code",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    assert updated.returncode == 0, updated.stderr
    assert "corrupt -> current" in updated.stdout

    home = tmp_path / "home"
    home.mkdir()
    user_env = {**os.environ, "HOME": str(home)}
    user_install = subprocess.run(
        [str(script), "skill", "install", "--scope", "user"],
        capture_output=True,
        text=True,
        env=user_env,
    )
    assert user_install.returncode == 0, user_install.stderr
    assert (home / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").is_file()

    vendor_install = subprocess.run(
        [str(script), "skill", "install", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=user_env,
    )
    assert vendor_install.returncode == 0, vendor_install.stderr
    assert (home / ".codex" / "skills" / "lumio-wiki" / "SKILL.md").is_file()

    project = tmp_path / "project"
    project.mkdir()
    project_install = subprocess.run(
        [str(script), "skill", "install", "--scope", "project"],
        capture_output=True,
        text=True,
        cwd=project,
    )
    assert project_install.returncode == 0, project_install.stderr
    assert (project / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").is_file()


def test_isolated_skill_upgrade_from_prior_wheel(
    isolated_wheel_env: dict,
    tmp_path: Path,
):
    """The genuine pre-#150 wheel becomes stale and updates after upgrade."""
    prior_wheel = (
        Path(__file__).parent / "fixtures" / "prior_wheels" / "lumio_wiki-0.1.1-py3-none-any.whl"
    )
    assert prior_wheel.is_file()
    assert hashlib.sha256(prior_wheel.read_bytes()).hexdigest() == (
        "043bc432fa361ef2a7827644f84c77b2cfb7c8c13cbcae1fae61cc9ddf9e113a"
    )
    python = _create_isolated_venv(tmp_path / "upgrade-venv")[0]
    bin_dir = python.parent
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(prior_wheel)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "msgpack>=1.0", "msgspec[yaml]>=0.21.1"],
        check=True,
        capture_output=True,
    )
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    home = tmp_path / "upgrade-home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}

    installed = subprocess.run(
        [str(script), "skill", "install", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert installed.returncode == 0, installed.stderr

    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(isolated_wheel_env["wheel"]),
        ],
        check=True,
        capture_output=True,
    )
    stale = subprocess.run(
        [str(script), "skill", "status", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert stale.returncode == 1
    assert "state:             stale" in stale.stdout
    assert "installed_version: (none)" in stale.stdout
    assert "legacy manifest-less" in stale.stdout

    updated = subprocess.run(
        [str(script), "skill", "update", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert updated.returncode == 0, updated.stderr
    current = subprocess.run(
        [str(script), "skill", "status", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert current.returncode == 0, current.stderr
    assert "state:             current" in current.stdout


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
    init_cmd = f'from lumio_wiki.cli import main; import sys; sys.exit(main(["init", "{kb_root}"]))'
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
    init_cmd = f'from lumio_wiki.cli import main; import sys; sys.exit(main(["init", "{kb_root}"]))'
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

    # Graph steps use a claim-bearing KB (known canonical graph:
    # Lumio Overview -> Architecture -> Technology Stack).
    kb = tmp_path / "kb"
    _write_graph_kb(kb)

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


# --- Issue #166: the S3 coding-agent journey install shape --------------------


def _build_lumio_lancedb_wheel(wheel_dir: Path) -> Path:
    """Build the lumio-lancedb wheel into ``wheel_dir`` and return its path."""
    return _build_workspace_wheel(wheel_dir, "lumio-lancedb", "lumio_lancedb-*.whl")


@pytest.fixture(scope="module")
def s3_journey_wheel_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The exact install shape of the S3 journey (issue #166 AC2).

    ``uv tool install 'lumio-wiki[s3]' --with 'lumio-lancedb[s3]'`` is the
    documented journey install; this fixture reproduces its resolution
    deterministically from the workspace wheels: lumio-wiki + the [s3] extra,
    lumio-lancedb + the [s3] extra, their declared dependencies, and nothing
    else — never the full ``lumio`` application and never unrelated extras.
    """
    wheel_dir = tmp_path_factory.mktemp("wheels-s3")
    venv_dir = tmp_path_factory.mktemp("venv-s3")
    wiki_wheel = _build_wheel(wheel_dir)
    lance_wheel = _build_lumio_lancedb_wheel(wheel_dir)
    python = _create_isolated_venv(venv_dir)[0]
    # One resolver run over both wheels (extras included) so lumio-lancedb's
    # lumio-wiki requirement is satisfied by the local wheel, not PyPI. The
    # pip fallback MUST run through the venv's own python — never the test
    # runner's — or the install lands in the wrong environment.
    targets = [f"{wiki_wheel}[s3]", f"{lance_wheel}[s3]"]
    if _has_uv():
        cmd = ["uv", "pip", "install", "--python", str(python), *targets]
    else:
        cmd = [str(python), "-m", "pip", "install", *targets]
    subprocess.run(cmd, check=True, capture_output=True, cwd=str(venv_dir))
    return {"python": python, "venv": venv_dir}


def test_s3_journey_installs_only_wiki_lancedb_and_skill(
    s3_journey_wheel_env: dict, tmp_path: Path
):
    """Issue #166 AC2: from the journey install, doctor reports exactly the
    S3 capabilities present and the unrelated extras absent; the full
    ``lumio`` application is not importable; and the packaged Agent Skill
    installs into a project without any heavyweight dependency."""
    python = s3_journey_wheel_env["python"]
    script = python.parent / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")

    # The journey's imports resolve...
    result = subprocess.run(
        [str(python), "-c", "import lumio_wiki, lumio_lancedb, obstore"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # ...and nothing beyond the journey's packages does: no full lumio app,
    # no unrelated lumio-wiki extras ([documents], [llm]).
    for forbidden in ("lumio", "openai", "liteparse", "markitdown", "anydoc"):
        probe = subprocess.run(
            [str(python), "-c", f"import {forbidden}"], capture_output=True, text=True
        )
        assert probe.returncode != 0, f"{forbidden!r} must not be part of the journey install"

    doctor = subprocess.run([str(script), "doctor"], capture_output=True, text=True)
    assert doctor.returncode == 0, doctor.stderr
    assert "extra[s3]: installed" in doctor.stdout
    assert "extra[lancedb]: installed" in doctor.stdout
    assert "extra[documents]: not installed" in doctor.stdout
    assert "extra[llm]: not installed" in doctor.stdout

    # The Agent Skill is the fourth journey deliverable: a project-scope
    # install works from the same isolated environment.
    project = tmp_path / "coding-agent-project"
    project.mkdir()
    skill = subprocess.run(
        [str(script), "skill", "install", "--scope", "project"],
        capture_output=True,
        text=True,
        cwd=str(project),
    )
    assert skill.returncode == 0, skill.stderr
    assert (project / ".agents" / "skills" / "lumio-wiki" / "SKILL.md").is_file()
    assert (project / ".agents" / "skills" / "lumio-wiki" / "PROTOCOL.md").is_file()
