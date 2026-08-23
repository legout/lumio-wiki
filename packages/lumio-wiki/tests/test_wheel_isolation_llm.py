"""Issue #101, AC6: the ``[llm]`` and ``[all]`` ingestion capabilities verified
from isolated built-wheel installations using a FAKE provider.

These tests are the authoritative proof that:

- the base wheel stays model-free (AC1): ``openai`` is not importable until the
  ``[llm]`` extra is installed;
- requesting unattended distillation from the base install fails actionably,
  naming the exact extra (AC4, PRD user story 18);
- the ``[llm]`` extra installs the OpenAI-compatible provider dependency and a
  smoke ingest → staged proposal succeeds with a FAKE provider (AC2, AC6) —
  never a real API call;
- the ``[all]`` extra installs both document and unattended-provider
  capabilities WITHOUT adding LanceDB / PyArrow (AC5).

The fake provider is injected by monkeypatching ``OpenAIDistiller`` so the
test never constructs a real OpenAI client and never makes a network call.
Building and installing wheels into fresh venvs is expensive, so the whole
module is marked ``slow`` and only runs when the workspace is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

# Building and installing into fresh venvs is expensive; skip under the
# standard fast suite unless explicitly requested or running from the repo
# root (where the workspace is available).
pytestmark = pytest.mark.slow


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _build_wheel(wheel_dir: Path) -> Path:
    """Build the lumio-wiki wheel into ``wheel_dir`` and return its path."""
    wheel_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(__file__).parents[3]
    if _has_uv():
        cmd = ["uv", "build", "--package", "lumio-wiki", "--wheel", "--out-dir", str(wheel_dir)]
    else:
        pkg = workspace_root / "packages" / "lumio-wiki"
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(pkg)]
    subprocess.run(cmd, cwd=str(workspace_root), check=True, capture_output=True)
    wheels = list(wheel_dir.glob("lumio_wiki-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _create_isolated_venv(venv_dir: Path) -> Path:
    """Create a fresh venv and return its python executable."""
    venv.create(venv_dir, with_pip=True, clear=True)
    if os.name == "nt":  # pragma: no cover
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _pip(python: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), "-m", "pip", *args],
        check=True,
        capture_output=True,
        text=True,
    )


# The distilled page a fake provider returns. It carries the routing/durability
# fields a provider-distilled page is expected to declare.
_FAKE_PROVIDER_MARKDOWN = (
    "---\n"
    'id: "entity:llm-wheel-page"\n'
    'title: "LLM Wheel Page"\n'
    "entity_types:\n"
    "  - page\n"
    "aliases: []\n"
    "tags:\n"
    '  - "llm"\n'
    'summary: "Distilled by the fake provider in the isolated wheel test."\n'
    'category: "concepts"\n'
    'type: "definition"\n'
    'durability_rationale: "Durable concept distilled for the wheel test."\n'
    'lifecycle: "draft"\n'
    'visibility: "internal"\n'
    "sources:\n"
    '  - id: "llm"\n'
    '    title: "Fake provider source"\n'
    "---\n\n"
    "# LLM Wheel Page\n\n"
    "Body distilled by the fake provider.\n"
)

# A tiny Python script that monkeypatches ``OpenAIDistiller`` with a fake
# client and then drives ``ingest --distiller llm`` through the CLI. Written
# to a temp file and run by the isolated interpreter so the in-process fake
# never touches the network.
_FAKE_DISTILLER_DRIVER = """
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import lumio_wiki
from lumio_wiki import cli

# argv: [script, fake_markdown, kb_root, source_file]
FAKE_MARKDOWN = sys.argv[1]
KB_ROOT = sys.argv[2]
SOURCE_FILE = sys.argv[3]

def _make_fake_distiller():
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=FAKE_MARKDOWN))]
    )
    return lumio_wiki.OpenAIDistiller(model="fake-model", client=client)

# Patch the CLI's distiller factory so --distiller llm uses the fake client.
real_build = cli._build_distiller
def _fake_build(args):
    if getattr(args, "distiller", None) == "llm":
        return _make_fake_distiller()
    return real_build(args)
cli._build_distiller = _fake_build

rc = cli.main(["ingest", KB_ROOT, SOURCE_FILE, "--distiller", "llm"])
sys.exit(rc)
"""


@pytest.fixture(scope="module")
def llm_wheel_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Build the wheel once and create two venvs: base and ``[llm]``."""
    wheel_dir = tmp_path_factory.mktemp("wheels")
    wheel_path = _build_wheel(wheel_dir)

    base_venv = tmp_path_factory.mktemp("base_venv")
    base_python = _create_isolated_venv(base_venv)
    _pip(base_python, "install", "--no-deps", str(wheel_path))
    _pip(base_python, "install", "msgpack>=1.0", "msgspec[yaml]>=0.21.1")

    llm_venv = tmp_path_factory.mktemp("llm_venv")
    llm_python = _create_isolated_venv(llm_venv)
    # Install the wheel WITH the [llm] extra so ``openai`` is present.
    _pip(llm_python, "install", str(wheel_path) + "[llm]")
    _pip(llm_python, "install", "msgpack>=1.0", "msgspec[yaml]>=0.21.1")

    return {
        "wheel": wheel_path,
        "base_python": base_python,
        "llm_python": llm_python,
    }


def test_base_wheel_does_not_import_openai(llm_wheel_env: dict):
    """AC1: the base wheel stays model-free; ``openai`` is not importable."""
    python = llm_wheel_env["base_python"]
    result = subprocess.run(
        [str(python), "-c", "import openai"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "openai must NOT be importable in the base wheel"


def test_llm_extra_installs_openai(llm_wheel_env: dict):
    """AC1: the ``[llm]`` extra installs the OpenAI-compatible provider dependency."""
    python = llm_wheel_env["llm_python"]
    result = subprocess.run(
        [str(python), "-c", "import openai; print('openai', openai.__version__)"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"openai must be importable with [llm]:\n{result.stderr}"
    assert "openai" in result.stdout


def test_base_wheel_doctor_reports_llm_not_installed(llm_wheel_env: dict):
    """AC4/AC6: ``doctor`` reports the llm extra as not installed from the base wheel."""
    python = llm_wheel_env["base_python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    result = subprocess.run(
        [str(script), "doctor"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "extra[llm]: not installed" in result.stdout
    assert "lumio-wiki[llm]" in result.stdout


def test_base_wheel_llm_distiller_fails_actionably(llm_wheel_env: dict, tmp_path: Path):
    """AC4/AC6: requesting ``--distiller llm`` from the base install fails actionably.

    The base wheel has no ``openai`` module, so the CLI must detect the
    missing extra and emit the exact install command — never a masked
    missing-config error (ADR-0010, PRD user story 18). Provider env vars are
    SET so the only reason for failure is the missing extra.
    """
    python = llm_wheel_env["base_python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    kb_root = tmp_path / "kb"
    subprocess.run([str(script), "init", str(kb_root)], check=True, capture_output=True)
    # A version-2 Knowledge Base requires the Entity contract (ADR-0021): the
    # Maintainer declares the ``page`` Entity Type in the control file's
    # ontology, and the distilled page declares a stable ``entity:<slug>`` id.
    control_file = kb_root / "lumio.yaml"
    control_file.write_text(
        control_file.read_text(encoding="utf-8").replace(
            "  entity_types:\n  predicates:",
            "  entity_types:\n    page: {}\n  predicates:",
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.txt"
    source.write_text("some source text", encoding="utf-8")

    # Set provider env vars so the ONLY failure reason is the missing extra.
    env = dict(os.environ)
    env["LUMIO_PROVIDER_MODEL"] = "fake-model"
    env["LUMIO_PROVIDER_BASE_URL"] = "http://localhost:1/v1"
    env["LUMIO_PROVIDER_API_KEY"] = "fake-key"
    result = subprocess.run(
        [str(script), "ingest", str(kb_root), str(source), "--distiller", "llm"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "pip install 'lumio-wiki[llm]'" in result.stderr, (
        "the base wheel must name the exact install command for the missing extra"
    )


def test_llm_extra_smoke_ingest_with_fake_provider(llm_wheel_env: dict, tmp_path: Path):
    """AC2/AC6: a smoke ingest→proposal succeeds with the [llm] extra + FAKE provider.

    The fake provider is injected by monkeypatching ``OpenAIDistiller`` so no
    real API call is ever made. The proposal carries the same provenance,
    routing, and validation as a host-agent proposal.
    """
    python = llm_wheel_env["llm_python"]
    bin_dir = python.parent
    script = bin_dir / ("lumio-wiki.exe" if os.name == "nt" else "lumio-wiki")
    kb_root = tmp_path / "kb"
    subprocess.run([str(script), "init", str(kb_root)], check=True, capture_output=True)
    # A version-2 Knowledge Base requires the Entity contract (ADR-0021): the
    # Maintainer declares the ``page`` Entity Type in the control file's
    # ontology, and the distilled page declares a stable ``entity:<slug>`` id.
    control_file = kb_root / "lumio.yaml"
    control_file.write_text(
        control_file.read_text(encoding="utf-8").replace(
            "  entity_types:\n  predicates:",
            "  entity_types:\n    page: {}\n  predicates:",
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.txt"
    source.write_text("some source text for the fake provider", encoding="utf-8")

    driver = tmp_path / "drive_fake_distiller.py"
    driver.write_text(_FAKE_DISTILLER_DRIVER, encoding="utf-8")

    result = subprocess.run(
        [
            str(python),
            str(driver),
            _FAKE_PROVIDER_MARKDOWN,
            str(kb_root),
            str(source),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"fake-provider ingest failed:\n{result.stderr}"
    assert "Staged proposal" in result.stdout
    assert "LLM Wheel Page" in result.stdout

    # The staged proposal validates (same proposal shape as host-agent ingest).
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


def test_all_extra_installs_documents_and_llm_without_lancedb(
    tmp_path_factory: pytest.TempPathFactory,
):
    """AC5: the ``[all]`` extra installs documents + llm WITHOUT LanceDB/PyArrow."""
    if not _has_uv():
        pytest.skip("uv required to build the wheel for the [all] extra test")

    wheel_dir = tmp_path_factory.mktemp("all_wheels")
    wheel_path = _build_wheel(wheel_dir)
    all_venv = tmp_path_factory.mktemp("all_venv")
    python = _create_isolated_venv(all_venv)
    _pip(python, "install", str(wheel_path) + "[all]")
    _pip(python, "install", "msgpack>=1.0", "msgspec[yaml]>=0.21.1")

    # The [all] extra brings in the documents deps (liteparse, markitdown) and
    # the llm dep (openai) ...
    result = subprocess.run(
        [str(python), "-c", "import liteparse, markitdown, openai; print('all extras ok')"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"[all] must install documents + llm deps:\n{result.stderr}"
    assert "all extras ok" in result.stdout

    # ... but MUST NOT bring in LanceDB or PyArrow.
    for forbidden in ("lancedb", "pyarrow"):
        result = subprocess.run(
            [str(python), "-c", f"import {forbidden}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"{forbidden} must NOT be importable from lumio-wiki[all] (AC5)"
        )
