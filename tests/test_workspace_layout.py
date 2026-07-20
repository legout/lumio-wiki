from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
MEMBER = ROOT / "packages" / "lumio-wiki"
LANCEDB_MEMBER = ROOT / "packages" / "lumio-lancedb"


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_uv_workspace_declares_members_and_explicit_member_dependencies():
    root = _toml(ROOT / "pyproject.toml")
    assert root["tool"]["uv"]["workspace"]["members"] == [
        "packages/lumio-lancedb",
        "packages/lumio-wiki",
    ]
    assert "lumio-wiki>=0.1.1,<0.2.0" in root["project"]["dependencies"]
    assert "lumio-lancedb>=0.1.1,<0.2.0" in root["project"]["dependencies"]
    assert root["tool"]["uv"]["sources"]["lumio-wiki"] == {"workspace": True}
    assert root["tool"]["uv"]["sources"]["lumio-lancedb"] == {"workspace": True}
    assert root["tool"]["pytest"]["ini_options"]["testpaths"] == [
        "tests",
        "packages/lumio-lancedb/tests",
        "packages/lumio-wiki/tests",
    ]

    member = _toml(MEMBER / "pyproject.toml")
    assert member["project"]["name"] == "lumio-wiki"
    assert member["project"]["requires-python"] == ">=3.14"
    assert member["project"]["dependencies"] == ["msgspec[yaml]>=0.21.1"]
    assert member["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/lumio_wiki"
    ]

    lancedb_member = _toml(LANCEDB_MEMBER / "pyproject.toml")
    assert lancedb_member["project"]["name"] == "lumio-lancedb"
    assert lancedb_member["project"]["requires-python"] == ">=3.14"
    assert "lumio-wiki>=0.1.1,<0.2.0" in lancedb_member["project"]["dependencies"]
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
    assert (ROOT / "src" / "lumio").is_dir()
    assert (MEMBER / "src" / "lumio_wiki").is_dir()
    assert (LANCEDB_MEMBER / "src" / "lumio_lancedb").is_dir()
    # No two wheels may own the same concrete Python module path (ADR-0010).
    assert not (MEMBER / "src" / "lumio").exists()
    assert not (LANCEDB_MEMBER / "src" / "lumio").exists()
    assert not (LANCEDB_MEMBER / "src" / "lumio_wiki").exists()
    assert not (MEMBER / "src" / "lumio_lancedb").exists()
    assert not (ROOT / "src" / "lumio_lancedb").exists()


def test_workspace_ci_runs_package_and_application_consumer_suites():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    marker = "- run: >-\n          uv run --locked pytest -q\n"
    assert marker in workflow
    command = ("uv run --locked pytest -q\n" + workflow.split(marker, 1)[1])
    command = command.split("\n\n", 1)[0].split()
    assert command == [
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "packages/lumio-wiki/tests",
        "tests/test_core_compatibility.py",
        "tests/test_workspace_layout.py",
        "tests/test_wheel_verifier.py",
        "tests/test_core_sdk.py",
        "tests/test_core_sdk_index.py",
        "tests/test_core_without_lancedb.py",
        "tests/test_zero_index_retrieval.py",
        "tests/test_retrieval_adapter_contract.py",
        "tests/test_kb_control.py",
        "tests/test_relationship_types.py",
        "tests/test_graph_retrieval.py",
        "tests/test_health_report.py",
        "tests/test_registry.py",
        "tests/test_okf_export.py",
        "tests/test_okf_import.py",
        "tests/test_okf_round_trip.py",
        "tests/test_okf_reject_unsafe.py",
    ]
