from __future__ import annotations

from pathlib import Path

import tomllib

ROOT = Path(__file__).parents[1]
MEMBER = ROOT / "packages" / "lumio-wiki"
LANCEDB_MEMBER = ROOT / "packages" / "lumio-lancedb"


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_uv_workspace_root_is_coordination_only_with_two_members():
    root = _toml(ROOT / "pyproject.toml")
    # The workspace root is coordination only: it is not itself a distributable
    # package (ADR-0010, issue #102; ADR-0025 repository split — the web
    # application lives in a separate private repository). No [project] table
    # and an explicit non-package marker so uv never tries to build the root.
    assert "project" not in root, "root pyproject.toml must not declare [project]"
    assert root["tool"]["uv"]["package"] is False
    assert root["tool"]["uv"]["workspace"]["members"] == [
        "packages/lumio-lancedb",
        "packages/lumio-wiki",
    ]
    assert root["tool"]["uv"]["sources"]["lumio-wiki"] == {"workspace": True}
    assert root["tool"]["uv"]["sources"]["lumio-lancedb"] == {"workspace": True}
    assert root["tool"]["pytest"]["ini_options"]["testpaths"] == [
        "tests",
        "packages/lumio-lancedb/tests",
        "packages/lumio-wiki/tests",
        "eval",
    ]

    member = _toml(MEMBER / "pyproject.toml")
    assert member["project"]["name"] == "lumio-wiki"
    assert member["project"]["requires-python"] == ">=3.14"
    assert member["project"]["dependencies"] == [
        "msgpack>=1.0",
        "msgspec[yaml]>=0.21.1",
    ]
    assert member["project"]["license"] == "Apache-2.0"
    assert member["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/lumio_wiki"
    ]

    lancedb_member = _toml(LANCEDB_MEMBER / "pyproject.toml")
    assert lancedb_member["project"]["name"] == "lumio-lancedb"
    assert lancedb_member["project"]["requires-python"] == ">=3.14"
    assert lancedb_member["project"]["license"] == "Apache-2.0"
    assert "lumio-wiki>=0.1.2,<0.2.0" in lancedb_member["project"]["dependencies"]
    assert "lancedb>=0.34.0" in lancedb_member["project"]["dependencies"]
    assert "pyarrow>=24.0.0" in lancedb_member["project"]["dependencies"]
    # Torch stays out of the base adapter (ADR-0010): the embeddings extra is
    # the only thing that pulls in sentence-transformers.
    assert "sentence-transformers" not in " ".join(
        lancedb_member["project"]["dependencies"]
    )
    assert lancedb_member["project"]["optional-dependencies"]["embeddings"] == [
        "sentence-transformers>=2.6.0"
    ]
    assert lancedb_member["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/lumio_lancedb"
    ]


def test_workspace_has_one_root_lockfile_and_disjoint_import_roots():
    assert (ROOT / "uv.lock").is_file()
    assert not (MEMBER / "uv.lock").exists()
    assert not (LANCEDB_MEMBER / "uv.lock").exists()
    assert not (ROOT / "src").exists()
    assert (MEMBER / "src" / "lumio_wiki").is_dir()
    assert (LANCEDB_MEMBER / "src" / "lumio_lancedb").is_dir()
    # No two wheels may own the same concrete Python module path (ADR-0010).
    assert not (MEMBER / "src" / "lumio").exists()
    assert not (MEMBER / "src" / "lumio_lancedb").exists()
    assert not (LANCEDB_MEMBER / "src" / "lumio_wiki").exists()
    assert not (LANCEDB_MEMBER / "src" / "lumio").exists()


def test_workspace_ci_runs_the_full_suite():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # ADR-0025: the public workspace CI runs the whole suite plus lint, not a
    # per-file enumeration.
    assert "uv run --locked ruff check ." in workflow
    assert "uv run --locked pytest -q -n 4" in workflow
    # The isolated wheel job builds only the two members this repo ships.
    assert "uv build --package lumio --wheel" not in workflow
    assert "uv build --package lumio-wiki --wheel" in workflow
    assert "uv build --package lumio-lancedb --wheel" in workflow
